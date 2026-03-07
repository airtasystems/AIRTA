"""Pre-discovery methods: trace capture and discovered API output."""
from .trace import build_trace_entry, build_websocket_trace_entry, is_image_path, write_trace
from .discovered_api import write_discovered

__all__ = [
    "build_trace_entry",
    "build_websocket_trace_entry",
    "is_image_path",
    "write_trace",
    "write_discovered",
]
