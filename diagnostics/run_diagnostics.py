"""
Separate flow for running diagnostics: build payloads from the discovered endpoint
format (messages array, single text field, etc.) and send them. Does not use
payloads.json or pollute the generic send_payloads / payload_format used for tests.

When site_profile.json exists, diagnostics simply produce {text, title} pairs and
delegate all formatting to the profile-based send engine.
"""
import json
import uuid
from pathlib import Path
from typing import Any

from pipeline import send_payloads as send_payloads_mod
from pipeline import profile_send


def _parse_response_json(raw: str | None) -> dict[str, Any] | list[Any] | None:
    """
    Parse API response string as JSON. If the result has a 'content' field that
    is a JSON string, parse that too so logs store structured data for meta/tools/capabilities.
    Handles markdown-wrapped JSON (```json ... ```). Returns dict, list, or None.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # Strip markdown code block
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str) and content.strip():
            s = content.strip()
            if (s.startswith("[") or s.startswith("{")) and len(s) > 1:
                try:
                    inner = json.loads(s)
                    parsed = dict(parsed)
                    parsed["content"] = inner
                except json.JSONDecodeError:
                    pass
    return parsed


def _find_text_field_path(fields: dict[str, Any]) -> str | None:
    """Detect which field carries the user's text in the payload format.
    Returns the top-level key name, or None if not found."""
    # messageInput: [{type: "text", text: "..."}]  (Mistral-style)
    if "messageInput" in fields:
        return "messageInput"
    # messages: [{role: "user", content: "..."}]  (OpenAI-style)
    if "messages" in fields:
        return "messages"
    # prompt / text / query / input — common single-field APIs
    for candidate in ("prompt", "text", "query", "input", "content", "message"):
        if candidate in fields:
            return candidate
    return None


def _build_diagnostics_payloads(
    diagnostics: list[str],
    api_fields: dict[str, Any],
    discovered: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Build a list of payload dicts (overrides for build_body) so each diagnostic
    string is sent in the shape the API expects. Inspects api_fields from
    discovered_endpoint.json payload_format.
    """
    # Prefer a strategy format that has messageInput/messages (the "append" shape)
    effective_fields = dict(api_fields)
    if discovered:
        for strat_key in ("few_shot", "multi_shot"):
            strat = (discovered.get("strategies") or {}).get(strat_key, {})
            strat_fields = (strat.get("payload_format") or {}).get("fields", {})
            if strat_fields and _find_text_field_path(strat_fields):
                effective_fields = dict(strat_fields)
                break

    text_field = _find_text_field_path(effective_fields)

    payloads: list[dict[str, Any]] = []
    for d in diagnostics:
        text = str(d).strip()
        if not text:
            continue

        if text_field == "messageInput":
            # Mistral-style two-step: first "start" to create chat, then "append" to send message
            chat_id = str(uuid.uuid4())
            # Step 1: create the chat (uses base "zero_shot" / start format)
            payloads.append({
                "chatId": chat_id,
                "title": f"[init] {text[:60]}",
                "_is_init": True,
            })
            # Step 2: append the actual message
            payloads.append({
                "messageInput": json.dumps([{"type": "text", "text": text}]),
                "messageId": str(uuid.uuid4()),
                "chatId": chat_id,
                "title": text,
                "strategy": "few_shot",
            })
        elif text_field == "messages":
            payloads.append({
                "messages": [{"role": "user", "content": text}],
                "title": text,
            })
        else:
            key = text_field or "text"
            payloads.append({key: text, "title": text})
    return payloads


async def run_diagnostics_send(
    discovery_config: Any,
    diagnostics_path: Path,
    log_dir: Path | None = None,
    verbose: bool = True,
    speed: int = 1,
) -> Path | None:
    """
    Load diagnostics from diagnostics_path, adapt to the discovered endpoint format,
    send each via send_payloads_from_list, and write a timestamp_log.json. Returns
    the log path or None if diagnostics could not be run.
    """
    if not diagnostics_path.exists():
        if verbose:
            print(f"[-] Diagnostics file not found: {diagnostics_path}")
        return None
    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        if verbose:
            print(f"[-] No discovered endpoint at {discovery_config.DISCOVERED_ENDPOINT_FILE}. Run discovery first.")
        return None
    if not discovery_config.AUTH_STATE_FILE.exists():
        if verbose:
            print(f"[-] No auth state. Run login first.")
        return None

    try:
        diag_data = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"[-] Failed to load diagnostics: {e}")
        return None

    diagnostics = diag_data.get("diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        if verbose:
            print("[*] No 'diagnostics' array in file; skipping diagnostics send.")
        return None

    # Profile-first path: when site_profile.json exists, just produce {text, title} pairs
    site_profile = profile_send.load_site_profile()
    if site_profile is not None:
        payloads = [{"text": str(d).strip(), "title": str(d).strip()} for d in diagnostics if str(d).strip()]
        if not payloads:
            return None
        if verbose:
            print(f"[*] Sending {len(payloads)} diagnostics via site profile (adaptive engine)...")
        results = await send_payloads_mod.send_payloads_from_list(payloads, verbose=verbose, speed=speed)
        if not results:
            return None
    else:
        discovered = json.loads(discovery_config.DISCOVERED_ENDPOINT_FILE.read_text(encoding="utf-8"))
        payload_format = discovered.get("payload_format") or {}
        api_fields = payload_format.get("fields", {})

        payloads = _build_diagnostics_payloads(diagnostics, api_fields, discovered=discovered)
        if not payloads:
            return None

        actual_count = sum(1 for p in payloads if not p.get("_is_init"))
        if verbose:
            print(f"[*] Sending {actual_count} diagnostics (format adapted from discovered endpoint)...")
        results = await send_payloads_mod.send_payloads_from_list(payloads, verbose=verbose, speed=speed)
        if not results:
            return None

        # Filter out init (chat-creation) results; keep only actual message responses
        results = [r for r in results if not r.get("title", "").startswith("[init] ")]

    # Parse JSON responses so logs have structured content for meta/tools/capabilities
    for r in results:
        raw = r.get("response")
        parsed = _parse_response_json(raw)
        if parsed is not None:
            r["response_parsed"] = parsed

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "diagnostics_log.json"
    else:
        log_path = discovery_config.SITE_STATE_DIR / f"{timestamp}_log.json"
    log_path.write_text(
        json.dumps({"timestamp": timestamp, "results": results}, indent=2),
        encoding="utf-8",
    )
    if verbose:
        print(f"[+] Diagnostics log: {log_path}")
    return log_path
