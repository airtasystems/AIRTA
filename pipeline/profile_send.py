"""
Profile-based adaptive send engine.

Reads site_profile.json and replays the message flow for any site, without
site-specific code. Supports direct API requests or in-page browser fetch
for WAF-protected sites.
"""
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from component_discovery import auth as auth_module
from component_discovery import config as _config
from . import evasion

try:
    _root = Path(__file__).resolve().parent.parent
    from dotenv import load_dotenv
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
    load_dotenv()
    from google import genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


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


def _log_request_format(
    method: str,
    url: str,
    headers: dict[str, str],
    body_str: str,
    multipart_data: dict[str, Any] | None,
) -> None:
    """Print exact request format to terminal for debugging."""
    print("    --- REQUEST ---")
    print(f"    {method} {url}")
    print("    Headers:")
    for k, v in (headers or {}).items():
        print(f"      {k}: {v}")
    if multipart_data is not None:
        print("    Body (multipart form-data):")
        for k, v in multipart_data.items():
            preview = repr(v)[:200] + ("..." if len(repr(v)) > 200 else "")
            print(f"      {k}: {preview}")
    elif body_str:
        preview = body_str[:500] + ("..." if len(body_str) > 500 else "")
        print(f"    Body: {preview}")
    print("    ---")


# ---------------------------------------------------------------------------
#  Response parsers
# ---------------------------------------------------------------------------

def _parse_json_response(body: str, content_path: str | None = None) -> str:
    """Extract text from a JSON response body."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body

    if content_path:
        for part in content_path.split("."):
            if isinstance(data, dict):
                data = data.get(part, data)
            elif isinstance(data, list) and part.isdigit():
                data = data[int(part)]
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("content", "text", "response", "output", "message", "answer"):
            if key in data and isinstance(data[key], str):
                return data[key]
    return json.dumps(data) if not isinstance(data, str) else data


def _parse_sse_response(body: str, content_path: str | None = None) -> str:
    """Parse SSE stream and concatenate data lines."""
    chunks: list[str] = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
                text = _extract_sse_text(obj)
                if text:
                    chunks.append(text)
            except json.JSONDecodeError:
                if payload:
                    chunks.append(payload)
    return "".join(chunks) if chunks else body


def _extract_sse_text(obj: Any) -> str:
    """Extract text content from a parsed SSE JSON object (OpenAI-style delta or direct)."""
    if isinstance(obj, dict):
        # OpenAI streaming: choices[0].delta.content
        choices = obj.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content:
                return content
        for key in ("content", "text", "response", "token", "output"):
            if key in obj and isinstance(obj[key], str):
                return obj[key]
    return ""


def _parse_sse_patches_response(body: str, content_path: str | None = None) -> str:
    """Parse SSE stream with JSON patches (e.g. Mistral-style)."""
    content = ""
    for line in body.split("\n"):
        try:
            idx = line.find("{")
            if idx < 0:
                continue
            obj = json.loads(line[idx:])
            j = obj.get("json", obj)
            patches = j.get("patches", [])
            for p in patches:
                op = p.get("op", "")
                path = p.get("path", "")
                value = p.get("value", "")
                if op == "replace" and path == "/contentChunks" and isinstance(value, list):
                    content = "".join(c.get("text", "") for c in value)
                elif op == "append" and "/text" in path:
                    content += value or ""
                elif op == "replace" and path == "/" and isinstance(value, dict) and "content" in value:
                    content = value["content"]
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return content


def _parse_text_response(body: str, content_path: str | None = None) -> str:
    return body


_PARSERS = {
    "json": _parse_json_response,
    "sse": _parse_sse_response,
    "sse_patches": _parse_sse_patches_response,
    "text": _parse_text_response,
}


# ---------------------------------------------------------------------------
#  Template rendering
# ---------------------------------------------------------------------------

def _render_template(template: Any, state: dict[str, str]) -> Any:
    """
    Recursively replace {{PLACEHOLDER}} in a body template with values from state.
    Works on strings, dicts, and lists.
    """
    if isinstance(template, str):
        def replacer(m: re.Match) -> str:
            key = m.group(1)
            return state.get(key, m.group(0))
        return re.sub(r"\{\{(\w+)\}\}", replacer, template)
    if isinstance(template, dict):
        return {k: _render_template(v, state) for k, v in template.items()}
    if isinstance(template, list):
        return [_render_template(item, state) for item in template]
    return template


def _resolve_dynamic_values(dynamic_values: dict, state: dict[str, str]) -> None:
    """Generate any values that should be created fresh (e.g. UUIDs, chat history from USER_TEXT)."""
    for name, spec in dynamic_values.items():
        if name in state:
            continue
        source = spec.get("source", "")
        vtype = spec.get("type", "")
        if source == "generate" and vtype == "uuidv4":
            state[name] = str(uuid.uuid4())
        elif vtype == "json_array" and "USER_TEXT" in state:
            # Build conversation history from current user message (canonical: CHAT_HISTORY)
            # Use compact JSON (no spaces) to match browser behavior; some APIs are strict
            if name in ("CHAT_HISTORY", "CHAT_HISTORY_JSON_ARRAY", "CHAT_HISTORY_JSON", "MESSAGES", "messages"):
                state[name] = json.dumps([{"role": "user", "content": state["USER_TEXT"]}], separators=(",", ":"))
        elif source == "input" and "USER_TEXT" in state:
            # Input-type placeholders (CURRENT_USER_MESSAGE, USER_MESSAGE, MESSAGE, etc.) receive the user text
            state[name] = state["USER_TEXT"]
        elif source == "static":
            # Static value from spec (e.g. value, default) or generate uuidv4 for session IDs
            if "value" in spec:
                state[name] = str(spec["value"])
            elif "default" in spec:
                state[name] = str(spec["default"])
            elif vtype == "uuidv4" or (not vtype and "identifier" in name.lower()):
                state[name] = str(uuid.uuid4())


def _get_placeholders_from_template(template: Any) -> set[str]:
    """Extract {{PLACEHOLDER}} names from a template (dict, list, or str)."""
    if isinstance(template, str):
        return set(re.findall(r"\{\{(\w+)\}\}", template))
    if isinstance(template, dict):
        return set().union(*(_get_placeholders_from_template(v) for v in template.values()))
    if isinstance(template, list):
        return set().union(*(_get_placeholders_from_template(x) for x in template))
    return set()


def _is_unsupported_source(source: str) -> bool:
    """True if source is not supported by _resolve_dynamic_values."""
    if not source:
        return False
    return source not in ("input", "generate", "static") and not source.startswith("step:") and not source.startswith("cookie:") and not source.startswith("header:")


def _resolve_placeholder_via_llm(name: str, spec: dict, state: dict[str, str]) -> str | None:
    """Call Gemini to produce value for unsupported placeholder. Returns value or None."""
    if not _GEMINI_AVAILABLE:
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    user_text = state.get("USER_TEXT", "")
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    prompt = f"""Given state={json.dumps({k: v[:200] + "..." if len(str(v)) > 200 else v for k, v in state.items()})}, user_text={json.dumps(user_text)}, and this dynamic value spec: {json.dumps(spec)}.

The spec describes building a value (e.g. a JSON array of messages for chat history). Produce the value string. Return ONLY the value string, no explanation, no markdown."""
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        text = (response.text or "").strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        return text if text else None
    except Exception:
        return None


def _resolve_unresolved_placeholders_via_llm(
    body_template: Any,
    dynamic_values: dict,
    state: dict[str, str],
) -> None:
    """For placeholders not in state with unsupported sources, try LLM fallback."""
    placeholders = _get_placeholders_from_template(body_template)
    for ph in placeholders:
        if ph in state:
            continue
        spec = dynamic_values.get(ph)
        if not spec or not _is_unsupported_source(spec.get("source", "")):
            continue
        value = _resolve_placeholder_via_llm(ph, spec, state)
        if value is not None:
            state[ph] = value


def _extract_values(
    response_body: str,
    extract_spec: dict[str, str],
    state: dict[str, str],
    fallback_extract: dict[str, str] | None = None,
) -> None:
    """
    Extract dynamic values from a response body using regex or jsonpath specs.
    When primary extract fails for a variable, tries fallback_extract (regex only).
    """
    fallback_extract = fallback_extract or {}

    def _try_extract(spec: dict[str, str]) -> set[str]:
        """Apply spec, return set of var names that were successfully extracted."""
        extracted_names: set[str] = set()
        for var_name, pattern in spec.items():
            if pattern.startswith("regex:"):
                regex = pattern[6:]
                m = re.search(regex, response_body)
                if m:
                    state[var_name] = m.group(1) if m.lastindex else m.group(0)
                    extracted_names.add(var_name)
            elif pattern.startswith("jsonpath:") or pattern.startswith("json:"):
                path = pattern[9:] if pattern.startswith("jsonpath:") else pattern[5:]
                for candidate in (response_body, *response_body.split("\n")):
                    try:
                        raw = candidate.strip()
                        if not raw:
                            continue
                        data = json.loads(raw)
                        parts = [p for p in re.split(r"[\.\[\]]+", path) if p and p != "$"]
                        for part in parts:
                            if isinstance(data, list) and part.isdigit():
                                idx = int(part)
                                data = data[idx] if idx < len(data) else None
                            elif isinstance(data, dict):
                                data = data.get(part)
                            else:
                                data = None
                            if data is None:
                                break
                        if isinstance(data, str):
                            state[var_name] = data
                            extracted_names.add(var_name)
                            break
                        elif data is not None and isinstance(data, (int, float, bool)):
                            state[var_name] = str(data)
                            extracted_names.add(var_name)
                            break
                    except (json.JSONDecodeError, TypeError, IndexError, KeyError):
                        continue
        return extracted_names

    _try_extract(extract_spec)
    # Try fallback for any variables that failed (fallback must use regex)
    missing = set(extract_spec.keys()) - set(state.keys())
    if missing and fallback_extract:
        fallback_spec = {k: v for k, v in fallback_extract.items() if k in missing and v.startswith("regex:")}
        if fallback_spec:
            _try_extract(fallback_spec)


# ---------------------------------------------------------------------------
#  Core: execute_message_flow
# ---------------------------------------------------------------------------

def _suggest_profile_fix_on_failure(
    step_id: str,
    method: str,
    url: str,
    headers: dict,
    body_preview: str,
    status: int,
    resp_body: str,
) -> str | None:
    """Call Gemini to suggest a minimal profile fix for a failed step. Returns suggestion or None."""
    if not _GEMINI_AVAILABLE:
        return None
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    prompt = f"""Profile step '{step_id}' failed.

Request: {method} {url}
Headers: {json.dumps(headers, indent=2)[:500]}
Body (preview): {body_preview[:500]}

Response: status={status}
Body: {resp_body[:1000]}

Suggest a minimal fix to the profile (e.g. wrong header, wrong body format, wrong URL). Return a brief JSON patch or the corrected step object. Be concise."""
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=model, contents=prompt)
        return (response.text or "").strip()
    except Exception:
        return None


async def execute_message_flow(
    profile: dict,
    user_text: str,
    *,
    page: Page | None = None,
    request_context: Any | None = None,
    state: dict[str, str] | None = None,
    is_first_message: bool = True,
    verbose: bool = False,
    repair_on_failure: bool = False,
) -> dict:
    """
    Execute the full message-send flow from a site profile.

    Returns {status, ok, response, state}.
    """
    if state is None:
        state = {}
    state["USER_TEXT"] = user_text

    flow = profile.get("message_flow", {})
    steps = flow.get("steps", [])
    dynamic_values = profile.get("dynamic_values", {})
    response_parsing = profile.get("response_parsing", {})
    security = profile.get("security", {})
    auth = profile.get("auth", {})

    _resolve_dynamic_values(dynamic_values, state)

    # Use browser when: profile requires it, or we have page but no request_context (retry-after-5xx/WAF)
    use_browser = page is not None and (
        security.get("requires_browser_fetch", False) or request_context is None
    )

    last_result: dict = {"status": None, "ok": False, "response": "", "state": state}

    for step in steps:
        when = step.get("when", "always")
        if when == "first_message_only" and not is_first_message:
            continue
        if when == "subsequent_messages" and is_first_message:
            continue

        # Re-resolve dynamic values before each step (a previous step may have populated sources)
        _resolve_dynamic_values(dynamic_values, state)

        step_url = step.get("url", "")
        if step_url.startswith("/"):
            api_base = profile.get("api_base", "")
            if api_base:
                base = api_base.rstrip("/")
                step_url = base + step_url
            else:
                base_url = _config.BASE_URL or ""
                step_url = base_url.rstrip("/") + step_url

        method = step.get("method", "POST").upper()
        step_headers = dict(step.get("headers", {}))

        extra_headers = auth.get("extra_headers", {})
        if extra_headers:
            step_headers.update(extra_headers)

        body_template = step.get("body_template")
        body_str = ""
        multipart_data: dict[str, Any] | None = None
        if body_template is not None:
            # LLM fallback for placeholders with unsupported sources (e.g. dynamic_composition)
            _resolve_unresolved_placeholders_via_llm(body_template, dynamic_values, state)
            rendered = _render_template(body_template, state)
            ct = (step_headers.get("Content-Type") or step_headers.get("content-type") or "").lower()
            # Convert array-of-{name,value} (Gemini output) to dict for multipart
            if "multipart/form-data" in ct and isinstance(rendered, list):
                _d: dict[str, str] = {}
                for item in rendered:
                    if isinstance(item, dict) and "name" in item:
                        _d[item["name"]] = str(item.get("value", ""))
                rendered = _d
            if "multipart/form-data" in ct and isinstance(rendered, dict):
                multipart_data = {k: str(v) if v is not None else "" for k, v in rendered.items()}
                # Browser fetch needs raw body; build it for that path
                boundary_match = re.search(r'boundary=(["\']?)([^\s;"\']+)\1', ct)
                if boundary_match:
                    boundary = boundary_match.group(2).strip()
                else:
                    import random
                    boundary = "----WebKitFormBoundary" + "".join(
                        random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
                        for _ in range(16)
                    )
                parts = []
                for k, v in multipart_data.items():
                    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
                parts.append(f"--{boundary}--\r\n")
                body_str = "".join(parts)
                # Browser fetch needs Content-Type with boundary in header
                step_headers = {k: v for k, v in step_headers.items() if k.lower() != "content-type"}
                step_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            else:
                body_str = json.dumps(rendered) if not isinstance(rendered, str) else rendered

        if "Content-Type" not in step_headers and "content-type" not in (k.lower() for k in step_headers):
            step_headers["Content-Type"] = "application/json"

        # When using browser + DOM extraction, navigate to chat URL with uuid so the page shows the right chat
        chat_page_url = profile.get("chat_page_url") or _config.BASE_URL or ""
        if (
            page is not None
            and chat_page_url
            and "CHAT_ID" in state
            and step.get("extract_response_from_dom")
        ):
            nav_url = f"{chat_page_url.rstrip('/')}#{state['CHAT_ID']}"
            try:
                await page.goto(nav_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass

        if verbose:
            print(f"    [{step.get('id', '?')}] {method} {step_url}")

        # For API fetch with multipart: use Playwright's multipart param (RFC-compliant) and drop Content-Type
        api_headers = step_headers
        if multipart_data is not None and not use_browser:
            api_headers = {k: v for k, v in step_headers.items() if k.lower() != "content-type"}

        if verbose:
            _log_request_format(method, step_url, api_headers, body_str, multipart_data)

        try:
            if use_browser:
                resp = await _browser_fetch(page, step_url, method, step_headers, body_str)
            elif request_context is not None:
                resp = await _api_fetch(request_context, step_url, method, api_headers, body_str, multipart_data)
            else:
                last_result = {"status": None, "ok": False, "response": "No request context or page", "state": state}
                break
        except Exception as exc:
            last_result = {"status": None, "ok": False, "response": str(exc), "state": state}
            break

        resp_body = resp.get("body", "")
        resp_status = resp.get("status", 0)
        resp_ok = resp.get("ok", False)

        # Extract values from response (primary + profile-defined fallback)
        extract_spec = step.get("extract", {})
        fallback_extract = step.get("fallback_extract", {})
        if extract_spec or fallback_extract:
            _extract_values(resp_body, extract_spec, state, fallback_extract=fallback_extract)

        # Parse response if this step has a response_format
        response_format = step.get("response_format")
        parsed_content = resp_body
        if response_format and response_format in _PARSERS:
            parsing_config = response_parsing.get(response_format, {})
            content_path = parsing_config.get("content_path")
            parsed_content = _PARSERS[response_format](resp_body, content_path)

        # When response_source is "dom", API returns save confirmation; AI text is in DOM.
        # Only run DOM extraction when response_source is "dom" (skip for api/sse/ws).
        response_source = step.get("response_source", "api")
        dom_extract = step.get("extract_response_from_dom")
        if (
            response_source == "dom"
            and dom_extract
            and page is not None
            and resp_ok
            and isinstance(dom_extract, dict)
        ):
            selector = dom_extract.get("selector", "")
            timeout_ms = dom_extract.get("timeout_ms", 15000)
            exclude_text = dom_extract.get("exclude_text")
            wait_after_send_ms = dom_extract.get("wait_after_send_ms", 0)
            if wait_after_send_ms > 0:
                await asyncio.sleep(wait_after_send_ms / 1000.0)
            if selector:
                dom_text = await _extract_text_from_dom(
                    page, selector, timeout_ms, exclude_text=exclude_text
                )
                if dom_text:
                    parsed_content = dom_text
                    if verbose:
                        print(f"    [dom] Extracted {len(dom_text)} chars from selector")
                elif _is_save_only_response(resp_body):
                    parsed_content = "(AI response not in API; DOM selector did not find message. Inspect page and update site_profile extract_response_from_dom.selector)"
                    if verbose:
                        print("    [dom] No message found; response is save-only")
        elif response_source == "api" and _is_save_only_response(resp_body):
            # Misconfiguration: profile says api but body is save-only (AI text likely in DOM)
            parsed_content = "(Misconfiguration: response_source=api but API returned save-only. Consider response_source=dom with extract_response_from_dom.)"
            if verbose:
                print("    [api] Save-only response; profile may need response_source=dom")

        last_result = {
            "status": resp_status,
            "ok": resp_ok,
            "response": parsed_content,
            "state": state,
        }

        if not resp_ok:
            if resp_status and resp_status >= 500:
                last_result["failed_step_id"] = step.get("id", "?")
                last_result["failed_method"] = method
                last_result["failed_url"] = step_url
                last_result["failed_body_preview"] = (body_str or "")[:1500]
                last_result["failed_resp_body"] = (resp_body or "")[:3000]
            if verbose:
                print(f"    [{step.get('id', '?')}] FAILED {resp_status}: {resp_body[:200]}")
            if (verbose or repair_on_failure) and resp_status and resp_status >= 400 and _GEMINI_AVAILABLE:
                suggestion = _suggest_profile_fix_on_failure(
                    step.get("id", "?"),
                    method,
                    step_url,
                    dict(step_headers),
                    body_str[:500] if body_str else "",
                    resp_status,
                    resp_body[:2000],
                )
                if suggestion and verbose:
                    print(f"    [LLM suggestion] {suggestion[:400]}")
            break

    return last_result


# ---------------------------------------------------------------------------
#  Transport: browser-based fetch and direct API fetch
# ---------------------------------------------------------------------------

async def _browser_fetch(
    page: Page,
    url: str,
    method: str,
    headers: dict[str, str],
    body: str,
) -> dict:
    """Execute fetch() inside the browser page context."""
    result = await page.evaluate(
        """async ([url, method, headers, body]) => {
            try {
                const opts = {method, headers, credentials: 'include'};
                if (body && method !== 'GET') opts.body = body;
                const resp = await fetch(url, opts);
                const text = await resp.text();
                return {status: resp.status, ok: resp.ok, body: text};
            } catch (e) {
                return {status: null, ok: false, body: e.message};
            }
        }""",
        [url, method, headers, body],
    )
    return result


async def _extract_text_from_dom(
    page: Page,
    selector: str,
    timeout_ms: int,
    exclude_text: str | None = None,
) -> str:
    """Wait for selector and return text of last matching visible element (for streaming AI responses).
    Skips elements whose text contains exclude_text (e.g. 'An error occurred')."""
    try:
        for sel in (s.strip() for s in selector.split(",") if s.strip()):
            try:
                await page.wait_for_selector(sel, timeout=timeout_ms / 1000.0, state="visible")
                all_els = await page.query_selector_all(sel)
                # From last to first, take the first visible element whose text is not excluded
                for target in reversed(all_els or []):
                    try:
                        if not await target.is_visible():
                            continue
                        text = (await target.text_content()) or ""
                        text = text.strip()
                        if not text:
                            continue
                        if exclude_text and exclude_text.lower() in text.lower():
                            continue
                        return text
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return ""


async def _api_fetch(
    request_context: Any,
    url: str,
    method: str,
    headers: dict[str, str],
    body: str,
    multipart_data: dict[str, Any] | None = None,
) -> dict:
    """Execute request via Playwright's API request context."""
    try:
        if method == "GET":
            response = await request_context.get(url, headers=headers)
        else:
            response = await evasion.post_with_retry_429(
                request_context, url, headers, body, multipart_data=multipart_data
            )
        resp_text = await response.text()
        return {"status": response.status, "ok": response.ok, "body": resp_text}
    except evasion.RateLimit429:
        return {"status": 429, "ok": False, "body": "Rate limited (429)"}
    except evasion.RetryableServerError as e:
        status = getattr(e.response, "status", 503)
        return {"status": status, "ok": False, "body": getattr(e, "body_text", "")}
    except Exception as exc:
        return {"status": None, "ok": False, "body": str(exc)}


# ---------------------------------------------------------------------------
#  Convenience: load profile, launch browser if needed, send all payloads
# ---------------------------------------------------------------------------

def load_site_profile() -> dict | None:
    """Load site_profile.json for the current site/component, or None if absent."""
    profile_path = _config.SITE_STATE_DIR / "site_profile.json"
    if not profile_path.exists():
        return None
    try:
        return json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


async def send_with_profile(
    payloads: list[dict],
    profile: dict,
    *,
    verbose: bool = True,
    speed: int = 1,
) -> list[dict]:
    """
    Send a list of payload dicts using the site profile.

    Each payload should have at least 'text' (the user message) and 'title' (for logging).
    The profile's message_flow handles all formatting.
    """
    from playwright.async_api import async_playwright

    security = profile.get("security", {})
    needs_browser = security.get("requires_browser_fetch", False)

    if not await auth_module.ensure_session_fresh():
        if verbose:
            print("[-] Session invalid. Run discovery (login + capture) first.")
        return []

    results: list[dict] = []
    concurrency = max(1, min(8, speed))
    token_bucket = evasion.get_token_bucket_or_default(concurrency)

    async with async_playwright() as p:
        page = None
        browser = None
        context = None
        request_context = None

        try:
            if needs_browser:
                browser = await p.chromium.launch(headless=True)
                ctx_kwargs: dict = {
                    "storage_state": str(_config.AUTH_STATE_FILE),
                    "viewport": {"width": 1280, "height": 720},
                }
                proxy = evasion.get_playwright_proxy()
                if proxy:
                    ctx_kwargs["proxy"] = proxy
                context = await browser.new_context(**ctx_kwargs)
                page = await context.new_page()
                if await evasion.apply_stealth(page):
                    if verbose:
                        print("[*] Stealth applied.")

                challenge_url = _config.BASE_URL or profile.get("api_base", "")
                if verbose:
                    print(f"[*] Navigating to {challenge_url} to solve challenge ...")
                await page.goto(challenge_url, wait_until="domcontentloaded", timeout=60000)

                _challenge_phrases = ("just a moment", "checking your browser", "please wait")
                for _attempt in range(24):
                    pg_title = await page.title()
                    if not any(ph in pg_title.lower() for ph in _challenge_phrases):
                        break
                    if verbose and _attempt == 0:
                        print("[*] Waiting for WAF challenge to resolve ...")
                    await page.wait_for_timeout(2500)

                if verbose:
                    print(f"[*] Page ready: {await page.title()}")
            else:
                proxy = evasion.get_playwright_proxy()
                request_context = await p.request.new_context(
                    storage_state=str(_config.AUTH_STATE_FILE),
                    proxy=proxy,
                )

            for i, payload in enumerate(payloads):
                title = payload.get("title", f"Payload {i+1}")
                user_text = payload.get("text") or payload.get("title") or ""

                if i > 0:
                    if token_bucket:
                        await token_bucket.acquire()
                    else:
                        await asyncio.sleep(evasion.THROTTLE_BETWEEN_PAYLOADS_SEC)

                # Each payload starts a new conversation (avoids 409 conflicts from
                # rapid appends to the same chat; diagnostics/tests are independent)
                state: dict[str, str] = {}
                is_first = True
                r = await execute_message_flow(
                    profile,
                    user_text,
                    page=page,
                    request_context=request_context,
                    state=state,
                    is_first_message=is_first,
                    verbose=verbose,
                )

                result = {
                    "title": title,
                    "status": r.get("status"),
                    "ok": r.get("ok", False),
                    "response": r.get("response", ""),
                }

                if verbose:
                    st = result["status"] or "?"
                    if result["ok"]:
                        snippet = (result["response"] or "")[:120]
                        print(f"  [{st}] {title}: {snippet}")
                    else:
                        print(f"  [{st}] {title}")

                results.append(result)

        finally:
            if request_context:
                await request_context.dispose()
            if context:
                await context.close()
            if browser:
                await browser.close()

    return results
