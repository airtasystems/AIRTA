"""Subprocess wrapper for discovery — gives run_training its own event loop."""
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root / "browser-bot"))

from browser_bot.record_submission import run_training  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python discover_worker.py <site> <component>", file=sys.stderr)
        sys.exit(1)
    ok = run_training(sys.argv[1], sys.argv[2])
    sys.exit(0 if ok else 1)
