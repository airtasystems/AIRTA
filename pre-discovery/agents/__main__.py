#!/usr/bin/env python3
"""
Pre-discovery agents: format guide and Playwright script generation.

Usage (from project root):
  python -m pre-discovery.agents guide              # Analyze format/ -> llm_api_guide.json
  python -m pre-discovery.agents playwright-script # Generate Playwright script from guide
"""
import argparse
import sys
from pathlib import Path

# Ensure project root on path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from .format_guide_team import run_format_guide_team
from .playwright_script_agent import generate_playwright_script

DEFAULT_FORMAT_DIR = _root / "pre-discovery" / "format"


def cmd_guide(args: argparse.Namespace) -> int:
    format_dir = getattr(args, "format_dir", DEFAULT_FORMAT_DIR).resolve()
    output_path = getattr(args, "output", None)
    if output_path:
        output_path = output_path.resolve()
    else:
        output_path = format_dir / "llm_api_guide.json"
    print(f"[*] Command: guide")
    print(f"[*] Format dir: {format_dir}")
    print(f"[*] Output: {output_path}")
    print(f"[*] Task: analyze discovered_api.json + full_trace.json -> definitive llm_api_guide.json")
    guide = run_format_guide_team(format_dir=format_dir, output_path=output_path)
    return 0 if guide else 1


def cmd_playwright_script(args: argparse.Namespace) -> int:
    format_dir = getattr(args, "format_dir", DEFAULT_FORMAT_DIR).resolve()
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-discovery agents: format guide and Playwright script generation.",
    )
    parser.add_argument(
        "--format-dir",
        type=Path,
        default=DEFAULT_FORMAT_DIR,
        help="Format directory (default: pre-discovery/format/)",
    )
    parser.add_argument("--output", type=Path, help="Output path (guide or script)")
    sub = parser.add_subparsers(dest="command", help="Command")

    guide_p = sub.add_parser("guide", help="Analyze format/ data -> llm_api_guide.json")
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

    args = parser.parse_args()
    if not args.command:
        args.command = "guide"
        args._run = cmd_guide

    return args._run(args)


if __name__ == "__main__":
    sys.exit(main())
