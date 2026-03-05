"""
Login + CSRF capture: open browser, user logs in (and MFA), extract CSRF from
meta/hidden inputs/localStorage/cookies, save auth state and CSRF token.
Session refresh: POST to refresh endpoint with CSRF; tokens must be refreshed every 14 min.
"""
import asyncio
import json
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from .config import (
    BASE_URL,
    LOGIN_URL,
    AUTH_STATE_FILE,
    CSRF_TOKEN_FILE,
    DISCOVERED_REFRESH_URL_FILE,
    REFRESH_MAX_AGE_SECONDS,
    LAST_REFRESH_FILE,
    get_refresh_url,
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


def _pick_refresh_url_candidate(post_urls: list[str]) -> str | None:
    """Choose best POST URL that looks like a session/token refresh endpoint."""
    for url in post_urls:
        lower = url.lower()
        if "refresh" in lower:
            return url
    for url in post_urls:
        lower = url.lower()
        if "token" in lower or "session" in lower:
            return url
    return post_urls[0] if post_urls else None


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

    if not get_refresh_url() and post_urls:
        candidate = _pick_refresh_url_candidate(post_urls)
        if candidate:
            DISCOVERED_REFRESH_URL_FILE.write_text(candidate)
            print(f"[+] Discovered refresh URL (from browser): {candidate}")

    await context.storage_state(path=str(AUTH_STATE_FILE))
    LAST_REFRESH_FILE.write_text(str(time.time()))
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

    if not get_refresh_url() and post_urls:
        candidate = _pick_refresh_url_candidate(post_urls)
        if candidate:
            DISCOVERED_REFRESH_URL_FILE.write_text(candidate)
            print(f"[+] Discovered refresh URL (from browser): {candidate}")

    await context.storage_state(path=str(AUTH_STATE_FILE))
    LAST_REFRESH_FILE.write_text(str(time.time()))
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
    Records POST requests to discover a refresh URL if REFRESH_URL is not set.

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


async def _reload_csrf_after_refresh(previous_csrf: str | None = None) -> str | bool | None:
    """
    Open a page with the refreshed session and re-extract CSRF (server may have
    rotated it). Does not write to disk; returns the token for the caller to save.
    Returns the new token if found and different from previous_csrf; False if
    the token from the page equals previous_csrf (likely stale); None if no token found.
    """
    if not BASE_URL:
        return None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(AUTH_STATE_FILE))
        page = await context.new_page()
        csrf_token = None
        for url in (BASE_URL, LOGIN_URL):
            if url == LOGIN_URL and url == BASE_URL:
                continue
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_load_state("networkidle", timeout=8000)
                await asyncio.sleep(1)
                csrf_token = await _extract_csrf(page, context)
                if csrf_token:
                    break
            except Exception:
                continue
        await browser.close()
    if not csrf_token:
        return None
    if previous_csrf and csrf_token.strip() == previous_csrf.strip():
        return False  # same token, likely stale after refresh
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


def _csrf_from_response_headers(headers: dict) -> str | None:
    """Try to get a CSRF token from response headers (e.g. refresh returns new token)."""
    try:
        lower = {k.lower(): v for k, v in headers.items() if isinstance(v, str)}
    except Exception:
        return None
    for key in ("x-csrf-token", "x-xsrf-token", "csrf-token", "xsrf-token"):
        v = lower.get(key)
        if v and v.strip():
            return v.strip()
    return None


def _csrf_from_response_body(body: str) -> str | None:
    """Try to get a CSRF token from a JSON response body (e.g. refresh returns new token)."""
    if not body or not body.strip():
        return None
    body = body.strip()
    if not (body.startswith("{") and body.endswith("}")):
        return None
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            return None
        for key in ("csrfToken", "csrf_token", "xsrfToken", "xsrf_token", "csrf", "token"):
            if key in data and data[key] and isinstance(data[key], str):
                return data[key].strip()
        return None
    except json.JSONDecodeError:
        return None


async def refresh_session() -> bool:
    """
    POST to the refresh endpoint with saved auth + CSRF. On success, overwrite
    auth_state.json with the new session and record last_refresh time.
    Re-extract CSRF from response body or by loading a page (server may rotate it).
    Uses REFRESH_URL from .env, or the URL discovered during login (Playwright).
    """
    if not AUTH_STATE_FILE.exists():
        print(f"[-] {AUTH_STATE_FILE} not found. Run login first.")
        return False

    refresh_url = get_refresh_url()
    if not refresh_url:
        print("[-] No refresh URL. Set REFRESH_URL in .env or run 'login' to discover it from the browser.")
        return False

    print(f"[*] Refreshing session at {refresh_url}...")
    csrf_token = _load_csrf()
    if not csrf_token:
        print("[-] No CSRF token; refresh may fail with 403.")

    async with async_playwright() as p:
        api_context = await p.request.new_context(storage_state=str(AUTH_STATE_FILE))
        headers = {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
            "X-XSRF-TOKEN": csrf_token,
        }
        try:
            response = await api_context.post(refresh_url, headers=headers)
            if response.ok:
                await api_context.storage_state(path=str(AUTH_STATE_FILE))
                LAST_REFRESH_FILE.write_text(str(time.time()))
                print("[+] Session refreshed and saved.")

                # Server may have rotated CSRF: try response headers, then body, then re-extract from page
                new_csrf = None
                try:
                    new_csrf = _csrf_from_response_headers(dict(response.headers or {}))
                except Exception:
                    pass
                if not new_csrf:
                    try:
                        resp_text = await response.text()
                        new_csrf = _csrf_from_response_body(resp_text)
                    except Exception:
                        pass
                if new_csrf:
                    CSRF_TOKEN_FILE.write_text(json.dumps({"csrf_token": new_csrf}, indent=2))
                    print("[+] CSRF token updated from refresh response.")
                else:
                    page_csrf = await _reload_csrf_after_refresh(previous_csrf=csrf_token)
                    if isinstance(page_csrf, str):
                        CSRF_TOKEN_FILE.write_text(json.dumps({"csrf_token": page_csrf}, indent=2))
                        print("[+] CSRF token updated from app page.")
                    elif page_csrf is False:
                        print("[!] CSRF from app page was unchanged (may be stale). API may return 403; run 'login' to get a fresh token.")
                    else:
                        print("[!] Could not get new CSRF after refresh. If API calls return 403, run 'login' again.")
                return True
            print(f"[-] Refresh failed: status {response.status} — {await response.text()}")
            return False
        except Exception as e:
            print(f"[-] Refresh error: {e}")
            return False
        finally:
            await api_context.dispose()


async def ensure_session_fresh() -> bool:
    """
    If the session was last refreshed more than REFRESH_MAX_AGE_SECONDS ago (14 min),
    call refresh_session(). Returns True if the session is (now) fresh.
    """
    now = time.time()
    if LAST_REFRESH_FILE.exists():
        try:
            last = float(LAST_REFRESH_FILE.read_text().strip())
            if now - last < REFRESH_MAX_AGE_SECONDS:
                return True
        except (ValueError, OSError):
            pass
    # No recent refresh: run refresh (or first time after login we may not have last_refresh)
    if not AUTH_STATE_FILE.exists():
        return True  # No session yet; discover will fail with its own message
    print("[*] Session older than 14 minutes; refreshing...")
    return await refresh_session()