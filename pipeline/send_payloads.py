"""
Send payloads from payloads.json to the discovered endpoint. Prefers site_profile.json
(profile-based adaptive send) when available. Falls back to site-specific
payload_format.py, then to a minimal fallback (title -> title, text -> description).
"""
import asyncio
import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from component_discovery import auth as auth_module
from component_discovery import config as _config
from component_discovery import payload_format as payload_format_shared
from . import evasion
from . import profile_send


def _AUTH_STATE_FILE():
    return _config.AUTH_STATE_FILE

def _CSRF_TOKEN_FILE():
    return _config.CSRF_TOKEN_FILE

def _DISCOVERED_ENDPOINT_FILE():
    return _config.DISCOVERED_ENDPOINT_FILE

def _PAYLOADS_FILE():
    return _config.PAYLOADS_FILE

def _SITE_STATE_DIR():
    return _config.SITE_STATE_DIR


def _get_site_payload_format_module():
    """Load site payload_format.py if present (e.g. localhost3000/payload_format.py)."""
    site_py = _SITE_STATE_DIR() / "payload_format.py"
    if not site_py.exists():
        return None
    spec = importlib.util.spec_from_file_location("site_payload_format", site_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_body_fallback(payload_format: dict[str, Any], overrides: dict[str, str] | None, **_kwargs: Any) -> tuple[str, str]:
    """Minimal build when no site payload_format: overrides are field names (e.g. title, description)."""
    overrides = overrides or {}
    encoding = payload_format.get("encoding", "unknown")
    fields = dict(payload_format.get("fields", {}))
    fields.update(overrides)
    if encoding == "multipart/form-data":
        boundary = payload_format.get("boundary", "----formboundary")
        lines = []
        for name, value in fields.items():
            lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
        lines.append(f"--{boundary}--\r\n")
        return "".join(lines), f"multipart/form-data; boundary={boundary}"
    return json.dumps(fields), "application/json"


def _load_csrf() -> str:
    if not _CSRF_TOKEN_FILE().exists():
        return ""
    try:
        data = json.loads(_CSRF_TOKEN_FILE().read_text())
        return data.get("csrf_token", "")
    except (json.JSONDecodeError, OSError):
        return ""


# Patterns indicating a security/WAF challenge page (Cloudflare, etc.) instead of API response
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


def _is_security_block_response(result: dict) -> bool:
    """Return True if the response indicates a security service (e.g. Cloudflare) blocked the request."""
    status = result.get("status")
    if status not in (403, 503, 520, 521, 522, 523):
        return False
    resp = result.get("response") or ""
    if not resp or len(resp) < 100:
        return False
    resp_lower = resp.lower()
    return any(p in resp_lower for p in _SECURITY_BLOCK_PATTERNS)


def _extract_assistant_content(resp_text: str | None) -> str:
    """Extract assistant message content from API response. Handles JSON like {\"content\":\"...\"}."""
    if not resp_text or not resp_text.strip():
        return ""
    text = resp_text.strip()
    if (text.startswith("{") or text.startswith("[")) and len(text) > 1:
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "content" in data:
                return str(data["content"]) if data["content"] is not None else ""
            if isinstance(data, list) and len(data) > 0:
                last = data[-1]
                if isinstance(last, dict) and "content" in last:
                    return str(last["content"]) if last["content"] is not None else ""
        except json.JSONDecodeError:
            pass
    return resp_text


async def send_payloads_from_list(
    payloads_list: list[dict],
    *,
    verbose: bool = True,
    speed: int = 1,
) -> list[dict]:
    """
    POST each payload in payloads_list to the discovered endpoint.

    speed: 1 = sequential with evasion (throttle + tenacity retry); 2–8 = up to N
    concurrent requests (asyncio semaphore). Tenacity retry still applies per request.

    For sites with a site-specific payload_format.py (e.g. chat components),
    each payload dict is passed through as overrides directly to build_body()
    so the site module can interpret keys like "messages", "title", etc.

    For sites without a site-specific payload_format, payloads_list items
    should have at least "title" and "text"; these are mapped via the shared
    fallback format (title -> title, text -> description).

    Returns list of {title, status, ok, response, error?} for the caller to log.
    """
    # Profile-first path: if site_profile.json exists, delegate to the adaptive engine
    site_profile = profile_send.load_site_profile()
    if site_profile is not None:
        if verbose:
            print("[*] Using site_profile.json (adaptive send engine).")
        return await profile_send.send_with_profile(
            payloads_list, site_profile, verbose=verbose, speed=speed,
        )

    if not _DISCOVERED_ENDPOINT_FILE().exists():
        if verbose:
            print(f"[-] No discovered endpoint at {_DISCOVERED_ENDPOINT_FILE()}. Run 'discover' first.")
        return []
    if not _AUTH_STATE_FILE().exists():
        if verbose:
            print(f"[-] No session at {_AUTH_STATE_FILE()}. Run 'login' first.")
        return []
    discovered = json.loads(_DISCOVERED_ENDPOINT_FILE().read_text())
    payload_format = discovered.get("payload_format")
    if not payload_format:
        payload_format = payload_format_shared.parse_payload_from_request(
            discovered.get("headers", {}),
            discovered.get("payload_schema"),
        )
        if not payload_format.get("fields"):
            if verbose:
                print("[-] Could not derive payload format from discovered endpoint.")
            return []
    site_payload = _get_site_payload_format_module()
    if site_payload is not None:
        build_body_fn = site_payload.build_body
        use_raw_overrides = True
        override_keys = None
    else:
        build_body_fn = _build_body_fallback
        use_raw_overrides = False
        override_keys = ("title", "description")
    if not await auth_module.ensure_session_fresh():
        if verbose:
            print("[-] Session invalid. Run discovery (login + capture) first.")
        return []
    url = discovered["url"]
    follow_up_url = discovered.get("follow_up_url")
    headers = dict(discovered.get("headers", {}))
    headers.pop("content-length", None)
    headers.pop("host", None)
    headers.pop("accept-encoding", None)
    csrf = _load_csrf()
    if csrf:
        headers["X-CSRF-Token"] = csrf
        headers["X-XSRF-TOKEN"] = csrf
    results: list[dict] = []
    proxy = evasion.get_playwright_proxy()
    concurrency = max(1, min(8, speed))
    token_bucket = evasion.get_token_bucket_or_default(concurrency)
    if verbose:
        parts = []
        if token_bucket:
            parts.append("token-bucket rate limit")
        if concurrency == 1:
            if not token_bucket:
                parts.append("throttle")
        else:
            parts.append(f"concurrent (up to {concurrency} at a time)")
            parts.append(f"{evasion.MIN_GAP_BETWEEN_REQUESTS_SEC}s gap between starts")
        parts.append("retry on 429")
        parts.append("header rotation")
        if proxy:
            parts.append("proxy=" + proxy["server"])
        print("[*] Evasion: " + ", ".join(parts) + ".")

    gap_lock = asyncio.Lock() if concurrency > 1 else None
    last_request_start: list[float] = [0.0] if concurrency > 1 else []

    async def _run_send(request_context, force_concurrency: int | None = None) -> list[dict]:
        """Send all payloads using the given request context (API or browser).
        force_concurrency: when set (e.g. 1 for browser retry), overrides concurrency to avoid triggering Cloudflare."""
        eff_concurrency = force_concurrency if force_concurrency is not None else concurrency
        out_results: list[dict] = []

        async def send_one(idx: int, p_item: dict, request_url: str | None = None) -> tuple[int, dict]:
            if token_bucket:
                await token_bucket.acquire()
            if gap_lock is not None:
                async with gap_lock:
                    now = time.monotonic()
                    if last_request_start[0] > 0:
                        wait = evasion.MIN_GAP_BETWEEN_REQUESTS_SEC - (now - last_request_start[0])
                        if wait > 0:
                            await asyncio.sleep(wait)
                    last_request_start[0] = time.monotonic()
            title = p_item.get("title", f"Payload {idx+1}")
            if use_raw_overrides:
                overrides = {k: v for k, v in p_item.items() if v is not None}
            else:
                text = p_item.get("text", "")
                overrides = {override_keys[0]: title, override_keys[1]: text}  # type: ignore[index]
            strategy = overrides.pop("strategy", "zero_shot") if use_raw_overrides else "zero_shot"
            overrides.pop("_is_init", None)
            body, content_type = build_body_fn(payload_format, overrides, strategy=strategy)
            req_headers = {**headers, "Content-Type": content_type, **evasion.rotated_headers()}
            target_url = request_url or url
            try:
                response = await evasion.post_with_retry_429(request_context, target_url, req_headers, body)
                resp_text = await response.text()
                out = {"title": title, "status": response.status, "ok": response.ok, "response": resp_text}
                if verbose and eff_concurrency == 1:
                    if not response.ok:
                        print(f"  [{response.status}] {title}" + (f" — {resp_text}" if resp_text else ""))
                    else:
                        print(f"  [{response.status}] {title}")
                return (idx, out)
            except evasion.RateLimit429:
                if verbose and eff_concurrency == 1:
                    print(f"  [429] {title} (max retries exceeded)")
                return (idx, {"title": title, "status": 429, "ok": False, "response": None})
            except evasion.RetryableServerError as e:
                status = getattr(e.response, "status", 503)
                if verbose and eff_concurrency == 1:
                    print(f"  [{status}] {title} (max retries exceeded)")
                return (idx, {"title": title, "status": status, "ok": False, "response": getattr(e, "body_text", None)})
            except Exception as e:
                if verbose and eff_concurrency == 1:
                    print(f"  [error] {title} — {e}")
                return (idx, {"title": title, "status": None, "ok": False, "error": str(e), "response": None})

        async def run_multiturn_async(i: int, p_item: dict) -> dict:
            """Run sequential multi-turn: send each prompt, capture response, append to history, repeat."""
            prompts = p_item.get("prompts") or []
            title = p_item.get("title", f"Payload {i+1}")
            if len(prompts) < 2:
                return {"title": title, "status": 400, "ok": False, "error": "multi_turn requires 2+ prompts", "response": None}
            messages: list[dict] = []
            last_r: dict | None = None
            for turn_idx, user_content in enumerate(prompts):
                messages.append({"role": "user", "content": user_content})
                payload_turn = {
                    "title": title,
                    "messages": json.dumps(messages),
                    "strategy": "multi_shot",
                }
                if not token_bucket and turn_idx > 0:
                    await asyncio.sleep(evasion.THROTTLE_BETWEEN_PAYLOADS_SEC)
                turn_url = follow_up_url if (follow_up_url and turn_idx > 0) else None
                _, r = await send_one(i, payload_turn, request_url=turn_url)
                last_r = r
                if not r.get("ok"):
                    return r
                content = _extract_assistant_content(r.get("response"))
                messages.append({"role": "assistant", "content": content})
            return last_r or {"title": title, "status": 500, "ok": False, "response": None}

        if eff_concurrency == 1:
            for i, p_item in enumerate(payloads_list):
                if i > 0 and not token_bucket:
                    await asyncio.sleep(evasion.THROTTLE_BETWEEN_PAYLOADS_SEC)
                if p_item.get("multi_turn") and p_item.get("prompts"):
                    r = await run_multiturn_async(i, p_item)
                else:
                    _, r = await send_one(i, p_item)
                out_results.append(r)
                if i == 0 and _is_security_block_response(r):
                    return out_results
        else:
            has_multiturn = any(p.get("multi_turn") for p in payloads_list)
            if has_multiturn:
                for i, p_item in enumerate(payloads_list):
                    if i > 0 and not token_bucket:
                        await asyncio.sleep(evasion.THROTTLE_BETWEEN_PAYLOADS_SEC)
                    if p_item.get("multi_turn") and p_item.get("prompts"):
                        r = await run_multiturn_async(i, p_item)
                        out_results.append(r)
                    else:
                        _, r = await send_one(i, p_item)
                        out_results.append(r)
                if verbose:
                    for r in out_results:
                        status = r.get("status", "?")
                        title = r.get("title", "")
                        ok = r.get("ok", False)
                        if not ok:
                            print(f"  [{status}] {title}")
                        else:
                            print(f"  [{status}] {title}")
            else:
                # Probe-first: when concurrent, send first request alone to avoid Cloudflare burst
                _, probe_r = await send_one(0, payloads_list[0])
                if _is_security_block_response(probe_r):
                    return [probe_r]  # Outer code will detect and retry with browser (concurrency 1)
                out_results.append(probe_r)
                # Send remaining payloads
                sem = asyncio.Semaphore(eff_concurrency)

                async def bounded_send(idx: int, p_item: dict) -> tuple[int, dict]:
                    async with sem:
                        return await send_one(idx, p_item)

                remaining = [(i, p) for i, p in enumerate(payloads_list) if i > 0]
                ordered: list[tuple[int, dict]] = await asyncio.gather(
                    *[bounded_send(i, p_item) for i, p_item in remaining]
                )
                ordered.sort(key=lambda x: x[0])
                out_results.extend(r for _, r in ordered)
                if verbose:
                    for r in out_results:
                        status = r.get("status", "?")
                        title = r.get("title", "")
                        ok = r.get("ok", False)
                        if not ok:
                            print(f"  [{status}] {title}")
                        else:
                            print(f"  [{status}] {title}")
        return out_results

    async with async_playwright() as p:
        api_context = await p.request.new_context(
            storage_state=str(_AUTH_STATE_FILE()),
            proxy=proxy,
        )
        try:
            results = await _run_send(api_context)
        finally:
            await api_context.dispose()

        if any(_is_security_block_response(r) for r in results):
            if verbose:
                print("[*] Security service detected (e.g. Cloudflare). Launching browser to solve JS challenge...")
            browser = await p.chromium.launch(headless=False)
            try:
                context_opts: dict = {
                    "storage_state": str(_AUTH_STATE_FILE()),
                    "viewport": {"width": 1280, "height": 720},
                }
                if proxy:
                    context_opts["proxy"] = proxy
                context = await browser.new_context(**context_opts)
                challenge_url = _config.BASE_URL or url
                page = await context.new_page()
                try:
                    if verbose:
                        print(f"[*] Navigating to {challenge_url} to pass security challenge...")
                    await page.goto(challenge_url, wait_until="domcontentloaded", timeout=60000)
                    _challenge_phrases = ("just a moment", "checking your browser", "please wait")
                    for _attempt in range(18):
                        pg_title = await page.title()
                        if not any(ph in pg_title.lower() for ph in _challenge_phrases):
                            break
                        if verbose and _attempt == 0:
                            print("[*] Waiting for security challenge to resolve...")
                        await page.wait_for_timeout(2500)
                    if verbose:
                        final_title = await page.title()
                        print(f"[*] Page title after challenge: {final_title}")

                    # Detect if this is a tRPC-based chat site (Mistral-style):
                    # needs message.newChat + /api/chat mode:start for first msg
                    _base_url = challenge_url.rstrip("/").rsplit("/chat", 1)[0]
                    _trpc_new_chat_url = f"{_base_url}/api/trpc/message.newChat?batch=1"
                    _uses_trpc = "messageInput" in json.dumps(discovered.get("strategies", {}))

                    if verbose:
                        print("[*] Sending payloads via in-page fetch (sequential)...")
                    browser_results: list[dict] = []
                    for i, p_item in enumerate(payloads_list):
                        p_title = p_item.get("title", f"Payload {i+1}")
                        is_init = p_item.get("_is_init", False)
                        if is_init:
                            browser_results.append({"title": p_title, "status": 200, "ok": True, "response": "{}"})
                            continue

                        # Extract the user's text from the payload
                        raw_text = p_item.get("text") or p_item.get("title") or ""
                        msg_input = p_item.get("messageInput")
                        if msg_input:
                            try:
                                parsed = json.loads(msg_input) if isinstance(msg_input, str) else msg_input
                                if isinstance(parsed, list) and parsed:
                                    raw_text = parsed[0].get("text", raw_text)
                            except (json.JSONDecodeError, TypeError):
                                pass

                        if i > 0:
                            await asyncio.sleep(evasion.THROTTLE_BETWEEN_PAYLOADS_SEC)

                        if _uses_trpc:
                            # Mistral two-step: 1) newChat to create chat+message, 2) /api/chat mode:start
                            try:
                                r = await page.evaluate(
                                    """async ([trpcUrl, chatUrl, userText, startFields]) => {
                                        try {
                                            // Step 1: create chat + user message via tRPC
                                            const newChatBody = JSON.stringify({"0": {"json": {
                                                "content": [{"type": "text", "text": userText}],
                                                "agentId": null, "agentsApiAgentId": null,
                                                "files": [], "isSampleChatForAgentId": null,
                                                "features": ["beta-websearch"], "integrations": [],
                                                "canva": null, "action": null, "libraries": [],
                                                "projectId": null, "transcriptionsMetadata": null,
                                                "incognito": false
                                            }, "meta": {"values": {
                                                "agentId": ["undefined"], "agentsApiAgentId": ["undefined"],
                                                "isSampleChatForAgentId": ["undefined"], "canva": ["undefined"],
                                                "action": ["undefined"], "projectId": ["undefined"],
                                                "transcriptionsMetadata": ["undefined"]
                                            }}}});
                                            const trpcResp = await fetch(trpcUrl, {
                                                method: 'POST',
                                                headers: {'Content-Type': 'application/json',
                                                           'trpc-accept': 'application/jsonl',
                                                           'x-trpc-source': 'nextjs-react'},
                                                body: newChatBody,
                                                credentials: 'include',
                                            });
                                            const trpcText = await trpcResp.text();
                                            if (!trpcResp.ok) {
                                                return {status: trpcResp.status, ok: false, response: trpcText, step: 'newChat'};
                                            }
                                            // Extract chatId from tRPC response
                                            let chatId = null;
                                            for (const line of trpcText.split('\\n')) {
                                                const m = line.match(/"chatId"\\s*:\\s*"([0-9a-f-]{36})"/);
                                                if (m) { chatId = m[1]; break; }
                                            }
                                            if (!chatId) {
                                                return {status: 200, ok: false, response: trpcText, error: 'Could not extract chatId from newChat response'};
                                            }

                                            // Step 2: kick off AI processing via /api/chat mode:start
                                            const startBody = Object.assign({}, startFields, {chatId: chatId});
                                            const chatResp = await fetch(chatUrl, {
                                                method: 'POST',
                                                headers: {'Content-Type': 'application/json'},
                                                body: JSON.stringify(startBody),
                                                credentials: 'include',
                                            });
                                            const chatText = await chatResp.text();

                                            // Parse SSE to extract assistant content
                                            let content = '';
                                            for (const line of chatText.split('\\n')) {
                                                try {
                                                    const idx = line.indexOf('{');
                                                    if (idx < 0) continue;
                                                    const obj = JSON.parse(line.slice(idx));
                                                    const j = obj.json || obj;
                                                    const patches = j.patches || [];
                                                    for (const p of patches) {
                                                        if (p.op === 'replace' && p.path === '/contentChunks' && Array.isArray(p.value)) {
                                                            content = p.value.map(c => c.text || '').join('');
                                                        } else if (p.op === 'append' && p.path && p.path.includes('/text')) {
                                                            content += (p.value || '');
                                                        } else if (p.op === 'replace' && p.path === '/' && p.value && p.value.content) {
                                                            content = p.value.content;
                                                        }
                                                    }
                                                } catch(e) {}
                                            }
                                            return {status: chatResp.status, ok: chatResp.ok, response: content || chatText, chatId: chatId};
                                        } catch (e) {
                                            return {status: null, ok: false, error: e.message, response: null};
                                        }
                                    }""",
                                    [_trpc_new_chat_url, url, raw_text, {
                                        "mode": "start",
                                        "disabledFeatures": discovered.get("payload_format", {}).get("fields", {}).get("disabledFeatures", []),
                                        "clientPromptData": discovered.get("payload_format", {}).get("fields", {}).get("clientPromptData", {}),
                                        "stableAnonymousIdentifier": discovered.get("payload_format", {}).get("fields", {}).get("stableAnonymousIdentifier", ""),
                                        "shouldAwaitStreamBackgroundTasks": True,
                                        "shouldUseMessagePatch": True,
                                        "shouldUsePersistentStream": True,
                                    }],
                                )
                                r["title"] = p_title
                            except Exception as e:
                                r = {"title": p_title, "status": None, "ok": False, "error": str(e), "response": None}
                        else:
                            # Generic: just POST directly
                            if use_raw_overrides:
                                overrides = {k: v for k, v in p_item.items() if v is not None}
                            else:
                                text = p_item.get("text", "")
                                overrides = {override_keys[0]: p_title, override_keys[1]: text}  # type: ignore[index]
                            b_strategy = overrides.pop("strategy", "zero_shot") if use_raw_overrides else "zero_shot"
                            overrides.pop("_is_init", None)
                            body, content_type = build_body_fn(payload_format, overrides, strategy=b_strategy)
                            req_headers = {**headers, "Content-Type": content_type}
                            try:
                                r = await page.evaluate(
                                    """async ([url, headers, body]) => {
                                        try {
                                            const resp = await fetch(url, {
                                                method: 'POST',
                                                headers: headers,
                                                body: body,
                                                credentials: 'include',
                                            });
                                            const text = await resp.text();
                                            return { status: resp.status, ok: resp.ok, response: text };
                                        } catch (e) {
                                            return { status: null, ok: false, error: e.message, response: null };
                                        }
                                    }""",
                                    [url, req_headers, body],
                                )
                                r["title"] = p_title
                            except Exception as e:
                                r = {"title": p_title, "status": None, "ok": False, "error": str(e), "response": None}

                        if verbose:
                            st = r.get("status", "?")
                            snippet = (r.get("response") or "")[:200]
                            if not r.get("ok"):
                                print(f"  [{st}] {p_title} — {snippet}")
                            else:
                                print(f"  [{st}] {p_title}")
                        browser_results.append(r)
                    results = browser_results
                finally:
                    await page.close()
            finally:
                await browser.close()

    return results


async def send_payloads() -> None:
    """
    Load discovered endpoint (with payload_format), load payloads.json, ensure
    session fresh, then POST each payload with overrides title + description (from text).
    """
    if not _PAYLOADS_FILE().exists():
        print(f"[-] No payloads file at {_PAYLOADS_FILE()}. Create one with a 'payloads' array of {{'title', 'text'}}.")
        return
    payloads_data = json.loads(_PAYLOADS_FILE().read_text())
    payloads = payloads_data.get("payloads", payloads_data) if isinstance(payloads_data, dict) else payloads_data
    if not payloads:
        print("[-] No payloads in file (expect 'payloads' array).")
        return
    results = await send_payloads_from_list(payloads, verbose=True)
    if not results:
        return
    ok_count = sum(1 for r in results if r.get("ok"))
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = _SITE_STATE_DIR() / f"{timestamp}_log.json"
    log_path.write_text(json.dumps({"timestamp": timestamp, "results": results}, indent=2), encoding="utf-8")
    print(f"[+] Log: {log_path}")
    print(f"\n[*] Sent {len(results)} payloads, {ok_count} OK.")


