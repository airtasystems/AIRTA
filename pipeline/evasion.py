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

# Right-half screen (e.g. for UI + browser side by side): position at 960,0 and use half width
HALF_VIEWPORT_WIDTH = 960
WINDOW_POSITION_RIGHT_HALF = (960, 0)

# Cloudflare / anti-bot evasion: launch args to hide automation indicators
CLOUDFLARE_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",  # Hides navigator.webdriver
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
]

# Context options for realistic browser fingerprint (Cloudflare evasion)
DEFAULT_LOCALE = "en-GB"
DEFAULT_TIMEZONE_ID = "Europe/London"

# Rate limit and server-error retry
MAX_ATTEMPTS_429 = 4
MAX_ATTEMPTS_5XX = 4  # 500, 502, 503, 504
DEFAULT_BACKOFF_SECONDS = 60      # 429 rate limit
DEFAULT_BACKOFF_5XX_SECONDS = 120  # 500/502/503/504 (server can take longer to recover)
THROTTLE_BETWEEN_PAYLOADS_SEC = 1.2
# Min gap between request starts when using concurrent mode (--speed > 1)
MIN_GAP_BETWEEN_REQUESTS_SEC = 0.3
# HTTP status codes we retry on (transient: rate limit + server overload/unavailable)
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

# Token bucket rate limiter (optional): set RATE_LIMIT_CAPACITY and RATE_LIMIT_REFILL_PER_SEC in .config
DEFAULT_RATE_LIMIT_CAPACITY = 4
DEFAULT_RATE_LIMIT_REFILL_PER_SEC = 0.5


class TokenBucket:
    """
    Async token bucket: refill at refill_per_sec, capacity cap. acquire() consumes
    one token, sleeping if necessary until a token is available.
    """

    def __init__(self, capacity: float, refill_per_sec: float):
        self._capacity = max(1.0, float(capacity))
        self._refill_per_sec = max(0.01, float(refill_per_sec))
        self._tokens = self._capacity
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_sec)
        self._last_refill = now

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            self._refill()
            need = tokens
            while self._tokens < need:
                wait_sec = (need - self._tokens) / self._refill_per_sec
                await asyncio.sleep(wait_sec)
                self._refill()
            self._tokens -= need


def get_token_bucket() -> TokenBucket | None:
    """
    Return a shared TokenBucket if RATE_LIMIT_CAPACITY and RATE_LIMIT_REFILL_PER_SEC
    are set (in .config or env). Otherwise return None (no client-side rate limiting).
    """
    cap_str = (os.getenv("RATE_LIMIT_CAPACITY") or "").strip()
    refill_str = (os.getenv("RATE_LIMIT_REFILL_PER_SEC") or "").strip()
    if not cap_str or not refill_str:
        return None
    try:
        capacity = float(cap_str)
        refill = float(refill_str)
    except ValueError:
        return None
    if capacity < 1 or refill <= 0:
        return None
    return TokenBucket(capacity=capacity, refill_per_sec=refill)


def get_token_bucket_or_default(concurrency: int) -> TokenBucket | None:
    """
    Return get_token_bucket() if configured; else when concurrency > 1 return a
    default token bucket to limit burst (capacity=2, refill=0.5/s). Use this so
    concurrent mode never blasts the server with an unbounded burst.
    Set RATE_LIMIT_DISABLE_DEFAULT=1 to skip the default bucket when using --speed>1.
    """
    if (os.getenv("RATE_LIMIT_DISABLE_DEFAULT") or "").strip() in ("1", "true", "yes"):
        return get_token_bucket()
    bucket = get_token_bucket()
    if bucket is not None:
        return bucket
    if concurrency <= 1:
        return None
    # Conservative default: burst cap 2, refill 0.5/s so we don't hammer burst limits
    capacity = min(2, concurrency)
    refill = 0.5
    return TokenBucket(capacity=capacity, refill_per_sec=refill)


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


def url_origin(url: str) -> str:
    """Return origin (scheme + netloc) for loading a page before fetch. E.g. https://chatgpt.com/backend/... -> https://chatgpt.com"""
    from urllib.parse import urlparse
    p = urlparse(url)
    scheme = p.scheme or "https"
    netloc = p.netloc or "localhost"
    return f"{scheme}://{netloc}"


def get_remote_browser_url() -> str | None:
    """
    Return WebSocket URL for remote browser (Browserless, Scrappey, etc.) if configured.
    Set REMOTE_BROWSER_URL or BROWSERLESS_URL (e.g. wss://chrome.browserless.io?token=...).
    When set, pre-discovery uses this instead of launching a local browser.
    """
    url = (os.getenv("REMOTE_BROWSER_URL") or os.getenv("BROWSERLESS_URL") or "").strip()
    return url if url else None


def get_cloudflare_launch_args(*, window_position: tuple[int, int] | None = None) -> list[str]:
    """
    Return launch args for Cloudflare/anti-bot evasion.
    Optionally prepend window position for right-half placement.
    """
    args = list(CLOUDFLARE_LAUNCH_ARGS)
    if window_position is not None:
        args.insert(0, f"--window-position={window_position[0]},{window_position[1]}")
    return args


def get_browser_context_options(viewport: dict[str, int] | None = None) -> dict[str, Any]:
    """
    Return context options for realistic browser fingerprint (Cloudflare evasion).
    Includes rotated User-Agent, locale, timezone. Merge with viewport/storage_state as needed.
    """
    opts: dict[str, Any] = {
        "user_agent": random.choice(USER_AGENTS),
        "locale": DEFAULT_LOCALE,
        "timezone_id": DEFAULT_TIMEZONE_ID,
        "permissions": [],
    }
    if viewport:
        opts["viewport"] = viewport
    return opts


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


def human_delay_long(min_ms: float = 800, max_ms: float = 1800) -> float:
    """Return a longer random delay in seconds (e.g. before page load) to mimic human hesitation."""
    return random.uniform(min_ms / 1000.0, max_ms / 1000.0)


async def warm_up_page_human_like(page, viewport_width: int, viewport_height: int) -> None:
    """
    Simulate human-like behavior after page load: move mouse to center, small scroll.
    Reduces bot-like "instant interaction" patterns that Cloudflare detects.
    """
    center_x = viewport_width / 2
    center_y = viewport_height / 2
    # Start from upper-left area (natural cursor position when tab opens)
    from_x = random.uniform(80, 180)
    from_y = random.uniform(80, 180)
    to_x = center_x + random.uniform(-40, 40)
    to_y = center_y + random.uniform(-40, 40)
    await move_mouse_human_like(page, (from_x, from_y), (to_x, to_y), steps=12)
    await asyncio.sleep(human_delay(150, 350))
    await scroll_human_like(page, random.uniform(-60, 60), steps=3)
    await asyncio.sleep(human_delay(200, 500))


async def apply_stealth(page) -> bool:
    """Apply playwright-stealth to the page if available. Returns True if applied."""
    if not _STEALTH_AVAILABLE:
        return False
    try:
        await Stealth().apply_stealth_async(page)
        return True
    except Exception:
        return False


def parse_retry_after(headers: dict, body_text: str | None = None, default_seconds: int | None = None) -> int:
    """Return wait seconds from Retry-After header or JSON body, else default_seconds or DEFAULT_BACKOFF_SECONDS."""
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
    return default_seconds if default_seconds is not None else DEFAULT_BACKOFF_SECONDS


class RateLimit429(Exception):
    """Raised when a request returns HTTP 429 so tenacity can retry with backoff."""
    def __init__(self, response: Any, body_text: str = ""):
        self.response = response
        self.body_text = body_text or ""


class RetryableServerError(Exception):
    """Raised when a request returns 500/502/503/504 so tenacity can retry with backoff."""
    def __init__(self, response: Any, body_text: str = ""):
        self.response = response
        self.body_text = body_text or ""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (RateLimit429, RetryableServerError))


def _wait_retry_after(retry_state) -> float:
    """Tenacity wait: return seconds from Retry-After header/body or default backoff (longer for 5xx)."""
    if retry_state.outcome is None or not retry_state.outcome.failed:
        return DEFAULT_BACKOFF_SECONDS
    exc = retry_state.outcome.exception()
    if not _is_retryable(exc):
        return DEFAULT_BACKOFF_SECONDS
    headers = {}
    if getattr(exc.response, "headers", None):
        headers = dict(exc.response.headers)
    default = DEFAULT_BACKOFF_5XX_SECONDS if isinstance(exc, RetryableServerError) else DEFAULT_BACKOFF_SECONDS
    return parse_retry_after(headers, exc.body_text, default_seconds=default)


def _log_retry(retry_state) -> None:
    """Tenacity before_sleep: log status and wait time."""
    if retry_state.outcome is None or not retry_state.outcome.failed:
        return
    exc = retry_state.outcome.exception()
    if not _is_retryable(exc):
        return
    status = getattr(exc.response, "status", None) or "?"
    headers = {}
    if getattr(exc.response, "headers", None):
        headers = dict(exc.response.headers)
    default = DEFAULT_BACKOFF_5XX_SECONDS if isinstance(exc, RetryableServerError) else DEFAULT_BACKOFF_SECONDS
    secs = parse_retry_after(headers, getattr(exc, "body_text", None), default_seconds=default)
    n = retry_state.attempt_number + 1
    max_attempts = MAX_ATTEMPTS_429 if isinstance(exc, RateLimit429) else MAX_ATTEMPTS_5XX
    print(f"    [{status}] waiting {secs}s then retry {n}/{max_attempts}")


class _BrowserResponse:
    """Response-like object for post_via_browser (compatible with retry logic)."""

    def __init__(self, status: int, ok: bool, text: str, headers: dict | None = None):
        self.status = status
        self.ok = ok
        self._text = text
        self.headers = dict(headers) if headers else {}

    async def text(self) -> str:
        return self._text


async def post_via_browser(page, url: str, headers: dict, data: str) -> _BrowserResponse:
    """
    POST via page.evaluate(fetch) — uses browser's TLS fingerprint instead of
    Python's OpenSSL. Requires page to be loaded on same origin (or CORS-allowed).
    credentials: 'include' sends cookies.
    """
    # Drop headers that fetch will set or that break serialization
    headers_copy = {k: v for k, v in headers.items() if k.lower() not in ("content-length",)}
    headers_json = json.dumps(headers_copy)
    result = await page.evaluate(
        """
        async ({url, headersJson, body}) => {
            const headers = JSON.parse(headersJson);
            const res = await fetch(url, {
                method: 'POST',
                headers: headers,
                body: body,
                credentials: 'include'
            });
            const headersObj = {};
            res.headers.forEach((v, k) => { headersObj[k] = v; });
            return { status: res.status, ok: res.ok, text: await res.text(), headers: headersObj };
        }
        """,
        {"url": url, "headersJson": headers_json, "body": data},
    )
    return _BrowserResponse(
        status=result["status"],
        ok=result["ok"],
        text=result["text"],
        headers=result.get("headers"),
    )


def post_with_retry_429(api_context, url: str, headers: dict, data: str):
    """
    POST once; on 429 or 5xx (500, 502, 503, 504) raise so tenacity can wait and retry.
    When tenacity is not available, does a single attempt (no retry).
    Pass api_context (APIRequestContext) for request API, or a page for browser-based POST.
    """
    async def _post():
        if hasattr(api_context, "evaluate"):
            # It's a page — use browser-based POST (Chrome TLS fingerprint)
            response = await post_via_browser(api_context, url, headers, data)
        else:
            response = await api_context.post(url, headers=headers, data=data)
        try:
            body_text = await response.text()
        except Exception:
            body_text = ""
        if response.status == 429:
            raise RateLimit429(response, body_text)
        if response.status in (500, 502, 503, 504):
            raise RetryableServerError(response, body_text)
        return response

    if _TENACITY_AVAILABLE:
        max_attempts = max(MAX_ATTEMPTS_429, MAX_ATTEMPTS_5XX)
        decorated = retry(
            stop=stop_after_attempt(max_attempts),
            retry=retry_if_exception(_is_retryable),
            wait=_wait_retry_after,
            before_sleep=_log_retry,
            reraise=True,
        )(_post)
        return decorated()
    return _post()
