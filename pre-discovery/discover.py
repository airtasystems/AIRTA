"""
Pre-discovery: infer TARGET_API_URL from APP_URL using Playwright.
Captures GET and POST requests, scores by LLM-likelihood heuristics, returns
comprehensive endpoint discovery (paths and payload formats).
Records full request trace for the entire LLM interaction session.
No TARGET_API_URL required — discovers it dynamically.
"""
import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

# Ensure project root is on path for pipeline.evasion
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from pipeline import evasion

from .heuristics import (
    MIN_SCORE,
    MIN_SCORE_GET,
    score_get_request,
    score_request,
    score_request_url_only,
)
from .methods.trace import (
    build_trace_entry,
    build_websocket_trace_entry,
    is_image_path,
)
from .methods.playwright_record import start_playwright_trace, stop_playwright_trace


async def _has_chat_input(page) -> bool:
    """Check if page has a chat input (textarea, contenteditable, etc.) without triggering."""
    selectors = [
        "textarea",
        '[contenteditable="true"][role="textbox"]',
        '[contenteditable="true"]',
        'input[type="text"]',
    ]
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=1500)
            if el:
                return True
        except Exception:
            continue
    return False


async def _try_auto_trigger_chat(page) -> bool:
    """
    Attempt to find a chat input and send a test message.
    Returns True if we triggered something, False if we should fall back to manual.
    """
    try:
        # Common selectors for chat inputs
        selectors = [
            'textarea[placeholder*="message" i], textarea[placeholder*="chat" i], textarea[placeholder*="ask" i]',
            'textarea[aria-label*="message" i], textarea[aria-label*="chat" i]',
            '[contenteditable="true"][role="textbox"]',
            'input[type="text"][placeholder*="message" i], input[type="text"][placeholder*="ask" i]',
            "textarea",
        ]
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=2000)
                if el:
                    await el.fill("hello")
                    await asyncio.sleep(evasion.human_delay(100, 300))
                    await page.keyboard.press("Enter")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def discover_api(
    app_url: str,
    *,
    headless: bool = False,
    timeout_seconds: float = 120.0,
    try_auto_trigger: bool = True,
    auth_state_path: Path | None = None,
    verbose: bool = False,
    record_playwright_trace: bool = True,
    output_dir: Path | None = None,
) -> dict:
    """
    Load app at app_url, capture GET and POST requests, score by LLM-likelihood.
    Returns {"post": [...], "get": [...]} with candidates sorted by score (best first).
    """
    post_candidates: list[dict] = []
    get_endpoints: list[dict] = []
    seen_post: set[str] = set()
    seen_get: set[str] = set()

    # Full trace: every request during the session
    trace_entries: list[dict] = []
    request_to_index: dict[int, int] = {}
    session_start = time.monotonic()

    def handle_request(request):
        url = request.url
        method = request.method
        parsed = urlparse(url)
        path = parsed.path or "/"

        # Full trace: record every request (except images), all headers unabridged
        if not is_image_path(path):
            try:
                post_data = request.post_data if method in ("POST", "PUT", "PATCH") else None
            except Exception:
                post_data = None
            entry = build_trace_entry(
                url=url,
                path=path,
                method=method,
                headers=dict(request.headers),
                session_start=session_start,
                post_data=post_data,
            )
            trace_entries.append(entry)
            request_to_index[id(request)] = len(trace_entries) - 1

        if method == "POST":
            if url in seen_post:
                return
            seen_post.add(url)
            try:
                post_data = request.post_data
            except Exception:
                post_data = None

            headers = dict(request.headers)
            score, reason = score_request(url, method, headers, post_data, app_url)
            if verbose:
                body_len = len(post_data) if post_data else 0
                print(f"[verbose] POST {url[:80]}... body={body_len} score={score} ({reason})")
            if score <= 0:
                return

            post_candidates.append({
                "url": url,
                "method": method,
                "score": score,
                "reason": reason,
                "headers": {k: v for k, v in headers.items() if k.lower() not in {"cookie", "content-length", "host"}},
                "post_data": post_data,
            })
        elif method == "GET":
            if url in seen_get:
                return
            score, reason = score_get_request(url, app_url)
            if verbose and score > 0:
                print(f"[verbose] GET {url[:80]}... score={score} ({reason})")
            if score < MIN_SCORE_GET:
                return
            seen_get.add(url)

            parsed = urlparse(url)
            query_params = dict(parse_qs(parsed.query)) if parsed.query else {}

            get_endpoints.append({
                "url": url,
                "path": parsed.path or "/",
                "method": "GET",
                "score": score,
                "reason": reason,
                "query_params": query_params,
            })

    async with async_playwright() as p:
        launch_args = [f"--window-position={evasion.WINDOW_POSITION_RIGHT_HALF[0]},{evasion.WINDOW_POSITION_RIGHT_HALF[1]}"]
        viewport = {"width": evasion.HALF_VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT}

        browser = await p.chromium.launch(headless=headless, args=launch_args)

        context_options = {"viewport": viewport}
        if auth_state_path and auth_state_path.exists():
            context_options["storage_state"] = str(auth_state_path)

        context = await browser.new_context(**context_options)
        page = await context.new_page()

        if record_playwright_trace and output_dir:
            await start_playwright_trace(context)

        if await evasion.apply_stealth(page):
            print("[*] Stealth applied (WAF evasion).")

        page.on("request", handle_request)

        def handle_response(response):
            idx = request_to_index.get(id(response.request))
            if idx is not None:
                trace_entries[idx]["response_status"] = response.status
                trace_entries[idx]["response_status_text"] = response.status_text

        page.on("response", handle_response)

        def handle_websocket(ws):
            ws_url = ws.url
            parsed_ws = urlparse(ws_url)
            path_ws = parsed_ws.path or "/"
            if not is_image_path(path_ws):
                trace_entries.append(
                    build_websocket_trace_entry(ws_url, path_ws, session_start)
                )
            if ws_url in seen_post:
                return
            seen_post.add(ws_url)
            score, reason = score_request_url_only(ws_url, "WS", app_url)
            if verbose:
                print(f"[verbose] WS {ws_url[:80]}... score={score} ({reason})")
            if score >= MIN_SCORE:
                post_candidates.append({
                    "url": ws_url,
                    "method": "WebSocket",
                    "score": score,
                    "reason": reason,
                    "headers": {},
                    "post_data": None,
                })

        page.on("websocket", handle_websocket)

        print(f"[*] Opening app at {app_url}...")
        await page.goto(app_url)
        # Some sites have chat at /chat, /chatbot, etc.; try each until we find one with a chat input
        parsed = urlparse(app_url)
        if parsed.path in ("", "/"):
            for chat_path in ["/chat", "/chatbot", "/ai-chat", "/conversation"]:
                try:
                    chat_url = f"{parsed.scheme}://{parsed.netloc}{chat_path}"
                    await page.goto(chat_url, wait_until="domcontentloaded", timeout=5000)
                    if await _has_chat_input(page):
                        print(f"[*] Navigated to chat at {chat_url}")
                        break
                    # Page loaded but no chat input; try next path
                except Exception:
                    continue
        # Capture actual chat page URL for trace/guide (ask_capital_script uses this)
        actual_app_url = page.url
        await asyncio.sleep(evasion.human_delay(500, 1000))
        await evasion.scroll_human_like(page, -80, steps=3)

        if try_auto_trigger:
            print("[*] Attempting to auto-trigger chat...")
            triggered = await _try_auto_trigger_chat(page)
            if triggered:
                await asyncio.sleep(2.0)
            else:
                print("[*] Could not auto-trigger; waiting for manual interaction.")

        print("\n" + "=" * 60)
        print("[!] Send one chat message in the browser (or wait if auto-trigger worked).")
        print("[!] This will capture the LLM API request.")
        print("=" * 60 + "\n")

        # Wait until we have at least one POST/WS candidate or timeout
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if post_candidates:
                break
            await asyncio.sleep(0.5)

        if record_playwright_trace and output_dir:
            extract_dir = await stop_playwright_trace(context, output_dir)
            print(f"[+] Playwright trace extracted to {extract_dir}")

        await context.close()
        await browser.close()

    # Sort by score descending, keep only valid candidates
    post_candidates.sort(key=lambda c: c["score"], reverse=True)
    post_candidates = [c for c in post_candidates if c["score"] >= MIN_SCORE]
    get_endpoints.sort(key=lambda c: c["score"], reverse=True)
    get_endpoints = [c for c in get_endpoints if c["score"] >= MIN_SCORE_GET]

    return {
        "post": post_candidates,
        "get": get_endpoints,
        "trace": trace_entries,
        "app_url": actual_app_url,
    }
