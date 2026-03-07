#!/usr/bin/env python3
"""
Pre-discovery full pipeline: prompt for site, then run discovery → guide → script → run prompts.

Run from project root:
  python pre-discovery/main.py
  python -m pre-discovery.main
"""
import asyncio
import subprocess
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
except ImportError:
    pass

FORMAT_DIR = _root / "pre-discovery" / "format"
PROMPTS_PATH = _root / "pre-discovery" / "prompts" / "prompts.json"
SCRIPT_PATH = FORMAT_DIR / "ask_capital_script.py"


def _normalize_app_url(site: str) -> str:
    """Convert site name or partial URL to full app URL."""
    s = site.strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    if "://" in s and "/" not in s.split("://", 1)[1]:
        s = s.rstrip("/") + "/"
    return s


def _prompt_site() -> str | None:
    """Prompt user for site name or app URL. Returns None on cancel."""
    print()
    print("  Pre-discovery full pipeline")
    print()
    print("  Enter the app URL (e.g. https://www.example.com/chatbot)")
    print("  Or site name (e.g. example.com) — will use https://")
    print()
    try:
        site = input("  Site or URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not site:
        return None
    return _normalize_app_url(site)


async def _run_discovery(app_url: str) -> bool:
    """Run discovery: capture API, full trace, Playwright trace. Returns True on success."""
    from . import discover, output

    out_dir = FORMAT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print("\n[1/4] Discovering API and capturing trace...")
    captured = await discover.discover_api(
        app_url,
        headless=False,
        timeout_seconds=120.0,
        try_auto_trigger=True,
        auth_state_path=None,
        verbose=False,
        record_playwright_trace=True,
        output_dir=out_dir,
    )

    trace_entries = captured.get("trace", [])
    # Use actual chat page URL (e.g. /chat) so ask_capital_script opens the right page
    trace_app_url = captured.get("app_url") or app_url
    trace_file = output.write_trace(trace_entries, out_dir, app_url=trace_app_url)
    print(f"[+] Full trace: {trace_file} ({len(trace_entries)} requests)")

    post_candidates = captured.get("post", [])
    if not post_candidates:
        print("[-] No LLM API candidates found. Send a chat message in the browser and try again.")
        return False

    out_file = output.write_discovered(captured, output_dir=out_dir, update_config=False, config_path=None)
    if not out_file:
        print("[-] Failed to write discovered_api.json")
        return False
    print(f"[+] Discovered API: {out_file}")
    return True


def _run_guide() -> bool:
    """Run format guide team -> llm_api_guide.json. Returns True on success."""
    print("\n[2/4] Generating llm_api_guide.json...")
    from .agents.format_guide_team import run_format_guide_team

    guide = run_format_guide_team(format_dir=FORMAT_DIR, output_path=FORMAT_DIR / "llm_api_guide.json")
    return guide is not None


def _run_playwright_script_agent() -> bool:
    """Generate ask_capital_script.py. Returns True on success."""
    print("\n[3/4] Generating ask_capital_script.py...")
    from .agents.playwright_script_agent import generate_playwright_script

    result = generate_playwright_script(
        guide_path=FORMAT_DIR / "llm_api_guide.json",
        output_path=SCRIPT_PATH,
        format_dir=FORMAT_DIR,
    )
    return result is not None


def _run_script(no_headless: bool = True) -> bool:
    """Run ask_capital_script.py with prompts. Returns True on success."""
    print("\n[4/4] Running prompts...")
    if not SCRIPT_PATH.exists():
        print(f"[-] Script not found: {SCRIPT_PATH}")
        return False

    cmd = [sys.executable, str(SCRIPT_PATH)]
    if no_headless:
        cmd.append("--no-headless")
    if PROMPTS_PATH.exists():
        cmd.extend(["--prompts", str(PROMPTS_PATH)])

    result = subprocess.run(cmd, cwd=str(_root))
    return result.returncode == 0


def run_pipeline(app_url: str, *, run_script: bool = True, no_headless: bool = True) -> bool:
    """
    Run the full pre-discovery pipeline for the given app URL.
    Returns True if all steps succeed.
    """
    if not _run_discovery_sync(app_url):
        return False
    if not _run_guide():
        return False
    if not _run_playwright_script_agent():
        return False
    if run_script and not _run_script(no_headless=no_headless):
        return False
    return True


def _run_discovery_sync(app_url: str) -> bool:
    """Synchronous wrapper for discovery."""
    return asyncio.run(_run_discovery(app_url))


def main() -> int:
    app_url = _prompt_site()
    if not app_url:
        print("  Cancelled.")
        return 0

    print(f"\n[*] App URL: {app_url}")
    print(f"[*] Format dir: {FORMAT_DIR}")

    try:
        ok = run_pipeline(app_url, run_script=True, no_headless=True)
        if ok:
            print("\n[+] Full pipeline complete.")
            return 0
        return 1
    except KeyboardInterrupt:
        print("\n[!] Interrupted.")
        return 130
    except Exception as e:
        print(f"\n[!] Pipeline failed: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
