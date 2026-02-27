"""
CLI for the LLM endpoint discovery app.

  python -m component_discovery run   # Interactive loop: login, discover, generate, refresh, test payloads
  python -m component_discovery login
  python -m component_discovery discover
  python -m component_discovery discover-multi   # Capture 3 messages to see full-history vs incremental
  python -m component_discovery generate-payload-module
  python -m component_discovery refresh
  python -m component_discovery send-payloads   # Or use "Test payloads" from the run menu
"""
import argparse
import asyncio
import threading
import time

from . import auth, discover, send_payloads
from .config import AUTH_STATE_FILE, SITE_STATE_DIR


def _run(coro):
    return asyncio.run(coro)


def cmd_refresh(args):
    _run(auth.refresh_session())


def _send_payloads_impl():
    """Run site send_payloads.py if present, else default."""
    site_send = SITE_STATE_DIR / "send_payloads.py"
    if site_send.exists():
        import importlib.util
        import sys
        # Generated code uses underscore package name (e.g. component_discovery); -m loads with hyphen.
        pkg = getattr(send_payloads, "__package__", "") or getattr(discover, "__package__", "")
        pkg_underscore = pkg.replace("-", "_")
        if pkg_underscore != pkg and pkg_underscore not in sys.modules and pkg in sys.modules:
            sys.modules[pkg_underscore] = sys.modules[pkg]
        spec = importlib.util.spec_from_file_location("site_send_payloads", site_send)
        mod = importlib.util.module_from_spec(spec)
        if str(SITE_STATE_DIR) not in sys.path:
            sys.path.insert(0, str(SITE_STATE_DIR))
        spec.loader.exec_module(mod)
        return _run(mod.send_payloads())
    return _run(send_payloads.send_payloads())


def cmd_send_payloads(args):
    _send_payloads_impl()


def cmd_analyze_log(args):
    import sys
    from pathlib import Path
    # diagnostics is a sibling package; ensure project root is on path
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    from diagnostics import analyze_log
    analyze_log.analyze_log_and_write_discovery(SITE_STATE_DIR)


def cmd_refresh_interval(args):
    _run(send_payloads.run_refresh_every(minutes=args.minutes))


async def _run_login_then_discover(headless: bool):
    await auth.capture_login_and_csrf(headless=headless)
    await discover.discover_endpoint(headless=headless)
    from . import generate_site_payload
    generate_site_payload.generate_payload_module()


def cmd_login(args):
    _run(auth.capture_login_and_csrf(headless=args.headless))


def cmd_discover(args):
    _run(discover.discover_endpoint(headless=args.headless))


def cmd_discover_multi(args):
    _run(discover.discover_endpoint_multi(
        num_messages=args.num_messages,
        headless=args.headless,
    ))


def cmd_generate_payload_module(args):
    from . import generate_site_payload
    generate_site_payload.generate_payload_module()


REFRESH_INTERVAL_MINUTES = 4


def _refresh_loop():
    """Background thread: refresh session every REFRESH_INTERVAL_MINUTES. Output prefixed [auto-refresh]."""
    import sys
    _stdout = sys.stdout

    class _PrefixStdout:
        def __init__(self, prefix):
            self._prefix = prefix
            self._inner = _stdout
        def write(self, s):
            if s:
                self._inner.write((self._prefix + s).replace("\n", "\n" + self._prefix))
        def flush(self):
            self._inner.flush()

    while True:
        time.sleep(REFRESH_INTERVAL_MINUTES * 60)
        if not AUTH_STATE_FILE.exists():
            continue
        try:
            sys.stdout = _PrefixStdout("[auto-refresh] ")
            try:
                print()  # newline so output doesn't run into the prompt line
                asyncio.run(auth.refresh_session())
            finally:
                sys.stdout = _stdout
        except Exception as e:
            print(f"[!] Background refresh failed: {e}")


def _run_loop_menu():
    """Interactive menu loop; refresh runs in background every 4 min."""
    print()
    print("  Component Discovery — interactive (auth refresh every 4 min)")
    print()
    # Start background refresh
    refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
    refresh_thread.start()

    while True:
        print()
        print("  1) Login          — capture session + CSRF (browser)")
        print("  2) Discover       — intercept one API request (browser)")
        print("  3) Discover multi — intercept 3 messages in one conversation (browser)")
        print("  4) Generate       — Gemini: payload_format + send_payloads; payloads.json from diagnostics")
        print("  5) Refresh now    — refresh session + CSRF once")
        print("  6) Test payloads — send payloads.json to discovered endpoint (same as send-payloads)")
        print("  7) Analyze log    — Gemini: analyze latest *_log.json → discovery.json")
        print("  8) Exit")
        print()
        try:
            choice = input("  Choice [1-8]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice:
            continue
        if choice == "8" or choice.lower() == "q":
            break
        if choice == "1":
            _run(auth.capture_login_and_csrf(headless=False))
        elif choice == "2":
            _run(discover.discover_endpoint(headless=False))
        elif choice == "3":
            _run(discover.discover_endpoint_multi(num_messages=3, headless=False))
        elif choice == "4":
            from . import generate_site_payload
            generate_site_payload.generate_payload_module()
        elif choice == "5":
            _run(auth.refresh_session())
        elif choice == "6":
            _send_payloads_impl()
        elif choice == "7":
            import sys
            from pathlib import Path
            _root = Path(__file__).resolve().parent.parent
            if str(_root) not in sys.path:
                sys.path.insert(0, str(_root))
            from diagnostics import analyze_log
            analyze_log.analyze_log_and_write_discovery(SITE_STATE_DIR)
        else:
            print("  Invalid choice.")
    print("  Bye.")


def cmd_run(args):
    _run_loop_menu()


def main():
    parser = argparse.ArgumentParser(
        description="Dynamic discovery of LLM endpoints in an auth-only app. "
                    "Use 'run' to do login then discover in one go."
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (not recommended for login or discover).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser(
        "run",
        help="Interactive loop: menu for login/discover/generate/refresh; auth refresh every 4 min in background.",
    )
    run_p.set_defaults(func=cmd_run)

    login_p = sub.add_parser("login", help="Open browser; you log in (and MFA). Session + CSRF are saved.")
    login_p.set_defaults(func=cmd_login)

    discover_p = sub.add_parser(
        "discover",
        help="Open app with saved session; you make one LLM request in the app. Endpoint is captured.",
    )
    discover_p.set_defaults(func=cmd_discover)

    discover_multi_p = sub.add_parser(
        "discover-multi",
        help="Open app; you send 3 messages in the UI. Each request is captured to see full-history vs incremental.",
    )
    discover_multi_p.set_defaults(func=cmd_discover_multi)
    discover_multi_p.add_argument(
        "--num-messages",
        type=int,
        default=3,
        metavar="N",
        help="Number of messages to capture (default: 3).",
    )

    gen_p = sub.add_parser(
        "generate-payload-module",
        help="Use Gemini to analyze payload_schema, write site-specific payload_format.py and send_payloads.py.",
    )
    gen_p.set_defaults(func=cmd_generate_payload_module)

    refresh_p = sub.add_parser(
        "refresh",
        help="Refresh auth tokens (site requires refresh every 14 minutes).",
    )
    refresh_p.set_defaults(func=cmd_refresh)

    send_p = sub.add_parser(
        "send-payloads",
        help="POST each payload from payloads.json to the discovered endpoint (uses payload_format).",
    )
    send_p.set_defaults(func=cmd_send_payloads)

    analyze_p = sub.add_parser(
        "analyze-log",
        help="Analyze most recent component *_log.json with Gemini; write discovery.json (meta, capabilities, tools).",
    )
    analyze_p.set_defaults(func=cmd_analyze_log)

    interval_p = sub.add_parser(
        "refresh-interval",
        help="Run refresh (session + CSRF) every N minutes. Default 4. Ctrl+C to stop.",
    )
    interval_p.set_defaults(func=cmd_refresh_interval)
    interval_p.add_argument(
        "--minutes",
        type=float,
        default=4,
        metavar="N",
        help="Interval in minutes (default: 4).",
    )

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
