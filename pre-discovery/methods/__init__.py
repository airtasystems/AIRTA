"""Pre-discovery methods: trace capture, discovered API output, and Playwright recording."""
from .trace import build_trace_entry, build_websocket_trace_entry, is_image_path, write_trace
from .discovered_api import write_discovered
from .playwright_record import start_playwright_trace, stop_playwright_trace

__all__ = [
    "build_trace_entry",
    "build_websocket_trace_entry",
    "is_image_path",
    "write_trace",
    "write_discovered",
    "start_playwright_trace",
    "stop_playwright_trace",
]
