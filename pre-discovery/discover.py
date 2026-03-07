"""
Pre-discovery: infer TARGET_API_URL from APP_URL using Playwright.
Captures GET and POST requests, scores by LLM-likelihood heuristics, returns
comprehensive endpoint discovery (paths and payload formats).
Records full request trace for the entire LLM interaction session.
No TARGET_API_URL required — discovers it dynamically.
"""
import asyncio
import os
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
    should_exclude_from_trace,
)


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


async def _try_auto_trigger_chat_multi(page, num_messages: int = 3) -> int:
    """
    Send num_messages to the chat UI (one at a time, waiting for response).
    Returns number of messages successfully sent.
    """
    prompts = ["hello", "and you?", "thanks"]
    selectors = [
        'textarea[placeholder*="message" i], textarea[placeholder*="chat" i], textarea[placeholder*="ask" i]',
        'textarea[aria-label*="message" i], textarea[aria-label*="chat" i]',
        '[contenteditable="true"][role="textbox"]',
        'input[type="text"][placeholder*="message" i], input[type="text"][placeholder*="ask" i]',
        "textarea",
    ]
    sent = 0
    for i in range(min(num_messages, len(prompts))):
        try:
            chat_input = None
            for sel in selectors:
                try:
                    chat_input = await page.wait_for_selector(sel, timeout=3000, state="visible")
                    if chat_input:
                        break
                except Exception:
                    continue
            if not chat_input:
                break
            await chat_input.fill(prompts[i])
            await asyncio.sleep(evasion.human_delay(100, 300))
            await page.keyboard.press("Enter")
            sent += 1
            if i < num_messages - 1:
                await asyncio.sleep(evasion.human_delay(3000, 5000))
        except Exception:
            break
    return sent


async def discover_api(
    app_url: str,
    *,
    headless: bool = False,
    timeout_seconds: float = 120.0,
    try_auto_trigger: bool = True,
    num_messages: int = 3,
    auth_state_path: Path | None = None,
    verbose: bool = False,
    output_dir: Path | None = None,
) -> dict:
    """
    Load app at app_url, capture GET and POST requests, score by LLM-likelihood.
    Captures up to num_messages (default 3) POSTs per chat URL to record verified
    single-turn and multi-turn formats.
    Returns {"post": [...], "get": [...], "trace": [...]} with candidates sorted by score.
    """
    post_candidates: list[dict] = []
    get_endpoints: list[dict] = []
    seen_post: set[str] = set()
    post_count_by_url: dict[str, int] = {}
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

        # Full trace: record every request (except images, js, css, analytics)
        if not should_exclude_from_trace(path, url):
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

            count = post_count_by_url.get(url, 0)
            if count >= num_messages:
                return
            post_count_by_url[url] = count + 1
            if url not in seen_post:
                seen_post.add(url)

            n = count + 1
            if verbose:
                print(f"[verbose] Captured request {n}/{num_messages} for {url[:60]}...")
            post_candidates.append({
                "url": url,
                "method": method,
                "score": score,
                "reason": reason,
                "headers": {k: v for k, v in headers.items() if k.lower() not in {"cookie", "content-length", "host"}},
                "post_data": post_data,
                "_request_index": n,
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
        viewport = {"width": evasion.HALF_VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT}
        remote_url = evasion.get_remote_browser_url()

        if remote_url:
            # Technique 8: Use remote browser (Browserless, Scrappey) for Cloudflare bypass
            print("[*] Connecting to remote browser (Cloudflare evasion)...")
            browser = await p.chromium.connect_over_cdp(remote_url)
        else:
            # Techniques 1 & 2: Cloudflare launch args + real Chrome
            launch_args = evasion.get_cloudflare_launch_args(
                window_position=evasion.WINDOW_POSITION_RIGHT_HALF,
            )
            use_chrome = (
                (os.environ.get("EVASION_USE_CHROMIUM") or "").strip().lower()
                not in ("1", "true", "yes")
            )
            launch_opts = {"headless": headless, "args": launch_args}
            if use_chrome:
                launch_opts["channel"] = "chrome"
            try:
                browser = await p.chromium.launch(**launch_opts)
            except Exception as e:
                if use_chrome and "channel" in str(e).lower():
                    # Chrome not installed; fall back to Chromium
                    del launch_opts["channel"]
                    print("[*] Chrome not found, using Chromium.")
                    browser = await p.chromium.launch(**launch_opts)
                else:
                    raise

        # Technique 3: Realistic context (user_agent, locale, timezone)
        context_options = evasion.get_browser_context_options(viewport=viewport)
        if auth_state_path and auth_state_path.exists():
            context_options["storage_state"] = str(auth_state_path)
        proxy = evasion.get_playwright_proxy()
        if proxy:
            context_options["proxy"] = proxy

        context = await browser.new_context(**context_options)
        page = await context.new_page()

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

        # Technique 5: Longer human-like delay before navigation (reduces bot-like timing)
        await asyncio.sleep(evasion.human_delay_long(500, 1200))

        print(f"[*] Opening app at {app_url}...")
        await page.goto(app_url)
        # Technique 5: Warm-up with human-like mouse movement and scroll
        await evasion.warm_up_page_human_like(
            page, evasion.HALF_VIEWPORT_WIDTH, evasion.VIEWPORT_HEIGHT
        )
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
            print(f"[*] Attempting to auto-trigger {num_messages} chat messages...")
            sent = await _try_auto_trigger_chat_multi(page, num_messages)
            if sent > 0:
                print(f"[*] Sent {sent} message(s); waiting for API responses...")
                await asyncio.sleep(2.0)
            if sent < num_messages:
                print(f"[*] Could not auto-trigger all {num_messages}; send remaining manually.")

        print("\n" + "=" * 60)
        print(f"[!] Send {num_messages} chat messages in the browser (one at a time).")
        print("[!] This captures single-turn and multi-turn formats for each component.")
        print("=" * 60 + "\n")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if post_candidates:
                best_url = post_candidates[0]["url"]
                if post_count_by_url.get(best_url, 0) >= num_messages:
                    break
            await asyncio.sleep(0.5)

        await context.close()
        await browser.close()

    post_candidates.sort(key=lambda c: (c["score"], -c.get("_request_index", 1)), reverse=True)
    post_candidates = [c for c in post_candidates if c["score"] >= MIN_SCORE]
    seen_urls: set[str] = set()
    post_deduped: list[dict] = []
    for c in post_candidates:
        url = c["url"]
        if url not in seen_urls:
            seen_urls.add(url)
            c_clean = {k: v for k, v in c.items() if k != "_request_index"}
            post_deduped.append(c_clean)
    post_candidates = post_deduped
    get_endpoints.sort(key=lambda c: c["score"], reverse=True)
    get_endpoints = [c for c in get_endpoints if c["score"] >= MIN_SCORE_GET]

    return {
        "post": post_candidates,
        "get": get_endpoints,
        "trace": trace_entries,
        "app_url": actual_app_url,
    }
