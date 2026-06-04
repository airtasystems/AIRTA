# Import AIRTA testing report batch — Client guide

Matches the AIRTA Systems OpenAPI operation:

**POST** `/api/v2/imported-reports/company` — Import AIRTA testing report batch  
**Scope:** `write:imported_reports`

Bulk-imports an AIRTA `pipeline_report.json` payload. The on-disk report uses `compliance_results[]`; this exporter sends `adversarial_results[]` (same row shape).

---

## Headers

| Header | Required | Description |
|---|---|---|
| `Authorization` | Yes | `Bearer <AIRTASYSTEMS_API_KEY>` |
| `X-Program-Id` | Yes | Target program id (`Airta-Program-Id` is also accepted) |
| `Content-Type` | Yes | `application/json` |

The API key must have scope **`write:imported_reports`**.

---

## Request body (`AirtaImportedReportBatchInput`)

| Field | Type | Required | Description |
|---|---|---|---|
| `framework` | string | Yes | e.g. `"EU AI Act"`, `"OECD AI Principles"` |
| `timestamp` | string | Yes | ISO-8601, e.g. `2026-06-02T12:00:00.000Z` |
| `source_file` | string | No | Label for the source report (exporter sends basename, default `pipeline_report.json`) |
| `adversarial_results` | array | Yes | Test result items (max 5,000 per request) |

### Each `adversarial_results` item

| Field | Type | Description |
|---|---|---|
| `id` | string | External test id |
| `mandate` | string | Regulatory / rubric reference |
| `prompt` | string | Test prompt |
| `response` | string | Model response |
| `risk_level` | string | `indeterminate`, `compliant`, `informational`, `low`, `medium`, `high`, `critical` |
| `judge_reasoning` | string | Assessment rationale |
| `experts_summary` | array | `[{ framework, risk_level, reasoning }]` |
| `description` | string | Test description |
| `ok` | boolean | Whether the run succeeded |
| `error` | string \| null | Error message if any |

Local-only fields in `pipeline_report.json` are **not** sent: `mandate_rollup`, `run_log_dir`, `compliance_log`, `response_html`, `status`, `expected_behavior`, etc.

### Legacy servers

Set `AIRTASYSTEMS_EXPORT_SCHEMA=legacy` to POST `compliance_results` instead of `adversarial_results` (older self-hosted imports only).

---

## Example request

```json
{
  "framework": "AIRTA Core",
  "timestamp": "2026-06-02T12:00:00.000Z",
  "source_file": "pipeline_report.json",
  "adversarial_results": [
    {
      "id": "imported-report-001",
      "mandate": "EU AI Act transparency",
      "prompt": "Summarise the customer complaint.",
      "response": "The customer reported an incorrect refund amount.",
      "risk_level": "medium",
      "judge_reasoning": "Imported AIRTA finding with moderate consumer harm risk.",
      "experts_summary": [
        {
          "framework": "EU AI Act",
          "risk_level": "medium",
          "reasoning": "Financial guidance mismatch."
        }
      ],
      "description": "Imported AIRTA report from external assessment tool.",
      "ok": true,
      "error": null
    }
  ]
}
```

---

## Example: curl

```bash
curl -X POST "https://dashboard.airtasystems.com/api/v2/imported-reports/company" \
  -H "Authorization: Bearer $AIRTASYSTEMS_API_KEY" \
  -H "X-Program-Id: $AIRTASYSTEMS_PROGRAM_ID" \
  -H "Content-Type: application/json" \
  -d @payload.json
```

AIRTA CLI / web UI run the same mapping via `pipeline/export_genbounty.py`.

---

## Success response (`201`)

```json
{
  "success": true,
  "message": "Imported testing report accepted",
  "data": {
    "importBatchId": "674a1b2c3d4e5f6789012399",
    "programId": "674a1b2c3d4e5f6789012345",
    "importedCount": 1
  }
}
```

Large reports are split into batches (default 10 rows per POST, configurable via `AIRTASYSTEMS_EXPORT_BATCH_SIZE`).

---

## Error responses

| Status | `error` | Typical cause |
|---|---|---|
| `400` | `validation_error` | Invalid body, missing program id, empty results, unknown fields |
| `401` | `invalid_api_key` | Key missing or inactive |
| `403` | `forbidden` | Missing `write:imported_reports`, or program not in your company |
| `404` | `not_found` | Program id does not exist |
| `415` | — | Wrong `Content-Type` |
| `429` | — | Rate limit (exporter retries with backoff) |

Validation errors include an `errors[]` array with `field` and `message` (surfaced in export job logs).

---

## Environment variables

| Variable | Purpose |
|---|---|
| `AIRTASYSTEMS_HOST` | API host (e.g. `https://dashboard.airtasystems.com`) |
| `AIRTASYSTEMS_API_KEY` | Bearer token |
| `AIRTASYSTEMS_PROGRAM_ID` | Program ObjectId (UI can override per export) |
| `AIRTASYSTEMS_EXPORT_SCHEMA` | `security` (default) or `legacy` |
| `AIRTASYSTEMS_IMPORT_PATH` | Override path if needed |
| `AIRTASYSTEMS_EXPORT_BATCH_SIZE` | Rows per request (default 10) |
| `AIRTASYSTEMS_EXPORT_BATCH_DELAY_S` | Pause between batches (default 2s) |

---

## Notes

- `pipeline_report.json` on disk keeps `compliance_results` for the risk-assess pipeline; only the HTTP export renames the array to `adversarial_results`.
- Requests time out after **5 minutes** (up to 5,000 items per request).
- Do not fork-merge security-repo export docs that still describe `compliance_results` as the API field — that causes `400 validation_error` on the dashboard.
