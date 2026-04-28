# AIRTA — AI Red Team Attack Suite

Generate adversarial test suites from compliance rubrics, run them against live targets via browser automation, and assess risk with multi-expert analysis.

## Requirements

- Python 3.10+

```bash
pip install -r requirements.txt
```

## Configuration

- **`.config`** — Non-sensitive settings: `GEMINI_MODEL`, `GEMINI_JUDGE`. Same format as `.env` (`KEY=value`, `#` comments).
- **`.env`** — Secrets only (e.g. `GEMINI_API_KEY`). Loaded after `.config` so it can override.

## Quick start (web UI)

```bash
python -m web.app
python web/app.py
uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
```

Then open http://localhost:8000. The web UI (FastAPI + SPA) wraps the full pipeline — generate, discover, run, risk-assess, export — with a browser-based interface. API docs are available at `/api/docs`.

Run from the repo root so `.config` and `.env` are picked up.


## Quick start (interactive menu)

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

- `main.py` — CLI entry point: `generate`, `discover`, `run`, `risk-assess`, `export`.
- `generate-tests/` — Test generation: `generator.py`, `core.py`, `strategies/`.
- `browser-bot/` — Browser automation: Playwright-based test runner with tiered fetchers.
- `risk-level-agent/` — Multi-expert + judge LangGraph agent for risk levels.
- `pipeline/` — `risk_assess.py`, `convert_log.py`, `export_airta.py`.
- `rubrics/` — Compliance framework rubrics (EU AI Act, OWASP, FRIA, MITRE, NIST, etc.).

# Troubleshooting
- Port is taken? 

```bash
lsof -i :8000
ss -tulnp | grep 8000
sudo ss -tulnp | grep 8000
kill -9 [pid]
```
