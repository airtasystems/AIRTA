# AIRTA — AI Red Team Attack Suite

Generate adversarial test suites from compliance rubrics, run them against live targets via browser automation, and assess risk with multi-expert analysis.

## Requirements

- Python 3.10+
- Chromium (for browser-bot). On first run, `start.py` installs Playwright's Chromium automatically. On Linux you can also use system Chromium at `/usr/bin/chromium-browser` (already configured in `browser-bot/browser_bot/config.py`).

## Quick start (web UI)

The easiest way to run AIRTA — creates a venv, installs dependencies, and launches the web UI:

```bash
python start.py
```

Or from anywhere:

```bash
python ~/Code/AIRTA/start.py
```

Optional shell alias:

```bash
alias airta='python ~/Code/AIRTA/start.py'
```

Then open **http://localhost:8000**. The web UI (FastAPI + SPA) wraps the full pipeline — generate, discover, run, risk-assess, export — with a browser-based interface. API docs are available at `/api/docs`.

`start.py` creates `airta-venv/` on first run, installs `requirements.txt`, runs `playwright install chromium` once, then starts the server. If port 8000 is busy, the app picks the next available port and prints it.

### Manual setup (alternative)

If you prefer to manage the venv yourself:

```bash
python -m venv airta-venv
source airta-venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python web/app.py
```

Run from the repo root so `.config` and `.env` are picked up.

## Local test target app

For browser-bot smoke tests without an external SaaS demo, run the in-repo mock chat app:

```bash
python test-target/app.py
```

Then run tests against site `localhost:3000`, component `test-target`. See [test-target/README.md](test-target/README.md).

## Configuration

AIRTA layers configuration from several files. Later layers override earlier ones:

| Layer | File | What it controls |
|-------|------|------------------|
| 1 (baseline) | [`config.defaults.yaml`](config.defaults.yaml) | Shipped browser/cache defaults — edit to change the out-of-box experience |
| 2 (global) | [`browser-bot/browser_bot/config.py`](browser-bot/browser_bot/config.py) | Browser behaviour — edited via **Settings → Browser Config** in the web UI |
| 2 (global) | [`.env`](.env) | Secrets and cache toggle (`GEMINI_USE_CACHE`) — **Settings → Cache Control** |
| 3 (site) | `browser-bot/sites/<site>/config.yaml` | Shared auth and optional `settings:` for all components on a site — **auto-created on first Discovery** |
| 4 (component) | `browser-bot/sites/<site>/<component>/config.yaml` | UI submission selectors and per-component `settings:` overrides |

Other files:

- **[`.config`](.config)** — Non-sensitive LLM settings (`GEMINI_MODEL`, `GEMINI_JUDGE`). Same `KEY=value` format as `.env`.
- **[`.env`](.env)** — Secrets only (e.g. `GEMINI_API_KEY`). Loaded after `.config`.

### Settings overrides (`settings:` block)

Browser and cache keys can be copied from [`config.defaults.yaml`](config.defaults.yaml) into any site or component `config.yaml`. Each key is documented there with allowed values. The web UI exposes the same options under **Settings → Component Config → Settings overrides** (check **Inherit** to omit a key and use the global default).

**Example — override one component to run headless:**

```yaml
# browser-bot/sites/localhost:3000/main/config.yaml
submission:
  start_url: http://localhost:3000/playground
  # ... other submission fields ...

settings:
  HEADLESS: true
```

**Example — site-wide default for every component on a host:**

```yaml
# browser-bot/sites/localhost:3000/config.yaml
settings:
  FETCH_METHOD: human
  HUMAN_COUNTRY: UK
```

**Common `settings:` keys**

| Key | Purpose | Options / notes |
|-----|---------|-----------------|
| `gemini_use_cache` | Gemini context cache for generate + risk assess | `true` / `false`; also `.env` `GEMINI_USE_CACHE` |
| `FETCH_METHOD` | Browser tier | `auto`, `pool`, `cluster`, `human` |
| `HEADLESS` | Hide browser window | `true` / `false` |
| `HUMAN_COUNTRY` | Locale / timezone / geo | `US`, `UK`, `DE`, `FR`, `JP`, `CA`, `AU`, `NL`, `ES`, `IT` |
| `CHROME_CHANNEL` | Playwright channel | `chromium`, `chrome`, `chrome-beta`, `msedge` |
| `BLOCKED_TYPES` | Blocked resource types | List: `image`, `font`, `media`, `stylesheet` |

See [`config.defaults.yaml`](config.defaults.yaml) for the full list and defaults.

### Component `config.yaml` (UI submission)

Each component lives at `browser-bot/sites/<site>/<component>/config.yaml`. This file defines how browser-bot interacts with the target UI for **Run Tests**, **Sample Request**, and **Discovery**. **Discovery**, **Settings → Save**, and new component creation all write this file with inline comments (see [`browser_bot/component_config_yaml.py`](browser-bot/browser_bot/component_config_yaml.py)). Full annotated example: [`browser-bot/sites/localhost:3000/main/config.yaml`](browser-bot/sites/localhost:3000/main/config.yaml).

| Field | Required | Description |
|-------|----------|-------------|
| `login_url` | Recommended | URL opened for **Add Login** (`http://localhost:…` or `https://…`) |
| `submission.start_url` | Yes | Page with the chat / prompt UI |
| `submission.inputs` | Yes | List of `{selector, type}` fields to fill before submit (in order) |
| `submission.submit_selector` | Yes | Button or control that sends the prompt |
| `submission.response_selector` | Yes | Container where assistant output appears |
| `submission.submit_via` | No | `click` (default) or `enter` |
| `submission.response_wait_ms` | No | Ms to wait for response after submit (default 5000) |
| `submission.response_within_selector` | No | Descendant under response root; last visible match wins |
| `submission.response_text_within_selector` | No | Narrower node for text extraction (e.g. `"> p"`) |
| `submission.mode` | No | `single` or `multi` for multi-turn / batched test suites |
| `settings` | No | Browser/cache overrides — same keys as [`config.defaults.yaml`](config.defaults.yaml) |

**`submission.inputs` types:** `text`, `textarea`, `contenteditable`, `password`, `email`, `search`, `select`, `combobox`, `checkbox`, `radio`.

### API submission (`transport: api`)

When the target exposes a chat/completion HTTP API (e.g. test target `POST /api/chat`), use **Connect Target → API endpoint** or set in component config:

```yaml
submission:
  transport: api
  api_url: http://localhost:3000/api/chat
  api_method: POST
  api_body:
    prompt: "{{prompt}}"
  api_response_path: response
```

- `api_body` — JSON template; `{{prompt}}` is replaced with each test prompt
- `api_response_path` — dot path into the JSON response (e.g. `response`, `data.message`)
- `api_headers` — optional; merged with saved auth headers/cookies from **Add Login**

Run Tests and Sample Request use direct HTTP (no browser) when `transport: api`. Prompts run concurrently up to **`API_CONCURRENCY`** (default 8; Settings → Browser Config or component `settings:` override). Set to `1` for fully sequential runs with the evasion delay between requests.

**Site-level config** (`browser-bot/sites/<site>/config.yaml`) can hold shared `login_url`, `refresh_url`, `refresh_mode` (`cookie` \| `both`), and `settings:` that apply to every component on that site unless overridden. **Discovery** creates this file automatically (with inline comments) if it does not exist yet — see [`browser_bot/site_config_yaml.py`](browser-bot/browser_bot/site_config_yaml.py).

## Quick start (interactive menu)

With the venv active (`source airta-venv/bin/activate`, or after running `start.py` once):

```bash
python main.py
```

This launches the interactive terminal menu: select a site, then a component, then choose from the pipeline steps. You can also create new sites and components from the menu.

## Commands (direct CLI)

All commands are also available as direct subcommands for scripting and CI.

### Generate tests

Generate adversarial test prompts from rubrics using a configurable prompting strategy.

```bash
# One strategy + one framework
python main.py generate --strategy zero_shot --framework oecd

# All frameworks for a strategy
python main.py generate --strategy zero_shot --all-frameworks

# All strategies for a framework
python main.py generate --framework eu_ai_act --all-strategies

# Everything: all strategies x all frameworks
python main.py generate --all

# With component rubric context
python main.py generate --strategy zero_shot --framework eu_ai_act --component-rubric path/to/rubric.json
```

Output is written to `generate-tests/<strategy>/<framework>.json`.

### Discover (browser-bot setup)

Interactive terminal menu for browser-bot: log in to a target site, create component configs (input/submit selectors), and manage saved sites.

```bash
python main.py discover
```

This opens the browser-bot menu where you can:
1. Add login (open browser, log in, save auth state)
2. Create component config (record input fields and submit button)
3. Remove sites
4. Manage tokens and site/component selection

### Run tests

Run a generated test suite against a configured browser target. Copies the suite into browser-bot's posts directory, executes via Playwright, and converts the run log into a compliance log for risk assessment.

```bash
# Run tests with interactive site/component picker
python main.py run generate-tests/zero-shot/eu-ai-act.json

# Specify site and component directly
python main.py run generate-tests/zero-shot/oecd.json --site chatgpt.com --component chat

# Run tests and immediately assess risk
python main.py run generate-tests/zero-shot/eu-ai-act.json --assess
```

The command auto-detects single-shot vs multi-shot suites. After the run, a `compliance_log.json` is written next to the browser-bot run log. With `--assess`, a `pipeline_report.json` is also generated.

### Risk assessment

Run multi-expert + judge risk assessment on a compliance log.

```bash
python main.py risk-assess path/to/compliance_log.json

# Also copy the report elsewhere
python main.py risk-assess path/to/compliance_log.json --report-dir ./reports
```

Writes `pipeline_report.json` alongside the compliance log.

### Export to AIRTA Systems

Export a pipeline report to AIRTA Systems via the bulk-import API.

```bash
python main.py export path/to/pipeline_report.json \
  --host app.airtasystems.com \
  --api-key YOUR_KEY \
  --program-id PROGRAM_ID
```

Credentials can also be set via `AIRTASYSTEMS_HOST`, `AIRTASYSTEMS_API_KEY`, `AIRTASYSTEMS_PROGRAM_ID` env vars.

### Direct generator usage

The generator can also be run directly with more options:

```bash
python generate-tests/generator.py --strategy zero_shot --framework eu_ai_act
python generate-tests/generate_all.py  # generate all missing strategy x framework files
```

## Strategies

| Strategy | Description |
|----------|-------------|
| `zero_shot` | Single-prompt adversarial tests |
| `multi_shot` | Multi-turn sequential conversations |
| `few_shot` | Example-based prompts |
| `iterative` | Multi-turn refinement |
| `chain_of_thought` | CoT-style reasoning prompts |
| `prompt_chaining` | Multi-step chained prompts |
| `tree_of_thoughts` | ToT-style exploration |
| `self_consistency` | Multiple runs for consistency |
| `self_reflection` | Review/revise pattern |
| `directional_stimulus` | Steering/hint-based prompts |

## Pipeline flow

```
generate  →  discover  →  run  →  risk-assess  →  export
(rubrics)    (auth+config)  (browser)  (multi-expert)   (AIRTA Systems)
```

1. **Generate** creates adversarial test suites from compliance rubrics.
2. **Discover** sets up browser-bot: login, record input/submit selectors.
3. **Run** submits the generated prompts to the live target and captures responses.
4. **Risk-assess** evaluates each prompt/response pair with multi-expert analysis.
5. **Export** pushes the pipeline report to AIRTA Systems.

## Project layout

- `start.py` — Bootstrap script: create venv, install deps, launch web UI.
- `main.py` — CLI entry point: `generate`, `discover`, `run`, `risk-assess`, `export`.
- `web/` — FastAPI backend and web UI.
- `generate-tests/` — Test generation: `generator.py`, `core.py`, `strategies/`.
- `browser-bot/` — Browser automation: Playwright-based test runner with tiered fetchers.
- `risk-level-agent/` — Multi-expert + judge LangGraph agent for risk levels.
- `pipeline/` — `risk_assess.py`, `convert_log.py`, `export_airta.py`.
- `rubrics/` — Compliance framework rubrics (EU AI Act, OWASP, FRIA, MITRE, NIST, etc.).
- `test-target/` — Local mock chat app for browser-bot automation (`python test-target/app.py`).
- `docs/` — Additional guides (e.g. `TEST_TARGET_APP_GUIDE.md` for building a local test target app).

## Troubleshooting

### Port is taken

```bash
lsof -i :8000
ss -tulnp | grep 8000
sudo ss -tulnp | grep 8000
kill -9 [pid]
```

The web app also auto-selects the next free port if 8000 is in use.

