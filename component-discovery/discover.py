"""
Dynamic discovery of LLM endpoint: load saved auth, open app, intercept the POST
request that matches the API URL set in .env when the user makes a manual request.

Includes capture_site_trace() for full network capture and LLM-powered analysis.
"""
import asyncio
import importlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

try:
    import zstandard as zstd
    _ZSTD_AVAILABLE = True
except ImportError:
    _ZSTD_AVAILABLE = False

from . import auth as auth_module
from . import config as config_module
from . import payload_format as payload_format_module
from .auth import _check_server_reachable
from pipeline import evasion

# Use config_module for values that may change after reload (first-run flow)
LOGIN_URL = config_module.LOGIN_URL
BASE_URL = config_module.BASE_URL
AUTH_STATE_FILE = config_module.AUTH_STATE_FILE
DISCOVERED_ENDPOINT_FILE = config_module.DISCOVERED_ENDPOINT_FILE
DISCOVERED_MULTI_FILE = config_module.DISCOVERED_MULTI_FILE
TARGET_API_URL = config_module.TARGET_API_URL


# ── Dynamic Discovery: Noise Filtering, Scoring, Response Analysis ──────────

_NOISE_DOMAINS = frozenset({
    "google-analytics.com", "www.google-analytics.com",
    "googletagmanager.com", "www.googletagmanager.com",
    "doubleclick.net",
    "facebook.net", "connect.facebook.net",
    "clarity.ms",
    "hotjar.com", "static.hotjar.com",
    "sentry.io",
    "segment.io", "cdn.segment.com", "api.segment.io",
    "mixpanel.com", "api.mixpanel.com",
    "plausible.io",
    "heapanalytics.com",
    "fullstory.com",
    "amplitude.com", "api.amplitude.com",
    "posthog.com", "app.posthog.com",
    "intercom.io", "api-iam.intercom.io",
    "crisp.chat",
    "hubspot.com", "forms.hubspot.com",
    "newrelic.com", "bam.nr-data.net",
    "datadoghq.com",
    "lr-ingest.io",
})

_NOISE_PATH_KEYWORDS = frozenset({
    "/collect", "/analytics", "/beacon", "/pixel", "/track",
    "/log", "/event", "/telemetry", "/metrics",
    "/__nextjs", "/_next/", "/_next/data",
    "/gtag/", "/gtm.js",
    "/r/collect", "/j/collect",
    "/_vercel/",
})

# Static asset extensions (exclude from capture)
_STATIC_EXTENSIONS = frozenset({".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".css", ".js", ".woff", ".woff2", ".ttf", ".eot"})

_NOISE_BODY_RE = re.compile(
    r"^en=|^tid=|^v=1&|^v=2&|&_et=|&en=page_view|&dl=http",
)

_AI_FIELD_NAMES = frozenset({
    "prompt", "message", "messages", "content", "query",
    "input", "text", "question", "user_message",
    "chat_input", "user_input", "instruction",
})

_AI_CONTEXT_FIELDS = frozenset({
    "stream", "model", "temperature", "max_tokens", "top_p",
    "role", "conversation_id", "chat_id", "thread_id",
    "session_id", "enable_thinking", "reasoning_effort",
})

_AI_PATH_KEYWORDS = (
    "/api/", "/v1/", "/v2/", "/chat", "/completions",
    "/generate", "/inference", "/agent", "/predict",
    "/query", "/ask", "/message", "/converse",
)

_AI_RESPONSE_FIELD_NAMES = frozenset({
    "content", "text", "response", "output", "answer",
    "message", "result", "completion", "generated_text",
    "reply", "assistant",
})

_MIN_SCORE_THRESHOLD = 0.3


def _is_noise(request) -> bool:
    """Return True if the request is analytics/telemetry noise, not an AI API call."""
    url = request.url
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()

    for domain in _NOISE_DOMAINS:
        if netloc == domain or netloc.endswith("." + domain):
            return True

    path = parsed.path.lower()
    for kw in _NOISE_PATH_KEYWORDS:
        if kw in path:
            return True

    try:
        body = request.post_data or ""
    except Exception:
        body = ""
    if body and _NOISE_BODY_RE.search(body):
        return True

    try:
        headers = request.headers
        ct = ""
        for k, v in headers.items():
            if k.lower() == "content-type":
                ct = v.lower()
                break
        if ct.startswith(("image/", "text/css", "font/", "audio/", "video/")):
            return True
    except Exception:
        pass

    return False


def _parse_body_fields(post_data: str | None, headers: dict) -> dict:
    """Quick parse of POST body into field-name -> value dict for scoring."""
    if not post_data:
        return {}
    try:
        data = json.loads(post_data)
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
    except (json.JSONDecodeError, TypeError):
        pass
    ct = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ct = v.lower()
            break
    if "multipart/form-data" in ct:
        fields = {}
        for m in re.finditer(r'name="([^"]+)"[^\r\n]*\r?\n\r?\n([^\r]*)', post_data):
            fields[m.group(1)] = m.group(2).strip()
        return fields
    if "=" in post_data and "&" in post_data and not post_data.strip().startswith("{"):
        fields = {}
        for part in post_data.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k] = v
        return fields
    return {}


def _score_request(request_data: dict, response_info: dict | None = None) -> float:
    """Score a request-response pair for 'LLM-ness'. Higher = more likely an AI API call."""
    score = 0.0
    url = request_data.get("url", "")
    headers = request_data.get("headers", {})
    post_data = request_data.get("post_data", "")

    fields = _parse_body_fields(post_data, headers)
    field_names_lower = {k.lower() for k in fields} if fields else set()

    prompt_hits = field_names_lower & _AI_FIELD_NAMES
    if prompt_hits:
        score += 0.25 * min(len(prompt_hits) / 2, 1.0)

    if fields and len(fields) >= 2:
        score += 0.15

    has_messages = "messages" in field_names_lower
    context_hits = field_names_lower & _AI_CONTEXT_FIELDS
    if has_messages:
        score += 0.20
    elif context_hits:
        score += 0.10 * min(len(context_hits) / 2, 1.0)

    if response_info:
        if response_info.get("is_sse"):
            score += 0.20
        elif response_info.get("has_long_text"):
            score += 0.15
        elif response_info.get("status") == 200:
            score += 0.05

    path = urlparse(url).path.lower()
    if any(kw in path for kw in _AI_PATH_KEYWORDS):
        score += 0.10

    netloc = urlparse(url).netloc.lower()
    is_analytics = any(netloc == d or netloc.endswith("." + d) for d in _NOISE_DOMAINS)
    if not is_analytics:
        score += 0.10

    return min(score, 1.0)


def _find_long_text_field(data, prefix: str = "", depth: int = 0) -> str | None:
    """Walk JSON looking for a long text field with an AI-like name."""
    if depth > 5:
        return None
    if isinstance(data, dict):
        for k, v in data.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, str) and len(v) > 50 and k.lower() in _AI_RESPONSE_FIELD_NAMES:
                return p
            result = _find_long_text_field(v, p, depth + 1)
            if result:
                return result
    elif isinstance(data, list):
        for i, item in enumerate(data):
            result = _find_long_text_field(item, f"{prefix}[{i}]", depth + 1)
            if result:
                return result
    return None


async def _probe_response(response) -> dict:
    """Capture response metadata for scoring: SSE, long text, content type."""
    info: dict = {
        "status": getattr(response, "status", 0),
        "content_type": "",
        "is_sse": False,
        "has_long_text": False,
        "text_field_path": None,
    }
    try:
        ct = response.headers.get("content-type", "")
        info["content_type"] = ct
        info["is_sse"] = "text/event-stream" in ct
    except Exception:
        pass
    if info["is_sse"]:
        return info
    try:
        body_bytes = await asyncio.wait_for(response.body(), timeout=5.0)
        body_text = body_bytes.decode("utf-8", errors="replace")[:4096]
        if body_text.strip().startswith(("{", "[")):
            try:
                data = json.loads(body_text)
                path = _find_long_text_field(data)
                if path:
                    info["has_long_text"] = True
                    info["text_field_path"] = path
            except json.JSONDecodeError:
                pass
        elif len(body_text) > 200:
            info["has_long_text"] = True
    except (asyncio.TimeoutError, Exception):
        pass
    return info


def _select_top_candidates(
    scored: list[tuple[float, dict]], n: int,
) -> list[dict]:
    """Pick the top *n* candidates by score, then re-order by arrival time."""
    above = [(s, d) for s, d in scored if s >= _MIN_SCORE_THRESHOLD]
    if len(above) >= n:
        pool = sorted(above, key=lambda x: x[0], reverse=True)[:n]
    else:
        pool = sorted(scored, key=lambda x: x[0], reverse=True)[:n]
    pool.sort(key=lambda x: x[1].get("timestamp", 0))
    return [d for _, d in pool]


def _detect_endpoint_chain(candidates: list[dict]) -> dict | None:
    """If the top candidates use different URLs, detect an initiate -> follow-up chain."""
    if len(candidates) < 2:
        return None
    urls = [c["url"] for c in candidates]
    unique = list(dict.fromkeys(urls))
    if len(unique) == 1:
        return None
    follow_up = unique[-1] if unique[-1] != unique[0] else (unique[1] if len(unique) > 1 else unique[0])
    return {
        "initiate_url": unique[0],
        "follow_up_url": follow_up,
        "chain": [
            {"url": u, "role": "initiate" if i == 0 else "follow_up"}
            for i, u in enumerate(unique)
        ],
    }


def _detect_prompt_field(candidates: list[dict]) -> str | None:
    """Infer which payload field carries the user prompt by comparing requests."""
    all_fields: list[dict] = []
    for c in candidates:
        fields = _parse_body_fields(c.get("post_data", ""), c.get("headers", {}))
        all_fields.append(fields)
    if not all_fields:
        return None

    _PROMPT_NAMES = ("prompt", "message", "content", "query", "input", "text", "messages")

    if len(all_fields) == 1:
        for name in _PROMPT_NAMES:
            if name in all_fields[0]:
                val = all_fields[0][name]
                if name == "messages" and isinstance(val, (list, str)):
                    return "messages[-1].content"
                return name
        return None

    changing: set[str] = set()
    base = all_fields[0]
    for fields in all_fields[1:]:
        for k in set(base) | set(fields):
            if base.get(k) != fields.get(k):
                changing.add(k)

    for name in _PROMPT_NAMES:
        if name in changing:
            val = base.get(name)
            if name == "messages" and isinstance(val, (list, str)):
                return "messages[-1].content"
            return name

    return next(iter(changing), None)


def _find_value_path(obj, target: str, prefix: str = "") -> str | None:
    """Find the JSONPath to a string value containing *target* in nested data."""
    if isinstance(obj, str) and target in obj:
        return prefix or "."
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            result = _find_value_path(v, target, p)
            if result:
                return result
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result = _find_value_path(item, target, f"{prefix}[{i}]")
            if result:
                return result
    return None


# Smaller discovery window to keep terminal instructions visible.
_DISCOVERY_WINDOW_WIDTH = 1280
_DISCOVERY_WINDOW_HEIGHT = 720
_DISCOVERY_WINDOW_POSITION = (80, 80)


def _discovery_browser_config(*, headless: bool, position_right_half: bool = False) -> tuple[list[str] | None, dict[str, int]]:
    """Return browser args and viewport for discovery flows."""
    viewport = {"width": _DISCOVERY_WINDOW_WIDTH, "height": _DISCOVERY_WINDOW_HEIGHT}
    args: list[str] | None = None
    if not headless:
        args = [
            f"--window-size={_DISCOVERY_WINDOW_WIDTH},{_DISCOVERY_WINDOW_HEIGHT}",
            f"--window-position={_DISCOVERY_WINDOW_POSITION[0]},{_DISCOVERY_WINDOW_POSITION[1]}",
        ]
        if position_right_half:
            viewport = {"width": evasion.HALF_VIEWPORT_WIDTH, "height": evasion.VIEWPORT_HEIGHT}
            args = [
                f"--window-position={evasion.WINDOW_POSITION_RIGHT_HALF[0]},{evasion.WINDOW_POSITION_RIGHT_HALF[1]}",
                f"--window-size={evasion.HALF_VIEWPORT_WIDTH},{evasion.VIEWPORT_HEIGHT}",
            ]
    return args if args else None, viewport


def _update_config_file(key: str, value: str) -> None:
    """Add or update KEY=value in project-root .config."""
    _root = Path(config_module.__file__).resolve().parent.parent
    config_path = _root / ".config"
    lines = []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.I)
    added = False
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            if pattern.match(line):
                lines.append(f"{key}={value}")
                added = True
            else:
                lines.append(line)
    if not added:
        lines.append(f"{key}={value}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def prompt_app_url() -> str:
    """Ask for APP_URL on first run. Updates .config and returns the URL."""
    url = input("  What is the APP_URL (base URL of the app under test)? (e.g. https://humanize.ai): ").strip()
    if not url:
        url = "https://localhost:3000"
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    _update_config_file("APP_URL", url)
    os.environ["APP_URL"] = url
    importlib.reload(config_module)
    importlib.reload(auth_module)
    print(f"  [*] Using APP_URL={url}")
    return url


def prompt_component_name() -> str:
    """Ask for AI component name on first run. Updates .config and returns the name."""
    name = input("  What is the name of the AI component you are testing? (e.g. chatbot, chat): ").strip()
    if not name:
        name = "chat"
    # Normalize to valid dir name (lowercase, no spaces)
    name = name.lower().replace(" ", "_") or "chat"
    _update_config_file("COMPONENT", name)
    os.environ["COMPONENT"] = name
    importlib.reload(config_module)
    importlib.reload(auth_module)
    print(f"  [*] Using COMPONENT={name}")
    return name


def _normalize_path(url: str) -> str:
    """Match 8_generate_schema: path or '/', rstrip '/', or '/'."""
    path = (urlparse(url).path or "/").rstrip("/") or "/"
    return path


def _path_matches_endpoint(request_url: str) -> bool:
    """Exact path match only (ignore e.g. /submissions/set-context). API URL comes from .env."""
    req_path = _normalize_path(request_url)
    end_path = _normalize_path(TARGET_API_URL)
    return req_path == end_path


async def capture_site_trace(*, headless: bool = False, num_messages: int = 3) -> Path | None:
    """
    Full network capture during user interaction.

    Opens browser, solves any WAF challenge, then records ALL api/* requests
    and responses while the user sends N messages.  Saves raw_trace.json for
    LLM analysis.

    Returns the path to raw_trace.json, or None on failure.
    """
    needs_setup = (
        not (config_module.BASE_URL or "").strip()
        or (config_module.COMPONENT or "default").strip() in ("", "default")
    )
    if needs_setup:
        print("\n  Capture — first-time setup\n")
    if not (config_module.BASE_URL or "").strip():
        prompt_app_url()
    if (config_module.COMPONENT or "default").strip() in ("", "default"):
        prompt_component_name()

    base_url = config_module.BASE_URL
    login_url = config_module.LOGIN_URL

    if not _check_server_reachable(login_url):
        raise ConnectionError(
            f"Cannot reach {login_url}. Start your app (e.g. dev server) and try again."
        )

    api_hostname = urlparse(base_url).netloc.lower()

    captured_requests: list[dict] = []
    captured_responses: list[dict] = []
    websocket_entries: list[dict] = []
    request_id_counter = [0]  # use list for closure
    request_to_id: dict = {}  # request obj -> id for pairing

    _BODY_LIMIT_REQUEST = 65536   # 64KB for payloads (zero/few/multishot with history)
    _BODY_LIMIT_RESPONSE = 131072  # 128KB for responses (SSE streams, extraction)
    _WS_PAYLOAD_LIMIT = 32768  # 32KB per WebSocket frame

    # Cross-origin only: minimal generic patterns (Supabase, Railway, etc.)
    _CROSS_ORIGIN_PATH_PATTERNS = (
        "/api/", "/api", "/graphql", "/trpc", "/rpc",
        "/v1/", "/v2/", "/rest/v1/", "/rest/",
        "/chat", "/completions", "/message", "/messages",
        "/converse", "/generate", "/inference", "/query", "/ask",
        "/model", "/stream", "/send", "/predict", "/text",
        "/sse", "/events", "/streaming",
    )

    def _path_is_static_or_noise(path: str) -> bool:
        """True if path should be excluded (static assets or noise keywords)."""
        path_lower = path.lower()
        if any(path_lower.endswith(ext) for ext in _STATIC_EXTENSIONS):
            return True
        if any(kw in path_lower for kw in _NOISE_PATH_KEYWORDS):
            return True
        return False

    def _path_looks_like_api_cross_origin(path: str) -> bool:
        """True when path matches generic API patterns (for cross-origin only)."""
        path_lower = path.lower()
        if _path_is_static_or_noise(path):
            return False
        return any(p in path_lower or path_lower.rstrip("/").endswith(p.rstrip("/")) for p in _CROSS_ORIGIN_PATH_PATTERNS)

    def _is_api_url(url: str) -> bool:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path or "/"

        # Exclude RSC (React Server Components) page fetches — not chat API
        if "_rsc=" in (parsed.query or ""):
            return False

        # Same-origin: capture ALL non-static, non-noise paths (broad capture)
        is_same_origin = (
            netloc == api_hostname
            or netloc == f"api.{api_hostname}"
            or (api_hostname.count(".") >= 1 and netloc.endswith("." + api_hostname))
        )
        if is_same_origin:
            if _path_is_static_or_noise(path):
                return False
            return len(path) > 1  # exclude bare "/" or empty

        # Cross-origin: use pattern list to avoid capturing random third-party APIs
        if _path_looks_like_api_cross_origin(path):
            if any(d in netloc for d in ("google-analytics", "googletagmanager", "facebook", "segment", "mixpanel", "hotjar", "sentry", "intercom")):
                return False
            return True
        return False

    def _should_capture(url: str) -> bool:
        if not _is_api_url(url):
            return False
        parsed = urlparse(url)
        path = parsed.path.lower()
        for kw in _NOISE_PATH_KEYWORDS:
            if kw in path:
                return False
        return True

    def on_request(request):
        if not _should_capture(request.url):
            return
        rid = request_id_counter[0]
        request_id_counter[0] += 1
        request_to_id[request] = rid
        parsed = urlparse(request.url)
        entry = {
            "direction": "request",
            "id": rid,
            "timestamp": time.time(),
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
        }
        if parsed.query:
            entry["query_params"] = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}
        try:
            pd = request.post_data
            if pd:
                entry["body"] = pd[:_BODY_LIMIT_REQUEST]
                # Add body_preview for JSON/multipart to help programmatic analysis
                req_ct = (dict(request.headers).get("content-type") or "").lower()
                if "application/json" in req_ct and pd.strip().startswith("{"):
                    try:
                        obj = json.loads(pd.split("\n")[0] if "\n" in pd[:500] else pd)
                        entry["body_preview"] = {"keys": list(obj.keys())[:25]} if isinstance(obj, dict) else {"array": True}
                    except (json.JSONDecodeError, TypeError):
                        pass
                elif "multipart/form-data" in req_ct:
                    field_names = re.findall(r'name="([^"]+)"', pd[:2048])
                    if field_names:
                        entry["body_preview"] = {"multipart_fields": field_names}
        except Exception:
            pass
        captured_requests.append(entry)

    async def on_response(response):
        if not _should_capture(response.url):
            return
        ct = (response.headers.get("content-type") or "").lower()
        entry = {
            "direction": "response",
            "timestamp": time.time(),
            "url": response.url,
            "status": response.status,
            "headers": dict(response.headers),
            "content_type": ct.split(";")[0].strip() if ct else None,
            "is_sse": "text/event-stream" in ct or "application/x-ndjson" in ct,
        }
        req_id = request_to_id.get(response.request)
        if req_id is not None:
            entry["paired_request_id"] = req_id
        try:
            body_bytes = await asyncio.wait_for(response.body(), timeout=15.0)
            ce = (response.headers.get("content-encoding") or "").lower()
            if "zstd" in ce and _ZSTD_AVAILABLE and body_bytes:
                try:
                    body_bytes = zstd.ZstdDecompressor().decompress(body_bytes)
                except Exception:
                    pass
            elif ("gzip" in ce or "deflate" in ce) and body_bytes:
                import gzip
                try:
                    body_bytes = gzip.decompress(body_bytes)
                except Exception:
                    pass
            text = body_bytes.decode("utf-8", errors="replace")[:_BODY_LIMIT_RESPONSE]
            entry["body"] = text
            # Add body_preview for JSON (top-level keys) to help programmatic analysis
            if text.strip() and ("application/json" in ct or text.strip().startswith("{")):
                try:
                    obj = json.loads(text.split("\n")[0] if "\n" in text[:500] else text)
                    if isinstance(obj, dict):
                        entry["body_preview"] = {"keys": list(obj.keys())[:20]}
                    elif isinstance(obj, list) and obj:
                        first = obj[0]
                        entry["body_preview"] = {"array": True, "first_item_keys": list(first.keys())[:15] if isinstance(first, dict) else None}
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            entry["body"] = ""
        captured_responses.append(entry)

    launch_args, viewport = _discovery_browser_config(headless=headless)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        ctx_kwargs: dict = {"viewport": viewport}
        if config_module.AUTH_STATE_FILE.exists():
            ctx_kwargs["storage_state"] = str(config_module.AUTH_STATE_FILE)
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        if await evasion.apply_stealth(page):
            print("[*] Stealth applied (WAF evasion).")

        print(f"[*] Navigating to {base_url} ...")
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            print(f"[!] Navigation failed: {exc}")
            await context.close()
            await browser.close()
            return None

        # WAF challenge wait (title polling)
        title = await page.title()
        if "just a moment" in title.lower() or "checking" in title.lower():
            print("[*] WAF challenge detected — waiting for resolution ...")
            for _ in range(120):
                await asyncio.sleep(0.5)
                title = await page.title()
                if "just a moment" not in title.lower() and "checking" not in title.lower():
                    break
            else:
                print("[!] WAF challenge did not resolve within 60 s.")
                await context.close()
                await browser.close()
                return None
            print(f"[*] WAF resolved. Page title: {title}")

        # Check if login is needed
        current_url = page.url
        if "/login" in current_url.lower() or "/signin" in current_url.lower():
            print("[*] Login page detected — performing login flow ...")
            await auth_module.login_and_save_state(context, page)
            await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)

        # Attach listeners after auth (HTTP + WebSocket for chat apps that use WS)
        _WS_NOISE_PATTERNS = (
            "webpack-hmr", "/_next/", "__nextjs", "hot-update", "sockjs",
            "live-reload", "hmr", "turbopack", "devIndicator",
        )

        def _is_ws_chat_or_inference(url: str) -> bool:
            """True if WebSocket looks like chat/inference, not dev tooling."""
            url_lower = url.lower()
            if any(p in url_lower for p in _WS_NOISE_PATTERNS):
                return False
            return any(p in url_lower for p in ("/chat", "/ws", "/stream", "/api/", "/completions", "/message", "/converse", "/inference"))

        def on_websocket(ws):
            if not _is_ws_chat_or_inference(ws.url):
                return

            def _append(direction: str, payload):
                p = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
                websocket_entries.append({
                    "direction": direction,
                    "timestamp": time.time(),
                    "url": ws.url,
                    "payload": p[:_WS_PAYLOAD_LIMIT],
                })
            ws.on("framereceived", lambda p: _append("ws_received", p))
            ws.on("framesent", lambda p: _append("ws_sent", p))

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", on_websocket)

        print(f"\n  ──────────────────────────────────────")
        print(f"  Send {num_messages} messages in the AI component,")
        print(f"  then press Enter here to finish capture.")
        print(f"  ──────────────────────────────────────\n")
        await asyncio.get_event_loop().run_in_executor(None, input, "  Press Enter when done ... ")

        # Collect final metadata
        final_url = page.url
        final_title = await page.title()
        cookies = await context.cookies()

        # Save auth state for session reuse
        post_urls = list({r["url"] for r in captured_requests if r.get("method") == "POST"})
        await auth_module.save_auth_from_context(context, page, post_urls=post_urls)

        await context.close()
        await browser.close()

    # Merge and sort by timestamp
    all_entries = captured_requests + captured_responses
    all_entries.sort(key=lambda e: e.get("timestamp", 0))

    # Build auth structure summary for discovery (headers + cookie names)
    auth_headers_seen: set[str] = set()
    for e in all_entries:
        for k in (e.get("headers") or {}).keys():
            kl = k.lower()
            if kl in ("cookie", "authorization", "x-csrf-token", "x-xsrf-token",
                      "x-csrftoken", "csrf-token", "x-api-key", "api-key",
                      "bearer", "x-auth-token", "trpc-accept", "x-trpc-source"):
                auth_headers_seen.add(k)
    cookie_names = [c.get("name") for c in cookies if c.get("name")]

    # Filter cookies to app-relevant only (domain matches api_hostname or api.* subdomain)
    def _cookie_relevant(c: dict) -> bool:
        domain = (c.get("domain") or "").lstrip(".")
        if not domain:
            return True
        return (
            domain == api_hostname
            or api_hostname.endswith("." + domain)
            or domain.endswith("." + api_hostname)
            or domain == f"api.{api_hostname}"
        )
    relevant_cookies = [c for c in cookies if _cookie_relevant(c)]

    # Merge WebSocket entries into chronological order with HTTP entries
    all_with_ws = all_entries + websocket_entries
    all_with_ws.sort(key=lambda e: e.get("timestamp", 0))

    # Annotate auth vs AI for LLM: indices into entries
    _AUTH_PATH_KEYWORDS = ("/login", "/signin", "/auth", "/csrf", "/session")
    _AI_PATH_KEYWORDS = ("/api/", "/chat", "/completions", "/message", "/converse", "/generate", "/inference", "/trpc", "/graphql")
    req_id_to_section: dict[int, str] = {}  # request id -> "auth" | "ai"
    auth_indices: list[int] = []
    ai_indices: list[int] = []
    for i, e in enumerate(all_with_ws):
        url = (e.get("url") or "").lower()
        direction = e.get("direction", "")
        if direction == "request" and e.get("method") == "POST":
            rid = e.get("id")
            if any(k in url for k in _AUTH_PATH_KEYWORDS):
                auth_indices.append(i)
                if rid is not None:
                    req_id_to_section[rid] = "auth"
            elif any(k in url for k in _AI_PATH_KEYWORDS):
                ai_indices.append(i)
                if rid is not None:
                    req_id_to_section[rid] = "ai"
        elif direction == "response" and e.get("paired_request_id") is not None:
            section = req_id_to_section.get(e["paired_request_id"])
            if section == "auth":
                auth_indices.append(i)
            elif section == "ai":
                ai_indices.append(i)

    trace = {
        "site": api_hostname,
        "base_url": base_url,
        "captured_at": datetime.now().isoformat(),
        "final_url": final_url,
        "final_title": final_title,
        "cookies": relevant_cookies,
        "auth_summary": {
            "headers_observed": sorted(auth_headers_seen),
            "cookie_names": cookie_names,
        },
        "sections": {
            "auth_entry_indices": sorted(set(auth_indices)),
            "ai_entry_indices": sorted(set(ai_indices)),
        },
        "num_entries": len(all_with_ws),
        "num_http": len(all_entries),
        "num_websocket": len(websocket_entries),
        "entries": all_with_ws,
        "capture_instructions": {
            "purpose": "Trace drives programmatic replay; ensure complete message flow is captured.",
            "recommendations": [
                "Send 3+ messages so zero/few/multi-shot payload formats are observed.",
                "Wait for each AI response to fully appear before sending the next message.",
                "If the app uses streaming/SSE, ensure you stay on the page until streaming completes.",
                "Capture from the chat page URL (not a landing page) so API calls are in scope.",
            ],
            "url_patterns_captured": list(_CROSS_ORIGIN_PATH_PATTERNS),
        },
    }

    out_dir = config_module.SITE_STATE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "raw_trace.json"
    out_path.write_text(json.dumps(trace, indent=2, default=str), encoding="utf-8")

    print(f"\n[+] Captured {len(all_with_ws)} entries -> {out_path}")
    print(f"    HTTP: {len(captured_requests)} requests, {len(captured_responses)} responses | WebSocket: {len(websocket_entries)} frames")
    if len(all_with_ws) == 0:
        print("[!] No HTTP or WebSocket traffic captured. Check that you sent messages in the chat.")
        print("    If the app uses a different domain, run from that domain or check DevTools → Network.")
    return out_path


def build_discovered_from_trace(trace_path: Path | None = None) -> Path | None:
    """
    Build a minimal discovered_endpoint.json from raw_trace.json so that legacy
    code paths (generate_payload_module, run_tests, etc.) still work.

    Scores all POST requests in the trace using the existing _score_request logic
    and picks the best candidates.
    """
    if trace_path is None:
        trace_path = config_module.SITE_STATE_DIR / "raw_trace.json"
    if not trace_path.exists():
        return None

    raw = json.loads(trace_path.read_text(encoding="utf-8"))
    entries = raw.get("entries", [])

    # Pair requests with their responses (match by URL + temporal order)
    requests_by_url: dict[str, list[dict]] = {}
    responses_by_url: dict[str, list[dict]] = {}
    for entry in entries:
        url = entry.get("url", "")
        if entry.get("direction") == "request" and entry.get("method") == "POST":
            requests_by_url.setdefault(url, []).append(entry)
        elif entry.get("direction") == "response":
            responses_by_url.setdefault(url, []).append(entry)

    scored: list[tuple[float, dict]] = []
    for url, reqs in requests_by_url.items():
        for req in reqs:
            resp_list = responses_by_url.get(url, [])
            resp_info = None
            if resp_list:
                resp = resp_list[0]
                resp_info = {
                    "status": resp.get("status", 0),
                    "is_sse": "text/event-stream" in resp.get("headers", {}).get("content-type", ""),
                    "has_long_text": len(resp.get("body", "")) > 200,
                }
            request_data = {
                "url": req.get("url", ""),
                "headers": req.get("headers", {}),
                "post_data": req.get("body", ""),
            }
            score = _score_request(request_data, resp_info)
            if score >= _MIN_SCORE_THRESHOLD:
                scored.append((score, {
                    "url": req.get("url", ""),
                    "method": req.get("method", "POST"),
                    "headers": req.get("headers", {}),
                    "post_data": req.get("body"),
                    "post_data_json": None,
                    "timestamp": req.get("timestamp", 0),
                }))

    if not scored:
        print("[!] No AI-like requests found in trace for discovered_endpoint.json")
        return None

    top = _select_top_candidates(scored, 3)

    requests_out = []
    for cap in top:
        headers_serializable = _serialize_headers(cap.get("headers", {}))
        raw_payload = cap.get("post_data")
        try:
            raw_payload_json = json.loads(raw_payload) if raw_payload else None
        except (json.JSONDecodeError, TypeError):
            raw_payload_json = None
        payload_fmt = payload_format_module.parse_payload_from_request(
            headers_serializable, raw_payload_json if raw_payload_json is not None else raw_payload,
        )
        requests_out.append({
            "url": cap["url"],
            "method": cap["method"],
            "headers": headers_serializable,
            "payload_format": payload_fmt,
            "payload_schema": raw_payload_json if raw_payload_json is not None else raw_payload,
        })

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

    chain = _detect_endpoint_chain(top)
    if chain:
        out["endpoint_chain"] = chain["chain"]
        out["follow_up_url"] = chain["follow_up_url"]

    prompt_field = _detect_prompt_field(top)
    if prompt_field:
        out["prompt_field"] = prompt_field

    discovered_file = config_module.DISCOVERED_ENDPOINT_FILE
    discovered_file.parent.mkdir(parents=True, exist_ok=True)
    discovered_file.write_text(json.dumps(out, indent=2))

    print(f"[+] Built discovered_endpoint.json from trace ({len(requests_out)} requests scored)")
    return discovered_file


async def discover_endpoint(*, headless: bool = False, position_right_half: bool = False) -> None:
    """
    Launch browser with saved auth, go to app. User makes one manual request
    to the auth-only LLM API in the app. We intercept that POST and save
    URL, method, headers, and payload schema.

    position_right_half: if True, place browser on right half of screen so UI stays visible.
    """
    if not TARGET_API_URL:
        print("[-] Set LOCAL_API_URL or TARGET_API_URL in .config (the API URL to intercept).")
        return
    if not AUTH_STATE_FILE.exists():
        print(f"[-] No saved session at {AUTH_STATE_FILE}. Run 'login' first.")
        return

    if not await auth_module.ensure_session_fresh():
        print("[-] Session invalid. Run discovery (login + capture) first.")
        return

    request_caught = asyncio.Event()
    captured = {}

    launch_args, viewport = _discovery_browser_config(
        headless=headless, position_right_half=position_right_half
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
            if _is_noise(request):
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
        print("[-] Set LOCAL_API_URL or TARGET_API_URL in .config (the API URL to intercept).")
        return
    if not AUTH_STATE_FILE.exists():
        print(f"[-] No saved session at {AUTH_STATE_FILE}. Run 'login' first.")
        return

    if not await auth_module.ensure_session_fresh():
        print("[-] Session invalid. Run discovery (login + capture) first.")
        return

    captured_list: list[dict] = []
    all_caught = asyncio.Event()
    expected_path = _normalize_path(TARGET_API_URL)

    def handle_request(request):
        if request.method != "POST":
            return
        if _is_noise(request):
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

    launch_args, viewport = _discovery_browser_config(headless=headless)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            storage_state=str(AUTH_STATE_FILE),
            viewport=viewport,
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

    First-run mode: when APP_URL or COMPONENT not set, prompts for them. When
    TARGET_API_URL is not set, captures any substantial POST, waits for Enter
    before counting, then updates .config with the discovered API URL.
    """
    needs_setup = (
        not (config_module.BASE_URL or "").strip()
        or (config_module.COMPONENT or "default").strip() in ("", "default")
    )
    if needs_setup:
        print()
        print("  Discovery — first-time setup")
        print()
    if not (config_module.BASE_URL or "").strip():
        prompt_app_url()
    if (config_module.COMPONENT or "default").strip() in ("", "default"):
        prompt_component_name()

    target_url = config_module.TARGET_API_URL
    base_url = config_module.BASE_URL
    login_url = config_module.LOGIN_URL
    discovered_file = config_module.DISCOVERED_ENDPOINT_FILE

    if not _check_server_reachable(login_url):
        raise ConnectionError(
            f"Cannot reach {login_url}. Start your app (e.g. dev server) and try again."
        )

    num_messages = 3
    all_caught = asyncio.Event()
    started = asyncio.Event()
    post_urls_seen: list[str] = []

    _pending: list[tuple[object, dict]] = []
    _scored: list[tuple[float, dict]] = []

    def handle_request(request):
        if request.method != "POST":
            return
        if _is_noise(request):
            return
        try:
            post_data = request.post_data
            if not post_data or len(post_data) < 20:
                return
        except Exception:
            return
        if not started.is_set():
            return
        if request.url and request.url not in post_urls_seen:
            post_urls_seen.append(request.url)
        if target_url:
            req_path = _normalize_path(request.url)
            expected_path = _normalize_path(target_url)
            if req_path != expected_path:
                return
        request_data = {
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
            "post_data": post_data,
            "timestamp": time.monotonic(),
        }
        try:
            request_data["post_data_json"] = request.post_data_json
        except Exception:
            request_data["post_data_json"] = None
        _pending.append((request, request_data))

    async def handle_response(response):
        req = response.request
        matched_data = None
        for i, (req_obj, req_data) in enumerate(_pending):
            if req_obj is req:
                _pending.pop(i)
                matched_data = req_data
                break
        if matched_data is None:
            return
        resp_info = await _probe_response(response)
        score = _score_request(matched_data, resp_info)
        matched_data["response_info"] = resp_info
        _scored.append((score, matched_data))
        if score >= _MIN_SCORE_THRESHOLD:
            n_high = len([s for s, _ in _scored if s >= _MIN_SCORE_THRESHOLD])
            print(f"\n[+] Candidate {n_high}/{num_messages} (score {score:.2f}): {matched_data['url']}")
            if n_high >= num_messages and not all_caught.is_set():
                all_caught.set()

    async def wait_for_enter():
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: input("\n  Press Enter when you are on the AI component page."))
        started.set()

    launch_args, viewport = _discovery_browser_config(headless=headless)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            viewport=viewport,
        )
        page = await context.new_page()
        if await evasion.apply_stealth(page):
            print("[*] Stealth applied (WAF evasion).")

        page.on("request", handle_request)
        page.on("response", handle_response)
        print(f"[*] Opening app at {base_url}...")
        await page.goto(base_url)
        await asyncio.sleep(evasion.human_delay(300, 700))
        await evasion.scroll_human_like(page, -80, steps=3)

        print("\n" + "=" * 60)
        if not target_url:
            print("[!] 1. Log in if needed")
            print("[!] 2. Go to the page where the AI component is visible")
            print("[!] 3. Press Enter in the terminal")
        else:
            print("[!] 1. Log in if needed")
            print("[!] 2. Go to the page where the AI component is visible")
        print("=" * 60)

        if not target_url:
            await wait_for_enter()
            print("[!] Now send 3 messages (one at a time) to the AI.")
        else:
            started.set()

        try:
            await asyncio.wait_for(all_caught.wait(), timeout=300)
        except asyncio.TimeoutError:
            print("\n[!] Timed out waiting for AI requests. Using best candidates so far.")

        await auth_module.save_auth_from_context(context, page, post_urls=post_urls_seen)

        top = _select_top_candidates(_scored, num_messages)
        if not top:
            print("[-] No AI requests detected. Try again and make sure to interact with the AI component.")
            await context.close()
            await browser.close()
            return

        requests_out = []
        for i, cap in enumerate(top):
            headers_serializable = _serialize_headers(cap.get("headers", {}))
            raw_payload = cap.get("post_data_json") if cap.get("post_data_json") is not None else cap.get("post_data")
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
        api_url = first["url"]
        if not target_url:
            _update_config_file("TARGET_API_URL", api_url)
            print(f"\n[+] Updated .config with TARGET_API_URL={api_url}")

        out = {
            "url": api_url,
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

        chain = _detect_endpoint_chain(top)
        if chain:
            out["endpoint_chain"] = chain["chain"]
            out["follow_up_url"] = chain["follow_up_url"]
            print(f"[*] Endpoint chain detected: {chain['initiate_url']} -> {chain['follow_up_url']}")

        prompt_field = _detect_prompt_field(top)
        if prompt_field:
            out["prompt_field"] = prompt_field
            print(f"[*] Prompt field detected: {prompt_field}")

        discovered_file.parent.mkdir(parents=True, exist_ok=True)
        discovered_file.write_text(json.dumps(out, indent=2))

        print(f"\n[+] Discovered endpoint saved to {discovered_file.name}")
        print("[*] Zero-shot (top-level) + strategies:", list(out.get("strategies", {}).keys()))

        await context.close()
        await browser.close()
