"""
Evasion helpers for A04 component discovery: rate limiting (tenacity), human
simulation (mouse, scroll, delays), WAF evasion (playwright-stealth), header
rotation, and optional proxy (e.g. Burp Suite via PROXY_LIST).
"""
import asyncio
import json
import os
import random
import time
from typing import Any

# Header rotation: vary User-Agent and Accept-Language per request
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]
ACCEPT_LANGUAGES = ["en-US,en;q=0.9", "en-GB,en;q=0.9", "en-US,en;q=0.9,es;q=0.8"]

# Optional tenacity for 429 retry
try:
    from tenacity import retry, retry_if_exception, stop_after_attempt
    _TENACITY_AVAILABLE = True
except ImportError:
    _TENACITY_AVAILABLE = False

# Optional playwright-stealth for WAF evasion
try:
    from playwright_stealth import Stealth
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False


# Viewport (full-screen style) for consistent, realistic browser signature
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080

# Rate limit retry
MAX_ATTEMPTS_429 = 4
DEFAULT_BACKOFF_SECONDS = 60
THROTTLE_BETWEEN_PAYLOADS_SEC = 1.2


def rotated_headers() -> dict[str, str]:
    """Return a dict of headers with rotated User-Agent and Accept-Language for fingerprint variation."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept": "application/json, text/plain, */*",
    }


def get_playwright_proxy() -> dict[str, str] | None:
    """
    Return a Playwright proxy dict from PROXY_LIST (e.g. PROXY_LIST=http://127.0.0.1:8080 for Burp).
    Uses the first URL if comma-separated. Returns None if PROXY_LIST is unset or empty.
    """
    raw = (os.getenv("PROXY_LIST") or "").strip()
    if not raw:
        return None
    first = raw.split(",")[0].strip()
    if not first:
        return None
    return {"server": first}


def human_delay(min_ms: float = 80, max_ms: float = 250) -> float:
    """Return a random delay in seconds to mimic human reaction time."""
    return random.uniform(min_ms / 1000.0, max_ms / 1000.0)


async def move_mouse_human_like(page, from_xy: tuple[float, float], to_xy: tuple[float, float], steps: int = 16) -> None:
    """Move the mouse along a slightly jittered path (WAF evasion: no teleport or perfect line)."""
    x0, y0 = from_xy
    x1, y1 = to_xy
    for i in range(1, steps + 1):
        t = i / steps
        jitter_x = random.uniform(-3, 3) if steps > 3 else 0
        jitter_y = random.uniform(-3, 3) if steps > 3 else 0
        x = x0 + (x1 - x0) * t + jitter_x
        y = y0 + (y1 - y0) * t + jitter_y
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.02, 0.08))


async def scroll_human_like(page, delta_y: float, steps: int = 5) -> None:
    """Scroll in steps with variable delay (WAF evasion: no instant full-page scroll)."""
    step = delta_y / steps
    for _ in range(steps):
        await page.mouse.wheel(0, step)
        await asyncio.sleep(random.uniform(0.1, 0.28))


async def apply_stealth(page) -> bool:
    """Apply playwright-stealth to the page if available. Returns True if applied."""
    if not _STEALTH_AVAILABLE:
        return False
    try:
        await Stealth().apply_stealth_async(page)
        return True
    except Exception:
        return False


def parse_retry_after(headers: dict, body_text: str | None = None) -> int:
    """Return wait seconds from Retry-After header or JSON body."""
    ra = None
    if isinstance(headers, dict):
        ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra is not None:
        ra = str(ra).strip()
        if ra.isdigit():
            return int(ra)
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(ra)
            return max(1, int(dt.timestamp() - time.time()))
        except Exception:
            pass
    if body_text:
        try:
            data = json.loads(body_text)
            if isinstance(data, dict):
                for key in ("retry_after", "retryAfter", "retry_after_seconds"):
                    if key in data and data[key] is not None:
                        return max(1, int(data[key]))
        except Exception:
            pass
    return DEFAULT_BACKOFF_SECONDS


class RateLimit429(Exception):
    """Raised when a request returns HTTP 429 so tenacity can retry with backoff."""
    def __init__(self, response: Any, body_text: str = ""):
        self.response = response
        self.body_text = body_text or ""


def _is_429(exc: BaseException) -> bool:
    return isinstance(exc, RateLimit429)


def _wait_retry_after(retry_state) -> float:
    """Tenacity wait: return seconds from 429 response Retry-After."""
    if retry_state.outcome is None or not retry_state.outcome.failed:
        return DEFAULT_BACKOFF_SECONDS
    exc = retry_state.outcome.exception()
    if not isinstance(exc, RateLimit429):
        return DEFAULT_BACKOFF_SECONDS
    headers = {}
    if getattr(exc.response, "headers", None):
        headers = dict(exc.response.headers)
    return parse_retry_after(headers, exc.body_text)


def _log_retry(retry_state) -> None:
    """Tenacity before_sleep: log 429 and wait time."""
    if retry_state.outcome is None or not retry_state.outcome.failed:
        return
    exc = retry_state.outcome.exception()
    if not isinstance(exc, RateLimit429):
        return
    headers = {}
    if getattr(exc.response, "headers", None):
        headers = dict(exc.response.headers)
    secs = parse_retry_after(headers, None)
    n = retry_state.attempt_number + 1
    print(f"    [429] waiting {secs}s (Retry-After) then retry {n}/{MAX_ATTEMPTS_429}")


def post_with_retry_429(api_context, url: str, headers: dict, data: str):
    """
    POST once; on 429 raise RateLimit429 so tenacity can wait and retry.
    When tenacity is not available, does a single attempt (no retry).
    """
    async def _post():
        response = await api_context.post(url, headers=headers, data=data)
        if response.status == 429:
            try:
                body_text = await response.text()
            except Exception:
                body_text = ""
            raise RateLimit429(response, body_text)
        return response

    if _TENACITY_AVAILABLE:
        decorated = retry(
            stop=stop_after_attempt(MAX_ATTEMPTS_429),
            retry=retry_if_exception(_is_429),
            wait=_wait_retry_after,
            before_sleep=_log_retry,
            reraise=True,
        )(_post)
        return decorated()
    return _post()
