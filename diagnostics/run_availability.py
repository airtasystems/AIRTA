"""
Run availability tests for tools and capabilities from discovery.json.
Sends each tool's and each capability's example_prompt to the endpoint, records
responses, and writes separate logs for tools and capabilities.
"""
import json
from pathlib import Path
from typing import Any

from pipeline import send_payloads as send_payloads_mod

from .run_diagnostics import _parse_response_json


def _example_prompt_from_item(item: dict[str, Any]) -> str:
    """Get sendable example prompt from a tool or capability item."""
    raw = (
        item.get("example_prompt")
        or item.get("example-prompt-to-call")
        or item.get("provide-example-of-a-prompt-used-to-call-the-tool")
    )
    return (raw if isinstance(raw, str) else "").strip()


def _build_availability_payloads(
    items: list[dict[str, Any]],
    api_fields: dict[str, Any],
    kind: str,
) -> list[dict[str, Any]]:
    """Build payloads for send_payloads_from_list: one per item, using example_prompt."""
    payloads: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        prompt = _example_prompt_from_item(item)
        if not prompt:
            continue
        name = item.get("name", "unknown")
        if "messages" in api_fields:
            payloads.append({
                "messages": json.dumps([{"role": "user", "content": prompt}]),
                "title": name,
                "_item": item,
            })
        else:
            keys = list(api_fields.keys())
            primary = keys[0] if keys else "text"
            payloads.append({
                primary: prompt,
                "title": name,
                "_item": item,
            })
    return payloads


def _strip_internal(payloads: list[dict]) -> list[dict]:
    """Remove _item from payloads before sending (send_payloads may not expect it)."""
    out = []
    for p in payloads:
        copy = {k: v for k, v in p.items() if k != "_item"}
        out.append(copy)
    return out


async def run_tools_availability(
    discovery_config: Any,
    discovery_path: Path,
    log_dir: Path | None = None,
    verbose: bool = True,
    speed: int = 1,
) -> Path | None:
    """
    Load discovery.json; for each tool with example_prompt, send it to the endpoint.
    Write tools_availability_log.json. Returns log path or None.
    """
    if not discovery_path.exists():
        if verbose:
            print(f"[-] Discovery file not found: {discovery_path}")
        return None
    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        if verbose:
            print(f"[-] No discovered endpoint. Run discovery first.")
        return None
    if not discovery_config.AUTH_STATE_FILE.exists():
        if verbose:
            print(f"[-] No auth state. Run login first.")
        return None

    try:
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"[-] Failed to load discovery: {e}")
        return None

    tools = discovery.get("tools")
    if not isinstance(tools, list) or not tools:
        if verbose:
            print("[*] No tools list in discovery; skipping tools availability.")
        return None

    discovered = json.loads(discovery_config.DISCOVERED_ENDPOINT_FILE.read_text(encoding="utf-8"))
    api_fields = discovered.get("payload_format") or {}
    api_fields = api_fields.get("fields", {})

    payloads_with_meta = _build_availability_payloads(tools, api_fields, "tool")
    if not payloads_with_meta:
        if verbose:
            print("[*] No tools with example_prompt; skipping.")
        return None

    payloads_to_send = _strip_internal(payloads_with_meta)
    if verbose:
        print(f"[*] Sending {len(payloads_to_send)} tool availability prompts...")
    results = await send_payloads_mod.send_payloads_from_list(payloads_to_send, verbose=verbose, speed=speed)
    if not results:
        return None

    # Attach full item and prompt to each result for the assessor
    by_title = {p.get("title"): p for p in payloads_with_meta if p.get("_item")}
    for r in results:
        title = r.get("title")
        if title and title in by_title:
            r["item"] = by_title[title]["_item"]
            r["example_prompt"] = _example_prompt_from_item(by_title[title]["_item"])
        raw = r.get("response")
        parsed = _parse_response_json(raw)
        if parsed is not None:
            r["response_parsed"] = parsed

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "tools_availability_log.json"
    else:
        log_path = discovery_config.SITE_STATE_DIR / f"tools_availability_{timestamp}.json"
    log_path.write_text(
        json.dumps({"timestamp": timestamp, "results": results}, indent=2),
        encoding="utf-8",
    )
    if verbose:
        print(f"[+] Tools availability log: {log_path}")
    return log_path


async def run_capabilities_availability(
    discovery_config: Any,
    discovery_path: Path,
    log_dir: Path | None = None,
    verbose: bool = True,
    speed: int = 1,
) -> Path | None:
    """
    Load discovery.json; for each capability with example_prompt, send it to the endpoint.
    Write capabilities_availability_log.json. Returns log path or None.
    """
    if not discovery_path.exists():
        if verbose:
            print(f"[-] Discovery file not found: {discovery_path}")
        return None
    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        if verbose:
            print(f"[-] No discovered endpoint. Run discovery first.")
        return None
    if not discovery_config.AUTH_STATE_FILE.exists():
        if verbose:
            print(f"[-] No auth state. Run login first.")
        return None

    try:
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if verbose:
            print(f"[-] Failed to load discovery: {e}")
        return None

    capabilities = discovery.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        if verbose:
            print("[*] No capabilities list in discovery; skipping capabilities availability.")
        return None

    discovered = json.loads(discovery_config.DISCOVERED_ENDPOINT_FILE.read_text(encoding="utf-8"))
    api_fields = discovered.get("payload_format") or {}
    api_fields = api_fields.get("fields", {})

    payloads_with_meta = _build_availability_payloads(capabilities, api_fields, "capability")
    if not payloads_with_meta:
        if verbose:
            print("[*] No capabilities with example_prompt; skipping.")
        return None

    payloads_to_send = _strip_internal(payloads_with_meta)
    if verbose:
        print(f"[*] Sending {len(payloads_to_send)} capability availability prompts...")
    results = await send_payloads_mod.send_payloads_from_list(payloads_to_send, verbose=verbose, speed=speed)
    if not results:
        return None

    by_title = {p.get("title"): p for p in payloads_with_meta if p.get("_item")}
    for r in results:
        title = r.get("title")
        if title and title in by_title:
            r["item"] = by_title[title]["_item"]
            r["example_prompt"] = _example_prompt_from_item(by_title[title]["_item"])
        raw = r.get("response")
        parsed = _parse_response_json(raw)
        if parsed is not None:
            r["response_parsed"] = parsed

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "capabilities_availability_log.json"
    else:
        log_path = discovery_config.SITE_STATE_DIR / f"capabilities_availability_{timestamp}.json"
    log_path.write_text(
        json.dumps({"timestamp": timestamp, "results": results}, indent=2),
        encoding="utf-8",
    )
    if verbose:
        print(f"[+] Capabilities availability log: {log_path}")
    return log_path
