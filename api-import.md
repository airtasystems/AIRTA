# Bulk Import API — Client Integration Guide

## Endpoint

```
POST /api/v2/imported-reports/company
```

## Required Headers

| Header | Value |
|---|---|
| `Authorization` | `Bearer <AIRTASYSTEMS_API_KEY>` |
| `X-Program-Id` | `<mongodb-program-id>` |
| `Content-Type` | `application/json` |

The API key must have the `write:bulk_import` scope assigned.

---

## Request Body

Send the export-safe subset of `pipeline_report.json` as the request body. Local-only fields such as file paths, mandate rollups, UI status fields, and rendered `response_html` should not be included.

### Expected top-level fields

| Field | Type | Required | Description |
|---|---|---|---|
| `adversarial_results` | array | Yes | Test result items (max 5,000) |
| `framework` | string | No | e.g. `"EU AI Act"` |
| `timestamp` | string | No | ISO-format report timestamp |

### Each `adversarial_results` item

| Field | Type | Description |
|---|---|---|
| `id` | string | External test ID |
| `mandate` | string | Regulatory article reference |
| `prompt` | string | Test prompt sent to the AI |
| `response` | string | AI response captured |
| `risk_level` | string | e.g. `critical`, `high`, `informational`, `compliant` |
| `judge_reasoning` | string | Expert assessment rationale |
| `experts_summary` | array | `[{ framework, risk_level, reasoning }]` |
| `description` | string | Test description |
| `ok` | boolean | Whether test passed |
| `error` | string\|null | Import error if any |

---

## Examples

### curl

```bash
curl -X POST https://<host>/api/v2/imported-reports/company \
  -H "Authorization: Bearer <AIRTASYSTEMS_API_KEY>" \
  -H "X-Program-Id: <programId>" \
  -H "Content-Type: application/json" \
  -d @pipeline_report.json
```

### Python

```python
import json, requests

with open("pipeline_report.json") as f:
    payload = json.load(f)

response = requests.post(
    "https://<host>/api/v2/imported-reports/company",
    headers={
        "Authorization": "Bearer <AIRTASYSTEMS_API_KEY>",
        "X-Program-Id": "<programId>",
        "Content-Type": "application/json",
    },
    json=payload
)
print(response.json())
```

### Node.js

```js
const fs = require("fs");

const payload = JSON.parse(fs.readFileSync("pipeline_report.json", "utf8"));

const res = await fetch("https://<host>/api/v2/imported-reports/company", {
  method: "POST",
  headers: {
    "Authorization": "Bearer <AIRTASYSTEMS_API_KEY>",
    "X-Program-Id": "<programId>",
    "Content-Type": "application/json",
  },
  body: JSON.stringify(payload),
});

console.log(await res.json());
```

---

## Responses

### Success — `207 Multi-Status`

```json
{
  "success": true,
  "message": "Bulk import complete: 8 created, 0 failed",
  "summary": {
    "total": 8,
    "created": 8,
    "failed": 0
  }
}
```

Partial failures are reported in an `errors` array alongside successful inserts:

```json
{
  "success": true,
  "summary": { "total": 8, "created": 7, "failed": 1 },
  "errors": [
    { "index": 3, "id": "art5-social-scoring-analyst-risk", "message": "Write error" }
  ]
}
```

### Error Responses

| Status | `error` | Cause |
|---|---|---|
| `400` | `invalid_body` | Request body is not valid JSON |
| `400` | `validation_error` | Missing/invalid `X-Program-Id` or empty `adversarial_results` |
| `401` | `invalid_api_key` | Key not found or inactive |
| `403` | `ip_not_allowed` | Client IP not on key's allowlist |
| `403` | `forbidden` | Key missing `write:bulk_import` scope, or program not owned by your company |
| `404` | `not_found` | Program ID does not exist |
| `500` | `internal_error` | Server error |

---

## Notes

- `programId` can be provided in the JSON body instead of the header if preferred; body value takes precedence.
- Requests time out after **5 minutes** — sufficient for the 5,000-item maximum.
- Imported reports are created with status `submitted` and `pointsAwarded: 0` by default.
- Manage imported reports after upload via `GET/PUT/DELETE /api/v2/imported-reports/company/[id]`.
