#!/usr/bin/env python3
"""
Pre-discovery CLI: discover LLM TARGET_API_URL from APP_URL using Playwright.
Run from project root: python -m pre-discovery [options]
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Load .config before any component-discovery imports
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
except ImportError:
    pass


def _find_auth_state(app_url: str) -> Path | None:
    """Locate auth_state.json for this app if it exists (from prior login)."""
    from urllib.parse import urlparse
    cd_dir = _root / "component-discovery"
    if not cd_dir.is_dir():
        return None
    netloc = urlparse(app_url).netloc.replace(":", "") or "default"
    site_config = cd_dir / netloc / "site_config" / "auth_state.json"
    if site_config.exists():
        return site_config
    # Legacy: check component dirs
    for comp_dir in cd_dir.iterdir():
        if comp_dir.is_dir():
            auth = comp_dir / "auth_state.json"
            if auth.exists():
                return auth
    return None


async def _run(app_url: str, args: argparse.Namespace) -> int:
    from . import discover, output
    from .paths import get_format_dir

    auth_state = _find_auth_state(app_url) if app_url else None
    if auth_state:
        print(f"[*] Using auth state: {auth_state}")

    out_dir = (Path(args.output_dir).resolve() if args.output_dir else get_format_dir(app_url)).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    captured = await discover.discover_api(
        app_url,
        headless=args.headless,
        timeout_seconds=args.timeout,
        try_auto_trigger=not args.no_auto_trigger,
        num_messages=getattr(args, "num_messages", 3),
        auth_state_path=auth_state,
        verbose=getattr(args, "verbose", False),
        output_dir=out_dir,
    )

    post_candidates = captured.get("post", [])
    get_endpoints = captured.get("get", [])
    trace_entries = captured.get("trace", [])

    trace_file = output.write_trace(trace_entries, out_dir, app_url=app_url)
    print(f"\n[+] Full trace written to {trace_file} ({len(trace_entries)} requests)")

    if not post_candidates:
        print("[-] No LLM API candidates found. Send a chat message in the browser and try again.")
        return 1

    print(f"\n[+] Found {len(post_candidates)} POST/WS candidate(s), {len(get_endpoints)} GET endpoint(s):")
    for i, c in enumerate(post_candidates[:5], 1):
        print(f"    {i}. {c['url']} (score={c['score']}, {c.get('reason', '')})")

    out_file = output.write_discovered(
        captured,
        output_dir=out_dir,
        update_config=args.update_config,
        config_path=_root / ".config" if args.update_config else None,
    )

    if out_file:
        print(f"\n[+] Discovered API written to {out_file}")
        print(f"[*] Format dir: {out_dir}")
        if args.update_config:
            print(f"[*] Updated TARGET_API_URL in .config")
        return 0

    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover LLM TARGET_API_URL from APP_URL using Playwright.",
    )
    parser.add_argument(
        "--app-url",
        default=os.getenv("APP_URL"),
        help="App URL to load (default: APP_URL from .config)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for discovered_api.json (default: pre-discovery/<sitename>/<component>/format/ from APP_URL)",
    )
    parser.add_argument(
        "--update-config",
        action="store_true",
        help="Update TARGET_API_URL in .config with discovered URL",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless",
    )
    parser.add_argument(
        "--no-auto-trigger",
        action="store_true",
        help="Skip auto-triggering chat; wait for manual message only",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for first LLM request (default: 120)",
    )
    parser.add_argument(
        "--num-messages",
        type=int,
        default=3,
        help="Number of chat messages to capture for verified single/multi-turn format (default: 3)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log every POST/WebSocket seen and its score (for debugging)",
    )

    args = parser.parse_args()
    app_url = (args.app_url or "").strip()

    if not app_url:
        print("[-] Set APP_URL in .config or pass --app-url")
        return 1

    return asyncio.run(_run(app_url, args))


if __name__ == "__main__":
    sys.exit(main())
