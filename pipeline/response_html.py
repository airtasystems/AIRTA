"""
Optional LLM step: add ``response_html`` to each adversarial result when writing
``pipeline_report.json``. Converts raw ``response`` text (plain, Markdown-like,
or mixed) into a small semantic HTML fragment — no outer wrappers (no html/body/div shell).
"""

from __future__ import annotations

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

_SYSTEM = """You convert chatbot/assistant response text into a concise HTML fragment.

Rules:
- Output ONLY an HTML fragment. No DOCTYPE, no html, head, body, and no wrapper div or section.
- Use semantic tags: p, h1, h2, h3, h4, strong, em, ul, ol, li, code, pre, br, blockquote as appropriate.
- Input may be plain text, Markdown-like (###, **, - bullets, numbered lists), or mixed. Normalize to clean, readable HTML.
- Do not add script, style, iframe, or event handlers.
- Output nothing before or after the fragment (no markdown code fences)."""


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    return s.strip()


def _text_from_lc_content(content: Any) -> str:
    """Normalize LangChain message content (str or list of text blocks) to a string."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        bits = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if t is not None and str(t).strip():
                    bits.append(str(t).strip())
            elif isinstance(block, str) and block.strip():
                bits.append(block.strip())
        if bits:
            return "\n".join(bits).strip()
    if content is not None:
        return str(content).strip()
    return ""


def _convert_one(text: str, llm: Any) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    truncated = text if len(text) <= _MAX_INPUT_CHARS else text[:_MAX_INPUT_CHARS] + "\n\n[…truncated]"
    msg = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(
            content="Convert this assistant response text to an HTML fragment only.\n\n---\n"
            + truncated
            + "\n---"
        ),
    ]
    out = llm.invoke(msg)
    raw = _text_from_lc_content(getattr(out, "content", out))
    return _strip_code_fences(raw)


def enrich_adversarial_results_with_response_html(results: list[dict[str, Any]]) -> None:
    """Mutate each row in place: set ``response_html`` from ``response`` via Gemini.

    Disabled when ``PIPELINE_RESPONSE_HTML`` is 0/false/no/off, or when ``GEMINI_API_KEY`` is unset.
    Model: ``PIPELINE_HTML_MODEL`` if set, else ``GEMINI_MODEL`` from ``.config``.
    On per-row failure, sets ``response_html`` to empty string and continues.
    """
    flag = os.getenv("PIPELINE_RESPONSE_HTML", "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return
    if not os.getenv("GEMINI_API_KEY", "").strip():
        print("[!] PIPELINE_RESPONSE_HTML: GEMINI_API_KEY not set; skipping response_html.")
        return
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as e:
        print(f"[!] PIPELINE_RESPONSE_HTML: langchain_google_genai not available ({e}); skipping.")
        return

    model = (
        os.getenv("PIPELINE_HTML_MODEL", "").strip()
        or os.getenv("GEMINI_MODEL", "").strip()
    )
    if not model:
        print("[!] PIPELINE_RESPONSE_HTML: GEMINI_MODEL not set; skipping response_html.")
        return
    api_key = os.environ["GEMINI_API_KEY"]
    llm = ChatGoogleGenerativeAI(
        model=model,
        api_key=api_key,
        temperature=0,
        max_output_tokens=8192,
    )
    n = len(results)
    for i, row in enumerate(results, 1):
        text = row.get("response")
        if not isinstance(text, str) or not text.strip():
            row["response_html"] = ""
            continue
        try:
            html = _convert_one(text, llm)
            # Minimal sanity: if model returned prose instead of HTML, wrap once
            if html and not re.search(r"<\s*[a-zA-Z]", html):
                html = f"<p>{html}</p>"
            row["response_html"] = html
            print(f"  [html {i}/{n}] {row.get('id', '')}", flush=True)
        except Exception as exc:
            print(f"  [!] response_html failed for {row.get('id', i)}: {exc}", flush=True)
            row["response_html"] = ""
