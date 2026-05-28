"""Detect and handle page blockers (login walls, cookies, captchas) before test submission."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from browser_bot.sites import load_component_config, load_component_config_raw, save_component_config
from browser_bot.submit.common import _TEXT_TYPES, _first_visible_locator, log_airta_progress

if TYPE_CHECKING:
    from playwright.async_api import Page

ADVICE_BY_KIND: dict[str, list[str]] = {
    "login_required": [
        "Click Log in in the dialog to open a browser window.",
        "After sign-in, click Save auth - tests will re-run automatically.",
    ],
    "captcha": [
        "Switch Fetch Method to human and disable Headless, complete the captcha during Add Login, then try again.",
    ],
    "cookie_consent": [],
    "rate_limited": [
        "Wait for the configured backoff period before retrying.",
        "Click Wait & retry in the dialog, or reduce pool size / concurrency in Settings.",
    ],
    "not_ready": [
        "Check the live preview - prompt box or send button may be missing.",
        "Re-run Discovery if selectors changed.",
    ],
}

DISMISS_SELECTORS: list[tuple[str, str]] = [
    ('button:has-text("Accept all")', "cookie consent"),
    ('button:has-text("Accept All")', "cookie consent"),
    ('button:has-text("Reject non-essential")', "cookie consent"),
    ('button:has-text("Allow all")', "cookie consent"),
    ('button:has-text("Allow All")', "cookie consent"),
    ('button:has-text("I agree")', "cookie consent"),
    ('button:has-text("Got it")', "cookie banner"),
    ('button:has-text("OK")', "dialog dismiss"),
    ('[aria-label="Close"]', "dialog close"),
    ('button[aria-label="Close"]', "dialog close"),
]

CAPTCHA_SELECTORS = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[title*="reCAPTCHA"]',
    'iframe[title*="hcaptcha"]',
    '[class*="captcha" i]',
)

LOGIN_URL_MARKERS = ("/login", "/signin", "/sign-in", "/auth", "/oauth")

LOGIN_PAGE_PHRASES = (
    "log in or sign up",
    "log in to continue",
    "sign in to continue",
    "sign up to continue",
    "continue with google",
    "continue with apple",
    "continue with microsoft",
)

# Visible login UI - more reliable than body text alone (ChatGPT keeps #prompt-textarea on login page).
LOGIN_WALL_SELECTORS = (
    'text=Log in or sign up',
    'text=Log in to continue',
    'text=Sign in to continue',
    'button:has-text("Continue with Google")',
    'button:has-text("Continue with Apple")',
    'button:has-text("Continue with Microsoft")',
)

RATE_LIMIT_PHRASES = (
    "too many requests",
    "making requests too quickly",
    "temporarily limited",
    "rate limit",
    "rate-limit",
    "slow down",
    "try again later",
)

RATE_LIMIT_SELECTORS = (
    "text=Too many requests",
    'h1:has-text("Too many requests")',
    'h2:has-text("Too many requests")',
    '[role="dialog"]:has-text("too many requests")',
)

# Dismiss only on rate-limit modals (not generic cookie/dialog heal).
RATE_LIMIT_DISMISS_SELECTORS = (
    'button:has-text("Got it")',
    'button:has-text("OK")',
)


class PageBlockedError(RuntimeError):
    """Raised when the page cannot proceed with test submission."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "not_ready",
        advice: list[str] | None = None,
        discovered: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.advice = advice or ADVICE_BY_KIND.get(kind, ADVICE_BY_KIND["not_ready"])
        self.discovered = discovered or []


def _normalize_blocker(raw: dict[str, Any]) -> dict[str, Any] | None:
    selector = str(raw.get("selector") or "").strip()
    if not selector:
        return None
    action = str(raw.get("action") or "detect").strip().lower()
    if action not in ("click", "detect"):
        action = "detect"
    label = str(raw.get("label") or raw.get("kind") or "blocker").strip()
    return {"selector": selector, "action": action, "label": label}


def persist_blockers(site: str, component: str, blockers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge new blockers into component config (dedupe by selector). Returns newly saved entries."""
    if not site or not component or not blockers:
        return []
    raw = load_component_config_raw(site, component)
    sub = raw.setdefault("submission", {})
    existing = sub.setdefault("blockers", [])
    if not isinstance(existing, list):
        existing = []
        sub["blockers"] = existing
    saved: list[dict[str, Any]] = []
    known = {str(b.get("selector", "")).strip() for b in existing if isinstance(b, dict)}
    for item in blockers:
        norm = _normalize_blocker(item)
        if not norm or norm["selector"] in known:
            continue
        existing.append(norm)
        known.add(norm["selector"])
        saved.append(norm)
    if saved:
        save_component_config(site, component, raw)
        for b in saved:
            print(f"[+] Saved blocker to config: {b['label']} ({b['selector']})", flush=True)
    return saved


async def _click_blocker(page: Page, blocker: dict[str, Any]) -> bool:
    if blocker.get("action") != "click":
        return False
    selector = blocker.get("selector", "")
    if not selector:
        return False
    try:
        loc = await _first_visible_locator(page, selector)
        if await loc.is_visible() and await loc.is_enabled():
            await loc.click(timeout=3000)
            await asyncio.sleep(0.35)
            return True
    except Exception:
        pass
    return False


async def apply_configured_blockers(page: Page, blockers: list[dict[str, Any]] | None) -> list[str]:
    """Click configured blockers with action=click. Returns labels that were clicked."""
    if await _rate_limit_visible(page):
        return []
    clicked: list[str] = []
    for raw in blockers or []:
        norm = _normalize_blocker(raw) if isinstance(raw, dict) else None
        if not norm:
            continue
        if await _click_blocker(page, norm):
            clicked.append(norm["label"])
    return clicked


async def _attempt_cookie_self_heal(
    page: Page,
    *,
    site: str,
    component: str,
    blockers: list[dict[str, Any]] | None,
    start_url: str = "",
) -> list[dict[str, Any]]:
    """Click cookie/dialog dismiss controls and persist selectors. Returns newly saved blockers."""
    if await _login_wall_visible(page, start_url=start_url):
        return []
    if await _rate_limit_visible(page):
        return []
    clicked_labels: list[str] = []
    await apply_configured_blockers(page, blockers)
    discovered = await discover_dismiss_blockers(page)
    saved: list[dict[str, Any]] = []
    if discovered and site and component:
        saved = persist_blockers(site, component, discovered)
    for item in discovered:
        if await _click_blocker(page, item):
            clicked_labels.append(item.get("label", "blocker"))
    if clicked_labels:
        print(f"[+] Dismissed: {', '.join(clicked_labels)}", flush=True)
        await asyncio.sleep(0.4)
    return saved


async def _login_wall_visible(page: Page, *, start_url: str = "") -> bool:
    if await _detect_login_wall(page, start_url=start_url):
        return True
    for selector in LOGIN_WALL_SELECTORS:
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                continue
            node = await _first_visible_locator(page, selector)
            if await node.is_visible():
                return True
        except Exception:
            continue
    return False


def _resolve_login_url(site: str, component: str = "", start_url: str = "") -> str:
    """Login page URL from component/site config, or sensible default for the site."""
    if site and component:
        url = (load_component_config(site, component).get("login_url") or "").strip()
        if url:
            return url
    if site:
        from browser_bot.sites import load_site_config

        url = (load_site_config(site).get("login_url") or "").strip()
        if url:
            return url
        if "localhost" in site or site.startswith("127."):
            return f"http://{site}"
        return f"https://{site}"
    return (start_url or "").strip()


def _site_has_saved_session(site: str) -> bool:
    """True when auth.json has real session data (not public/no-login stub)."""
    if not site:
        return False
    from browser_bot.auth_state import load_auth_config

    cfg = load_auth_config(site) or {}
    if cfg.get("auth_mode") in ("none", "api_key"):
        return False
    if cfg.get("cookies"):
        return True
    for origin in cfg.get("origins") or []:
        if origin.get("localStorage") or origin.get("sessionStorage"):
            return True
    return False


async def _attempt_auth_self_heal(page: Page, site: str, start_url: str) -> bool:
    """Reload page using cookies already on the browser context. Returns True if login wall cleared."""
    if not _site_has_saved_session(site):
        return False
    target = start_url or page.url
    if not target:
        return False
    print("[*] Login screen detected - reloading saved session…", flush=True)
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(0.75)
    except Exception:
        return False
    return not await _login_wall_visible(page, start_url=start_url)


def _raise_login_blocked(
    site: str,
    *,
    component: str = "",
    start_url: str = "",
) -> None:
    if component:
        persist_blockers(
            site,
            component,
            [{"selector": 'text=Log in or sign up', "label": "login wall", "action": "detect"}],
        )
    login_url = _resolve_login_url(site, component, start_url)
    if _site_has_saved_session(site):
        msg = "Session expired - sign in to continue tests."
    else:
        msg = "Sign-in required to continue tests."
    advice = ADVICE_BY_KIND["login_required"]
    _emit_blocked(
        "login_required",
        msg,
        advice=advice,
        action="prompt_login",
        login_url=login_url,
        site=site,
    )
    raise PageBlockedError(msg, kind="login_required", advice=advice)


async def _resolve_login_wall(
    page: Page,
    *,
    site: str,
    component: str,
    start_url: str,
) -> None:
    if not await _login_wall_visible(page, start_url=start_url):
        return
    if await _attempt_auth_self_heal(page, site, start_url):
        print("[+] Session reload cleared login screen", flush=True)
        return
    _raise_login_blocked(site, component=component, start_url=start_url)


async def check_login_wall_before_submit(
    page: Page,
    *,
    site: str,
    component: str = "",
    start_url: str = "",
) -> None:
    """Lightweight login check before each prompt (multi-turn mid-session redirects)."""
    await _resolve_login_wall(page, site=site, component=component, start_url=start_url)
    await _resolve_rate_limit(page, site=site, component=component, start_url=start_url)


def _rate_limit_settings(site: str, component: str) -> tuple[float, bool]:
    """Return (backoff_seconds, auto_wait_before_blocking)."""
    from browser_bot.config import RATE_LIMIT_AUTO_WAIT, RATE_LIMIT_BACKOFF_S

    backoff = float(RATE_LIMIT_BACKOFF_S)
    auto_wait = bool(RATE_LIMIT_AUTO_WAIT)
    if site and component:
        settings = (load_component_config(site, component).get("settings") or {})
        if settings.get("RATE_LIMIT_BACKOFF_S") is not None:
            backoff = float(settings["RATE_LIMIT_BACKOFF_S"])
        if settings.get("RATE_LIMIT_AUTO_WAIT") is not None:
            auto_wait = bool(settings["RATE_LIMIT_AUTO_WAIT"])
    return max(0.0, backoff), auto_wait


async def _detect_rate_limit(page: Page) -> bool:
    try:
        body_text = (await page.inner_text("body"))[:12000].lower()
    except Exception:
        body_text = ""
    return any(phrase in body_text for phrase in RATE_LIMIT_PHRASES)


async def _rate_limit_visible(page: Page) -> bool:
    if await _detect_rate_limit(page):
        return True
    for selector in RATE_LIMIT_SELECTORS:
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                continue
            node = await _first_visible_locator(page, selector)
            if await node.is_visible():
                return True
        except Exception:
            continue
    return False


async def _dismiss_rate_limit_modal(page: Page) -> bool:
    for selector in RATE_LIMIT_DISMISS_SELECTORS:
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                continue
            node = await _first_visible_locator(page, selector)
            if await node.is_visible() and await node.is_enabled():
                await node.click(timeout=3000)
                await asyncio.sleep(0.35)
                return True
        except Exception:
            continue
    return False


async def _attempt_rate_limit_self_heal(
    page: Page,
    *,
    site: str,
    component: str,
    start_url: str,
) -> bool:
    """Wait configured backoff, dismiss modal, reload. Returns True if rate limit cleared."""
    backoff, auto_wait = _rate_limit_settings(site, component)
    if not auto_wait or backoff <= 0:
        return False
    print(f"[*] Rate limit detected - waiting {backoff:.0f}s before retry…", flush=True)
    log_airta_progress(
        {
            "type": "rate_limit_wait",
            "backoff_sec": backoff,
            "message": f"Rate limited - waiting {backoff:.0f}s…",
        }
    )
    await asyncio.sleep(backoff)
    await _dismiss_rate_limit_modal(page)
    target = start_url or page.url
    if target:
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_load_state("load", timeout=10000)
            except Exception:
                pass
            await asyncio.sleep(0.75)
        except Exception:
            pass
    cleared = not await _rate_limit_visible(page)
    if cleared:
        print("[+] Rate limit cleared after backoff", flush=True)
    return cleared


def _raise_rate_limit_blocked(
    site: str,
    *,
    component: str = "",
    backoff_sec: float = 120.0,
    auto_wait_attempted: bool = False,
) -> None:
    if component:
        persist_blockers(
            site,
            component,
            [{"selector": "text=Too many requests", "label": "rate limit", "action": "detect"}],
        )
    msg = "Rate limited - wait before retrying tests."
    advice = ADVICE_BY_KIND["rate_limited"]
    _emit_blocked(
        "rate_limited",
        msg,
        advice=advice,
        action="prompt_rate_limit",
        backoff_sec=backoff_sec,
        auto_wait_attempted=auto_wait_attempted,
        site=site,
    )
    raise PageBlockedError(msg, kind="rate_limited", advice=advice)


async def _resolve_rate_limit(
    page: Page,
    *,
    site: str,
    component: str,
    start_url: str,
) -> None:
    if not await _rate_limit_visible(page):
        return
    if await _attempt_rate_limit_self_heal(page, site=site, component=component, start_url=start_url):
        return
    backoff, auto_wait = _rate_limit_settings(site, component)
    _raise_rate_limit_blocked(
        site,
        component=component,
        backoff_sec=backoff,
        auto_wait_attempted=auto_wait,
    )


async def check_rate_limit_before_submit(
    page: Page,
    *,
    site: str,
    component: str = "",
    start_url: str = "",
) -> None:
    """Rate-limit check before each prompt (multi-turn)."""
    await _resolve_rate_limit(page, site=site, component=component, start_url=start_url)


async def _detect_login_wall(page: Page, *, start_url: str = "") -> bool:
    url = (page.url or start_url or "").lower()
    if any(marker in url for marker in LOGIN_URL_MARKERS):
        return True
    try:
        body_text = (await page.inner_text("body"))[:8000].lower()
    except Exception:
        body_text = ""
    return any(phrase in body_text for phrase in LOGIN_PAGE_PHRASES)


async def discover_dismiss_blockers(page: Page) -> list[dict[str, Any]]:
    """Find visible dismiss/accept controls for cookie banners and dialogs."""
    if await _rate_limit_visible(page):
        return []
    found: list[dict[str, Any]] = []
    for selector, label in DISMISS_SELECTORS:
        try:
            loc = page.locator(selector)
            if await loc.count() == 0:
                continue
            node = await _first_visible_locator(page, selector)
            if await node.is_visible() and await node.is_enabled():
                found.append({"selector": selector, "label": label, "action": "click"})
        except Exception:
            continue
    return found


async def detect_heuristic_blockers(page: Page, *, start_url: str = "") -> list[dict[str, Any]]:
    """Heuristic signals: login wall, captcha widgets, cookie consent text."""
    found: list[dict[str, Any]] = []
    if await _login_wall_visible(page, start_url=start_url):
        found.append(
            {
                "kind": "login_required",
                "message": "Sign-in required to continue tests.",
            }
        )

    if await _rate_limit_visible(page):
        found.append(
            {
                "kind": "rate_limited",
                "message": "Rate limited - wait before retrying tests.",
            }
        )

    for sel in CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                found.append(
                    {
                        "kind": "captcha",
                        "message": f"Possible captcha detected ({sel})",
                    }
                )
                break
        except Exception:
            continue

    try:
        body_text = (await page.inner_text("body"))[:12000].lower()
    except Exception:
        body_text = ""

    cookie_phrases = ("we use cookies", "cookie policy", "cookie preferences", "accept cookies")
    if any(p in body_text for p in cookie_phrases) and (
        "accept all" in body_text or "reject non-essential" in body_text or "allow all" in body_text
    ):
        found.append(
            {
                "kind": "cookie_consent",
                "message": "Cookie consent language detected on page",
            }
        )

    return found


async def check_submission_readiness(
    page: Page,
    inputs: list[dict],
    submit_selector: str = "",
    *,
    timeout_ms: int = 5000,
) -> tuple[bool, str]:
    """Return (ready, reason) when prompt inputs are visible.

    Submit button is intentionally not checked here - many chat UIs hide or
    disable send until the prompt field has text (_do_one_submit_step handles that).
    """
    del submit_selector  # kept for call-site compatibility
    deadline = time.perf_counter() + max(timeout_ms, 500) / 1000.0
    last_reason = "prompt input not ready"
    text_inputs = [inp for inp in inputs if inp.get("type", "text") in _TEXT_TYPES and inp.get("selector")]
    if not text_inputs:
        return True, ""

    while time.perf_counter() < deadline:
        reasons: list[str] = []
        for inp in text_inputs:
            selector = inp.get("selector", "")
            try:
                loc = await _first_visible_locator(page, selector)
                if not await loc.is_visible():
                    reasons.append(f"input not visible: {selector}")
            except Exception:
                reasons.append(f"input missing: {selector}")

        if not reasons:
            return True, ""
        last_reason = "; ".join(reasons)
        await asyncio.sleep(0.2)
    return False, last_reason


def _emit_blocked(
    kind: str,
    message: str,
    *,
    advice: list[str] | None = None,
    discovered: list | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {
        "type": "blocked",
        "kind": kind,
        "message": message,
        "advice": advice or ADVICE_BY_KIND.get(kind, ADVICE_BY_KIND["not_ready"]),
        "discovered": discovered or [],
    }
    payload.update(extra)
    log_airta_progress(payload)


def _pick_primary_kind(heuristics: list[dict[str, Any]], readiness_reason: str) -> str:
    order = ("captcha", "rate_limited", "login_required", "cookie_consent", "not_ready")
    kinds = [h.get("kind") for h in heuristics if h.get("kind")]
    for kind in order:
        if kind in kinds:
            return str(kind)
    if "input missing" in readiness_reason.lower() or "input not visible" in readiness_reason.lower():
        return "login_required"
    return "not_ready"


def _blocked_message(kind: str, detail: str) -> str:
    if kind == "login_required":
        return "Sign-in required to continue tests."
    if kind == "rate_limited":
        return "Rate limited - wait before retrying tests."
    if kind == "captcha":
        return "Captcha detected - use Add Login with Headless off, then try again."
    if kind == "not_ready":
        return f"Prompt UI not ready: {detail}"
    return f"Cannot proceed: {detail}"


async def ensure_page_ready_for_submit(
    page: Page,
    *,
    site: str,
    component: str,
    inputs: list[dict],
    submit_selector: str,
    start_url: str = "",
    blockers: list[dict[str, Any]] | None = None,
    readiness_timeout_ms: int = 5000,
) -> None:
    """Cookie self-heal, login/rate-limit detection, then captcha checks."""
    await asyncio.sleep(0.35)

    await _resolve_login_wall(page, site=site, component=component, start_url=start_url)
    await _resolve_rate_limit(page, site=site, component=component, start_url=start_url)

    await _attempt_cookie_self_heal(
        page, site=site, component=component, blockers=blockers, start_url=start_url
    )

    await _resolve_login_wall(page, site=site, component=component, start_url=start_url)
    await _resolve_rate_limit(page, site=site, component=component, start_url=start_url)

    await check_submission_readiness(
        page, inputs, submit_selector, timeout_ms=min(readiness_timeout_ms, 3000)
    )

    heuristics = await detect_heuristic_blockers(page, start_url=start_url)
    for h in heuristics:
        kind = h.get("kind")
        if kind == "login_required":
            _raise_login_blocked(site, component=component, start_url=start_url)
        if kind == "rate_limited":
            backoff, auto_wait = _rate_limit_settings(site, component)
            _raise_rate_limit_blocked(
                site,
                component=component,
                backoff_sec=backoff,
                auto_wait_attempted=auto_wait,
            )
        if kind == "captcha":
            msg = _blocked_message("captcha", h.get("message", "") or "")
            advice = ADVICE_BY_KIND["captcha"]
            _emit_blocked("captcha", msg, advice=advice)
            raise PageBlockedError(msg, kind="captcha", advice=advice)
