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

Options include `--component`, `--strategy`, `--framework`, `--skip-discovery`, `--skip-diagnostics`, `--force-discovery`, `--test-file`, `--report-dir`. Default test file is `generate-tests/<strategy>/<framework>.json` (underscore and hyphen names both resolved).

Flow: optional discovery → diagnostics (if not skipped) → compliance tests → risk assessment → report under `component-discovery/<site>/<component>/logs/<timestamp>/`.

## Run the UI (Streamlit)

From project root:

```bash
PYTHONDONTWRITEBYTECODE=1 streamlit run ui/streamlit_app.py
```

Then open http://localhost:8501. The env var prevents Python from writing `__pycache__` under the project when Streamlit (and its workers) run.

**UI features:** Discovery (login + discover endpoint + generate payload), Diagnostics, Pipeline (run full flow), Config view, Reports. Discovery opens the browser on the right half of the screen and uses a **Confirm login** button instead of terminal input.

## Optional API server (Gunicorn)

For running the pipeline via HTTP or offloading long runs from Streamlit:

```bash
gunicorn ui.api:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000
```

Then in the Streamlit sidebar you can set **Pipeline API URL** to `http://localhost:8000` so **Run pipeline** uses the API. See `ui/README.md` for endpoints.

## Discovery (first-time setup)

1. Set `APP_URL`, `TARGET_API_URL`, and (optionally) `COMPONENT` in `.config`.
2. **Capture login** — Browser opens; log in (and MFA if required), then click **Confirm login** in the UI (or press Enter in the terminal if using CLI).
3. **Discover endpoint** — Make one request to the LLM in the app; the pipeline intercepts it and saves URL, headers, and payload shape.
4. **Generate site payload** — Produces site-specific `payload_format.py` and `send_payloads.py` from the discovered schema.

After that, use **Skip discovery** for normal pipeline runs.

## Project layout (high level)

- `main.py` — CLI entry point; calls `run_pipeline()`.
- `component-discovery/` — Auth, discovery, payload format, send payloads, diagnostics; state under `component-discovery/<site>/<component>/`.
- `diagnostics/` — Diagnostic prompts and `analyze_log` (writes `discovery.json`).
- `generate-tests/` — Compliance test prompts by strategy (e.g. zero-shot) and framework (e.g. eu_ai_act).
- `pipeline/` — Runs compliance tests and risk assessment.
- `risk-level-agent/` — Multi-expert + judge for risk levels (local file cache can be disabled via `LOCAL_CACHE_ENABLED`).
- `ui/` — Streamlit app and FastAPI app for the pipeline.
