"""
Pre-discovery output paths: pre-discovery/<sitename>/<component>/format/

Enables per-site, per-component format files (discovered_api.json, full_trace.json,
llm_api_guide.json, ask_capital_script.py, etc.).
"""
import os
from pathlib import Path
from urllib.parse import urlparse

_PRE_DISCOVERY = Path(__file__).resolve().parent


def sitename_from_url(app_url: str) -> str:
    """Derive sitename from app URL. E.g. http://localhost:3000 -> localhost3000."""
    if not app_url or not app_url.strip():
        return "default"
    parsed = urlparse(app_url.strip())
    netloc = parsed.netloc or ""
    if isinstance(netloc, bytes):
        netloc = netloc.decode("utf-8", errors="replace")
    return netloc.replace(":", "") or "default"


def component_from_url(app_url: str) -> str:
    """Infer component from app URL path. E.g. /chat -> chat, /chatbot -> chatbot."""
    if not app_url or not app_url.strip():
        return "chat"
    env = (os.getenv("PRE_DISCOVERY_COMPONENT") or os.getenv("COMPONENT") or "").strip()
    if env:
        return env or "chat"
    parsed = urlparse(app_url.strip())
    path = (parsed.path or "/").strip("/").lower()
    if "chatbot" in path:
        return "chatbot"
    if path in ("chat", "ai-chat", "conversation"):
        return path
    if path.startswith("chat"):
        return "chat"
    return "chat"


def get_format_dir(app_url: str, *, component: str | None = None) -> Path:
    """
    Return format directory for the given app URL.
    pre-discovery/<sitename>/<component>/format/

    E.g. http://localhost:3000/chat -> pre-discovery/localhost3000/chat/format/
    """
    sitename = sitename_from_url(app_url)
    comp = component if component is not None else component_from_url(app_url)
    return _PRE_DISCOVERY / sitename / comp / "format"
