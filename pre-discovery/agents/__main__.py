#!/usr/bin/env python3
"""
Pre-discovery agents: format guide and Playwright script generation.

Usage (from project root):
  python -m pre-discovery.agents guide              # Analyze format/ -> llm_api_guide.json
  python -m pre-discovery.agents playwright-script  # Generate Playwright script from guide
  python -m pre-discovery.agents run-diagnostics    # Run diagnostics prompts via chat UI, log to pre-discovery/<site>/<component>/logs/
"""
import argparse
import subprocess
import sys
from pathlib import Path

# Ensure project root on path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from .format_guide_team import run_format_guide_team
from .playwright_script_agent import generate_playwright_script

from ..paths import get_format_dir

DEFAULT_FORMAT_DIR = _root / "pre-discovery" / "format"


def _resolve_format_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "format_dir", None) is not None:
        return Path(args.format_dir).resolve()
    app_url = getattr(args, "app_url", None) or ""
    if app_url.strip():
        component = getattr(args, "component_name", None) or getattr(args, "component", None)
        comp = str(component).strip() if component else None
        return get_format_dir(app_url.strip(), component=comp or None).resolve()
    return DEFAULT_FORMAT_DIR.resolve()


def _component_from_format_dir(format_dir: Path) -> str | None:
    """Derive component from format dir path (pre-discovery/<sitename>/<component>/format/)."""
    # Legacy: pre-discovery/format/ -> parent.name == "pre-discovery", no component
    # New: pre-discovery/localhost3000/chatbot/format/ -> parent.name == "chatbot"
    if format_dir.parent.name == "pre-discovery":
        return None
    return format_dir.parent.name or None


def cmd_guide(args: argparse.Namespace) -> int:
    format_dir = _resolve_format_dir(args)
    output_path = getattr(args, "output", None)
    if output_path:
        output_path = output_path.resolve()
    else:
        output_path = format_dir / "llm_api_guide.json"
    component_name = (
        getattr(args, "component_name", None)
        or getattr(args, "component", None)
        or _component_from_format_dir(format_dir)
    )
    print(f"[*] Command: guide")
    print(f"[*] Format dir: {format_dir}")
    print(f"[*] Output: {output_path}")
    print(f"[*] Task: analyze discovered_api.json + full_trace.json -> definitive llm_api_guide.json")
    guide = run_format_guide_team(
        format_dir=format_dir,
        output_path=output_path,
        component_name=component_name,
    )
    return 0 if guide else 1


def cmd_playwright_script(args: argparse.Namespace) -> int:
    format_dir = _resolve_format_dir(args)
    guide_path = format_dir / "llm_api_guide.json"
    prompt = getattr(args, "prompt", "what is the capital of america")
    output_path = getattr(args, "output", None)
    if output_path:
        output_path = output_path.resolve()
    else:
        output_path = format_dir / "ask_capital_script.py"
    print(f"[*] Command: playwright-script")
    print(f"[*] Guide: {guide_path}")
    print(f"[*] Prompt: {prompt}")
    print(f"[*] Output: {output_path}")
    print(f"[*] Task: generate Playwright script to ask \"{prompt}\" via chat UI")
    result = generate_playwright_script(
        guide_path=guide_path,
        prompt=prompt,
        output_path=output_path,
        format_dir=format_dir,
    )
    return 0 if result else 1


def cmd_run_diagnostics(args: argparse.Namespace) -> int:
    """Run diagnostics prompts through the Playwright script; log to pre-discovery/<site>/<component>/logs/diagnostics_<timestamp>.log."""
    format_dir = _resolve_format_dir(args)
    script_path = format_dir / "ask_capital_script.py"
    if not script_path.exists():
        print(f"[-] Script not found: {script_path}")
        print("[*] Run 'playwright-script' first to generate it.")
        return 1
    headful = not getattr(args, "headless", False)
    cmd = [sys.executable, str(script_path), "--diagnostics"]
    if headful:
        cmd.append("--no-headless")
    diagnostics_file = getattr(args, "diagnostics_file", None)
    if diagnostics_file:
        cmd.extend(["--diagnostics-file", str(Path(diagnostics_file).resolve())])
    print(f"[*] Command: run-diagnostics")
    print(f"[*] Script: {script_path}")
    print(f"[*] Log: pre-discovery/<site>/<component>/logs/diagnostics_<timestamp>.log")
    result = subprocess.run(cmd, cwd=str(_root))
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-discovery agents: format guide and Playwright script generation.",
    )
    parser.add_argument(
        "--format-dir",
        type=Path,
        default=None,
        help="Format directory (default: pre-discovery/<sitename>/<component>/format/ from --app-url)",
    )
    parser.add_argument(
        "--app-url",
        type=str,
        default="",
        help="App URL to derive format dir when --format-dir not set (e.g. http://localhost:3000/chat)",
    )
    parser.add_argument(
        "--component-name",
        type=str,
        default=None,
        help="Component name for format dir when using --app-url (e.g. chatbot, chat)",
    )
    parser.add_argument("--output", type=Path, help="Output path (guide or script)")
    sub = parser.add_subparsers(dest="command", help="Command")

    guide_p = sub.add_parser("guide", help="Analyze format/ data -> llm_api_guide.json")
    guide_p.add_argument("--component", type=str, help="Component name (first entry in llm_api_guide.json)")
    guide_p.set_defaults(_run=cmd_guide)

    script_p = sub.add_parser(
        "playwright-script",
        help="Generate Playwright headless script from llm_api_guide.json",
    )
    script_p.add_argument(
        "--prompt",
        default="what is the capital of america",
        help="Question to ask in the chat (default: what is the capital of america)",
    )
    script_p.set_defaults(_run=cmd_playwright_script)

    diag_p = sub.add_parser(
        "run-diagnostics",
        help="Run diagnostics prompts via chat UI; log to pre-discovery/<site>/<component>/logs/diagnostics_<timestamp>.log",
    )
    diag_p.add_argument("--headless", action="store_true", help="Run browser headless (default: headful)")
    diag_p.add_argument("--diagnostics-file", type=Path, help="Path to diagnostics JSON (default: diagnostics/diagnostics.json)")
    diag_p.set_defaults(_run=cmd_run_diagnostics)

    args = parser.parse_args()
    if not args.command:
        args.command = "guide"
        args._run = cmd_guide

    return args._run(args)


if __name__ == "__main__":
    sys.exit(main())
