"""AIRTA Web UI — FastAPI backend."""

from __future__ import annotations

import ast as _ast
import json
import os
import re as _re
import socket
import sys
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_root = Path(__file__).resolve().parent.parent
_bb_dir = _root / "browser-bot"

if str(_bb_dir) not in sys.path:
    sys.path.insert(0, str(_bb_dir))
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from browser_bot.auth_state import auth_config_exists, load_auth_config
from browser_bot.sites import (
    ensure_component_dir,
    ensure_site_dir,
    get_component_path,
    get_component_rubric_path,
    get_domain_from_url,
    get_site_company_rubric_path,
    list_components,
    list_sites,
    load_component_config,
    load_component_config_raw,
    remove_site,
    save_component_config,
)

from web.jobs import cancel_job, get_job, list_jobs, send_stdin, start_job, stream_job

STRATEGIES = [
    "zero_shot", "multi_shot", "few_shot", "iterative", "chain_of_thought",
    "prompt_chaining", "tree_of_thoughts", "self_consistency", "self_reflection",
    "directional_stimulus",
]

app = FastAPI(title="AIRTA", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Static / SPA fallback
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).resolve().parent / "static"
_img_dir = Path(__file__).resolve().parent / "IMG"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
if _img_dir.is_dir():
    app.mount("/img", StaticFiles(directory=str(_img_dir)), name="img")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_static_dir / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Data endpoints
# ---------------------------------------------------------------------------

@app.get("/api/sites")
async def api_list_sites():
    return list_sites()


class CreateSiteBody(BaseModel):
    domain: str


class RenameSiteBody(BaseModel):
    domain: str


@app.post("/api/sites")
async def api_create_site(body: CreateSiteBody):
    domain = get_domain_from_url(body.domain) if "://" in body.domain else body.domain
    domain = domain.rstrip("/")
    ensure_site_dir(domain)
    return {"ok": True, "domain": domain}


@app.patch("/api/sites/{site}")
async def api_rename_site(site: str, body: RenameSiteBody):
    domain = get_domain_from_url(body.domain) if "://" in body.domain else body.domain
    domain = domain.rstrip("/")
    if not domain:
        raise HTTPException(400, "Domain is required")
    src = _bb_dir / "sites" / site
    dst = _bb_dir / "sites" / domain
    if not src.is_dir():
        raise HTTPException(404, "Site not found")
    if dst.exists() and dst != src:
        raise HTTPException(409, "Site already exists")
    src.rename(dst)
    return {"ok": True, "domain": domain}


@app.delete("/api/sites/{site}")
async def api_delete_site(site: str):
    if remove_site(site):
        return {"ok": True}
    raise HTTPException(404, "Site not found")


@app.get("/api/sites/{site}/auth-status")
async def api_auth_status(site: str):
    if not auth_config_exists(site):
        return {"configured": False}
    config = load_auth_config(site)
    has_cookies = bool(config and config.get("cookies"))
    has_storage = any(
        o.get("localStorage") or o.get("sessionStorage")
        for o in (config or {}).get("origins", [])
    )
    return {"configured": has_cookies or has_storage}


@app.get("/api/sites/{site}/components")
async def api_list_components(site: str):
    return list_components(site)


class CreateComponentBody(BaseModel):
    name: str


class RenameComponentBody(BaseModel):
    name: str


@app.post("/api/sites/{site}/components")
async def api_create_component(site: str, body: CreateComponentBody):
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in body.name).strip("_") or "default"
    ensure_component_dir(site, name)
    return {"ok": True, "name": name}


@app.patch("/api/sites/{site}/components/{component}")
async def api_rename_component(site: str, component: str, body: RenameComponentBody):
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in body.name).strip("_")
    if not name:
        raise HTTPException(400, "Component name is required")
    src = get_component_path(site, component)
    dst = get_component_path(site, name)
    if not src.is_dir():
        raise HTTPException(404, "Component not found")
    if dst.exists() and dst != src:
        raise HTTPException(409, "Component already exists")
    src.rename(dst)
    return {"ok": True, "name": name}


@app.delete("/api/sites/{site}/components/{component}")
async def api_delete_component(site: str, component: str):
    import shutil

    p = get_component_path(site, component)
    if not p.is_dir():
        raise HTTPException(404, "Component not found")
    shutil.rmtree(p)
    return {"ok": True}


@app.get("/api/sites/{site}/{component}/config")
async def api_component_config(site: str, component: str):
    return load_component_config_raw(site, component)


class SaveComponentConfigBody(BaseModel):
    config: dict

@app.post("/api/sites/{site}/{component}/config")
async def api_save_component_config(site: str, component: str, body: SaveComponentConfigBody):
    save_component_config(site, component, body.config)
    return {"ok": True}


class RubricBody(BaseModel):
    content: dict


def _global_company_rubric() -> Path | None:
    p = _root / "rubrics" / "company.json"
    return p if p.is_file() else None


def _global_component_rubric() -> Path | None:
    p = _root / "rubrics" / "component.json"
    return p if p.is_file() else None


@app.get("/api/sites/{site}/company-rubric")
async def api_get_company_rubric(site: str):
    """Return per-site company.json, falling back to global rubrics/company.json."""
    p = get_site_company_rubric_path(site) or _global_company_rubric()
    if not p:
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


@app.put("/api/sites/{site}/company-rubric")
async def api_put_company_rubric(site: str, body: RubricBody):
    """Save per-site company.json."""
    ensure_site_dir(site)
    p = _bb_dir / "sites" / site / "company.json"
    p.write_text(json.dumps(body.content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(p)}


@app.get("/api/sites/{site}/{component}/component-rubric")
async def api_get_component_rubric(site: str, component: str):
    """Return per-component component.json, falling back to global rubrics/component.json."""
    p = get_component_rubric_path(site, component) or _global_component_rubric()
    if not p:
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


@app.put("/api/sites/{site}/{component}/component-rubric")
async def api_put_component_rubric(site: str, component: str, body: RubricBody):
    """Save per-component component.json."""
    ensure_component_dir(site, component)
    p = _bb_dir / "sites" / site / component / "component.json"
    p.write_text(json.dumps(body.content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(p)}


def _fetch_url_html(url: str) -> str:
    """Fetch a URL and return the response body as text (no browser, best-effort)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        charset = "utf-8"
        ct = resp.headers.get_content_charset()
        if ct:
            charset = ct
        return resp.read().decode(charset, errors="replace")


class GenerateRubricBody(BaseModel):
    url: str


@app.post("/api/sites/{site}/company-rubric/generate")
async def api_generate_company_rubric(site: str, body: GenerateRubricBody):
    """Fetch a URL, extract company context via Gemini, save & return company.json."""
    try:
        html = _fetch_url_html(body.url)
    except Exception as exc:
        raise HTTPException(400, f"Failed to fetch URL: {exc}")

    bb_path = str(_bb_dir)
    if bb_path not in sys.path:
        sys.path.insert(0, bb_path)
    try:
        from browser_bot.rubric_discovery import generate_company_json
    except ImportError as exc:
        raise HTTPException(500, f"rubric_discovery module unavailable: {exc}")

    result = generate_company_json(html, body.url)
    if not result:
        raise HTTPException(500, "LLM returned empty result")

    ensure_site_dir(site)
    p = _bb_dir / "sites" / site / "company.json"
    p.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "content": result}


@app.post("/api/sites/{site}/{component}/component-rubric/generate")
async def api_generate_component_rubric(site: str, component: str, body: GenerateRubricBody):
    """Fetch a URL, extract component context via Gemini (+ company.json context), save & return component.json."""
    try:
        html = _fetch_url_html(body.url)
    except Exception as exc:
        raise HTTPException(400, f"Failed to fetch URL: {exc}")

    bb_path = str(_bb_dir)
    if bb_path not in sys.path:
        sys.path.insert(0, bb_path)
    try:
        from browser_bot.rubric_discovery import generate_component_json
    except ImportError as exc:
        raise HTTPException(500, f"rubric_discovery module unavailable: {exc}")

    # Load company.json to ground the component rubric in real company context
    company_data: dict | None = None
    company_p = _bb_dir / "sites" / site / "company.json"
    if company_p.is_file():
        try:
            company_data = json.loads(company_p.read_text(encoding="utf-8"))
        except Exception:
            pass

    result = generate_component_json(html, body.url, component, company_data=company_data)
    if not result:
        raise HTTPException(500, "LLM returned empty result")

    ensure_component_dir(site, component)
    p = _bb_dir / "sites" / site / component / "component.json"
    p.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "content": result}


@app.get("/api/strategies")
async def api_strategies():
    return STRATEGIES


@app.get("/api/frameworks")
async def api_frameworks():
    rubrics_dir = _root / "rubrics"
    if not rubrics_dir.is_dir():
        return []
    out = []
    for p in sorted(rubrics_dir.glob("*.json")):
        if p.stem in ("company", "component"):
            continue
        out.append(p.stem.replace("-", "_"))
    return out


def _pretty(slug: str) -> str:
    short = {"eu", "ai", "uk", "us"}
    long_ = {"oecd", "gdpr", "iso"}
    parts = slug.replace("_", "-").split("-")
    words = []
    for p in parts:
        if not p:
            continue
        pl = p.lower()
        if pl in short or pl in long_:
            words.append(p.upper())
        else:
            words.append(p.capitalize())
    return " ".join(words)


@app.get("/api/sites/{site}/{component}/strategies")
async def api_component_strategies(site: str, component: str):
    tests = _bb_dir / "sites" / site / component / "tests"
    if not tests.is_dir():
        return []
    return [
        {"slug": p.name, "label": _pretty(p.name)}
        for p in sorted(tests.iterdir())
        if p.is_dir() and any(p.glob("*.json"))
    ]


@app.get("/api/sites/{site}/{component}/all-frameworks")
async def api_all_frameworks(site: str, component: str):
    """Return unique framework stems available across all strategy test dirs."""
    tests = _bb_dir / "sites" / site / component / "tests"
    if not tests.is_dir():
        return []
    stems: set[str] = set()
    for strat_dir in tests.iterdir():
        if strat_dir.is_dir():
            for f in strat_dir.glob("*.json"):
                stems.add(f.stem)
    return [{"slug": s, "label": _pretty(s)} for s in sorted(stems)]


@app.get("/api/sites/{site}/{component}/strategies/{strategy}/frameworks")
async def api_strategy_frameworks(site: str, component: str, strategy: str):
    d = _bb_dir / "sites" / site / component / "tests" / strategy
    if not d.is_dir():
        return []
    return [
        {"slug": p.stem, "label": _pretty(p.stem), "path": str(p)}
        for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    ]


@app.get("/api/sites/{site}/{component}/tests/{strategy}/{framework}")
async def api_get_test_file(site: str, component: str, strategy: str, framework: str):
    p = _bb_dir / "sites" / site / component / "tests" / strategy / f"{framework}.json"
    if not p.exists():
        raise HTTPException(404, "Test file not found")
    return json.loads(p.read_text(encoding="utf-8"))


class TestFileBody(BaseModel):
    data: dict


class ImportZeroShotTestsBody(BaseModel):
    filename: str
    data: dict | list


def _slugify_test_filename(name: str) -> str:
    stem = Path(name).stem
    slug = _re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_").lower()
    return slug or "imported_prompts"


def _normalize_prompt(raw, idx: int) -> dict:
    if isinstance(raw, str):
        prompt = raw.strip()
        description = "Imported zero-shot prompt"
        prompt_id = f"imported-zs-{idx:03d}"
    elif isinstance(raw, dict):
        prompt = str(raw.get("prompt") or raw.get("text") or raw.get("content") or "").strip()
        description = str(raw.get("description") or "Imported zero-shot prompt").strip()
        prompt_id = str(raw.get("id") or f"imported-zs-{idx:03d}").strip()
    else:
        raise HTTPException(400, f"Prompt {idx} must be a string or object")
    if not prompt:
        raise HTTPException(400, f"Prompt {idx} is empty")
    if not prompt_id:
        prompt_id = f"imported-zs-{idx:03d}"
    return {"id": prompt_id, "description": description, "prompt": prompt}


def _normalize_zero_shot_suite(data, filename: str) -> dict:
    if isinstance(data, dict) and isinstance(data.get("mandates"), list):
        mandates = data["mandates"]
        for mandate_idx, mandate in enumerate(mandates, start=1):
            if not isinstance(mandate, dict) or not isinstance(mandate.get("prompts"), list):
                raise HTTPException(400, f"Mandate {mandate_idx} must include a prompts list")
            mandate["prompts"] = [
                _normalize_prompt(prompt, prompt_idx)
                for prompt_idx, prompt in enumerate(mandate["prompts"], start=1)
            ]
        return {
            "framework": data.get("framework") or Path(filename).stem,
            "description": data.get("description") or "Imported zero-shot prompts",
            "mandates": mandates,
        }

    prompts = data.get("prompts") if isinstance(data, dict) else data
    if not isinstance(prompts, list) or not prompts:
        raise HTTPException(400, "JSON must be a prompt array, an object with prompts, or a full test suite")

    framework = (data.get("framework") or Path(filename).stem) if isinstance(data, dict) else Path(filename).stem
    description = (data.get("description") or "Imported zero-shot prompts") if isinstance(data, dict) else "Imported zero-shot prompts"
    return {
        "framework": framework,
        "description": description,
        "mandates": [
            {
                "mandate": data.get("mandate", "Imported zero-shot prompts") if isinstance(data, dict) else "Imported zero-shot prompts",
                "focus": data.get("focus", "Imported") if isinstance(data, dict) else "Imported",
                "prompts": [
                    _normalize_prompt(prompt, idx)
                    for idx, prompt in enumerate(prompts, start=1)
                ],
            }
        ],
    }


@app.put("/api/sites/{site}/{component}/tests/{strategy}/{framework}")
async def api_put_test_file(site: str, component: str, strategy: str, framework: str, body: TestFileBody):
    p = _bb_dir / "sites" / site / component / "tests" / strategy / f"{framework}.json"
    if not p.exists():
        raise HTTPException(404, "Test file not found")
    p.write_text(json.dumps(body.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True}


@app.post("/api/sites/{site}/{component}/tests/import-zero-shot")
async def api_import_zero_shot_tests(site: str, component: str, body: ImportZeroShotTestsBody):
    ensure_component_dir(site, component)
    filename = _slugify_test_filename(body.filename)
    suite = _normalize_zero_shot_suite(body.data, body.filename)
    tests_dir = _bb_dir / "sites" / site / component / "tests" / "zero-shot"
    tests_dir.mkdir(parents=True, exist_ok=True)
    p = tests_dir / f"{filename}.json"
    p.write_text(json.dumps(suite, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "strategy": "zero-shot", "framework": filename, "path": str(p), "data": suite}


@app.get("/api/sites/{site}/{component}/logs")
async def api_component_logs(site: str, component: str):
    logs_dir = get_component_path(site, component) / "logs"
    if not logs_dir.is_dir():
        return {"runs": [], "compliance": [], "reports": []}

    def _label(p: Path) -> str:
        """Human-friendly label: prefer parent dir name (timestamp) over bare filename."""
        if p.parent != logs_dir:
            return f"{p.parent.name} / {p.name}"
        return p.name

    # New-style: logs/{timestamp}/run_log.json
    # Old-style: logs/run_{timestamp}.json (backward compat)
    runs = sorted(
        list(logs_dir.glob("*/run_log.json")) + list(logs_dir.glob("run_*.json")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    compliance = sorted(
        list(logs_dir.glob("*/compliance_log.json")) + list(logs_dir.glob("compliance_log*.json")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    reports = sorted(
        list(logs_dir.glob("*/pipeline_report.json")) + list(logs_dir.glob("pipeline_report*.json")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return {
        "runs": [{"name": _label(p), "path": str(p)} for p in runs],
        "compliance": [{"name": _label(p), "path": str(p)} for p in compliance],
        "reports": [{"name": _label(p), "path": str(p)} for p in reports],
    }


_CONFIG_PY = _bb_dir / "browser_bot" / "config.py"

_EDITABLE_VARS = {
    "FETCH_METHOD", "POOL_SIZE", "CONTEXT_COUNT", "PAGES_PER_CONTEXT",
    "POOL_CLUSTER_HUMAN_LIKE", "POOL_CLUSTER_ALLOW_STYLES", "POOL_CLUSTER_USE_STEALTH",
    "POOL_CLUSTER_USE_HUMAN_CHROME", "POOL_CLUSTER_USE_HUMAN_CONTEXT",
    "EVASION_REQUEST_DELAY_S", "EVASION_RETRY_WAIT_S", "EVASION_MAX_RETRIES",
    "HUMAN_COUNTRY", "HUMAN_ALLOW_STYLES", "HUMAN_READ_DELAY_MS",
    "HUMAN_SCROLL_AFTER_LOAD", "HUMAN_USER_AGENT",
    "HEADLESS", "BLOCKED_TYPES", "CHROMIUM_EXECUTABLE_PATH", "CHROME_CHANNEL",
}


def _parse_config() -> dict:
    source = _CONFIG_PY.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    result: dict = {}
    for node in _ast.walk(tree):
        # Plain assignment:  NAME = value
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id in _EDITABLE_VARS:
                    try:
                        val = _ast.literal_eval(node.value)
                        result[target.id] = sorted(val) if isinstance(val, (set, frozenset)) else val
                    except Exception:
                        pass
        # Annotated assignment:  NAME: type = value
        elif isinstance(node, _ast.AnnAssign):
            if isinstance(node.target, _ast.Name) and node.target.id in _EDITABLE_VARS and node.value is not None:
                try:
                    val = _ast.literal_eval(node.value)
                    result[node.target.id] = sorted(val) if isinstance(val, (set, frozenset)) else val
                except Exception:
                    pass
    return result


def _write_config_value(source: str, name: str, value) -> str:
    if name == "BLOCKED_TYPES":
        items = ", ".join(f'"{v}"' for v in sorted(value))
        new_repr = ("{" + items + "}") if items else "set()"
    elif isinstance(value, bool):
        new_repr = "True" if value else "False"
    elif isinstance(value, str):
        new_repr = repr(value)
    elif isinstance(value, (int, float)):
        new_repr = repr(value)
    elif isinstance(value, list):
        new_repr = repr(value)
    else:
        new_repr = repr(value)

    pattern = _re.compile(r"^(" + _re.escape(name) + r"\s*=\s*)(.*)$", _re.MULTILINE)
    return pattern.sub(lambda m: m.group(1) + new_repr, source, count=1)


@app.get("/api/config")
async def api_get_config():
    return _parse_config()


class SaveConfigBody(BaseModel):
    changes: dict

@app.post("/api/config")
async def api_save_config(body: SaveConfigBody):
    source = _CONFIG_PY.read_text(encoding="utf-8")
    for name, value in body.changes.items():
        if name not in _EDITABLE_VARS:
            raise HTTPException(400, f"Not an editable config key: {name}")
        source = _write_config_value(source, name, value)
    _CONFIG_PY.write_text(source, encoding="utf-8")
    return {"ok": True, "updated": list(body.changes.keys())}


@app.get("/api/files")
async def api_read_file(path: str):
    """Read a JSON file by absolute path (scoped to project root for safety)."""
    p = Path(path)
    if not str(p).startswith(str(_root)):
        raise HTTPException(403, "Path outside project root")
    if not p.exists():
        raise HTTPException(404, "File not found")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
async def api_list_jobs():
    return list_jobs()


class StartJobBody(BaseModel):
    type: str
    site: str = ""
    component: str = ""
    params: dict = {}

@app.post("/api/jobs")
async def api_start_job(body: StartJobBody):
    job = await start_job(body.type, body.site, body.component, body.params)
    return job.to_dict()


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    d = job.to_dict()
    d["output"] = job.output
    return d


@app.get("/api/jobs/{job_id}/stream")
async def api_stream_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return StreamingResponse(stream_job(job_id), media_type="text/event-stream")


class StdinBody(BaseModel):
    text: str = "\n"

@app.post("/api/jobs/{job_id}/stdin")
async def api_send_stdin(job_id: str, body: StdinBody):
    ok = await send_stdin(job_id, body.text)
    if not ok:
        raise HTTPException(400, "Cannot send stdin")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def api_cancel_job(job_id: str):
    ok = await cancel_job(job_id)
    if not ok:
        raise HTTPException(400, "Cannot cancel job")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export-result")
async def api_export_result(job_id: str):
    """Return a structured summary of a completed export job."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ("done", "error"):
        raise HTTPException(409, "Job not finished")

    # Parse result lines from output for a structured summary
    created = failed = total = 0
    batches: list[dict] = []
    errors: list[str] = []
    for line in job.output:
        import re as _re2
        m = _re2.search(r"total=(\d+).*?created=(\d+).*?failed=(\d+)", line)
        if m:
            total += int(m.group(1))
            created += int(m.group(2))
            failed += int(m.group(3))
        if line.startswith("[!]"):
            errors.append(line)

    return {
        "status": job.status,
        "total": total,
        "created": created,
        "failed": failed,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Credentials — stored in root .env, never returned to browser
# ---------------------------------------------------------------------------

_ENV_FILE = _root / ".env"
_GB_VARS = ("AIRTASYSTEMS_HOST", "AIRTASYSTEMS_API_KEY")


def _read_env() -> dict[str, str]:
    """Parse key=value lines from .env, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return result
    for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _write_env(updates: dict[str, str | None]) -> None:
    """Update or remove specific keys in .env without touching other lines."""
    lines: list[str] = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()

    replaced: set[str] = set()
    new_lines: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(raw)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                replaced.add(k)
                if updates[k] is not None:
                    new_lines.append(f'{k}="{updates[k]}"')
                # None → delete the line
                continue
        new_lines.append(raw)

    # Append keys that weren't already in the file
    for k, v in updates.items():
        if k not in replaced and v is not None:
            new_lines.append(f'{k}="{v}"')

    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class CredentialsBody(BaseModel):
    host: str = ""
    api_key: str = ""


@app.get("/api/credentials")
async def api_get_credentials():
    """Return saved AIRTA Systems credentials — api_key is never sent, only has_api_key flag."""
    env = _read_env()
    return {
        "host": env.get("AIRTASYSTEMS_HOST", ""),
        "has_api_key": bool(env.get("AIRTASYSTEMS_API_KEY", "")),
        # program_id is per-export but we surface a default if present in .env
        "program_id": env.get("AIRTASYSTEMS_PROGRAM_ID", ""),
    }


@app.get("/api/env-defaults")
async def api_env_defaults():
    """Return TARGET and COMPONENT from .env for auto-selecting site/component on startup."""
    env = _read_env()
    return {
        "target": env.get("TARGET", ""),
        "component": env.get("COMPONENT", ""),
    }


@app.post("/api/credentials")
async def api_save_credentials(body: CredentialsBody):
    """Persist AIRTA Systems credentials to .env. Empty string = leave existing value."""
    updates: dict[str, str | None] = {}
    if body.host:
        updates["AIRTASYSTEMS_HOST"] = body.host
    if body.api_key:
        updates["AIRTASYSTEMS_API_KEY"] = body.api_key
    if updates:
        _write_env(updates)
    env = _read_env()
    return {
        "ok": True,
        "host": env.get("AIRTASYSTEMS_HOST", ""),
        "has_api_key": bool(env.get("AIRTASYSTEMS_API_KEY", "")),
    }


@app.delete("/api/credentials")
async def api_clear_credentials():
    """Remove all AIRTA Systems credentials from .env."""
    _write_env({k: None for k in _GB_VARS})
    return {"ok": True}


@app.get("/api/log")
async def api_serve_log(path: str):
    """Return the raw JSON contents of a log file by absolute path."""
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(400, "Path must be absolute")
    if not p.exists():
        raise HTTPException(404, "File not found")
    # Safety: only allow files under the workspace root
    try:
        p.resolve().relative_to(_root.resolve())
    except ValueError:
        raise HTTPException(403, "Access denied")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _next_available_port(host: str, preferred_port: int) -> int:
    port = preferred_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                port += 1
                continue
        return port


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    preferred_port = int(os.getenv("PORT", "8000"))
    port = _next_available_port(host, preferred_port)
    if port != preferred_port:
        print(f"Port {preferred_port} is in use; starting on {port} instead.")
    uvicorn.run("web.app:app", host=host, port=port, reload=True)
