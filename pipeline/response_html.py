"""
Add ``response_html`` to each compliance result when writing ``pipeline_report.json``.
Converts raw ``response`` text (plain, Markdown-like, or mixed) into a small
semantic HTML fragment - no outer wrappers (no html/body/div shell).
"""

from __future__ import annotations

import html
import os
import re
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    _ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_ROOT / ".config")
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

_MAX_INPUT_CHARS = 120_000


def _format_inline(text: str) -> str:
    """Escape text and apply small inline markup for code, bold, and emphasis."""
    parts = re.split(r"(`[^`\n]+`)", text)
    out: list[str] = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1], quote=False)}</code>")
            continue

        escaped = html.escape(part, quote=False)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
        out.append(escaped)
    return "".join(out)


def _convert_one(text: str) -> str:
    """Convert plain/Markdown-like response text to a safe HTML fragment."""
    text = text if len(text) <= _MAX_INPUT_CHARS else text[:_MAX_INPUT_CHARS] + "\n\n[truncated]"

    output: list[str] = []
    paragraph: list[str] = []
    list_type: str | None = None
    code_block: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            output.append(f"<p>{' '.join(paragraph)}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            output.append(f"</{list_type}>")
            list_type = None

    def start_list(kind: str) -> None:
        nonlocal list_type
        flush_paragraph()
        if list_type != kind:
            close_list()
            output.append(f"<{kind}>")
            list_type = kind

    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if code_block is not None:
            if stripped.startswith("```"):
                output.append(f"<pre><code>{html.escape(chr(10).join(code_block), quote=False)}</code></pre>")
                code_block = None
            else:
                code_block.append(line)
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            close_list()
            code_block = []
            continue

        if not stripped:
            flush_paragraph()
            close_list()
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{_format_inline(heading.group(2).strip())}</h{level}>")
            continue

        unordered = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if unordered:
            start_list("ul")
            output.append(f"<li>{_format_inline(unordered.group(1).strip())}</li>")
            continue

        ordered = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if ordered:
            start_list("ol")
            output.append(f"<li>{_format_inline(ordered.group(1).strip())}</li>")
            continue

        quote = re.match(r"^>\s?(.+)$", stripped)
        if quote:
            flush_paragraph()
            close_list()
            output.append(f"<blockquote>{_format_inline(quote.group(1).strip())}</blockquote>")
            continue

        close_list()
        paragraph.append(_format_inline(stripped))

    if code_block is not None:
        output.append(f"<pre><code>{html.escape(chr(10).join(code_block), quote=False)}</code></pre>")
    flush_paragraph()
    close_list()
    return "\n".join(output)


def enrich_compliance_results_with_response_html(results: list[dict[str, Any]]) -> None:
    """Mutate each row in place: set ``response_html`` from ``response`` locally.

    Disabled when ``PIPELINE_RESPONSE_HTML`` is 0/false/no/off.
    On per-row failure, sets ``response_html`` to empty string and continues.
    """
    flag = os.getenv("PIPELINE_RESPONSE_HTML", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return

    n = len(results)
    for i, row in enumerate(results, 1):
        text = row.get("response")
        if not isinstance(text, str) or not text.strip():
            row["response_html"] = ""
            continue
        try:
            row["response_html"] = _convert_one(text)
            print(f"  [html {i}/{n}] {row.get('id', '')}", flush=True)
        except Exception as exc:
            print(f"  [!] response_html failed for {row.get('id', i)}: {exc}", flush=True)
            row["response_html"] = ""
