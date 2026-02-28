"""
Separate flow for running diagnostics: build payloads from the discovered endpoint
format (messages array, single text field, etc.) and send them. Does not use
payloads.json or pollute the generic send_payloads / payload_format used for tests.
"""
import json
from pathlib import Path
from typing import Any

from . import send_payloads as send_payloads_mod


def _parse_response_json(raw: str | None) -> dict[str, Any] | None:
    """
    Parse API response string as JSON. If the result has a 'content' field that
    is a JSON string, parse that too so logs store structured data for meta/tools/capabilities.
    Returns a dict with 'content' as parsed object when possible, or None if parse failed.
    """
    if not raw or not raw.strip():
        return None
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(outer, dict):
        return outer
    content = outer.get("content")
    if isinstance(content, str) and content.strip():
        s = content.strip()
        if (s.startswith("[") or s.startswith("{")) and len(s) > 1:
            try:
                inner = json.loads(s)
                outer = dict(outer)
                outer["content"] = inner
                return outer
            except json.JSONDecodeError:
                pass
    return outer


def _build_diagnostics_payloads(
    diagnostics: list[str],
    api_fields: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Build a list of payload dicts (overrides for build_body) so each diagnostic
    string is sent in the shape the API expects. Inspects api_fields from
    discovered_endpoint.json payload_format.
    """
    payloads: list[dict[str, Any]] = []
    for d in diagnostics:
        text = str(d).strip()
        if not text:
            continue
        # Adapt to discovered API shape
        if "messages" in api_fields:
            # Chat-style: API expects messages = array of {role, content}
            payloads.append({
                "messages": [{"role": "user", "content": text}],
                "title": text,
            })
        else:
            # Single primary field: use first key from schema as the content field
            keys = list(api_fields.keys())
            primary = keys[0] if keys else "text"
            payloads.append({primary: text, "title": text})
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

    discovered = json.loads(discovery_config.DISCOVERED_ENDPOINT_FILE.read_text(encoding="utf-8"))
    payload_format = discovered.get("payload_format") or {}
    api_fields = payload_format.get("fields", {})

    payloads = _build_diagnostics_payloads(diagnostics, api_fields)
    if not payloads:
        return None

    if verbose:
        print(f"[*] Sending {len(payloads)} diagnostics (format adapted from discovered endpoint)...")
    results = await send_payloads_mod.send_payloads_from_list(payloads, verbose=verbose, speed=speed)
    if not results:
        return None

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
