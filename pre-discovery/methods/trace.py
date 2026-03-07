"""
Full stack trace capture: record all requests (except images) with unabridged headers.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..heuristics import IMAGE_EXTENSIONS


def is_image_path(path: str) -> bool:
    """Exclude image requests from trace (complete unabridged except IMAGE_EXTENSIONS)."""
    return any((path or "").lower().endswith(ext) for ext in IMAGE_EXTENSIONS)


def build_trace_entry(
    url: str,
    path: str,
    method: str,
    headers: dict,
    session_start: float,
    *,
    post_data: str | None = None,
) -> dict:
    """Build a trace entry for an HTTP request."""
    parsed = urlparse(url)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((time.monotonic() - session_start) * 1000),
        "url": url,
        "path": path or "/",
        "method": method,
        "headers": headers,
        "query_params": dict(parse_qs(parsed.query)) if parsed.query else {},
    }
    if post_data:
        entry["post_data"] = post_data
    return entry


def build_websocket_trace_entry(ws_url: str, path: str, session_start: float) -> dict:
    """Build a trace entry for a WebSocket connection."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((time.monotonic() - session_start) * 1000),
        "url": ws_url,
        "path": path or "/",
        "method": "WebSocket",
        "headers": {},
        "query_params": {},
    }


def write_trace(trace_entries: list[dict], output_dir: Path, app_url: str = "") -> Path:
    """
    Write full request trace to full_trace.json.
    trace_entries: list of {timestamp, elapsed_ms, url, path, method, headers, query_params?, post_data?, response_status?}.
    Returns path to full_trace.json.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / "full_trace.json"
    out = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "app_url": app_url,
        "request_count": len(trace_entries),
        "requests": trace_entries,
    }
    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out_file
