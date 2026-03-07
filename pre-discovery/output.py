"""
Write pre-discovery results. Re-exports from methods for backward compatibility.
"""
from .methods.trace import write_trace
from .methods.discovered_api import write_discovered

__all__ = ["write_trace", "write_discovered"]
