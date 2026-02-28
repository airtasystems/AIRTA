"""
FastAPI app for AIRTA pipeline. Run with Gunicorn + Uvicorn worker:

  gunicorn ui.api:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000

Endpoints: config, test files, run pipeline, list reports.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Project root = parent of ui/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Load main's path setup and run_pipeline
import main

app = FastAPI(title="AIRTA API", description="Discovery, diagnostics, compliance tests, risk assessment")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class PipelineRequest(BaseModel):
    component: str = "default"
    strategy: str = "zero_shot"
    framework: str = "eu_ai_act"
    skip_discovery: bool = False
    skip_diagnostics: bool = False
    force_discovery: bool = False
    test_file: str | None = None
    report_dir: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def get_config():
    """Return discovery config (after path setup)."""
    main._setup_paths(ROOT)
    try:
        from component_discovery import config as discovery_config
        return {
            "component": discovery_config.COMPONENT,
            "base_url": discovery_config.BASE_URL,
            "site_state_dir": str(discovery_config.SITE_STATE_DIR),
            "discovered_endpoint_file": str(discovery_config.DISCOVERED_ENDPOINT_FILE),
            "auth_state_file": str(discovery_config.AUTH_STATE_FILE),
            "has_discovered_endpoint": discovery_config.DISCOVERED_ENDPOINT_FILE.exists(),
            "has_auth_state": discovery_config.AUTH_STATE_FILE.exists(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _list_test_files():
    """List available strategy/framework test JSON files."""
    base = ROOT / "generate-tests"
    if not base.exists():
        return []
    out = []
    for strategy_dir in sorted(base.iterdir()):
        if not strategy_dir.is_dir():
            continue
        strategy = strategy_dir.name.replace("-", "_")
        for f in strategy_dir.glob("*.json"):
            framework = f.stem.replace("-", "_")
            out.append({"strategy": strategy, "framework": framework, "path": str(f)})
    return out


@app.get("/test-files")
def list_test_files():
    return {"test_files": _list_test_files()}


@app.post("/run/pipeline")
def run_pipeline_endpoint(body: PipelineRequest):
    """
    Run the full pipeline (discovery optional, diagnostics, compliance tests, risk assessment).
    Blocks until complete. For long runs, consider running from CLI or Streamlit in-process.
    """
    main._setup_paths(ROOT)
    os.environ["COMPONENT"] = body.component
    test_file = None
    if body.test_file:
        p = Path(body.test_file)
        test_file = p if p.is_absolute() else ROOT / p
    report_dir = Path(body.report_dir) if body.report_dir else None
    if report_dir and not report_dir.is_absolute():
        report_dir = ROOT / report_dir
    args = SimpleNamespace(
        component=body.component,
        strategy=body.strategy,
        framework=body.framework,
        test_file=test_file,
        report_dir=report_dir,
        skip_discovery=body.skip_discovery,
        skip_diagnostics=body.skip_diagnostics,
        force_discovery=body.force_discovery,
    )
    try:
        report = main.run_pipeline(args)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if report is None:
        raise HTTPException(status_code=422, detail="Pipeline failed (see server logs)")
    return report


def _list_reports():
    """List pipeline_report.json under component-discovery/<sitename>/<component>/logs/."""
    cd = ROOT / "component-discovery"
    if not cd.exists():
        return []
    reports = []
    for site_dir in cd.iterdir():
        if not site_dir.is_dir() or site_dir.name.startswith("."):
            continue
        for comp_dir in site_dir.iterdir():
            if not comp_dir.is_dir() or comp_dir.name == "site_config":
                continue
            logs = comp_dir / "logs"
            if not logs.exists():
                continue
            for run_dir in sorted(logs.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                rp = run_dir / "pipeline_report.json"
                if rp.exists():
                    try:
                        data = json.loads(rp.read_text(encoding="utf-8"))
                        data["_path"] = str(rp)
                        data["_run_dir"] = str(run_dir)
                        reports.append(data)
                    except Exception:
                        pass
    return reports


@app.get("/reports")
def list_reports():
    return {"reports": _list_reports()}


@app.get("/reports/{timestamp}")
def get_report(timestamp: str):
    """Get a single report by timestamp (e.g. 2026-02-28T10-05-00)."""
    for r in _list_reports():
        if r.get("timestamp") == timestamp:
            return r
    raise HTTPException(status_code=404, detail="Report not found")
