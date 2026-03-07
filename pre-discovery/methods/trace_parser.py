"""
Parse Playwright trace.trace (NDJSON) to extract UI hints for script generation.
Handles format variations and uses first matching waitForSelector+locator pair (same callId).
"""
import json
import re
from pathlib import Path
from typing import Any

# Input-like tags for chat input (excludes button, div, etc.)
INPUT_TAGS = frozenset({"textarea", "input"})


def extract_ui_hints(trace_path: Path) -> dict[str, Any] | None:
    """
    Parse trace.trace and extract chat input selectors, response container, and consent selectors.

    Edge cases:
    - No trace / parse fails: returns None (caller uses generic selectors).
    - Multiple waitForSelector: uses first one with matching "locator resolved" log (same callId).
    - Format variations: tries multiple parsing strategies per entry.

    Returns:
        {
            "chat_input_selectors": list[str],
            "response_container": str | None,
            "consent_selectors": list[str],
        }
        Returns None if trace missing or parse fails.
    """
    if not trace_path or not trace_path.exists():
        return None

    chat_input_selectors: list[str] = []
    response_container: str | None = None
    consent_selectors: list[str] = []

    try:
        content = trace_path.read_text(encoding="utf-8")
    except Exception:
        return None

    lines = content.strip().split("\n")
    if not lines:
        return None

    # callId -> selector for waitForSelector (before) entries; use first matching pair
    wait_for_selector_by_call: dict[str, str] = {}
    chat_input_finalized = False  # Use only first locator-resolved for input

    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        obj_type = obj.get("type")
        call_id = obj.get("callId", "")

        if obj_type == "log":
            msg = obj.get("message", "")
            # "locator resolved to visible <textarea id=\"chatbot-text-input\" ...>"
            match = re.search(
                r'locator resolved to visible\s+<(\w+)([^>]*)>',
                msg,
                re.IGNORECASE | re.DOTALL,
            )
            if match:
                tag = match.group(1).lower()
                attrs_str = match.group(2)
                # Only use input-like elements for chat input (excludes button, etc.)
                if tag not in INPUT_TAGS:
                    continue
                # Use first matching pair: log's callId should match a waitForSelector (same callId)
                if chat_input_finalized:
                    continue
                if call_id and call_id not in wait_for_selector_by_call:
                    continue  # No matching before for this callId, skip (format resilience)
                chat_input_finalized = True
                selectors = _attrs_to_selectors(tag, attrs_str)
                for s in selectors:
                    if s and s not in chat_input_selectors:
                        chat_input_selectors.append(s)

        elif obj_type == "before":
            method = obj.get("method", "")
            if method == "waitForSelector":
                params = obj.get("params") or {}
                sel = params.get("selector")
                if sel and call_id and call_id not in wait_for_selector_by_call:
                    wait_for_selector_by_call[call_id] = sel

        elif obj_type == "frame-snapshot":
            rc, consent = _parse_frame_snapshot(obj)
            if rc and not response_container:
                response_container = rc
            for c in consent:
                if c not in consent_selectors:
                    consent_selectors.append(c)

    # Fallback: use first waitForSelector param if no locator-resolved selectors
    if not chat_input_selectors and wait_for_selector_by_call:
        first_sel = next(iter(wait_for_selector_by_call.values()), None)
        if first_sel:
            chat_input_selectors.append(first_sel)

    # Deduplicate consent_selectors
    unique_consent = list(dict.fromkeys(consent_selectors))

    return {
        "chat_input_selectors": chat_input_selectors,
        "response_container": response_container,
        "consent_selectors": unique_consent,
    }


def _parse_frame_snapshot(obj: dict) -> tuple[str | None, list[str]]:
    """Extract response_container and consent_selectors from frame-snapshot. Returns (response_container, consent_list)."""
    response_container: str | None = None
    consent: list[str] = []

    snapshot = obj.get("snapshot")
    if not isinstance(snapshot, dict):
        return None, []

    html_data = snapshot.get("html")
    if html_data is None:
        return None, []

    try:
        html_str = json.dumps(html_data) if not isinstance(html_data, str) else html_data
    except (TypeError, ValueError):
        return None, []

    # Response container: try multiple patterns for format resilience
    patterns = [
        r'"id"\s*:\s*"([^"]*chatbot[^"]*)"',
        r'"id"\s*:\s*"([^"]*page-root[^"]*)"',
        r'"id"\s*:\s*"([^"]*chat[^"]*root[^"]*)"',
        r'id["\s:=]+([a-zA-Z0-9_-]*chatbot[a-zA-Z0-9_-]*)',
        r'id["\s:=]+([a-zA-Z0-9_-]*page-root[a-zA-Z0-9_-]*)',
    ]
    for pat in patterns:
        m = re.search(pat, html_str, re.I)
        if m:
            raw = m.group(1)
            if not raw.startswith("#"):
                response_container = f"#{raw}"
            else:
                response_container = raw
            break

    # Consent: cmpwrapper, cmpbox
    if "cmpwrapper" in html_str:
        consent.extend([
            '#cmpwrapper button:has-text("Accept")',
            '#cmpwrapper button:has-text("Allow")',
            '#cmpwrapper button:has-text("Agree")',
        ])
    if "cmpbox" in html_str:
        consent.append('#cmpbox button:has-text("Accept")')

    return response_container, consent


def _attrs_to_selectors(tag: str, attrs_str: str) -> list[str]:
    """Convert attribute string to Playwright selectors."""
    selectors: list[str] = []

    # id="chatbot-text-input" -> #chatbot-text-input
    id_match = re.search(r'\bid\s*=\s*["\']([^"\']+)["\']', attrs_str, re.I)
    if id_match:
        selectors.append(f"#{id_match.group(1)}")

    # placeholder="Ask a question" -> textarea[placeholder*="Ask" i]
    placeholder_match = re.search(r'\bplaceholder\s*=\s*["\']([^"\']+)["\']', attrs_str, re.I)
    if placeholder_match and tag in ("textarea", "input"):
        val = placeholder_match.group(1)
        # Use first meaningful word for partial match
        words = [w for w in val.split() if len(w) >= 3]
        first_word = words[0] if words else val[:10]
        selectors.append(f'{tag}[placeholder*="{first_word}" i]')

    return selectors
