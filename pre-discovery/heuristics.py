"""
Heuristics for identifying LLM API requests from captured network traffic.
Scores POST requests by payload structure and URL patterns.
Scores GET requests by URL path patterns (conversations, models, sessions, etc.).
"""
import json
from urllib.parse import parse_qs, urlparse

# Payload field -> score (stronger signals = higher)
LLM_PAYLOAD_SIGNALS = {
    "messages": 4,
    "prompt": 3,
    "input": 3,
    "query": 3,
    "model": 2,
    "userMessageContent": 2,
    "content": 1,
    "stream": 1,
    "temperature": 1,
    "max_tokens": 1,
    "maxTokens": 1,
}

# Fields that suggest auth, not LLM chat (exclude)
AUTH_SIGNALS = {"grant_type", "refresh_token", "client_id", "client_secret"}

# Minimum score to consider a POST/WS candidate valid
MIN_SCORE = 4

# Minimum score to include a GET request (lower bar; GET is supplementary)
MIN_SCORE_GET = 1

# URL path substrings that indicate framework internals or non-LLM (exclude)
URL_EXCLUSIONS = (
    # Framework / build
    "__nextjs",
    "_next",
    "webpack",
    "hot-update",
    "/__",
    "/_next/",
    "original-stack-frames",
    "on-demand-entries",
    "hmr",
    # Ad / analytics / tracking
    "ad-proxy",
    "ad_proxy",
    "analytics",
    "/track",
    "pixel",
    "beacon",
    "telemetry",
)

# Image file extensions (exclude from discovery)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".svg", ".ico", ".webp", "woff2", "gif")


def _is_excluded_path(path: str) -> bool:
    """Return True if path should be excluded (framework, analytics, or image)."""
    path_lower = path.lower()
    if any(excl in path_lower for excl in URL_EXCLUSIONS):
        return True
    if any(path_lower.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return True
    return False


def _has_messages_structure(obj: dict) -> bool:
    """Check if obj has messages array with role/content structure."""
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or len(msgs) == 0:
        return False
    for m in msgs[:5]:
        if isinstance(m, dict) and "role" in m and "content" in m:
            return True
    return False


def _is_auth_request(data: dict) -> bool:
    """Exclude OAuth/token refresh requests."""
    return bool(AUTH_SIGNALS & set(k.lower() for k in data.keys()))


def score_get_request(url: str, app_origin: str) -> tuple[int, str]:
    """
    Score a GET request by LLM-API relevance (conversations, models, sessions, etc.).
    Returns (score, reason).
    """
    path = urlparse(url).path or "/"
    if _is_excluded_path(path):
        return 0, "excluded path"

    path_lower = path.lower()
    score = 0
    reasons: list[str] = []

    # Strong signals: conversation/session/model endpoints
    if any(x in path_lower for x in ["conversation", "conversations", "chat", "completion"]):
        score += 3
        reasons.append("path:conversation/chat")
    elif any(x in path_lower for x in ["session", "sessions", "message", "messages"]):
        score += 2
        reasons.append("path:session/message")
    elif any(x in path_lower for x in ["model", "models", "history", "thread"]):
        score += 2
        reasons.append("path:model/history")
    elif "api" in path_lower or "/v1/" in path_lower or "/v2/" in path_lower:
        score += 1
        reasons.append("path:api")

    try:
        req_netloc = urlparse(url).netloc
        app_netloc = urlparse(app_origin).netloc
        if req_netloc == app_netloc:
            score += 1
            reasons.append("same-origin")
        elif "api." in req_netloc or "api-" in req_netloc:
            score += 1
            reasons.append("api-subdomain")
    except Exception:
        pass

    reason = "+".join(reasons) if reasons else ""
    return score, reason


def extract_payload_format(body: str | None) -> dict | None:
    """
    Extract payload schema from JSON POST body for discovered_api.json.
    Returns dict with: fields, messages_structure (if messages present), field_types.
    """
    if not body or len(body) < 10:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if _is_auth_request(data):
        return None

    out: dict = {"fields": list(data.keys())}
    field_types: dict[str, str] = {}
    for k, v in data.items():
        if v is None:
            field_types[k] = "null"
        elif isinstance(v, bool):
            field_types[k] = "boolean"
        elif isinstance(v, int):
            field_types[k] = "integer"
        elif isinstance(v, float):
            field_types[k] = "number"
        elif isinstance(v, str):
            field_types[k] = "string"
        elif isinstance(v, list):
            if len(v) > 0 and isinstance(v[0], dict):
                field_types[k] = "array<object>"
            else:
                field_types[k] = "array"
        elif isinstance(v, dict):
            field_types[k] = "object"
        else:
            field_types[k] = "unknown"
    out["field_types"] = field_types

    msgs = data.get("messages")
    if isinstance(msgs, list) and len(msgs) > 0:
        first = msgs[0]
        if isinstance(first, dict):
            type_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "NoneType": "null"}
            out["messages_structure"] = {
                k: type_map.get(type(v).__name__, type(v).__name__) for k, v in first.items()
            }
    return out


def find_multishot_from_trace(trace_entries: list, target_url: str) -> list | None:
    """
    Find POST to target URL with the most messages (multi-turn).
    Returns messages array from the best request, or None if none with 2+ messages.
    """
    target_path = (urlparse(target_url).path or "/").rstrip("/") or "/"
    best: list | None = None
    best_len = 0
    for req in trace_entries:
        if req.get("method") != "POST":
            continue
        path = (urlparse(req.get("url", "")).path or "/").rstrip("/") or "/"
        if path != target_path:
            continue
        pd = req.get("post_data")
        if not pd or len(pd) < 10:
            continue
        try:
            data = json.loads(pd)
            msgs = data.get("messages")
            if isinstance(msgs, list) and len(msgs) >= 2 and len(msgs) > best_len:
                best = msgs
                best_len = len(msgs)
        except json.JSONDecodeError:
            continue
    return best


def build_multishot_example(messages_structure: dict | None) -> list[dict]:
    """
    Build synthetic multishot example [user, assistant, user] from messages_structure.
    Used when trace has no multi-turn requests.
    """
    keys = list(messages_structure.keys()) if messages_structure else ["role", "content"]
    role_key = "role" if "role" in keys else (keys[0] if keys else "role")
    content_key = "content" if "content" in keys else (keys[1] if len(keys) > 1 else keys[0])
    return [
        {role_key: "user", content_key: "First user message."},
        {role_key: "assistant", content_key: "Assistant response."},
        {role_key: "user", content_key: "Follow-up user message."},
    ]


def score_request_url_only(url: str, method: str, app_origin: str) -> tuple[int, str]:
    """
    Score by URL path only (when body is missing or unavailable).
    Used for cross-origin requests where post_data may not be exposed.
    Accepts POST or WebSocket (WS).
    """
    if method not in ("POST", "WS"):
        return 0, ""

    path = (urlparse(url).path or "/").lower()
    if _is_excluded_path(path):
        return 0, "excluded path"

    score = 0
    reasons: list[str] = []

    if any(x in path for x in ["chat", "completion", "completions"]):
        score += 3
        reasons.append("path:chat/completion")
    elif "session" in path or "message" in path:
        score += 2
        reasons.append("path:session/message")
    elif "api" in path or "/v1/" in path or "/v2/" in path:
        score += 1
        reasons.append("path:api")

    try:
        req_netloc = urlparse(url).netloc
        app_netloc = urlparse(app_origin).netloc
        if req_netloc == app_netloc:
            score += 1
            reasons.append("same-origin")
        elif "api." in req_netloc or "api-" in req_netloc:
            score += 1
            reasons.append("api-subdomain")
    except Exception:
        pass

    reason = "+".join(reasons) if reasons else ""
    return score, reason


def score_request(
    url: str,
    method: str,
    headers: dict,
    body: str | None,
    app_origin: str,
) -> tuple[int, str]:
    """
    Score a request by LLM-likelihood. Returns (score, reason).
    Falls back to URL-only scoring when body is missing or too small.
    """
    if method != "POST":
        return 0, ""

    path = (urlparse(url).path or "/").lower()
    if _is_excluded_path(path):
        return 0, "excluded path"

    # URL-only path when body unavailable (cross-origin, streaming, etc.)
    if not body or len(body) < 20:
        return score_request_url_only(url, method, app_origin)

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return score_request_url_only(url, method, app_origin)

    if not isinstance(data, dict):
        return score_request_url_only(url, method, app_origin)

    if _is_auth_request(data):
        return 0, "auth request"

    score = 0
    reasons: list[str] = []

    if _has_messages_structure(data):
        score += LLM_PAYLOAD_SIGNALS["messages"]
        reasons.append("messages")

    for key, points in LLM_PAYLOAD_SIGNALS.items():
        if key == "messages":
            continue
        if key in data:
            score += points
            reasons.append(key)

    if any(x in path for x in ["chat", "completion", "completions"]):
        score += 2
        reasons.append("path:chat/completion")
    if "api" in path or "/v1/" in path or "/v2/" in path:
        score += 1
        reasons.append("path:api")

    try:
        req_netloc = urlparse(url).netloc
        app_netloc = urlparse(app_origin).netloc
        if req_netloc == app_netloc:
            score += 1
            reasons.append("same-origin")
    except Exception:
        pass

    content_type = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            content_type = (v if isinstance(v, str) else (v[0] if v else "")).lower()
            break
    if "application/json" in content_type:
        score += 1
        reasons.append("json")

    reason = "+".join(reasons) if reasons else ""
    return score, reason
