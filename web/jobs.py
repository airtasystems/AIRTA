"""Job manager — spawn, track, stream, and cancel long-running tasks."""

from __future__ import annotations

import asyncio
import io
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_root = Path(__file__).resolve().parent.parent


@dataclass
class Job:
    id: str
    type: str
    status: str  # pending | running | done | failed | cancelled
    site: str
    component: str
    params: dict
    output: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "site": self.site,
            "component": self.component,
            "params": self.params,
            "created_at": self.created_at.isoformat(),
            "output_lines": len(self.output),
        }


class _OutputCapture(io.TextIOBase):
    """File-like that appends lines to a Job's output buffer and pokes its event."""

    def __init__(self, job: Job):
        self._job = job

    def write(self, s: str) -> int:
        if s:
            for line in s.split("\n"):
                stripped = line.rstrip("\r")
                if stripped.strip():
                    self._job.output.append(stripped)
                    self._job._event.set()
        return len(s)

    def flush(self) -> None:
        pass


_jobs: dict[str, Job] = {}


def list_jobs() -> list[dict]:
    return [j.to_dict() for j in _jobs.values()]


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


async def stream_job(job_id: str):
    """Async generator yielding SSE-formatted lines as they appear."""
    job = _jobs.get(job_id)
    if not job:
        return
    cursor = 0
    while True:
        while cursor < len(job.output):
            # Embed any residual newlines as separate SSE data lines so the
            # EventSource parser receives a single logical event per line.
            text = job.output[cursor].replace("\n", "\ndata: ")
            yield f"data: {text}\n\n"
            cursor += 1
        if job.status in ("done", "failed", "cancelled"):
            yield f"event: done\ndata: {job.status}\n\n"
            return
        job._event.clear()
        try:
            await asyncio.wait_for(job._event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"


async def send_stdin(job_id: str, text: str) -> bool:
    """Write text to a subprocess job's stdin. Returns True if sent."""
    job = _jobs.get(job_id)
    if not job or not job._process or job._process.stdin is None:
        return False
    try:
        job._process.stdin.write(text.encode())
        await job._process.stdin.drain()
        return True
    except Exception:
        return False


async def cancel_job(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if not job or job.status not in ("pending", "running"):
        return False
    job.status = "cancelled"
    if job._process:
        try:
            job._process.terminate()
        except Exception:
            pass
    if job._task and not job._task.done():
        job._task.cancel()
    job._event.set()
    return True


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------

async def _run_subprocess_job(job: Job, cmd: list[str], *, cwd: str | None = None, env: dict | None = None):
    """Run a command as an async subprocess, streaming stdout line by line."""
    job.status = "running"
    job._event.set()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
            cwd=cwd or str(_root),
            env=env,
        )
        job._process = proc
        assert proc.stdout
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip("\n")
            job.output.append(line)
            job._event.set()
        await proc.wait()
        job.status = "done" if proc.returncode == 0 else "failed"
    except asyncio.CancelledError:
        job.status = "cancelled"
    except Exception as exc:
        job.output.append(f"[error] {exc}")
        job.status = "failed"
    finally:
        job._event.set()


async def _run_thread_job(job: Job, fn, *args: Any, **kwargs: Any):
    """Run a blocking function in a thread, capturing its stdout."""
    job.status = "running"
    job._event.set()

    def _wrapped():
        cap = _OutputCapture(job)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = cap  # type: ignore[assignment]
        sys.stderr = cap  # type: ignore[assignment]
        try:
            return fn(*args, **kwargs)
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    try:
        await asyncio.to_thread(_wrapped)
        job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
    except Exception as exc:
        job.output.append(f"[error] {exc}")
        job.status = "failed"
    finally:
        job._event.set()


# ---------------------------------------------------------------------------
# Public start_job dispatcher
# ---------------------------------------------------------------------------

async def start_job(job_type: str, site: str, component: str, params: dict | None = None) -> Job:
    params = params or {}
    job = Job(
        id=uuid.uuid4().hex[:12],
        type=job_type,
        status="pending",
        site=site,
        component=component,
        params=params,
    )
    _jobs[job.id] = job

    if job_type == "generate":
        job._task = asyncio.create_task(_start_generate(job))
    elif job_type == "discover":
        job._task = asyncio.create_task(_start_discover(job))
    elif job_type == "run_tests":
        job._task = asyncio.create_task(_start_run_tests(job))
    elif job_type == "risk_assess":
        job._task = asyncio.create_task(_start_risk_assess(job))
    elif job_type == "export":
        job._task = asyncio.create_task(_start_export(job))
    elif job_type == "clear_cache":
        job._task = asyncio.create_task(_start_clear_cache(job))
    else:
        job.status = "failed"
        job.output.append(f"Unknown job type: {job_type}")
        job._event.set()

    return job


# ---------------------------------------------------------------------------
# Per-type starters
# ---------------------------------------------------------------------------

async def _start_generate(job: Job):
    generator_py = _root / "generate-tests" / "generator.py"
    strategy = job.params.get("strategy", "zero_shot")
    framework = job.params.get("framework", "eu_ai_act")
    cmd = [
        sys.executable, str(generator_py),
        "--strategy", strategy,
        "--framework", framework,
    ]
    if job.site and job.component:
        cmd += ["--site", job.site, "--component", job.component]
    component_rubric = _root / "rubrics" / "component.json"
    if component_rubric.exists():
        cmd += ["--component-rubric", str(component_rubric)]
    env = os.environ.copy()
    if component_rubric.exists():
        env["COMPONENT_RUBRIC_CACHE_JSON"] = str(component_rubric)
    await _run_subprocess_job(job, cmd, env=env)


async def _start_discover(job: Job):
    worker = _root / "web" / "discover_worker.py"
    cmd = [sys.executable, "-u", str(worker), job.site, job.component]
    await _run_subprocess_job(job, cmd)


async def _start_run_tests(job: Job):
    suite = job.params.get("suite", "")

    def _do_run():
        import importlib.util
        import json as _json

        bb_dir = _root / "browser-bot"
        if str(bb_dir) not in sys.path:
            sys.path.insert(0, str(bb_dir))
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))

        suite_path = Path(suite)
        if not suite_path.is_absolute():
            suite_path = _root / suite_path
        suite_data = _json.loads(suite_path.read_text(encoding="utf-8"))

        for m in suite_data.get("mandates") or []:
            for p in m.get("prompts") or []:
                if isinstance(p.get("prompts"), list):
                    mode = "multi"
                    break
            else:
                continue
            break
        else:
            mode = "single"

        bb_main_path = bb_dir / "main.py"
        spec = importlib.util.spec_from_file_location("browser_bot_main", bb_main_path)
        bb_main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bb_main)
        import asyncio as _aio
        _aio.run(bb_main.run_posts(site=job.site, component=job.component, mode=mode, suite_path=suite_path))

        logs_dir = bb_dir / "sites" / job.site / job.component / "logs"
        if logs_dir.is_dir():
            # New-style: logs/{timestamp}/run_log.json; old-style: logs/run_*.json
            logs = sorted(
                list(logs_dir.glob("*/run_log.json")) + list(logs_dir.glob("run_*.json")),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if logs:
                run_log = logs[0]
                print(f"[+] Run log: {run_log}")
                from pipeline.convert_log import convert_run_log
                compliance_log = convert_run_log(run_log, suite_path)
                print(f"[+] Compliance log: {compliance_log}")

                if job.params.get("assess"):
                    print("[*] Running risk assessment...")
                    from pipeline.risk_assess import run_risk_assessment
                    risk_results = run_risk_assessment(compliance_log)

                    log_data = _json.loads(compliance_log.read_text(encoding="utf-8"))
                    compliance_by_id = {r["id"]: r for r in log_data.get("results", []) if "id" in r}
                    for r in risk_results:
                        cl = compliance_by_id.get(r.get("id", ""), {})
                        for fld in ("description", "expected_behavior", "status", "ok", "error"):
                            if fld not in r:
                                r[fld] = cl.get(fld)

                    severity_order = ("critical", "high", "medium", "low", "informational", "compliant", "indeterminate")
                    mandate_rollup = {}
                    for r in risk_results:
                        m = r.get("mandate", "")
                        if m:
                            cur = mandate_rollup.get(m, "compliant")
                            nl = r.get("risk_level", "indeterminate")
                            ci = severity_order.index(cur) if cur in severity_order else len(severity_order)
                            ni = severity_order.index(nl) if nl in severity_order else len(severity_order)
                            if ni < ci:
                                mandate_rollup[m] = nl

                    from datetime import datetime as _dt
                    ts = _dt.now().strftime("%Y-%m-%dT%H-%M-%S")
                    report = {
                        "timestamp": ts,
                        "framework": log_data.get("framework", ""),
                        "source_file": log_data.get("source_file", ""),
                        "run_log_dir": str(run_log.parent),
                        "compliance_log": str(compliance_log),
                        "adversarial_results": risk_results,
                        "mandate_rollup": mandate_rollup,
                    }
                    report_path = compliance_log.parent / "pipeline_report.json"
                    report_path.write_text(_json.dumps(report, indent=2), encoding="utf-8")
                    print(f"[+] Pipeline report: {report_path}")
                    print(f"[+] Assessed: {len(risk_results)}")

    await _run_thread_job(job, _do_run)


async def _start_risk_assess(job: Job):
    compliance_log = job.params.get("compliance_log", "")

    def _do():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))

        import importlib.util
        rla_file = _root / "risk-level-agent" / "risk_level_agent.py"
        if rla_file.exists() and "risk_level_agent" not in sys.modules:
            spec = importlib.util.spec_from_file_location("risk_level_agent", rla_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["risk_level_agent"] = mod
                spec.loader.exec_module(mod)

        from pipeline.risk_assess import run_risk_assessment
        import json as _json

        cl_path = Path(compliance_log)
        if not cl_path.is_absolute():
            cl_path = _root / cl_path
        print(f"[*] Running risk assessment on: {cl_path.name}")
        risk_results = run_risk_assessment(cl_path)

        log_data = _json.loads(cl_path.read_text(encoding="utf-8"))
        all_log_results = log_data.get("results", [])
        compliance_by_id = {r["id"]: r for r in all_log_results if "id" in r}
        for r in risk_results:
            cl_entry = compliance_by_id.get(r.get("id", ""), {})
            for fld in ("description", "expected_behavior", "status", "ok", "error"):
                if fld not in r:
                    r[fld] = cl_entry.get(fld)

        severity_order = ("critical", "high", "medium", "low", "informational", "compliant", "indeterminate")
        mandate_rollup = {}
        for r in risk_results:
            m = r.get("mandate", "")
            if m:
                cur = mandate_rollup.get(m, "compliant")
                nl = r.get("risk_level", "indeterminate")
                ci = severity_order.index(cur) if cur in severity_order else len(severity_order)
                ni = severity_order.index(nl) if nl in severity_order else len(severity_order)
                if ni < ci:
                    mandate_rollup[m] = nl

        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y-%m-%dT%H-%M-%S")
        report = {
            "timestamp": ts,
            "framework": log_data.get("framework", ""),
            "source_file": log_data.get("source_file", ""),
            "run_log_dir": str(cl_path.parent),
            "compliance_log": str(cl_path),
            "adversarial_results": risk_results,
            "mandate_rollup": mandate_rollup,
        }
        report_path = cl_path.parent / "pipeline_report.json"
        report_path.write_text(_json.dumps(report, indent=2), encoding="utf-8")
        print(f"[+] Pipeline report: {report_path}")
        print(f"[+] Assessed: {len(risk_results)}")
        for m, level in sorted(mandate_rollup.items()):
            print(f"  {m[:60]}: {level}")

    await _run_thread_job(job, _do)


def _load_env_vars() -> dict[str, str]:
    """Parse key=value pairs from the root .env file."""
    env_file = _root / ".env"
    result: dict[str, str] = {}
    if not env_file.exists():
        return result
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


async def _start_export(job: Job):
    report = job.params.get("report", "")
    default_level = job.params.get("default_level")

    # host + api_key come from .env; program_id is per-export from params
    env = _load_env_vars()
    host = env.get("GENBOUNTY_HOST", "")
    api_key = env.get("GENBOUNTY_API_KEY", "")
    program_id = job.params.get("program_id", "")

    missing = [k for k, v in [("GENBOUNTY_HOST", host), ("GENBOUNTY_API_KEY", api_key), ("program_id", program_id)] if not v]
    if missing:
        job.output.append(f"[!] Missing credentials in .env: {', '.join(missing)}")
        job.status = "error"
        return

    def _do():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        from pipeline.export_genbounty import export_pipeline_report
        rp = Path(report)
        if not rp.is_absolute():
            rp = _root / rp
        export_pipeline_report(rp, host=host, api_key=api_key, program_id=program_id, default_level=default_level)

    await _run_thread_job(job, _do)


async def _start_clear_cache(job: Job):
    delete_on_server = job.params.get("delete_on_server", False)

    def _do():
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))
        gen_tests_dir = str(_root / "generate-tests")
        if gen_tests_dir not in sys.path:
            sys.path.insert(0, gen_tests_dir)

        cleared = []
        try:
            import core as gen_core
            gen_core.clear_gemini_cache(delete_on_server=delete_on_server)
            cleared.append("generator")
        except Exception as exc:
            print(f"[!] Generator cache clear failed: {exc}")

        try:
            import importlib.util
            rla_file = _root / "risk-level-agent" / "risk_level_agent.py"
            if rla_file.exists() and "risk_level_agent" not in sys.modules:
                spec = importlib.util.spec_from_file_location("risk_level_agent", rla_file)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules["risk_level_agent"] = mod
                    spec.loader.exec_module(mod)
            import risk_level_agent as rla
            rla.clear_gemini_cache(delete_on_server=delete_on_server)
            cleared.append("risk-level-agent")
        except Exception as exc:
            print(f"[!] Risk-level-agent cache clear failed: {exc}")

        if cleared:
            action = "Cleared in-process + deleted server-side" if delete_on_server else "Cleared in-process"
            print(f"[+] {action} Gemini cache ({', '.join(cleared)}).")
        else:
            print("[-] Nothing was cleared.")

    await _run_thread_job(job, _do)
