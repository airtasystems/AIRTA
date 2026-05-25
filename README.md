# AIRTA — AI Risk Testing Agent

**Automated regulatory compliance testing for AI systems.** Generate framework-aligned test suites, exercise live UIs and APIs, and produce multi-expert risk assessments — from one open-source pipeline.

AIRTA is a free, production-ready **AI Risk Testing Agent**. Point it at your chatbot, copilot, or API; build structured compliance tests from rubrics such as the EU AI Act and OECD; run them through browser automation with saved auth and live previews; then assess every response against regulatory mandates. Export structured reports to [AIRTA Systems](https://airtasystems.com) when you are ready to operationalize results.

```bash
python start.py
# → http://localhost:8000
```

---

## Why teams use AIRTA

| Capability | What you get |
|------------|----------------|
| **Rubric-grounded tests** | Compliance suites generated from JSON regulatory frameworks — aligned to mandates, not ad-hoc prompts. |
| **Real target interaction** | Playwright drives the same UI your users see: login, cookies, blockers, multi-turn flows. |
| **API or browser** | One component config: `transport: ui` for SaaS apps, `transport: api` for HTTP chat endpoints. |
| **10 prompting strategies** | Zero-shot through tree-of-thoughts, few-shot, self-reflection, and more — each tuned for regulatory scenarios. |
| **Risk pipeline** | Compliance logs → multi-expert LangGraph assessment → mandate rollups → export. |
| **Operator-first UI** | FastAPI web app: discover selectors, save auth, run suites, watch live browser previews, resolve login walls. |
| **Runs anywhere** | Single `start.py` bootstraps venv, Chromium, and the server. CLI subcommands for CI and scripting. |

You own the stack: local execution, your API keys, your targets, your logs under `browser-bot/sites/<site>/<component>/logs/`.

---

## Production-ready by design

- **Persistent auth** — Session cookies, localStorage, and sessionStorage captured to `auth.json` and replayed on every new browser context (including multi-origin dashboards).
- **Tiered fetchers** — Pool, cluster, and human-mimic browser tiers with stealth, locale, and retry handling.
- **Parallel test runs** — Configurable concurrency with per-slot live screenshots during suite execution.
- **Login & blocker handling** — Detects login walls, rate limits, and cookie banners; guides re-auth from the UI.
- **Layered config** — Global defaults → site → component overrides; documented in YAML with inline comments.
- **Local smoke target** — [`test-target/`](test-target/) mock chat app for end-to-end validation without an external SaaS.

---

## How it works

```
  rubrics/          generate-tests/       browser-bot/          pipeline/
  (EU AI Act,        compliance JSON   →   Playwright runs  →  compliance_log.json
   OECD, NIST…)                          + auth + logs            │
                                                                    ▼
                                                          risk-level-agent/
                                                          pipeline_report.json
                                                                    │
                                                                    ▼
                                                          export → AIRTA Systems
```

1. **Generate** — LLM builds regulatory compliance prompts from a framework + strategy.
2. **Discover** — Record login, input, submit, and response selectors (or wire an API).
3. **Run** — Submit every prompt; capture responses and HTTP metadata.
4. **Risk-assess** — Multi-expert + judge scoring per mandate.
5. **Export** — Push structured reports to your AIRTA Systems program (optional).

---

## Quick start

**Requirements:** Python 3.10+, network for LLM calls (`GEMINI_API_KEY` in `.env`).

```bash
git clone <your-repo-url>
cd AIRTA
python start.py
```

Open the URL printed in the terminal (default **http://localhost:8000**). API reference: `/api/docs`.

`start.py` creates `airta-venv/`, installs dependencies, runs `playwright install chromium` once, and starts the web UI.

### Try it locally (no external SaaS)

```bash
python test-target/app.py          # mock chat on :3000
# In the UI: site localhost:3000, component test-target — see test-target/README.md
```

### Manual setup

```bash
python -m venv airta-venv && source airta-venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python web/app.py
```

Run from the repo root so `.config` and `.env` load correctly.

---

## Web UI workflow

1. **Create site** — Domain or URL (e.g. `www.example.com`).
2. **Add login** — Browser opens with saved auth; sign in; **Save auth**.
3. **Connect target** — Discovery (UI) or API endpoint; AIRTA writes `config.yaml`.
4. **Generate tests** — Pick strategy + framework (EU AI Act, OECD, FRIA, NIST, PLD, …).
5. **Run tests** — Live previews, compliance log, optional inline risk assessment.
6. **Export** — Send `pipeline_report.json` to AIRTA Systems when configured.

Terminal users: `python main.py` for the interactive menu, or direct subcommands below.

---

## Strategies & frameworks

**Strategies** (under `generate-tests/strategies/`):

| Strategy | Pattern |
|----------|---------|
| `zero_shot` | Single compliance prompt |
| `multi_shot` | Multi-turn regulatory dialogue |
| `few_shot` | In-context examples before the test case |
| `iterative` | Refinement across turns |
| `chain_of_thought` | Reasoning-style prompts |
| `prompt_chaining` | Chained steps |
| `tree_of_thoughts` | Branching exploration |
| `self_consistency` | Multiple samples |
| `self_reflection` | Critique-and-revise |
| `directional_stimulus` | Stimulus-guided framing |

**Frameworks** (under `rubrics/`): `eu_ai_act`, `oecd`, `nist_ai_rmf`, `fria_core`, `fria_extended`, `pld`, and more.

```bash
python main.py generate --strategy zero_shot --framework eu_ai_act
python main.py generate --all
```

---

## CLI reference

| Command | Purpose |
|---------|---------|
| `python main.py` | Interactive site/component menu |
| `python main.py generate …` | Build compliance test suites from rubrics |
| `python main.py discover` | Browser-bot login & config menu |
| `python main.py run <suite.json> --site S --component C` | Execute suite against target |
| `python main.py run … --assess` | Run + risk assessment in one step |
| `python main.py risk-assess <compliance_log.json>` | Assess existing log |
| `python main.py export <pipeline_report.json> …` | Upload to AIRTA Systems |

Example:

```bash
python main.py run generate-tests/zero_shot/eu_ai_act.json \
  --site chatgpt.com --component chat --assess
```

---

## Configuration

| Layer | Location |
|-------|----------|
| Defaults | [`config.defaults.yaml`](config.defaults.yaml) |
| Secrets / cache | [`.env`](.env) |
| LLM model | [`.config`](.config) |
| Site | `browser-bot/sites/<site>/config.yaml` |
| Component | `browser-bot/sites/<site>/<component>/config.yaml` |

See [`config.defaults.yaml`](config.defaults.yaml) and annotated examples under `browser-bot/sites/`.

---

## Project layout

| Path | Role |
|------|------|
| `start.py` | Bootstrap venv + launch web UI |
| `main.py` | CLI: generate, discover, run, risk-assess, export |
| `web/` | FastAPI backend + SPA |
| `generate-tests/` | Rubric → compliance suite generation |
| `browser-bot/` | Playwright runner, sites, auth, fetchers |
| `risk-level-agent/` | Multi-expert + judge assessment |
| `pipeline/` | Log conversion, export helpers |
| `rubrics/` | Regulatory framework JSON |
| `test-target/` | Local mock chat for smoke tests |

---

## License

AIRTA is **free and open source** under the [AIRTA License](LICENSE):

- **Free use** — Run AIRTA as-is for any purpose, including commercial production.
- **No commercial modification** — You may not modify the software and use or distribute those modifications commercially.
- **Unmodified redistribution** — Share copies freely with the license intact.

See [LICENSE](LICENSE) for full terms.

---

## Troubleshooting

**Port in use** — The app auto-picks the next free port, or free 8000 with `lsof -i :8000` and `kill`.

**Login / auth** — Re-run **Add login** if sessions expire; site folder name must match the URL host (e.g. `www.example.com`).

**Chromium** — `start.py` installs Playwright Chromium automatically.

---

**AIRTA — AI Risk Testing Agent.** Regulatory compliance testing, real browsers, structured risk reports. Run `python start.py` and start validating your AI systems.
