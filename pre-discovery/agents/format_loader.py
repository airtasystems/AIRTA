"""
Load format/ data for agent analysis.
"""
import json
from pathlib import Path
from typing import Any

_AGENTS_DIR = Path(__file__).resolve().parent
_PRE_DISCOVERY = _AGENTS_DIR.parent
DEFAULT_FORMAT_DIR = _PRE_DISCOVERY / "format"


def load_format_data(format_dir: Path | None = None) -> dict[str, Any]:
    """
    Load discovered_api.json, full_trace.json, and playwright trace metadata.
    Returns dict with keys: discovered_api, full_trace, playwright_available.
    """
    format_dir = format_dir or DEFAULT_FORMAT_DIR
    out: dict[str, Any] = {
        "discovered_api": None,
        "full_trace": None,
        "playwright_available": False,
        "format_dir": str(format_dir),
    }

    discovered_path = format_dir / "discovered_api.json"
    if discovered_path.exists():
        try:
            out["discovered_api"] = json.loads(discovered_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    trace_path = format_dir / "full_trace.json"
    if trace_path.exists():
        try:
            out["full_trace"] = json.loads(trace_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    playwright_dir = format_dir / "playwright"
    if playwright_dir.is_dir():
        out["playwright_available"] = True
        trace_trace_path = playwright_dir / "trace.trace"
        out["trace_trace_path"] = trace_trace_path if trace_trace_path.exists() else None
        # Include trace.network summary (first N lines) if small enough
        network_path = playwright_dir / "trace.network"
        if network_path.exists():
            try:
                content = network_path.read_text(encoding="utf-8")
                # Truncate for context - full file can be huge
                out["trace_network_preview"] = content[:15000] + ("..." if len(content) > 15000 else "")
            except Exception:
                pass

    return out
