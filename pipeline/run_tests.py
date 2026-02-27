"""
Run compliance tests from a test JSON file against the discovered endpoint.
Loads test file, flattens mandates + calibration_prompts, formats payloads using the
site's payload_format (e.g. messages for chat), sends via component_discovery, and writes
both a per-component tests.json and a compliance log.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _flatten_prompts(test_data: dict) -> list[dict]:
    """Flatten mandates[].prompts[] and calibration_prompts[] into list of {id, mandate, description, prompt, expected_behavior?}."""
    out: list[dict] = []
    for m in test_data.get("mandates", []):
        mandate = m.get("mandate", "")
        for p in m.get("prompts", []):
            out.append({
                "id": p.get("id", ""),
                "mandate": mandate,
                "description": p.get("description", ""),
                "prompt": p.get("prompt", ""),
                "expected_behavior": None,
            })
    for p in test_data.get("calibration_prompts", []):
        out.append({
            "id": p.get("id", ""),
            "mandate": "calibration",
            "description": p.get("description", ""),
            "prompt": p.get("prompt", ""),
            "expected_behavior": p.get("expected_behavior"),
        })
    return out


async def run_compliance_tests(
    test_file_path: Path,
    *,
    log_dir: Path | None = None,
    verbose: bool = True,
) -> Path | None:
    """
    Load test file, format prompts using the site's payload_format (e.g. messages for chat),
    send each prompt to the discovered endpoint, write compliance log, and emit a
    component-local tests.json alongside payloads.json.
    Returns path to compliance log, or None if no results (e.g. discovery not done).
    """
    from component_discovery import config as discovery_config
    from component_discovery import send_payloads as discovery_send_payloads

    if not test_file_path.exists():
        if verbose:
            print(f"[-] Test file not found: {test_file_path}")
        return None

    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        if verbose:
            print(f"[-] No discovered endpoint at {discovery_config.DISCOVERED_ENDPOINT_FILE}. Run discovery first.")
        return None

    # Inspect payload_format to decide how to map prompts to payload fields
    discovered = json.loads(discovery_config.DISCOVERED_ENDPOINT_FILE.read_text())
    payload_format = discovered.get("payload_format") or {}
    fields = payload_format.get("fields", {}) or {}
    is_chat_messages = "messages" in fields

    test_data = json.loads(test_file_path.read_text(encoding="utf-8"))
    items = _flatten_prompts(test_data)
    if not items:
        if verbose:
            print("[-] No prompts in test file.")
        return None

    # Build payloads in the same shape as component payloads.json for this site
    payloads_list: list[dict] = []
    for item in items:
        if is_chat_messages:
            # Chat-style endpoint: wrap prompt as messages JSON string (same as chat/payloads.json)
            messages = json.dumps([{"role": "user", "content": item["prompt"]}])
            payloads_list.append({
                "messages": messages,
                "title": item["id"],
            })
        else:
            # Fallback: title + text (for non-chat JSON endpoints)
            payloads_list.append({
                "title": item["id"],
                "text": item["prompt"],
            })

    # Write a component-local tests.json in the same layout as payloads.json
    tests_file = discovery_config.SITE_STATE_DIR / "tests.json"
    tests_file.write_text(json.dumps({"payloads": payloads_list}, indent=2), encoding="utf-8")
    if verbose:
        print(f"[*] Wrote component tests file: {tests_file}")

    if verbose:
        print(f"[*] Sending {len(payloads_list)} compliance prompts to discovered endpoint...")
    results = await discovery_send_payloads.send_payloads_from_list(payloads_list, verbose=verbose)
    if not results:
        return None

    # Map title (id) back to metadata and build compliance log entries
    by_title = {item["id"]: item for item in items}
    compliance_results: list[dict] = []
    for r in results:
        tid = r.get("title", "")
        meta = by_title.get(tid, {})
        compliance_results.append({
            "id": tid,
            "mandate": meta.get("mandate", ""),
            "description": meta.get("description", ""),
            "prompt": meta.get("prompt", ""),
            "expected_behavior": meta.get("expected_behavior"),
            "status": r.get("status"),
            "ok": r.get("ok", False),
            "response": r.get("response"),
            "error": r.get("error"),
        })

    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "compliance_log.json"
    else:
        log_path = discovery_config.SITE_STATE_DIR / f"compliance_{timestamp}_log.json"
    log_payload = {
        "timestamp": timestamp,
        "framework": test_data.get("framework", "EU AI Act"),
        "source_file": str(test_file_path),
        "results": compliance_results,
    }
    log_path.write_text(json.dumps(log_payload, indent=2), encoding="utf-8")
    if verbose:
        print(f"[+] Compliance log: {log_path}")
    return log_path
