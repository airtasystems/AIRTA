"""
Export pipeline_report.json results to AIRTA Systems via the bulk-import API.

API: POST /api/v2/imported-reports/company
Headers: Authorization: Bearer <key>, X-Program-Id: <id>
Body: pipeline_report.json fields accepted by the import endpoint.

Required env vars (or supplied via CLI / GUI):
  AIRTASYSTEMS_HOST        — hostname (e.g. app.airtasystems.com or localhost:4000)
  AIRTASYSTEMS_API_KEY     — API key scoped to write:bulk_import
  AIRTASYSTEMS_PROGRAM_ID  — MongoDB ObjectId of the target program

Optional env vars:
  AIRTASYSTEMS_DEFAULT_LEVEL   — indeterminate | compliant | informational | low | medium | high | critical
  AIRTASYSTEMS_EXPORT_BATCH_SIZE   — items per request (default: 10)
  AIRTASYSTEMS_EXPORT_BATCH_DELAY_S — pause between requests in seconds (default: 2)
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

IMPORT_PATH = "/api/v2/imported-reports/company"
# API accepts up to 5 000 items per request; default chunk size is much smaller to avoid rate limits.
DEFAULT_BATCH_SIZE = 10
API_MAX_BATCH_SIZE = 5000
DEFAULT_BATCH_DELAY_S = 2.0
MAX_POST_RETRIES = 6
TOP_LEVEL_FIELDS = {
    "timestamp",
    "framework",
    "source_file",
    "run_log_dir",
    "compliance_log",
    "compliance_results",
}
RESULT_FIELDS = {
    "id",
    "mandate",
    "prompt",
    "response",
    "risk_level",
    "judge_reasoning",
    "experts_summary",
    "description",
    "expected_behavior",
    "ok",
    "status",
    "error",
}
VALID_RISK_LEVELS = {
    "indeterminate",
    "compliant",
    "informational",
    "low",
    "medium",
    "high",
    "critical",
}


def _build_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host + IMPORT_PATH


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[!] Invalid {name}={raw!r}; using default {default}.")
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[!] Invalid {name}={raw!r}; using default {default}.")
        return default


def _resolve_batch_size(batch_size: int | None) -> int:
    size = batch_size if batch_size is not None else _env_int("AIRTASYSTEMS_EXPORT_BATCH_SIZE", DEFAULT_BATCH_SIZE)
    return max(1, min(size, API_MAX_BATCH_SIZE))


def _resolve_batch_delay(batch_delay_s: float | None) -> float:
    delay = batch_delay_s if batch_delay_s is not None else _env_float("AIRTASYSTEMS_EXPORT_BATCH_DELAY_S", DEFAULT_BATCH_DELAY_S)
    return max(0.0, delay)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        return None


def _http_error_message(e: urllib.error.HTTPError) -> str:
    body_text = e.read().decode("utf-8", errors="replace")
    try:
        error_body = json.loads(body_text)
    except Exception:
        return f"HTTP {e.code}: {body_text}"
    message = error_body.get("message") or error_body.get("error") or body_text
    return f"HTTP {e.code}: {message}"


def _post_json(
    url: str,
    api_key: str,
    program_id: str,
    payload: dict,
    *,
    max_retries: int = MAX_POST_RETRIES,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_error: RuntimeError | None = None

    for attempt in range(max_retries):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "X-Program-Id": program_id,
                # Avoid edge filters that reject Python's default urllib user agent.
                "User-Agent": "AIRTA-Pipeline-Exporter/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            message = _http_error_message(e)
            last_error = RuntimeError(message)
            retryable = e.code in (429, 503)
            if retryable and attempt < max_retries - 1:
                retry_after = _parse_retry_after(e.headers.get("Retry-After"))
                wait_s = retry_after if retry_after is not None else min(60.0, 2.0 ** attempt)
                print(
                    f"    HTTP {e.code} — waiting {wait_s:.1f}s before retry "
                    f"({attempt + 2}/{max_retries})..."
                )
                time.sleep(wait_s)
                continue
            raise last_error from e

    if last_error is not None:
        raise last_error
    raise RuntimeError("Export request failed without a response.")


def _normalize_timestamp(value: object) -> object:
    """Convert AIRTA's filename-safe timestamp into an ISO-8601 datetime string."""
    if not isinstance(value, str) or not value.strip():
        return value

    text = value.strip()
    for fmt in ("%Y-%m-%dT%H-%M-%S", "%Y-%m-%d_%H-%M-%S"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="milliseconds") + "Z"
        except ValueError:
            pass
    return text


def _normalize_risk_level(value: object, default_level: str | None = None) -> object:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_RISK_LEVELS:
            return normalized
    if default_level:
        normalized_default = default_level.strip().lower()
        if normalized_default in VALID_RISK_LEVELS:
            return normalized_default
    return value


def _sanitize_result(item: dict, default_level: str | None = None) -> dict:
    result = {k: item[k] for k in RESULT_FIELDS if k in item}
    if "risk_level" in result:
        result["risk_level"] = _normalize_risk_level(result["risk_level"], default_level)
    elif default_level:
        result["risk_level"] = _normalize_risk_level(default_level)
    return result


def _sanitize_payload(data: dict, results: list[dict], default_level: str | None = None) -> dict:
    """
    Keep fields accepted by the imported-reports API and drop local rollups.
    """
    payload = {k: data[k] for k in TOP_LEVEL_FIELDS if k in data and k != "compliance_results"}
    if "timestamp" in payload:
        payload["timestamp"] = _normalize_timestamp(payload["timestamp"])
    payload["compliance_results"] = [
        _sanitize_result(item, default_level)
        for item in results
    ]
    return payload


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _extract_summary(resp: dict, batch_size: int) -> dict[str, int]:
    """Return numeric import metrics, falling back to full-batch success."""
    summary = resp.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    data = resp.get("data")
    if not isinstance(data, dict):
        data = {}

    failed = _coerce_int(summary.get("failed"))
    if failed is None:
        failed = _coerce_int(resp.get("failed"))
    if failed is None:
        errors = resp.get("errors")
        failed = len(errors) if isinstance(errors, list) else 0

    total = _coerce_int(summary.get("total"))
    if total is None:
        total = _coerce_int(resp.get("total"))
    if total is None:
        total = batch_size

    created = _coerce_int(summary.get("created"))
    if created is None:
        created = _coerce_int(resp.get("created"))
    if created is None:
        created = _coerce_int(resp.get("inserted"))
    if created is None:
        created = _coerce_int(resp.get("imported"))
    if created is None:
        created = _coerce_int(data.get("importedCount"))
    if created is None:
        created = max(0, total - failed)

    return {"total": total, "created": created, "failed": failed}


def export_pipeline_report(
    report_path: Path,
    *,
    host: str,
    api_key: str,
    program_id: str,
    default_level: str | None = None,
    batch_size: int | None = None,
    batch_delay_s: float | None = None,
) -> list[dict]:
    """
    Read pipeline_report.json and POST it to the AIRTA Systems imported-reports
    endpoint.  compliance_results are split into small batches (default 10 items)
    with a pause between requests to reduce rate-limit failures.

    Returns a list of response dicts (one per batch).
    """
    data = json.loads(report_path.read_text(encoding="utf-8"))
    results: list[dict] = data.get("compliance_results", [])

    if not results:
        print("[-] No compliance_results found in report.")
        return []

    chunk_size = _resolve_batch_size(batch_size)
    chunk_delay = _resolve_batch_delay(batch_delay_s)
    url = _build_url(host)
    total = len(results)
    batches = [results[i : i + chunk_size] for i in range(0, total, chunk_size)]

    print(
        f"[*] Exporting {total} result(s) in {len(batches)} batch(es) "
        f"({chunk_size} item(s) max per request, {chunk_delay:.1f}s between batches) to {url}"
    )

    meta = {k: data[k] for k in TOP_LEVEL_FIELDS if k in data and k != "compliance_results"}

    responses: list[dict] = []
    for idx, batch in enumerate(batches, 1):
        print(f"[*] Sending batch {idx}/{len(batches)} ({len(batch)} item(s))...")
        payload = _sanitize_payload(meta, batch, default_level)

        try:
            resp = _post_json(url, api_key, program_id, payload)
        except Exception as e:
            print(f"[!] Batch {idx} failed: {e}")
            responses.append({
                "batch": idx,
                "error": str(e),
                "summary": {"total": len(batch), "created": 0, "failed": len(batch)},
            })
            if idx < len(batches) and chunk_delay > 0:
                time.sleep(chunk_delay)
            continue

        success = resp.get("success", False)
        summary = _extract_summary(resp, len(batch))
        errors = resp.get("errors", [])
        resp["summary"] = summary

        if success:
            print(
                f"[+] Batch {idx} accepted — "
                f"total={summary.get('total', '?')}, "
                f"created={summary.get('created', '?')}, "
                f"failed={summary.get('failed', '?')}"
            )
        else:
            err_code = resp.get("error", "unknown")
            print(f"[!] Batch {idx} returned success=false: {err_code}")

        if errors:
            print(f"    {len(errors)} import error(s):")
            for err in errors[:10]:
                print(f"      index={err.get('index')}, id={err.get('id')}: {err.get('message')}")
            if len(errors) > 10:
                print(f"      ... and {len(errors) - 10} more.")

        resp["batch"] = idx
        responses.append(resp)

        if idx < len(batches) and chunk_delay > 0:
            time.sleep(chunk_delay)

    created_total = sum(r.get("summary", {}).get("created", 0) for r in responses if "summary" in r)
    failed_total = sum(r.get("summary", {}).get("failed", 0) for r in responses if "summary" in r)
    print(f"\n[+] Export complete — {created_total} created, {failed_total} failed across {len(batches)} batch(es).")
    return responses
