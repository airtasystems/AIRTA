"""
Dynamic discovery of LLM endpoint: load saved auth, open app, intercept the POST
request that matches the API URL set in .env when the user makes a manual request.
"""
import asyncio
import json
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from . import auth as auth_module
from . import payload_format as payload_format_module
from .config import (
    LOGIN_URL,
    BASE_URL,
    AUTH_STATE_FILE,
    DISCOVERED_ENDPOINT_FILE,
    DISCOVERED_MULTI_FILE,
    TARGET_API_URL,
)
from .auth import _check_server_reachable
from pipeline import evasion


def _normalize_path(url: str) -> str:
    """Match 8_generate_schema: path or '/', rstrip '/', or '/'."""
    path = (urlparse(url).path or "/").rstrip("/") or "/"
    return path


def _path_matches_endpoint(request_url: str) -> bool:
    """Exact path match only (ignore e.g. /submissions/set-context). API URL comes from .env."""
    req_path = _normalize_path(request_url)
    end_path = _normalize_path(TARGET_API_URL)
    return req_path == end_path


def _normalize_netloc(netloc: str) -> str:
    """Treat localhost and 127.0.0.1 as the same host for same-origin checks."""
    host, _, port = netloc.partition(":")
    if host in ("localhost", "127.0.0.1", "::1"):
        return f"localhost:{port}" if port else "localhost"
    return netloc


def _is_same_origin(url: str) -> bool:
    base = urlparse(BASE_URL)
    target = urlparse(url)
    return (
        target.scheme == base.scheme
        and _normalize_netloc(target.netloc) == _normalize_netloc(base.netloc)
    )


async def discover_endpoint(*, headless: bool = False, position_right_half: bool = False) -> None:
    """
    Launch browser with saved auth, go to app. User makes one manual request
    to the auth-only LLM API in the app. We intercept that POST and save
    URL, method, headers, and payload schema.

    position_right_half: if True, place browser on right half of screen so UI stays visible.
    """
    if not TARGET_API_URL:
        print("[-] Set LOCAL_API_URL or TARGET_API_URL in .env (the API URL to intercept).")
        return
    if not AUTH_STATE_FILE.exists():
        print(f"[-] No saved session at {AUTH_STATE_FILE}. Run 'login' first.")
        return

    if not await auth_module.ensure_session_fresh():
        print("[-] Session refresh failed. Run 'login' or 'refresh' and try again.")
        return

    request_caught = asyncio.Event()
    captured = {}

    launch_args = [f"--window-position={evasion.WINDOW_POSITION_RIGHT_HALF[0]},{evasion.WINDOW_POSITION_RIGHT_HALF[1]}"] if position_right_half else None
    viewport = (
        {"width": evasion.HALF_VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT}
        if position_right_half
        else {"width": evasion.VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT}
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            storage_state=str(AUTH_STATE_FILE),
            viewport=viewport,
        )
        page = await context.new_page()
        if await evasion.apply_stealth(page):
            print("[*] Stealth applied (WAF evasion).")

        expected_path = _normalize_path(TARGET_API_URL)

        def handle_request(request):
            if request.method != "POST":
                return
            if not _is_same_origin(request.url):
                return
            req_path = _normalize_path(request.url)
            if req_path != expected_path:
                print(f"[*] POST (skip): {req_path} — expected {expected_path}")
                return
            # Optional: skip very small bodies (e.g. ping)
            try:
                post_data = request.post_data
                if post_data and len(post_data) < 20:
                    print(f"[*] POST (skip): body too small ({len(post_data)} chars)")
                    return
            except Exception:
                pass

            print(f"\n[+] Intercepted LLM API request: {request.url}")
            captured["url"] = request.url
            captured["method"] = request.method
            captured["headers"] = dict(request.headers)
            try:
                captured["payload_schema"] = request.post_data_json
            except Exception:
                captured["payload_schema"] = request.post_data
            request_caught.set()

        page.on("request", handle_request)

        print(f"[*] Opening app at {BASE_URL} (session loaded)...")
        await page.goto(BASE_URL)
        await asyncio.sleep(evasion.human_delay(300, 700))
        await evasion.scroll_human_like(page, -80, steps=3)

        print("\n" + "=" * 60)
        print("[!] In the browser: make one request to the LLM (e.g. send a chat message).")
        print("[!] This app will capture that request and save the endpoint details.")
        print("=" * 60 + "\n")

        await request_caught.wait()

        headers_serializable = _serialize_headers(captured.get("headers", {}))

        # Derive structured payload format so we can build requests later without re-parsing
        raw_payload = captured.get("payload_schema")
        payload_fmt = payload_format_module.parse_payload_from_request(
            headers_serializable, raw_payload
        )

        out = {
            "url": captured["url"],
            "method": captured["method"],
            "headers": headers_serializable,
            "payload_format": payload_fmt,
            "payload_schema": raw_payload,
        }
        DISCOVERED_ENDPOINT_FILE.write_text(json.dumps(out, indent=2))

        print(f"\n[+] Discovered endpoint saved to {DISCOVERED_ENDPOINT_FILE.name}")
        print("[*] Payload format:", payload_fmt.get("encoding"), "— fields:", list(payload_fmt.get("fields", {}).keys()))
        print("[*] URL:", out["url"])

        await context.close()
        await browser.close()


# Headers we omit when serializing so we can inject current token at send time
_SKIP_HEADERS = {"x-csrf-token", "x-xsrf-token", "cookie", "content-length", "host", "accept-encoding"}


def _serialize_headers(headers: dict) -> dict:
    """Normalize headers for JSON; omit volatile ones."""
    out = {}
    for k, v in headers.items():
        if k.lower() in _SKIP_HEADERS:
            continue
        out[k] = v if isinstance(v, str) else (v[0] if v else "")
    return out


async def discover_endpoint_multi(
    *,
    num_messages: int = 3,
    headless: bool = False,
) -> None:
    """
    Launch browser with saved auth; user sends num_messages (default 3) in the UI.
    We intercept each matching POST and save URL, method, headers, and payload
    for every request so we can see how the UI sends follow-up messages (full
    history vs incremental).
    """
    if not TARGET_API_URL:
        print("[-] Set LOCAL_API_URL or TARGET_API_URL in .env (the API URL to intercept).")
        return
    if not AUTH_STATE_FILE.exists():
        print(f"[-] No saved session at {AUTH_STATE_FILE}. Run 'login' first.")
        return

    if not await auth_module.ensure_session_fresh():
        print("[-] Session refresh failed. Run 'login' or 'refresh' and try again.")
        return

    captured_list: list[dict] = []
    all_caught = asyncio.Event()
    expected_path = _normalize_path(TARGET_API_URL)

    def handle_request(request):
        if request.method != "POST":
            return
        if not _is_same_origin(request.url):
            return
        req_path = _normalize_path(request.url)
        if req_path != expected_path:
            print(f"[*] POST (skip): {req_path} — expected {expected_path}")
            return
        try:
            post_data = request.post_data
            if post_data and len(post_data) < 20:
                print(f"[*] POST (skip): body too small ({len(post_data)} chars)")
                return
        except Exception:
            pass

        n = len(captured_list) + 1
        print(f"\n[+] Intercepted request {n}/{num_messages}: {request.url}")
        try:
            payload_schema = request.post_data_json
        except Exception:
            payload_schema = request.post_data
        captured_list.append({
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
            "payload_schema": payload_schema,
        })
        if len(captured_list) >= num_messages:
            all_caught.set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(AUTH_STATE_FILE),
            viewport={"width": evasion.VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT},
        )
        page = await context.new_page()
        if await evasion.apply_stealth(page):
            print("[*] Stealth applied (WAF evasion).")

        page.on("request", handle_request)

        print(f"[*] Opening app at {BASE_URL} (session loaded)...")
        await page.goto(BASE_URL)
        await asyncio.sleep(evasion.human_delay(300, 700))
        await evasion.scroll_human_like(page, -80, steps=3)

        print("\n" + "=" * 60)
        print(f"[!] In the browser: send exactly {num_messages} messages to the LLM (one at a time).")
        print("[!] This app will capture each request to see how the UI sends follow-up messages.")
        print("=" * 60 + "\n")

        await all_caught.wait()

        requests_out = []
        for i, cap in enumerate(captured_list):
            headers_serializable = _serialize_headers(cap.get("headers", {}))
            raw_payload = cap.get("payload_schema")
            payload_fmt = payload_format_module.parse_payload_from_request(
                headers_serializable, raw_payload
            )
            requests_out.append({
                "url": cap["url"],
                "method": cap["method"],
                "headers": headers_serializable,
                "payload_format": payload_fmt,
                "payload_schema": raw_payload,
            })
            print(f"[*] Request {i + 1}: fields {list(payload_fmt.get('fields', {}).keys())}")

        out = {
            "num_captured": len(requests_out),
            "requests": requests_out,
        }
        DISCOVERED_MULTI_FILE.write_text(json.dumps(out, indent=2))

        print(f"\n[+] Multi-message discovery saved to {DISCOVERED_MULTI_FILE.name}")
        print(f"[*] Captured {len(requests_out)} requests; compare payload_schema to see full-history vs incremental.")

        await context.close()
        await browser.close()


async def discover_unified(*, headless: bool = False) -> None:
    """
    Single browser session: login, then navigate to app and capture 3 LLM requests.
    Writes discovered_endpoint.json with top-level zero-shot format and
    strategies.few_shot / strategies.multi_shot for requests 2 and 3.
    """
    if not TARGET_API_URL:
        print("[-] Set LOCAL_API_URL or TARGET_API_URL in .env (the API URL to intercept).")
        return
    if not _check_server_reachable(LOGIN_URL):
        raise ConnectionError(
            f"Cannot reach {LOGIN_URL}. Start your app (e.g. dev server) and try again."
        )

    num_messages = 3
    captured_list: list[dict] = []
    all_caught = asyncio.Event()
    expected_path = _normalize_path(TARGET_API_URL)
    post_urls_seen: list[str] = []

    def handle_request(request):
        if request.method != "POST":
            return
        if request.url and request.url not in post_urls_seen:
            post_urls_seen.append(request.url)
        if not _is_same_origin(request.url):
            return
        req_path = _normalize_path(request.url)
        if req_path != expected_path:
            print(f"[*] POST (skip): {req_path} — expected {expected_path}")
            return
        try:
            post_data = request.post_data
            if post_data and len(post_data) < 20:
                print(f"[*] POST (skip): body too small ({len(post_data)} chars)")
                return
        except Exception:
            pass

        n = len(captured_list) + 1
        print(f"\n[+] Intercepted request {n}/{num_messages}: {request.url}")
        try:
            payload_schema = request.post_data_json
        except Exception:
            payload_schema = request.post_data
        captured_list.append({
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
            "payload_schema": payload_schema,
        })
        if len(captured_list) >= num_messages:
            all_caught.set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": evasion.VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT},
        )
        page = await context.new_page()
        if await evasion.apply_stealth(page):
            print("[*] Stealth applied (WAF evasion).")

        page.on("request", handle_request)
        print(f"[*] Opening app at {BASE_URL}...")
        await page.goto(BASE_URL)
        await asyncio.sleep(evasion.human_delay(300, 700))
        await evasion.scroll_human_like(page, -80, steps=3)

        print("\n" + "=" * 60)
        print("[!] Log in if needed, go to the AI component, then send 3 messages (one at a time).")
        print("[!] Auth and request formats (zero/few/multi-shot) will be saved when the 3rd message is captured.")
        print("=" * 60 + "\n")

        await all_caught.wait()

        await auth_module.save_auth_from_context(context, page, post_urls=post_urls_seen)

        requests_out = []
        for i, cap in enumerate(captured_list):
            headers_serializable = _serialize_headers(cap.get("headers", {}))
            raw_payload = cap.get("payload_schema")
            payload_fmt = payload_format_module.parse_payload_from_request(
                headers_serializable, raw_payload
            )
            requests_out.append({
                "url": cap["url"],
                "method": cap["method"],
                "headers": headers_serializable,
                "payload_format": payload_fmt,
                "payload_schema": raw_payload,
            })
            print(f"[*] Request {i + 1}: fields {list(payload_fmt.get('fields', {}).keys())}")

        first = requests_out[0]
        out = {
            "url": first["url"],
            "method": first["method"],
            "headers": first["headers"],
            "payload_format": first["payload_format"],
            "payload_schema": first["payload_schema"],
        }
        if len(requests_out) >= 3:
            out["strategies"] = {
                "few_shot": {
                    "payload_format": requests_out[1]["payload_format"],
                    "payload_schema": requests_out[1]["payload_schema"],
                },
                "multi_shot": {
                    "payload_format": requests_out[2]["payload_format"],
                    "payload_schema": requests_out[2]["payload_schema"],
                },
            }
        elif len(requests_out) == 2:
            out["strategies"] = {
                "few_shot": {
                    "payload_format": requests_out[1]["payload_format"],
                    "payload_schema": requests_out[1]["payload_schema"],
                },
            }

        DISCOVERED_ENDPOINT_FILE.write_text(json.dumps(out, indent=2))

        print(f"\n[+] Discovered endpoint saved to {DISCOVERED_ENDPOINT_FILE.name}")
        print("[*] Zero-shot (top-level) + strategies:", list(out.get("strategies", {}).keys()))

        await context.close()
        await browser.close()
