# AIRTA — AI Red Team Attack Suite

Unified pipeline for discovery, diagnostics, compliance testing, and risk assessment of LLM endpoints (e.g. EU AI Act, OWASP, FRIA). Discovers the app’s API and auth, sends adversarial prompts, and scores results with a multi-expert risk-level agent.

## Requirements

- Python 3.10+
- [Playwright](https://playwright.dev/python/) (browser for discovery)

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuration

- **`.config`** — Non-sensitive settings (loaded first): `APP_URL`, `TARGET_API_URL`, `COMPONENT`, `GEMINI_MODEL`, `REFRESH_URL`, etc. Same format as `.env` (KEY=value, `#` comments).
- **`.env`** — Secrets only (e.g. `GEMINI_API_KEY`). Loaded after `.config` so it can override.

Example `.config`:

```ini
APP_URL=http://localhost:3000
TARGET_API_URL=http://localhost:3000/api/chat
COMPONENT=chat
GEMINI_MODEL=gemini-2.5-flash
REFRESH_URL=http://localhost:3000/api/v2/auth/refresh
```

## Run the pipeline (CLI)

From project root:

```bash
python main.py [options]
```

### Flags

| Flag | Default | Description |
|------|--------|-------------|
| `--skip-discovery` | — | Skip discovery; assume endpoint and auth already exist. |
| `--skip-diagnostics` | — | Skip diagnostics (send + analyze_log) before compliance tests. |
| `--force-discovery` | — | Run discovery even if endpoint and auth exist. |
| `--strategy` | `zero_shot` | Strategy subdir under `generate-tests/` (e.g. `zero_shot`, `multi_shot`, `few_shot`). Hyphens auto-corrected to underscores. |
| `--framework` | `eu_ai_act` | Framework name for test file; resolves to `generate-tests/<strategy>/<framework>.json`. |
| `--test-file` | — | Override: path to test prompts JSON. If unset, uses strategy/framework path above. |
| `--component` | `COMPONENT` env or `default` | Component name for discovery state. |
| `--report-dir` | — | Also copy `pipeline_report.json` to this directory. |
| `--speed` | `1` | Request concurrency: `1` = sequential with evasion (throttle + tenacity); `2`–`8` = up to N concurrent requests (token-bucket + 0.3s gap between starts). |

Flow: optional discovery → diagnostics (if not skipped) → compliance tests → risk assessment → report under `component-discovery/<site>/<component>/logs/<timestamp>/`.

## Discovery (first-time setup)

1. Set `APP_URL`, `TARGET_API_URL`, and (optionally) `COMPONENT` in `.config`.
2. **Capture login** — Browser opens; log in (and MFA if required), then press Enter in the terminal when done.
3. **Discover endpoint** — Make one request to the LLM in the app; the pipeline intercepts it and saves URL, headers, and payload shape.
4. **Generate site payload** — Produces site-specific `payload_format.py` and `send_payloads.py` from the discovered schema.

After that, use **Skip discovery** for normal pipeline runs.

## Project layout (high level)

- `main.py` — CLI entry point; calls `run_pipeline()`.
- `component-discovery/` — Auth, discovery, payload format, send payloads, diagnostics; state under `component-discovery/<site>/<component>/`.
- `diagnostics/` — Diagnostic prompts and `analyze_log` (writes `discovery.json`).
- `generate-tests/` — Compliance test prompts by strategy (e.g. zero-shot) and framework (e.g. eu_ai_act). Use `generator.py --strategy X --framework Y` for one pair, or `generate_all.py` to create all missing strategy×framework files (from `strategies/` and `rubrics/`). Each file is written as it’s generated, so if a run fails you can re-run and only missing files will be generated.
- `pipeline/` — Runs compliance tests and risk assessment.
- `risk-level-agent/` — Multi-expert + judge for risk levels (local file cache can be disabled via `LOCAL_CACHE_ENABLED`).
