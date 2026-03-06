"""
Export pipeline_report.json results to Genbounty via the bulk-import API.

The pipeline_report.json is produced by risk assessment and contains
adversarial_results enriched with description, expected_behavior, status,
ok, and error fields merged from the corresponding compliance_log.json.

Required env vars (or supplied interactively):
  GENBOUNTY_HOST        — hostname (e.g. app.genbounty.com or localhost:4000)
  GENBOUNTY_API_KEY     — Bearer key scoped to write:bulk_import
  GENBOUNTY_PROGRAM_ID  — MongoDB ObjectId of the target program

Optional env var:
  GENBOUNTY_DEFAULT_LEVEL — informational | low | medium | critical
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

BULK_IMPORT_PATH = "/api/v2/submissions/bulk-import"
MAX_BATCH_SIZE = 2500


def _build_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host + BULK_IMPORT_PATH


def _post_json(url: str, api_key: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body_text)
        except Exception:
            raise RuntimeError(f"HTTP {e.code}: {body_text}") from e


def export_pipeline_report(
    report_path: Path,
    *,
    host: str,
    api_key: str,
    program_id: str,
    default_level: str | None = None,
) -> list[dict]:
    """
    Read pipeline_report.json and POST its adversarial_results to the Genbounty
    bulk-import endpoint.  Batches into chunks of ≤ MAX_BATCH_SIZE results.

    Each result is expected to carry description, expected_behavior, status, ok,
    and error fields merged in at risk-assessment time.

    Returns a list of response dicts (one per batch).
    """
    data = json.loads(report_path.read_text(encoding="utf-8"))

    timestamp = data.get("timestamp", "")
    framework = data.get("framework", "")
    source_file = data.get("source_file", str(report_path))
    results: list[dict] = data.get("adversarial_results", [])

    if not results:
        print("[-] No results found in compliance log.")
        return []

    url = _build_url(host)
    total = len(results)
    batches = [results[i : i + MAX_BATCH_SIZE] for i in range(0, total, MAX_BATCH_SIZE)]

    print(f"[*] Exporting {total} result(s) in {len(batches)} batch(es) to {url}")

    responses: list[dict] = []
    for idx, batch in enumerate(batches, 1):
        print(f"[*] Sending batch {idx}/{len(batches)} ({len(batch)} items)...")
        payload: dict = {
            "programId": program_id,
            "timestamp": timestamp,
            "framework": framework,
            "source_file": source_file,
            "results": batch,
        }
        if default_level:
            payload["defaultLevel"] = default_level

        try:
            resp = _post_json(url, api_key, payload)
        except Exception as e:
            print(f"[!] Batch {idx} failed: {e}")
            responses.append({"batch": idx, "error": str(e)})
            continue

        success = resp.get("success", False)
        summary = resp.get("summary", {})
        errors = resp.get("errors", [])

        if success:
            print(
                f"[+] Batch {idx} accepted — "
                f"total={summary.get('total', '?')}, "
                f"created={summary.get('created', '?')}, "
                f"failed={summary.get('failed', '?')}"
            )
        else:
            print(f"[!] Batch {idx} returned success=false")

        if errors:
            print(f"    {len(errors)} import error(s):")
            for err in errors[:10]:
                print(f"      index={err.get('index')}, id={err.get('id')}: {err.get('message')}")
            if len(errors) > 10:
                print(f"      ... and {len(errors) - 10} more.")

        resp["batch"] = idx
        responses.append(resp)

    created_total = sum(r.get("summary", {}).get("created", 0) for r in responses if "summary" in r)
    failed_total = sum(r.get("summary", {}).get("failed", 0) for r in responses if "summary" in r)
    print(f"\n[+] Export complete — {created_total} created, {failed_total} failed across {len(batches)} batch(es).")
    return responses
