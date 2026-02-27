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
    BASE_URL,
    AUTH_STATE_FILE,
    DISCOVERED_ENDPOINT_FILE,
    TARGET_API_URL,
)
from . import evasion


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


async def discover_endpoint(*, headless: bool = False) -> None:
    """
    Launch browser with saved auth, go to app. User makes one manual request
    to the auth-only LLM API in the app. We intercept that POST and save
    URL, method, headers, and payload schema.
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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(AUTH_STATE_FILE),
            viewport={"width": evasion.VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT},
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

        # Normalize headers for JSON (some values are lists). Omit volatile headers
        # (CSRF, cookie) so we always inject the current token from csrf_token.json at send time.
        SKIP_HEADERS = {"x-csrf-token", "x-xsrf-token", "cookie", "content-length", "host", "accept-encoding"}
        headers_serializable = {}
        for k, v in captured.get("headers", {}).items():
            if k.lower() in SKIP_HEADERS:
                continue
            headers_serializable[k] = v if isinstance(v, str) else v[0] if v else ""

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
