"""
Login + CSRF capture: open browser, user logs in (and MFA), extract CSRF from
meta/hidden inputs/localStorage/cookies, save auth state and CSRF token.
"""
import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import (
    BASE_URL,
    LOGIN_URL,
    AUTH_STATE_FILE,
    CSRF_TOKEN_FILE,
)
from pipeline import evasion


def _check_server_reachable(url: str, timeout: float = 3.0) -> bool:
    """Return True if the host:port for url is accepting connections."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


async def _do_login_steps(
    context: BrowserContext,
    page: Page,
    post_urls: list[str],
    *,
    wait_for_login: Awaitable[Any] | Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """
    Run login flow in an existing context: navigate to LOGIN_URL, wait for user,
    extract CSRF, save auth state. Does not launch or close browser.
    """
    async def on_request(request):
        if request.method == "POST":
            url = request.url
            if url and url not in post_urls:
                post_urls.append(url)

    page.on("request", on_request)
    if await evasion.apply_stealth(page):
        print("[*] Stealth applied (WAF evasion).")

    print(f"[*] Navigating to {LOGIN_URL}...")
    await page.goto(LOGIN_URL)
    await asyncio.sleep(evasion.human_delay(400, 900))

    print("\n[!] Complete login (and MFA if required) in the browser, then come back here.")
    if wait_for_login is not None:
        to_await = wait_for_login() if callable(wait_for_login) else wait_for_login
        await to_await
    else:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("Press Enter when you are fully logged in and the app is loaded... ")
        )
    print("[+] Extracting CSRF and saving session...")

    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        print("[*] Network idle timeout; proceeding with extraction anyway.")

    csrf_token = await _extract_csrf(page, context)

    if csrf_token:
        print(f"[+] CSRF token extracted: {csrf_token[:10]}...")
        CSRF_TOKEN_FILE.write_text(json.dumps({"csrf_token": csrf_token}, indent=2))
    else:
        print("[-] No CSRF token found (app may use Bearer or per-request tokens).")

    await context.storage_state(path=str(AUTH_STATE_FILE))
    print(f"[+] Session saved to {AUTH_STATE_FILE.name}")


async def save_auth_from_context(
    context: BrowserContext,
    page: Page,
    *,
    post_urls: list[str] | None = None,
) -> None:
    """
    Extract CSRF and save auth state from an existing context/page (e.g. after
    the user has finished in the AI component). No navigation or prompt; use
    when auth is captured at exit from the flow.
    """
    post_urls = post_urls or []
    print("[+] Extracting CSRF and saving session...")
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        print("[*] Network idle timeout; proceeding with extraction anyway.")

    csrf_token = await _extract_csrf(page, context)

    if csrf_token:
        print(f"[+] CSRF token extracted: {csrf_token[:10]}...")
        CSRF_TOKEN_FILE.write_text(json.dumps({"csrf_token": csrf_token}, indent=2))
    else:
        print("[-] No CSRF token found (app may use Bearer or per-request tokens).")

    await context.storage_state(path=str(AUTH_STATE_FILE))
    print(f"[+] Session saved to {AUTH_STATE_FILE.name}")


async def capture_login_and_csrf(
    *,
    headless: bool = False,
    wait_for_login: Awaitable[Any] | Callable[[], Awaitable[Any]] | None = None,
    position_right_half: bool = False,
    context: BrowserContext | None = None,
    page: Page | None = None,
) -> None:
    """
    Launch browser, navigate to login URL. User completes login and MFA.
    Then extract CSRF token from common locations and save auth state + CSRF.

    wait_for_login: optional awaitable or async callable; when provided, awaited instead of
        blocking on terminal input (for UI: e.g. wait on an event until user clicks "Confirm login").
    position_right_half: if True, place browser window on right half of screen (960px wide at x=960)
        so the UI remains visible on the left.
    context, page: when both provided, use this existing context/page and do not launch or close
        the browser (for unified discovery flow that keeps one browser session).
    """
    if context is not None and page is not None:
        post_urls: list[str] = []
        await _do_login_steps(context, page, post_urls, wait_for_login=wait_for_login)
        return

    print("[*] Launching browser to capture authentication and CSRF...")

    if not _check_server_reachable(LOGIN_URL):
        raise ConnectionError(
            f"Cannot reach {LOGIN_URL}. Start your app (e.g. dev server on port 3000) and try again."
        )

    post_urls = []
    launch_args = None
    viewport_width = evasion.VIEWPORT_WIDTH
    viewport_height = evasion.VIEWPORT_HEIGHT
    if position_right_half:
        launch_args = [f"--window-position={evasion.WINDOW_POSITION_RIGHT_HALF[0]},{evasion.WINDOW_POSITION_RIGHT_HALF[1]}"]
        viewport_width = evasion.HALF_VIEWPORT_WIDTH

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        ctx = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
        )
        pg = await ctx.new_page()
        await _do_login_steps(ctx, pg, post_urls, wait_for_login=wait_for_login)
        await browser.close()


async def _extract_csrf(page, context) -> str | None:
    """Try meta, hidden inputs, localStorage, sessionStorage, cookies (multi-vector; matches A01 approach)."""
    csrf_token = None

    # Meta tags (Rails, Laravel)
    if not csrf_token:
        csrf_token = await page.evaluate("""() => {
            const meta = document.querySelector('meta[name="csrf-token"], meta[name="_csrf"]');
            return meta ? meta.getAttribute('content') : null;
        }""")
        if csrf_token:
            print("[*] Found CSRF in meta tag.")

    # Hidden inputs (Django, Flask)
    if not csrf_token:
        csrf_token = await page.evaluate("""() => {
            const input = document.querySelector(
                'input[name="csrfmiddlewaretoken"], input[name="_csrf"], input[name="csrf_token"]'
            );
            return input ? input.value : null;
        }""")
        if csrf_token:
            print("[*] Found CSRF in hidden input.")

    # Local storage (React, Next.js, SPAs)
    if not csrf_token:
        csrf_token = await page.evaluate("""() => {
            return localStorage.getItem('csrf_token') ||
                   localStorage.getItem('csrfToken') ||
                   localStorage.getItem('xsrf-token');
        }""")
        if csrf_token:
            print("[*] Found CSRF in localStorage.")

    # Session storage (SPAs that keep CSRF per tab/session)
    if not csrf_token:
        csrf_token = await page.evaluate("""() => {
            return sessionStorage.getItem('csrf_token') ||
                   sessionStorage.getItem('csrfToken') ||
                   sessionStorage.getItem('xsrf-token');
        }""")
        if csrf_token:
            print("[*] Found CSRF in sessionStorage.")

    # Cookies (Spring, Express/CSURF, Angular)
    if not csrf_token:
        cookies = await context.cookies()
        for c in cookies:
            if "csrf" in c["name"].lower() or "xsrf" in c["name"].lower():
                csrf_token = c["value"]
                print(f"[*] Found CSRF in cookie: {c['name']}")
                break

    return csrf_token


def _load_csrf() -> str:
    """Load CSRF token from file. Returns empty string if missing or invalid."""
    if not CSRF_TOKEN_FILE.exists():
        return ""
    try:
        data = json.loads(CSRF_TOKEN_FILE.read_text())
        return data.get("csrf_token", "")
    except (json.JSONDecodeError, OSError):
        return ""


async def ensure_session_fresh() -> bool:
    """No-op: session refresh has been removed. Returns True so callers proceed."""
    return True