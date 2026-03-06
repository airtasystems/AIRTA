"""
Dry-run validation of site_profile.json before diagnostics/tests.

Loads the profile, runs execute_message_flow with a test payload ("Hello"),
and checks that at least one step succeeded (2xx) and the final response
is non-empty and not the save-only placeholder.
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from component_discovery import auth as auth_module
from component_discovery import config as _config
from . import evasion
from . import profile_send

try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parent.parent
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
    load_dotenv()
    from google import genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


def _is_save_only_response(body: str) -> bool:
    """True if body is only {success, uuid} (save confirmation, no AI text)."""
    try:
        o = json.loads(body) if isinstance(body, str) else body
        return (
            isinstance(o, dict)
            and set(o.keys()) <= {"success", "uuid"}
            and o.get("success") is True
        )
    except (TypeError, json.JSONDecodeError):
        return False


def _is_failure_placeholder(response: str) -> bool:
    """True if response indicates validation/DOM extraction failure."""
    if not response or not isinstance(response, str):
        return True
    r = response.strip().lower()
    return (
        "(misconfiguration:" in r
        or "(ai response not in api;" in r
        or "dom selector did not find" in r
    )


# Patterns indicating WAF/security block (Cloudflare, etc.) instead of API response
_SECURITY_BLOCK_PATTERNS = (
    "just a moment",
    "enable javascript and cookies",
    "cloudflare",
    "cf-chl-opt",
    "challenge-platform",
    "checking your browser",
    "ddos protection",
    "access denied",
    "blocked",
    "security check",
)


def _should_retry_with_browser(result: dict, profile: dict, used_browser: bool) -> bool:
    """True if we got WAF block and should retry with Playwright headless browser.
    500 is excluded: it usually indicates badly formatted payloads or wrong paths, handled by LLM repair."""
    if used_browser:
        return False
    status = result.get("status")
    if status in (502, 503, 504):
        return True
    security = profile.get("security", {})
    if security.get("has_waf"):
        return True
    resp = (result.get("response") or "").lower()
    if resp and len(resp) >= 50:
        return any(p in resp for p in _SECURITY_BLOCK_PATTERNS)
    return False


def _auth_state_for_profile(profile_path: Path) -> Path:
    """Derive auth_state.json path from profile path: .../sitename/component/site_profile.json -> .../sitename/site_config/auth_state.json"""
    # profile_path: component-discovery/sitename/component/site_profile.json
    # auth_state: component-discovery/sitename/site_config/auth_state.json
    site_dir = profile_path.parent.parent  # component-discovery/sitename
    return site_dir / "site_config" / "auth_state.json"


def _get_discovery_test_prompt() -> str:
    """Test prompt for discovery verification. Configurable via DISCOVERY_TEST_PROMPT env."""
    return os.getenv("DISCOVERY_TEST_PROMPT", "What is 2+2? Reply with only the number.")


def _build_verbose_failure(result: dict, test_payload: str) -> dict:
    """Build a verbose failure dict for LLM debugging."""
    status = result.get("status")
    response = (result.get("response") or "").strip()
    ok = result.get("ok", False) or (status and 200 <= status < 300)
    failure = {
        "test_payload": test_payload,
        "status": status,
        "ok": ok,
        "response_preview": (response or "(empty)")[:1500],
        "response_full_len": len(response or ""),
    }
    if result.get("failed_step_id"):
        failure["failed_step_id"] = result["failed_step_id"]
        failure["failed_method"] = result.get("failed_method", "")
        failure["failed_url"] = result.get("failed_url", "")
        failure["failed_body_preview"] = (result.get("failed_body_preview") or "")[:2000]
        failure["failed_resp_body"] = (result.get("failed_resp_body") or "")[:3000]
    return failure


async def run_discovery_test_prompt(
    profile_path: Path | None = None,
    *,
    verbose: bool = True,
) -> tuple[bool, dict | None]:
    """
    Run a single test prompt through the profile to verify discovery works.
    No repair, no WAF retry. Success = 2xx and non-empty response.

    Returns (ok, result). ok=True if test passed. result is the full execute_message_flow
    return dict (status, response, failed_*, etc.) for debugging when ok=False.
    """
    if profile_path is None:
        profile_path = _config.SITE_STATE_DIR / "site_profile.json"
    if not profile_path.exists():
        if verbose:
            print(f"[-] Profile not found: {profile_path}")
        return False, {"error": "profile_not_found", "path": str(profile_path)}

    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"[-] Invalid profile: {e}")
        return False, {"error": "invalid_profile", "message": str(e)}

    auth_state_path = _auth_state_for_profile(profile_path)
    if not auth_state_path.exists():
        if verbose:
            print(f"[-] No auth state at {auth_state_path}. Run discovery (login + capture) first.")
        return False, {"error": "no_auth_state", "path": str(auth_state_path)}

    if not await auth_module.ensure_session_fresh():
        if verbose:
            print("[-] Session invalid. Run discovery (login + capture) first.")
        return False, {"error": "session_invalid"}

    test_payload = _get_discovery_test_prompt()
    security = profile.get("security", {})
    needs_browser = security.get("requires_browser_fetch", False)

    async with async_playwright() as p:
        page = None
        browser = None
        context = None
        request_context = None
        try:
            if needs_browser:
                browser = await p.chromium.launch(headless=True)
                ctx_kwargs = {"storage_state": str(auth_state_path), "viewport": {"width": 1280, "height": 720}}
                proxy = evasion.get_playwright_proxy()
                if proxy:
                    ctx_kwargs["proxy"] = proxy
                context = await browser.new_context(**ctx_kwargs)
                page = await context.new_page()
                if await evasion.apply_stealth(page):
                    pass
                challenge_url = _config.BASE_URL or profile.get("api_base", "")
                await page.goto(challenge_url, wait_until="domcontentloaded", timeout=60000)
                for _ in range(12):
                    pg_title = await page.title()
                    if not any(ph in pg_title.lower() for ph in ("just a moment", "checking your browser", "please wait")):
                        break
                    await page.wait_for_timeout(2500)
            else:
                proxy = evasion.get_playwright_proxy()
                request_context = await p.request.new_context(storage_state=str(auth_state_path), proxy=proxy)

            r = await profile_send.execute_message_flow(
                profile, test_payload, page=page, request_context=request_context,
                state={}, is_first_message=True, verbose=verbose,
            )
        finally:
            if request_context:
                await request_context.dispose()
            if context:
                await context.close()
            if browser:
                await browser.close()

    status = r.get("status")
    response = (r.get("response") or "").strip()
    ok = r.get("ok", False) or (status and 200 <= status < 300)

    # Additional failure checks
    if ok and response:
        if _is_save_only_response(response):
            ok = False
            r["failure_reason"] = "save_only_response"
        elif _is_failure_placeholder(response):
            ok = False
            r["failure_reason"] = "failure_placeholder"
    elif ok and not response:
        ok = False
        r["failure_reason"] = "empty_response"

    if verbose:
        preview = response[:200] + ("..." if len(response) > 200 else "")
        print(f"[*] Test prompt sent. Response: {preview}")

    if not ok:
        if verbose:
            print(f"[-] Test failed: status={status}, response empty or non-2xx")
        return False, r
    if verbose:
        print("[+] Test passed.")
    return True, r


def _repair_profile_from_test_failure(
    profile: dict,
    failure: dict,
    raw_trace: dict | None,
) -> dict | None:
    """
    Call Gemini to analyze error + raw trace + site profile and return a fixed profile.
    Used when the discovery test prompt fails for any reason.
    """
    if not _GEMINI_AVAILABLE:
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    failure_str = json.dumps(failure, indent=2)
    trace_str = json.dumps(raw_trace, indent=2)[:12000] if raw_trace else "(no raw trace available)"
    profile_str = json.dumps(profile, indent=2)

    prompt = f"""The discovery test prompt failed. Your job is to analyze the error, the raw network trace, and the site profile, then return a corrected site_profile.json that will make the test pass.

## Failure details
{failure_str}

## Raw network trace (from capture)
{trace_str}

## Current site_profile.json
{profile_str}

Analyze:
1. **Error**: What went wrong? (status code, empty response, save-only, wrong payload, wrong path, missing headers, etc.)
2. **Raw trace**: Which requests in the trace succeeded? What do the actual request/response bodies look like? What paths, headers, and body structures does the real app use?
3. **Profile**: What in the profile is wrong? (body_template structure, URL path, dynamic_values, extract jsonpaths, response_source, extract_response_from_dom, etc.)

Fix the profile to match what the trace shows. Common fixes:
- body_template: must match the actual request structure from the trace (flat JSON, correct field names)
- extract: jsonpath must match the actual response structure
- response_source=dom: if API returns save-only, add extract_response_from_dom with correct selector
- dynamic_values: use only input, generate, step:<exact_step_id>, cookie:<name>, header:<name>, static
- URL path: ensure it matches the trace (e.g. /api/chat vs /api/trpc/...)

Return ONLY valid JSON (the complete corrected site_profile.json). No markdown, no explanation."""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = response.text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)
    except Exception:
        return None


async def run_discovery_test_prompt_with_repair_loop(
    profile_path: Path | None = None,
    *,
    max_repairs: int = 5,
    verbose: bool = True,
) -> bool:
    """
    Run the discovery test prompt. On failure, collect verbose error + raw trace + profile,
    pass to LLM for repair, save, and retry. Loop until test passes or max_repairs reached.
    """
    if profile_path is None:
        profile_path = _config.SITE_STATE_DIR / "site_profile.json"
    raw_trace_path = profile_path.parent / "raw_trace.json"

    for attempt in range(max_repairs + 1):
        ok, result = await run_discovery_test_prompt(profile_path, verbose=verbose)
        if ok:
            return True

        if attempt >= max_repairs:
            if verbose:
                print(f"[!] Test failed after {max_repairs} repair attempts.")

            return False

        if not _GEMINI_AVAILABLE:
            if verbose:
                print("[!] LLM repair unavailable (google-genai not installed).")
            return False

        if result and result.get("error") in ("profile_not_found", "invalid_profile", "no_auth_state", "session_invalid"):
            if verbose:
                print(f"[!] Cannot repair: {result.get('error', 'unknown')}")
            return False

        if verbose:
            print(f"[*] Test failed (attempt {attempt + 1}/{max_repairs + 1}). Calling LLM to analyze and fix ...")

        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            if verbose:
                print("[-] Could not read profile for repair.")
            return False

        raw_trace = None
        if raw_trace_path.exists():
            try:
                raw_trace = json.loads(raw_trace_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        failure = _build_verbose_failure(result, _get_discovery_test_prompt())
        repaired = _repair_profile_from_test_failure(profile, failure, raw_trace)
        if repaired is None:
            if verbose:
                print("[!] LLM repair failed or returned invalid JSON.")
            return False

        profile_path.write_text(json.dumps(repaired, indent=2), encoding="utf-8")
        if verbose:
            print("[*] Repaired profile saved. Retrying test ...")


async def validate_profile_dry_run(
    profile_path: Path | None = None,
    *,
    test_payload: str = "Hello",
    llm_repair: bool = True,
    verbose: bool = False,
) -> tuple[bool, str | None, dict | None]:
    """
    Run a dry-run of the profile with a test payload.

    Returns (ok, error_message, result). ok=True means validation passed.
    When ok=False, result may contain failed_step_id, failed_body_preview, etc. for 500 repair.
    """
    if profile_path is None:
        profile_path = _config.SITE_STATE_DIR / "site_profile.json"
    if not profile_path.exists():
        return False, f"Profile not found: {profile_path}", None

    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"Invalid profile JSON: {e}", None

    auth_state_path = _auth_state_for_profile(profile_path)
    if not auth_state_path.exists():
        return False, f"No auth state at {auth_state_path}. Run discovery (login + capture) first.", None

    if not await auth_module.ensure_session_fresh():
        return False, "Session invalid. Run discovery (login + capture) first.", None

    security = profile.get("security", {})
    needs_browser = security.get("requires_browser_fetch", False)

    async def _run_dry_run(page, request_context):
        return await profile_send.execute_message_flow(
            profile,
            test_payload,
            page=page,
            request_context=request_context,
            state={},
            is_first_message=True,
            verbose=verbose,
        )

    def _check_result(r):
        status = r.get("status")
        ok = r.get("ok", False)
        response = r.get("response", "")
        if not ok and status and 200 <= status < 300:
            ok = True
        if ok and status and status >= 400:
            ok = False
        if not ok:
            return False, f"Dry-run failed: status={status}, response={response[:200] if response else 'empty'}"
        if not response or not str(response).strip():
            return False, "Dry-run produced empty response"
        if _is_save_only_response(str(response)):
            return False, "Dry-run returned save-only placeholder; profile may need response_source=dom with extract_response_from_dom"
        if _is_failure_placeholder(str(response)):
            return False, f"Dry-run produced failure placeholder: {response[:150]}"
        return True, None

    async with async_playwright() as p:
        page = None
        browser = None
        context = None
        request_context = None

        try:
            if needs_browser:
                browser = await p.chromium.launch(headless=True)
                ctx_kwargs: dict = {
                    "storage_state": str(auth_state_path),
                    "viewport": {"width": 1280, "height": 720},
                }
                proxy = evasion.get_playwright_proxy()
                if proxy:
                    ctx_kwargs["proxy"] = proxy
                context = await browser.new_context(**ctx_kwargs)
                page = await context.new_page()
                if await evasion.apply_stealth(page):
                    pass
                challenge_url = _config.BASE_URL or profile.get("api_base", "")
                await page.goto(challenge_url, wait_until="domcontentloaded", timeout=60000)
                for _ in range(12):
                    pg_title = await page.title()
                    if not any(ph in pg_title.lower() for ph in ("just a moment", "checking your browser", "please wait")):
                        break
                    await page.wait_for_timeout(2500)
            else:
                proxy = evasion.get_playwright_proxy()
                request_context = await p.request.new_context(
                    storage_state=str(auth_state_path),
                    proxy=proxy,
                )

            r = await _run_dry_run(page, request_context)
            ok, err = _check_result(r)

            if ok:
                return True, None, None

            # Retry with Playwright headless browser if WAF detected (not 500: that's payload/path, use LLM)
            if _should_retry_with_browser(r, profile, used_browser=(page is not None)):
                if verbose:
                    print("[*] 5xx or WAF detected; retrying with Playwright headless browser ...")
                if request_context:
                    await request_context.dispose()
                    request_context = None
                if context:
                    await context.close()
                    context = None
                if browser:
                    await browser.close()
                    browser = None

                browser = await p.chromium.launch(headless=True)
                ctx_kwargs = {
                    "storage_state": str(auth_state_path),
                    "viewport": {"width": 1280, "height": 720},
                }
                proxy = evasion.get_playwright_proxy()
                if proxy:
                    ctx_kwargs["proxy"] = proxy
                context = await browser.new_context(**ctx_kwargs)
                page = await context.new_page()
                if await evasion.apply_stealth(page):
                    pass
                challenge_url = _config.BASE_URL or profile.get("api_base", "")
                await page.goto(challenge_url, wait_until="domcontentloaded", timeout=60000)
                for _ in range(12):
                    pg_title = await page.title()
                    if not any(ph in pg_title.lower() for ph in ("just a moment", "checking your browser", "please wait")):
                        break
                    await page.wait_for_timeout(2500)

                r = await _run_dry_run(page, None)
                ok2, err2 = _check_result(r)
                if ok2:
                    if verbose:
                        print("[+] Browser retry succeeded.")
                    # Persist so future runs use browser by default
                    if "security" not in profile:
                        profile["security"] = {}
                    profile["security"]["requires_browser_fetch"] = True
                    profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
                    return True, None, None
                return False, err2, r

            return False, err, r

        finally:
            if request_context:
                await request_context.dispose()
            if context:
                await context.close()
            if browser:
                await browser.close()


def _repair_profile_for_500(profile: dict, result: dict) -> dict | None:
    """Call Gemini to fix profile when 500 indicates badly formatted payloads or wrong paths."""
    if not _GEMINI_AVAILABLE:
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    step_id = result.get("failed_step_id", "?")
    method = result.get("failed_method", "POST")
    url = result.get("failed_url", "")
    body_preview = result.get("failed_body_preview", "")
    resp_body = result.get("failed_resp_body", "")

    prompt = f"""The site profile step '{step_id}' returned HTTP 500. This usually means:
- Badly formatted request body (wrong JSON structure, wrong field names, wrong types)
- Incorrect API path or URL
- Missing or incorrect required fields
- Wrong Content-Type or headers

Request: {method} {url}
Body sent (preview): {body_preview[:1200]}

Server response (500): {resp_body[:2000]}

Current profile:
```json
{json.dumps(profile, indent=2)}
```

Identify the cause and return a corrected site_profile.json. Focus on:
- message_flow.steps: fix body_template structure, field names, URL path
- dynamic_values: ensure placeholders resolve correctly (use only input, generate, step:<id>, cookie:<name>, header:<name>)
- auth.extra_headers: add any required headers

Return ONLY valid JSON, no markdown or explanation."""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = response.text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)
    except Exception:
        return None


def _repair_profile_with_llm(profile: dict, error_msg: str, trace_snippet: str | None = None) -> dict | None:
    """Call Gemini to suggest a corrected profile. Returns repaired profile or None."""
    if not _GEMINI_AVAILABLE:
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    prompt = f"""The site profile has validation/dry-run failure:

{error_msg}

Current profile:
```json
{json.dumps(profile, indent=2)}
```

Return a corrected site_profile.json that fixes the issue. Common fixes:
- Wrong response_source: if API returns save-only, use response_source=dom and add extract_response_from_dom with selector
- Missing extract_response_from_dom.selector when response_source=dom
- Unsupported dynamic value sources: use only input, generate, step:<id>, cookie:<name>, header:<name>
- body_template must be a flat JSON object, not array of {{name, value}}

Return ONLY valid JSON, no markdown or explanation."""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = response.text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)
    except Exception:
        return None


async def validate_profile(
    profile_path: Path | None = None,
    *,
    llm_repair: bool = True,
    verbose: bool = False,
) -> bool:
    """
    Validate profile with dry-run. If validation fails and llm_repair=True,
    attempt LLM-based repair and re-validate once.

    Returns True if validation passed (or repair succeeded).
    """
    path = profile_path or _config.SITE_STATE_DIR / "site_profile.json"
    ok, err, result = await validate_profile_dry_run(
        path, llm_repair=False, verbose=verbose
    )
    if ok:
        return True
    if not llm_repair or not err:
        if verbose and err:
            print(f"[!] Profile validation failed: {err}")
        return False

    # Attempt LLM repair
    if not _GEMINI_AVAILABLE:
        if verbose:
            print("[!] Profile validation failed. LLM repair unavailable (google-genai not installed).")
        return False

    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    # 500 = payload/path issue: use specialized repair with full error context
    if result and result.get("status") == 500:
        if verbose:
            print(f"[*] 500 error (payload/path). Attempting LLM repair with error context ...")
        repaired = _repair_profile_for_500(profile, result)
    else:
        if verbose:
            print(f"[*] Validation failed: {err}. Attempting LLM repair ...")
        repaired = _repair_profile_with_llm(profile, err)
    if repaired is None:
        if verbose:
            print("[!] LLM repair failed or returned invalid JSON.")
        return False

    path.write_text(json.dumps(repaired, indent=2), encoding="utf-8")
    if verbose:
        print("[*] Repaired profile saved. Re-validating ...")

    ok2, _ = await validate_profile_dry_run(path, llm_repair=False, verbose=verbose)
    return ok2
