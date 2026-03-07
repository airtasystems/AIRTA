"""
Discovered API output: write discovered_api.json with GET/POST endpoints and payload formats.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

from ..heuristics import (
    MIN_SCORE,
    build_multishot_example,
    extract_payload_format,
    find_multishot_from_trace,
)


def _build_post_endpoint(c: dict, multishot_messages: list | None = None) -> dict:
    """Build a POST endpoint entry with payload_format."""
    entry = {
        "url": c["url"],
        "path": urlparse(c["url"]).path or "/",
        "method": c.get("method", "POST"),
        "score": c["score"],
        "reason": c.get("reason", ""),
    }
    headers = c.get("headers") or {}
    if headers:
        entry["headers"] = headers

    payload_fmt = extract_payload_format(c.get("post_data"))
    if payload_fmt:
        entry["payload_format"] = payload_fmt
        if payload_fmt.get("messages_structure") and "messages" in (payload_fmt.get("fields") or []):
            if multishot_messages:
                payload_fmt["multishot_example"] = multishot_messages
                payload_fmt["multishot_verified"] = True
            else:
                payload_fmt["multishot_example"] = build_multishot_example(payload_fmt.get("messages_structure"))
                payload_fmt["multishot_verified"] = False
    return entry


def _build_get_endpoint(c: dict) -> dict:
    """Build a GET endpoint entry."""
    return {
        "url": c["url"],
        "path": c.get("path", urlparse(c["url"]).path or "/"),
        "method": "GET",
        "score": c["score"],
        "reason": c.get("reason", ""),
        "query_params": c.get("query_params", {}),
    }


def write_discovered(
    captured: dict,
    output_dir: Path,
    *,
    update_config: bool = False,
    config_path: Path | None = None,
) -> Path | None:
    """
    Write discovered_api.json with comprehensive GET and POST endpoints.
    captured: {"post": [...], "get": [...]} from discover.discover_api().
    If update_config and best POST candidate exists, append/update TARGET_API_URL in .config.
    Returns path to discovered_api.json, or None if no POST candidates.
    """
    post_candidates = captured.get("post", [])
    get_endpoints = captured.get("get", [])

    if not post_candidates:
        return None

    best = post_candidates[0]
    if best["score"] < MIN_SCORE:
        return None

    trace_entries = captured.get("trace", [])
    multishot_messages = find_multishot_from_trace(trace_entries, best["url"])

    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / "discovered_api.json"
    confidence = "high" if best["score"] >= 6 else "medium" if best["score"] >= 3 else "low"

    out = {
        "target_api_url": best["url"],
        "method": best.get("method", "POST"),
        "confidence": confidence,
        "score": best["score"],
        "reason": best.get("reason", ""),
        "candidates": [
            {"url": c["url"], "method": c.get("method", "POST"), "score": c["score"], "reason": c.get("reason", "")}
            for c in post_candidates[:10]
        ],
        "endpoints": {
            "post": [
                _build_post_endpoint(c, multishot_messages=(multishot_messages if c["url"] == best["url"] else None))
                for c in post_candidates[:20]
            ],
            "get": [_build_get_endpoint(c) for c in get_endpoints[:20]],
        },
    }

    out_file.write_text(json.dumps(out, indent=2), encoding="utf-8")

    if update_config and config_path and config_path.exists():
        _update_config(config_path, best["url"])

    return out_file


def _update_config(config_path: Path, target_url: str) -> None:
    """Update or add TARGET_API_URL in .config file."""
    lines = config_path.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TARGET_API_URL="):
            indent = line[: len(line) - len(line.lstrip())]
            if "#" in stripped:
                comment = " #" + stripped.split("#", 1)[1]
            else:
                comment = ""
            new_lines.append(f"{indent}TARGET_API_URL={target_url}{comment}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"TARGET_API_URL={target_url}")

    config_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
