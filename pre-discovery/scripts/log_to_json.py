#!/usr/bin/env python3
"""
Convert pre-discovery diagnostics log to JSON format: [{prompt, response}, ...].

Handles two prompt types:
1. Text prompts: "=== Prompt N/M: <text>" — prompt on same line, response on next line(s)
2. JSON prompts: "=== Prompt N/M: {" — prompt is multi-line JSON, response follows

Ignores "Send", blank lines, and UI junk between responses.
"""
import argparse
import json
import re
import sys
from pathlib import Path


def _find_balanced_brace(s: str, start: int) -> int:
    """Return index of matching '}' for '{' at start, or -1."""
    depth = 0
    i = start
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _clean_response(text: str) -> str:
    """Strip Send, trailing UI junk, and normalize whitespace."""
    if not text:
        return ""
    # Remove trailing "Send" and everything after
    if "Send" in text:
        idx = text.find("Send")
        text = text[:idx]
    text = text.strip()
    # Remove leading markdown code fence if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def parse_diagnostics_log(content: str) -> list[dict[str, str]]:
    """
    Parse diagnostics log content into list of {prompt, response}.
    """
    results: list[dict[str, str]] = []
    pattern = re.compile(r"=== Prompt \d+/\d+:\s*", re.IGNORECASE)
    matches = list(pattern.finditer(content))

    for i, m in enumerate(matches):
        start = m.end()
        next_match = matches[i + 1] if i + 1 < len(matches) else None
        block_end = next_match.start() if next_match else len(content)
        block = content[start:block_end]

        prompt = ""
        response = ""

        if block.lstrip().startswith("{"):
            # JSON prompt: find balanced }
            json_start = block.find("{")
            if json_start >= 0:
                json_end = _find_balanced_brace(block, json_start)
                if json_end >= 0:
                    prompt = block[json_start : json_end + 1]
                    response_raw = block[json_end + 1 :].strip()
                    response = _clean_response(response_raw)
        else:
            # Text prompt: first line is prompt, rest is response
            first_newline = block.find("\n")
            if first_newline >= 0:
                prompt = block[:first_newline].strip()
                response_raw = block[first_newline + 1 :]
                response = _clean_response(response_raw)
            else:
                prompt = block.strip()

        if prompt:
            results.append({"prompt": prompt, "response": response})

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert pre-discovery diagnostics log to JSON (prompt/response pairs).",
    )
    parser.add_argument(
        "log_path",
        type=Path,
        help="Path to diagnostics log (e.g. pre-discovery/localhost3000/chatbot/logs/diagnostics_*.log)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (default: same as log with .json extension)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent (default: 2)",
    )
    args = parser.parse_args()

    log_path = args.log_path.resolve()
    if not log_path.exists():
        print(f"[-] Log not found: {log_path}", file=sys.stderr)
        return 1

    content = log_path.read_text(encoding="utf-8")
    results = parse_diagnostics_log(content)

    out_path = args.output or log_path.with_suffix(".json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=args.indent, ensure_ascii=False), encoding="utf-8")

    print(f"[+] Wrote {len(results)} prompt/response pairs to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
