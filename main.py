#!/usr/bin/env python3
"""
Unified AIRTA pipeline entry point.
Run from project root: python main.py [options]

Commands:
  pre-discovery  Prompt for site, then run discovery → guide → script → run prompts (pre-discovery pipeline).
  menu          Interactive menu: discovery, diagnostics, tests, risk assessment (default).
  run           Full pipeline: discovery, diagnostics, compliance tests, risk assessment.

Orchestrates: optional discovery -> diagnostics (send payloads + analyze_log -> discovery.json) -> compliance tests -> risk assessment -> report.

discovery.json (meta, capabilities, tools, has_context, uses_rag, uses_mcp) is produced before tests so future runs can use it to decide whether to run multishot, agent, or capabilities tests.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import threading
import time
import types
from pathlib import Path
from typing import Any
from datetime import datetime

REFRESH_INTERVAL_MINUTES = 4

# Strategies for generate-tests (must match generator.py choices)
GENERATE_TESTS_STRATEGIES = [
    "zero_shot", "multi_shot", "few_shot", "iterative", "chain_of_thought",
    "prompt_chaining", "tree_of_thoughts", "self_consistency", "self_reflection", "directional_stimulus",
]


def _get_generate_tests_frameworks(root: Path) -> list[str]:
    """Return framework names (rubric stems) from rubrics/*.json."""
    rubrics_dir = root / "rubrics"
    if not rubrics_dir.is_dir():
        return []
    return sorted(p.stem.replace("-", "_") for p in rubrics_dir.glob("*.json"))


def _run_generate_tests_submenu(root: Path, discovery_config: Any = None) -> None:
    """Sub-menu: generate tests for one or many strategy/framework combinations.
    If discovery_config is provided and a component rubric file exists (e.g. chat/chat_rubric.json),
    sets COMPONENT_RUBRIC_CACHE_JSON so the generator uses its cache_name.
    """
    frameworks = _get_generate_tests_frameworks(root)
    if not frameworks:
        print("[-] No rubrics found in rubrics/. Add rubrics/*.json to enable frameworks.")
        return

    generator_py = root / "generate-tests" / "generator.py"
    if not generator_py.exists():
        print(f"[-] Generator not found: {generator_py}")
        return

    env = os.environ.copy()
    component_rubric_args: list[str] = []
    if discovery_config is not None:
        cache_path = discovery_config.SITE_STATE_DIR / f"{discovery_config.SITE_STATE_DIR.name}_rubric.json"
        if cache_path.exists():
            env["COMPONENT_RUBRIC_CACHE_JSON"] = str(cache_path)
            component_rubric_args = ["--component-rubric", str(cache_path)]
            print(f"[*] Using component rubric cache: {cache_path.name}")

    def _gen_one(strategy: str, framework: str) -> None:
        gen_args = [sys.executable, str(generator_py), "--strategy", strategy, "--framework", framework] + component_rubric_args
        print(f"[*] Generating: strategy={strategy}, framework={framework}...")
        result = subprocess.run(gen_args, cwd=str(root), env=env)
        if result.returncode == 0:
            print(f"[+] Done: generate-tests/{strategy.replace('_', '-')}/{framework.replace('_', '-')}.json")
        else:
            print(f"[!] Generator exited {result.returncode} for {strategy}/{framework}.")

    def _pick_strategy(label: str) -> str | None:
        print()
        print(f"  Generate tests — {label}")
        print()
        for i, name in enumerate(GENERATE_TESTS_STRATEGIES, 1):
            print(f"  {i:2}) {name}")
        print(f"  {len(GENERATE_TESTS_STRATEGIES) + 1:2}) Back")
        print()
        try:
            s = input(f"  Strategy [1-{len(GENERATE_TESTS_STRATEGIES) + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not s:
            return None
        try:
            idx = int(s)
        except ValueError:
            print("  Invalid choice.")
            return None
        if idx == len(GENERATE_TESTS_STRATEGIES) + 1 or idx < 1 or idx > len(GENERATE_TESTS_STRATEGIES):
            return None
        return GENERATE_TESTS_STRATEGIES[idx - 1]

    def _pick_framework(label: str) -> str | None:
        print()
        print(f"  Generate tests — {label}")
        print()
        for i, name in enumerate(frameworks, 1):
            print(f"  {i:2}) {name}")
        print(f"  {len(frameworks) + 1:2}) Back")
        print()
        try:
            f = input(f"  Framework [1-{len(frameworks) + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not f:
            return None
        try:
            fidx = int(f)
        except ValueError:
            print("  Invalid choice.")
            return None
        if fidx == len(frameworks) + 1 or fidx < 1 or fidx > len(frameworks):
            return None
        return frameworks[fidx - 1]

    print()
    print("  Generate tests")
    print()
    print("   1) Strategy + framework    — one combination")
    print("   2) All frameworks          — pick strategy, run all frameworks")
    print("   3) All strategies          — pick framework, run all strategies")
    print("   4) All                     — all strategies × all frameworks")
    print("   5) Back")
    print()
    try:
        mode = input("  Mode [1-5]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not mode or mode == "5":
        return

    if mode == "1":
        strategy = _pick_strategy("strategy")
        if not strategy:
            return
        framework = _pick_framework("framework")
        if not framework:
            return
        _gen_one(strategy, framework)
    elif mode == "2":
        strategy = _pick_strategy("strategy")
        if not strategy:
            return
        total = len(frameworks)
        for n, fw in enumerate(frameworks, 1):
            print(f"\n  [{n}/{total}]")
            _gen_one(strategy, fw)
    elif mode == "3":
        framework = _pick_framework("framework")
        if not framework:
            return
        total = len(GENERATE_TESTS_STRATEGIES)
        for n, strat in enumerate(GENERATE_TESTS_STRATEGIES, 1):
            print(f"\n  [{n}/{total}]")
            _gen_one(strat, framework)
    elif mode == "4":
        total = len(GENERATE_TESTS_STRATEGIES) * len(frameworks)
        n = 0
        for strat in GENERATE_TESTS_STRATEGIES:
            for fw in frameworks:
                n += 1
                print(f"\n  [{n}/{total}]")
                _gen_one(strat, fw)
    else:
        print("  Invalid choice.")


def _menu_refresh_loop() -> None:
    """Background thread: refresh session every REFRESH_INTERVAL_MINUTES. Output prefixed [auto-refresh]."""
    from component_discovery import auth, config as discovery_config
    _stdout = sys.stdout

    class _PrefixStdout:
        def __init__(self, prefix: str) -> None:
            self._prefix = prefix
            self._inner = _stdout

        def write(self, s: str) -> None:
            if s:
                self._inner.write((self._prefix + s).replace("\n", "\n" + self._prefix))

        def flush(self) -> None:
            self._inner.flush()

    while True:
        time.sleep(REFRESH_INTERVAL_MINUTES * 60)
        if not discovery_config.AUTH_STATE_FILE.exists():
            continue
        try:
            sys.stdout = _PrefixStdout("[auto-refresh] ")
            try:
                print()
                asyncio.run(auth.refresh_session())
            finally:
                sys.stdout = _stdout
        except Exception as e:
            print(f"[!] Background refresh failed: {e}")

# Load .config and .env so COMPONENT etc. are available for CLI defaults
_root = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
except ImportError:
    pass


def _setup_paths(root: Path) -> None:
    """Make component_discovery (from component-discovery/) and risk_level_agent loadable."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    cd_dir = root / "component-discovery"
    if not cd_dir.is_dir():
        return

    # Create component_discovery package and load submodules (directory is component-discovery with hyphen)
    pkg = types.ModuleType("component_discovery")
    pkg.__path__ = [str(cd_dir)]
    pkg.__file__ = str(cd_dir / "__init__.py")
    sys.modules["component_discovery"] = pkg

    load_order = ["config", "payload_format", "auth", "discover", "generate_site_payload"]
    for name in load_order:
        py_file = cd_dir / f"{name}.py"
        if not py_file.exists():
            continue
        spec = importlib.util.spec_from_file_location(
            f"component_discovery.{name}",
            py_file,
            submodule_search_locations=[str(cd_dir)],
        )
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        setattr(pkg, name, mod)
        sys.modules[f"component_discovery.{name}"] = mod
        spec.loader.exec_module(mod)

    # Load risk_level_agent from risk-level-agent/risk_level_agent.py
    rla_file = root / "risk-level-agent" / "risk_level_agent.py"
    if rla_file.exists():
        spec = importlib.util.spec_from_file_location("risk_level_agent", rla_file)
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["risk_level_agent"] = mod
            spec.loader.exec_module(mod)

    # Diagnostics: ensure diagnostics.analyze_log, assess_availability, run_diagnostics, run_availability are loadable
    diag_dir = root / "diagnostics"
    if (diag_dir / "analyze_log.py").exists():
        diag_pkg = types.ModuleType("diagnostics")
        diag_pkg.__path__ = [str(diag_dir)]
        sys.modules["diagnostics"] = diag_pkg
        for subname in ("analyze_log", "assess_availability", "run_diagnostics", "run_availability", "site_info", "rubric_from_assessment"):
            py_file = diag_dir / f"{subname}.py"
            if not py_file.exists():
                continue
            spec = importlib.util.spec_from_file_location(
                f"diagnostics.{subname}",
                py_file,
                submodule_search_locations=[str(diag_dir)],
            )
            if spec is not None and spec.loader is not None:
                mod = importlib.util.module_from_spec(spec)
                setattr(diag_pkg, subname, mod)
                sys.modules[f"diagnostics.{subname}"] = mod
                spec.loader.exec_module(mod)


async def _run_discovery() -> bool:
    """Run unified discovery: one browser session (login, then 3 messages), then generate-payload-module. Returns True if successful."""
    from component_discovery import discover, generate_site_payload

    print("[*] Running unified discovery (browser will open: login, then send 3 messages in the AI component)...")
    await discover.discover_unified(headless=False)
    generate_site_payload.generate_payload_module()
    return True


async def _run_diagnostics_send(discovery_config, diagnostics_path: Path, log_dir: Path | None = None, speed: int = 1) -> Path | None:
    """Run diagnostics-only flow: adapt format from discovered endpoint, send, write log. Does not use payloads.json."""
    from diagnostics import run_diagnostics
    return await run_diagnostics.run_diagnostics_send(discovery_config, diagnostics_path, log_dir=log_dir, verbose=True, speed=speed)


def _get_run_tests_frameworks(root: Path) -> list[str]:
    """Return frameworks that have at least one generated test JSON under generate-tests/."""
    gen_dir = root / "generate-tests"
    if not gen_dir.is_dir():
        return []
    found: set[str] = set()
    for strat in GENERATE_TESTS_STRATEGIES:
        strat_dir = gen_dir / strat.replace("_", "-")
        if strat_dir.is_dir():
            for p in strat_dir.glob("*.json"):
                found.add(p.stem.replace("-", "_"))
    return sorted(found)


def _run_compliance_tests_only(
    test_file: Path,
    log_dir: Path,
    speed: int,
) -> Path | None:
    """
    Send compliance prompts to the discovered endpoint and write compliance_log.json.
    Returns the path to the log, or None on failure.
    Caller must have already called _setup_paths().
    """
    from pipeline.run_tests import run_compliance_tests

    compliance_log_path = asyncio.run(
        run_compliance_tests(test_file, log_dir=log_dir, verbose=True, speed=speed)
    )
    if compliance_log_path is None:
        print("[-] Compliance test run produced no log.")
        return None
    return compliance_log_path


def _run_risk_assessment_on_log(
    compliance_log_path: Path,
    log_dir: Path,
) -> dict | None:
    """
    Run risk assessment on an existing compliance_log.json and write pipeline_report.json
    into log_dir (which may be the same directory as the log).
    Returns the report dict, or None on failure.
    Caller must have already called _setup_paths().
    """
    from pipeline.risk_assess import run_risk_assessment

    if not compliance_log_path.exists():
        print(f"[-] Compliance log not found: {compliance_log_path}")
        return None

    print(f"[*] Running risk assessment on: {compliance_log_path.name}")
    risk_results = run_risk_assessment(compliance_log_path)

    log_data = json.loads(compliance_log_path.read_text(encoding="utf-8"))
    all_log_results = log_data.get("results", [])

    # Build a lookup so each risk result can be enriched with compliance-log fields
    # (description, expected_behavior, status, ok, error) that risk_assess doesn't carry.
    compliance_by_id: dict[str, dict] = {r["id"]: r for r in all_log_results if "id" in r}
    for r in risk_results:
        entry_id = r.get("id", "")
        cl = compliance_by_id.get(entry_id, {})
        for field in ("description", "expected_behavior", "status", "ok", "error"):
            if field not in r:
                r[field] = cl.get(field)

    severity_order = ("critical", "high", "medium", "low", "informational", "compliant", "indeterminate")

    def _severity_index(level: str) -> int:
        return severity_order.index(level) if level in severity_order else len(severity_order)

    mandate_rollup: dict[str, str] = {}
    for r in risk_results:
        m = r.get("mandate", "")
        if m:
            current = mandate_rollup.get(m, "compliant")
            new_level = r.get("risk_level", "indeterminate")
            if _severity_index(new_level) < _severity_index(current):
                mandate_rollup[m] = new_level

    run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    report = {
        "timestamp": run_timestamp,
        "framework": log_data.get("framework", ""),
        "source_file": log_data.get("source_file", ""),
        "run_log_dir": str(log_dir),
        "compliance_log": str(compliance_log_path),
        "adversarial_results": risk_results,
        "mandate_rollup": mandate_rollup,
    }
    report_path = log_dir / "pipeline_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] Pipeline report: {report_path}")

    print("\n=== Summary ===")
    print(f"  Assessed: {len(risk_results)}")
    if mandate_rollup:
        for m, level in sorted(mandate_rollup.items()):
            print(f"  {m[:60]}: {level}")
    return report


def _run_tests_and_assess(
    test_file: Path,
    log_dir: Path,
    speed: int,
) -> dict | None:
    """
    Run compliance tests then immediately risk-assess (used by the CLI pipeline).
    Writes compliance_log.json + pipeline_report.json into log_dir.
    Returns the report dict, or None on failure.
    """
    compliance_log_path = _run_compliance_tests_only(test_file, log_dir, speed)
    if compliance_log_path is None:
        return None
    return _run_risk_assessment_on_log(compliance_log_path, log_dir)


def _run_run_tests_submenu(root: Path, discovery_config: Any, speed: int = 1) -> None:
    """Sub-menu: run compliance tests + risk assessment for one or many strategy/framework combinations."""
    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        print("[-] No discovered endpoint. Run Discovery (2) first.")
        return
    if not discovery_config.AUTH_STATE_FILE.exists():
        print("[-] No auth state. Run Discovery (2) first.")
        return

    # Index all available test files: (strategy, framework) -> Path
    gen_dir = root / "generate-tests"
    available: dict[tuple[str, str], Path] = {}
    for strat in GENERATE_TESTS_STRATEGIES:
        strat_dir = gen_dir / strat.replace("_", "-")
        if strat_dir.is_dir():
            for p in sorted(strat_dir.glob("*.json")):
                fw = p.stem.replace("-", "_")
                available[(strat, fw)] = p

    if not available:
        print("[-] No generated test files found under generate-tests/. Run Generate tests (6) first.")
        return

    strats_with_files = [s for s in GENERATE_TESTS_STRATEGIES if any(s == k[0] for k in available)]
    frameworks_with_files = sorted({fw for _, fw in available})

    def _run_one(strategy: str, framework: str) -> None:
        test_file = available.get((strategy, framework))
        if test_file is None:
            print(f"[-] No test file for {strategy}/{framework}; skipping.")
            return
        run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        log_dir = discovery_config.SITE_STATE_DIR / "logs" / run_timestamp
        log_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] Running: strategy={strategy}, framework={framework}")
        print(f"[*] Run logs: {log_dir}")
        try:
            log_path = _run_compliance_tests_only(test_file, log_dir, speed)
            if log_path:
                print(f"[+] Compliance log: {log_path}")
                print(f"[*] Run Risk Assessment (8) to assess this log.")
        except Exception as e:
            print(f"[!] Run tests failed: {e}")

    def _pick_strategy(label: str) -> str | None:
        print()
        print(f"  Run tests — {label}")
        print()
        for i, name in enumerate(strats_with_files, 1):
            print(f"  {i:2}) {name}")
        print(f"  {len(strats_with_files) + 1:2}) Back")
        print()
        try:
            s = input(f"  Strategy [1-{len(strats_with_files) + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not s:
            return None
        try:
            idx = int(s)
        except ValueError:
            print("  Invalid choice.")
            return None
        if idx == len(strats_with_files) + 1 or idx < 1 or idx > len(strats_with_files):
            return None
        return strats_with_files[idx - 1]

    def _pick_framework(label: str) -> str | None:
        print()
        print(f"  Run tests — {label}")
        print()
        for i, name in enumerate(frameworks_with_files, 1):
            print(f"  {i:2}) {name}")
        print(f"  {len(frameworks_with_files) + 1:2}) Back")
        print()
        try:
            f = input(f"  Framework [1-{len(frameworks_with_files) + 1}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not f:
            return None
        try:
            fidx = int(f)
        except ValueError:
            print("  Invalid choice.")
            return None
        if fidx == len(frameworks_with_files) + 1 or fidx < 1 or fidx > len(frameworks_with_files):
            return None
        return frameworks_with_files[fidx - 1]

    print()
    print("  Run tests")
    print()
    print("   1) Strategy + framework    — one test file")
    print("   2) All frameworks          — pick strategy, run all its framework files")
    print("   3) All strategies          — pick framework, run all strategies that have it")
    print("   4) All                     — all available test files")
    print("   5) Back")
    print()
    try:
        mode = input("  Mode [1-5]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not mode or mode == "5":
        return

    if mode == "1":
        strategy = _pick_strategy("strategy")
        if not strategy:
            return
        framework = _pick_framework("framework")
        if not framework:
            return
        _run_one(strategy, framework)
    elif mode == "2":
        strategy = _pick_strategy("strategy")
        if not strategy:
            return
        fws = sorted(fw for s, fw in available if s == strategy)
        total = len(fws)
        for n, fw in enumerate(fws, 1):
            print(f"\n  [{n}/{total}]")
            _run_one(strategy, fw)
    elif mode == "3":
        framework = _pick_framework("framework")
        if not framework:
            return
        strats = [s for s in GENERATE_TESTS_STRATEGIES if (s, framework) in available]
        total = len(strats)
        for n, strat in enumerate(strats, 1):
            print(f"\n  [{n}/{total}]")
            _run_one(strat, framework)
    elif mode == "4":
        pairs = [(s, fw) for s in GENERATE_TESTS_STRATEGIES for fw in frameworks_with_files if (s, fw) in available]
        total = len(pairs)
        for n, (strat, fw) in enumerate(pairs, 1):
            print(f"\n  [{n}/{total}]")
            _run_one(strat, fw)
    else:
        print("  Invalid choice.")


def _run_risk_assessment_submenu(discovery_config: Any) -> None:
    """Sub-menu: pick a compliance log from the component's log dirs and run risk assessment."""
    logs_root = discovery_config.SITE_STATE_DIR / "logs"
    if not logs_root.is_dir():
        print("[-] No logs directory found. Run tests (7) first to produce compliance logs.")
        return

    # Collect all compliance_log.json files, sorted newest-first by directory name
    log_files: list[Path] = sorted(
        logs_root.glob("*/compliance_log.json"),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    if not log_files:
        print("[-] No compliance logs found. Run tests (7) first.")
        return

    print()
    print("  Risk assessment — select compliance log")
    print()
    for i, p in enumerate(log_files, 1):
        # Show timestamp + framework from the log if readable
        label = p.parent.name
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            fw = meta.get("framework", "")
            n_results = len(meta.get("results", []))
            if fw:
                label = f"{p.parent.name}  {fw} ({n_results} adversarial)"
        except Exception:
            pass
        print(f"  {i:2}) {label}")
    all_idx = len(log_files) + 1
    back_idx = len(log_files) + 2
    print(f"  {all_idx:2}) All — run risk assessment on every log above")
    print(f"  {back_idx:2}) Back")
    print()
    try:
        s = input(f"  Log [1-{back_idx}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not s:
        return
    try:
        idx = int(s)
    except ValueError:
        print("  Invalid choice.")
        return
    if idx == back_idx:
        return
    if idx == all_idx:
        total = len(log_files)
        print(f"\n  Running risk assessment on all {total} compliance logs...")
        for n, log_file in enumerate(log_files, 1):
            label = log_file.parent.name
            try:
                meta = json.loads(log_file.read_text(encoding="utf-8"))
                fw = meta.get("framework", "")
                if fw:
                    label = f"{fw}  ({log_file.parent.name})"
            except Exception:
                pass
            print(f"\n  [{n}/{total}] {label}")
            try:
                _run_risk_assessment_on_log(log_file, log_file.parent)
            except Exception as e:
                print(f"[!] Risk assessment failed for {log_file.parent.name}: {e}")
        print(f"\n  All {total} risk assessments complete.")
        return
    if idx < 1 or idx > len(log_files):
        print("  Invalid choice.")
        return

    selected = log_files[idx - 1]
    log_dir = selected.parent
    try:
        _run_risk_assessment_on_log(selected, log_dir)
    except Exception as e:
        print(f"[!] Risk assessment failed: {e}")


def _run_export_genbounty_submenu(discovery_config: Any) -> None:
    """Sub-menu: pick a pipeline report and export its results to Genbounty."""
    logs_root = discovery_config.SITE_STATE_DIR / "logs"
    if not logs_root.is_dir():
        print("[-] No logs directory found. Run tests (7) + risk assessment (8) first.")
        return

    report_files: list[Path] = sorted(
        logs_root.glob("*/pipeline_report.json"),
        key=lambda p: p.parent.name,
        reverse=True,
    )
    if not report_files:
        print("[-] No pipeline reports found. Run risk assessment (8) first.")
        return

    print()
    print("  Export to Genbounty — select pipeline report")
    print()
    for i, p in enumerate(report_files, 1):
        label = p.parent.name
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            fw = meta.get("framework", "")
            n_results = len(meta.get("adversarial_results", []))
            if fw:
                label = f"{p.parent.name}  {fw} ({n_results} adversarial)"
        except Exception:
            pass
        print(f"  {i:2}) {label}")
        print(f"  {len(report_files) + 1:2}) Back")
    print()
    try:
        s = input(f"  Report [1-{len(report_files) + 1}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not s:
        return
    try:
        idx = int(s)
    except ValueError:
        print("  Invalid choice.")
        return
    if idx == len(report_files) + 1:
        return
    if idx < 1 or idx > len(report_files):
        print("  Invalid choice.")
        return

    selected = report_files[idx - 1]

    # Resolve credentials: env vars first, then prompt
    host = os.getenv("GENBOUNTY_HOST", "").strip()
    api_key = os.getenv("GENBOUNTY_API_KEY", "").strip()
    program_id = os.getenv("GENBOUNTY_PROGRAM_ID", "").strip()
    default_level = os.getenv("GENBOUNTY_DEFAULT_LEVEL", "").strip() or None

    print()
    if not host:
        try:
            host = input("  Genbounty host (e.g. app.genbounty.com): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    else:
        print(f"  Host: {host}  (from GENBOUNTY_HOST)")

    if not api_key:
        try:
            api_key = input("  API key (write:bulk_import scope): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    else:
        print(f"  API key: {'*' * min(8, len(api_key))}...  (from GENBOUNTY_API_KEY)")

    if not program_id:
        try:
            program_id = input("  Program ID (MongoDB ObjectId): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
    else:
        print(f"  Program ID: {program_id}  (from GENBOUNTY_PROGRAM_ID)")

    if not default_level:
        print()
        print("  Default severity level (leave blank to auto-derive from mandate):")
        print("   1) informational")
        print("   2) low")
        print("   3) medium")
        print("   4) critical")
        print("   5) Auto-derive (no override)")
        print()
        try:
            lc = input("  Level [1-5, default 5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            lc = ""
        level_map = {"1": "informational", "2": "low", "3": "medium", "4": "critical"}
        default_level = level_map.get(lc)

    if not host or not api_key or not program_id:
        print("[-] Host, API key, and Program ID are all required.")
        return

    print()
    from pipeline.export_genbounty import export_pipeline_report
    try:
        export_pipeline_report(
            selected,
            host=host,
            api_key=api_key,
            program_id=program_id,
            default_level=default_level,
        )
    except Exception as e:
        print(f"[!] Export failed: {e}")


def _run_menu(args) -> None:
    """Interactive menu: discovery, diagnostics, or refresh. Auth refresh runs in background every 4 min."""
    root = Path(__file__).resolve().parent
    os.chdir(root)
    os.environ["COMPONENT"] = getattr(args, "component", None) or os.getenv("COMPONENT", "default")
    _setup_paths(root)

    from component_discovery import config as discovery_config

    # Start background auth refresh every 4 minutes
    refresh_thread = threading.Thread(target=_menu_refresh_loop, daemon=True)
    refresh_thread.start()
    print()
    print("  AIRTA — menu (auth refresh every 4 min in background)")
    print()

    while True:
        print()
        print("  AIRTA — menu")
        print()
        print("  1) Full configuration — Run Discovery → Diagnostics → Tools & caps → Refresh (auto)")
        print("  2) Discovery           — Login + capture 3 messages + generate payload module")
        print("  3) Diagnostics        — Send diagnostics payloads + analyze_log → discovery.json")
        print("  4) Tools & caps        — Test each tool/capability (example_prompt), then assess → component_assessment.json")
        print("  5) Refresh now         — Refresh session + CSRF once")
        print("  6) Generate tests      — Select strategy + framework, generate test prompts JSON")
        print("  7) Run tests           — Select strategy + framework, send prompts → compliance_log.json")
        print("  8) Risk assessment     — Pick a compliance log, run risk assessment → pipeline_report.json")
        print("  9) Export to Genbounty — Pick a compliance log, upload results via bulk-import API")
        print(" 10) Exit")
        print()
        try:
            choice = input("  Choice [1-10]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice:
            continue
        if choice == "10" or choice.lower() == "q":
            break
        if choice == "1":
            # Full configuration: 1 → 2 → 3 → 4 in sequence, single log dir
            run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            log_dir = discovery_config.SITE_STATE_DIR / "logs" / run_timestamp
            log_dir.mkdir(parents=True, exist_ok=True)
            speed = getattr(args, "speed", 1)
            try:
                print("[*] Full configuration: 1) Discovery...")
                asyncio.run(_run_discovery())
                print("[+] Discovery done.")
                print("[*] Full configuration: 2) Diagnostics...")
                diagnostics_path = root / "diagnostics" / "diagnostics.json"
                if not diagnostics_path.exists():
                    print(f"[!] Diagnostics file not found: {diagnostics_path}; skipping.")
                else:
                    diag_log_path = asyncio.run(_run_diagnostics_send(
                        discovery_config, diagnostics_path, log_dir=log_dir, speed=speed
                    ))
                    if diag_log_path is not None:
                        from diagnostics import analyze_log
                        analyze_log.analyze_log_and_write_discovery(
                            discovery_config.SITE_STATE_DIR, diagnostics_log_path=diag_log_path
                        )
                    print("[+] Diagnostics done.")
                discovery_json_path = discovery_config.SITE_STATE_DIR / "discovery.json"
                if discovery_json_path.exists():
                    print("[*] Full configuration: 3) Tools & caps...")
                    from diagnostics import run_availability, assess_availability
                    asyncio.run(run_availability.run_tools_availability(
                        discovery_config, discovery_json_path, log_dir=log_dir, verbose=True, speed=speed
                    ))
                    asyncio.run(run_availability.run_capabilities_availability(
                        discovery_config, discovery_json_path, log_dir=log_dir, verbose=True, speed=speed
                    ))
                assess_availability.assess_availability_and_write(
                    discovery_config.SITE_STATE_DIR,
                    tools_log_path=log_dir / "tools_availability_log.json",
                    capabilities_log_path=log_dir / "capabilities_availability_log.json",
                    base_url=getattr(discovery_config, "BASE_URL", None) or os.getenv("APP_URL"),
                    log_dir=log_dir,
                )
                print("[+] Tools & caps done.")
                print("[*] Full configuration: 4) Refresh...")
                from component_discovery import auth
                ok = asyncio.run(auth.refresh_session())
                print("[+] Session refreshed." if ok else "[-] Refresh failed.")
                print(f"[+] Full configuration complete. Logs: {log_dir}")
            except Exception as e:
                print(f"[!] Full configuration failed: {e}")
        elif choice == "2":
            try:
                asyncio.run(_run_discovery())
                print("[+] Discovery done.")
            except Exception as e:
                print(f"[!] Discovery failed: {e}")
        elif choice == "3":
            if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
                print("[-] No discovered endpoint. Run Discovery (2) first.")
                continue
            if not discovery_config.AUTH_STATE_FILE.exists():
                print("[-] No auth state. Run Discovery (2) first.")
                continue
            diagnostics_path = root / "diagnostics" / "diagnostics.json"
            if not diagnostics_path.exists():
                print(f"[-] Diagnostics file not found: {diagnostics_path}")
                continue
            run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            log_dir = discovery_config.SITE_STATE_DIR / "logs" / run_timestamp
            log_dir.mkdir(parents=True, exist_ok=True)
            print("[*] Running diagnostics (send + analyze_log)...")
            try:
                diag_log_path = asyncio.run(_run_diagnostics_send(
                    discovery_config, diagnostics_path, log_dir=log_dir, speed=getattr(args, "speed", 1)
                ))
                if diag_log_path is None:
                    print("[*] No diagnostics or no results.")
                else:
                    from diagnostics import analyze_log
                    analyze_log.analyze_log_and_write_discovery(
                        discovery_config.SITE_STATE_DIR, diagnostics_log_path=diag_log_path
                    )
                    print("[+] Diagnostics done; discovery.json updated.")
            except Exception as e:
                print(f"[!] Diagnostics failed: {e}")
        elif choice == "4":
            # Tools & capabilities availability: send example_prompt per tool/capability, then agent assessment
            discovery_json_path = discovery_config.SITE_STATE_DIR / "discovery.json"
            if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
                print("[-] No discovered endpoint. Run Discovery (2) first.")
                continue
            if not discovery_config.AUTH_STATE_FILE.exists():
                print("[-] No auth state. Run Discovery (2) first.")
                continue
            if not discovery_json_path.exists():
                print("[-] No discovery.json. Run Diagnostics (3) first to produce discovery.json (tools/capabilities lists).")
                continue
            run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
            log_dir = discovery_config.SITE_STATE_DIR / "logs" / run_timestamp
            log_dir.mkdir(parents=True, exist_ok=True)
            speed = getattr(args, "speed", 1)
            print("[*] Running tools availability (send example_prompt per tool)...")
            try:
                from diagnostics import run_availability, assess_availability
                asyncio.run(run_availability.run_tools_availability(
                    discovery_config, discovery_json_path, log_dir=log_dir, verbose=True, speed=speed
                ))
                print("[*] Running capabilities availability (send example_prompt per capability)...")
                asyncio.run(run_availability.run_capabilities_availability(
                    discovery_config, discovery_json_path, log_dir=log_dir, verbose=True, speed=speed
                ))
                print("[*] Assessing with agent (verified vs unavailable tools/capabilities)...")
                assess_availability.assess_availability_and_write(
                    discovery_config.SITE_STATE_DIR,
                    tools_log_path=log_dir / "tools_availability_log.json",
                    capabilities_log_path=log_dir / "capabilities_availability_log.json",
                    base_url=getattr(discovery_config, "BASE_URL", None) or os.getenv("APP_URL"),
                    log_dir=log_dir,
                )
                print(f"[+] component_assessment.json written (canonical + copy in {log_dir})")
            except Exception as e:
                print(f"[!] Tools/capabilities availability or assessment failed: {e}")
        elif choice == "5":
            from component_discovery import auth
            try:
                ok = asyncio.run(auth.refresh_session())
                print("[+] Session refreshed." if ok else "[-] Refresh failed.")
            except Exception as e:
                print(f"[!] Refresh failed: {e}")
        elif choice == "6":
            _run_generate_tests_submenu(root, discovery_config=discovery_config)
        elif choice == "7":
            _run_run_tests_submenu(root, discovery_config, speed=getattr(args, "speed", 1))
        elif choice == "8":
            _run_risk_assessment_submenu(discovery_config)
        elif choice == "9":
            _run_export_genbounty_submenu(discovery_config)
        else:
            print("  Invalid choice.")
    print("  Bye.")


def _run_pre_discovery(args) -> int:
    """Run pre-discovery pipeline: prompt for site, then discover → guide → script → run prompts."""
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # Run as module so pre-discovery package imports work
    result = subprocess.run(
        [sys.executable, "-m", "pre-discovery.main"],
        cwd=str(root),
    )
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified AIRTA pipeline: menu (discovery/diagnostics) or full run (discovery, diagnostics, tests, risk assessment).",
    )
    sub = parser.add_subparsers(dest="command", help="Command")
    run_p = sub.add_parser("run", help="Run full pipeline (discovery, diagnostics, compliance tests, risk assessment).")
    menu_p = sub.add_parser("menu", help="Interactive menu: run Discovery or Diagnostics only (default if no command).")
    pre_p = sub.add_parser(
        "pre-discovery",
        help="Pre-discovery pipeline: prompt for site, then discover API → guide → script → run prompts.",
    )
    pre_p.set_defaults(_run=_run_pre_discovery)

    def _norm_hyphens(s: str) -> str:
        return s.strip().replace("-", "_")

    for p in (run_p, menu_p):
        p.add_argument(
            "--component",
            type=str,
            default=os.getenv("COMPONENT", "default"),
            help="Component name for discovery state (default: COMPONENT env or 'default').",
        )
    menu_p.add_argument(
        "--speed",
        type=int,
        default=1,
        choices=range(1, 9),
        metavar="N",
        help="Request concurrency for diagnostics (default: 1).",
    )

    run_p.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip discovery; assume endpoint and auth already exist.",
    )
    run_p.add_argument(
        "--skip-diagnostics",
        action="store_true",
        help="Skip running diagnostics (send diagnostics payloads + analyze_log) before compliance tests.",
    )
    run_p.add_argument(
        "--strategy",
        type=_norm_hyphens,
        choices=["zero_shot", "multi_shot", "few_shot", "iterative", "chain_of_thought", "prompt_chaining", "tree_of_thoughts", "self_consistency", "self_reflection", "directional_stimulus"],
        default="zero_shot",
        help="Strategy subdir under generate-tests (default: zero_shot). Hyphens auto-corrected to underscores.",
    )
    run_p.add_argument(
        "--framework",
        type=_norm_hyphens,
        default="eu_ai_act",
        help="Framework name for test file (default: eu_ai_act). Resolves to generate-tests/<strategy>/<framework>.json (e.g. eu-ai-act.json).",
    )
    run_p.add_argument(
        "--test-file",
        type=Path,
        default=None,
        help="Override: path to test prompts JSON. If unset, uses generate-tests/<strategy>/<framework>.json.",
    )
    run_p.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Optional: also copy pipeline_report.json here. Run logs are in sitename/component/logs/{timestamp}/.",
    )
    run_p.add_argument(
        "--force-discovery",
        action="store_true",
        help="Run discovery even if endpoint and auth exist.",
    )
    run_p.add_argument(
        "--speed",
        type=int,
        default=1,
        choices=range(1, 9),
        metavar="N",
        help="Request concurrency: 1=sequential with evasion (throttle + tenacity); 2–8=up to N concurrent requests.",
    )
    run_p.add_argument(
        "--run-availability",
        action="store_true",
        help="After diagnostics: run tools/capabilities availability, assess with Gemini, write component_assessment.json into run log.",
    )

    args = parser.parse_args()
    command = getattr(args, "command", None)

    if command == "pre-discovery":
        sys.exit(getattr(args, "_run", _run_pre_discovery)(args))

    if command is None or command == "menu":
        menu_args = args if command == "menu" else argparse.Namespace(
            component=os.getenv("COMPONENT", "default"),
            speed=1,
        )
        _run_menu(menu_args)
        return

    report = run_pipeline(args)
    if report is None:
        sys.exit(1)


def run_pipeline(args) -> dict | None:
    """
    Run the full pipeline: discovery (optional), diagnostics, compliance tests, risk assessment, report.
    Caller must have set up cwd to project root. Returns report dict or None on failure.
    """
    root = Path(__file__).resolve().parent
    os.chdir(root)
    # Set COMPONENT (and ensure APP_URL from .config) before config is loaded so paths are correct
    os.environ["COMPONENT"] = args.component
    _setup_paths(root)

    # Resolve test file: --test-file override, or generate-tests/<strategy>/<framework>.json
    if args.test_file is not None:
        test_file = args.test_file if args.test_file.is_absolute() else root / args.test_file
    else:
        strategy_subdir = args.strategy.replace("_", "-")
        base_dir = root / "generate-tests" / strategy_subdir
        hyphen_name = args.framework.replace("_", "-") + ".json"
        underscore_name = args.framework + ".json"
        test_file = base_dir / hyphen_name
        if not test_file.exists():
            test_file = base_dir / underscore_name
    print(f"[*] Test file: {test_file}")

    from component_discovery import config as discovery_config

    # Run log dir: sitename/component/logs/{timestamp}/ for this round
    run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_log_dir = discovery_config.SITE_STATE_DIR / "logs" / run_timestamp
    run_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[*] Run logs: {run_log_dir}")

    # 1. Optional discovery: skip if component dir exists with endpoint + auth files
    discovery_state_present = (
        discovery_config.SITE_STATE_DIR.exists()
        and discovery_config.DISCOVERED_ENDPOINT_FILE.exists()
        and discovery_config.AUTH_STATE_FILE.exists()
    )
    if not args.skip_discovery and not args.force_discovery:
        if discovery_state_present:
            print(f"[*] Using existing discovery state ({discovery_config.SITE_STATE_DIR}); skipping discovery.")
        else:
            print("[*] Discovery state missing; running discovery (login + discover + generate-payload)...")
            asyncio.run(_run_discovery())
    elif args.force_discovery:
        print("[*] Force discovery...")
        asyncio.run(_run_discovery())

    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        print("[-] No discovered endpoint. Run without --skip-discovery or run discovery first.")
        return None
    if not discovery_config.AUTH_STATE_FILE.exists():
        print("[-] No auth state. Run without --skip-discovery or run login first.")
        return None

    # 2. Diagnostics (before tests): send diagnostics from diagnostics.json (format adapted to endpoint), then analyze_log -> discovery.json
    discovery_json_path = discovery_config.SITE_STATE_DIR / "discovery.json"
    diagnostics_path = root / "diagnostics" / "diagnostics.json"
    if not args.skip_diagnostics:
        print("[*] Running diagnostics (format-adapted send + analyze_log) before compliance tests...")
        try:
            diag_log_path = asyncio.run(_run_diagnostics_send(discovery_config, diagnostics_path, log_dir=run_log_dir, speed=args.speed))
            if diag_log_path is None:
                print("[*] No diagnostics or no results; skipping analyze_log.")
            else:
                from diagnostics import analyze_log
                analyze_log.analyze_log_and_write_discovery(
                    discovery_config.SITE_STATE_DIR, diagnostics_log_path=diag_log_path
                )
        except Exception as e:
            print(f"[!] Diagnostics failed: {e}")
    elif discovery_json_path.exists():
        print("[*] Using existing discovery.json (diagnostics skipped).")

    # 2b. Optional: tools/capabilities availability + assessment
    if getattr(args, "run_availability", False) and discovery_json_path.exists():
        print("[*] Running tools/capabilities availability and assessment...")
        try:
            from diagnostics import run_availability
            from component_discovery import config as discovery_config
            asyncio.run(run_availability.run_tools_availability(
                discovery_config, discovery_json_path, log_dir=run_log_dir, verbose=True, speed=args.speed
            ))
            asyncio.run(run_availability.run_capabilities_availability(
                discovery_config, discovery_json_path, log_dir=run_log_dir, verbose=True, speed=args.speed
            ))
            from diagnostics import assess_availability
            assess_availability.assess_availability_and_write(
                discovery_config.SITE_STATE_DIR,
                tools_log_path=run_log_dir / "tools_availability_log.json",
                capabilities_log_path=run_log_dir / "capabilities_availability_log.json",
                base_url=getattr(discovery_config, "BASE_URL", None) or os.getenv("APP_URL"),
                log_dir=run_log_dir,
            )
            print(f"[+] component_assessment.json written to run log.")
        except Exception as e:
            print(f"[!] Availability/assessment failed: {e}")

    # Copy discovery.json into run log dir so this round has everything in one place
    if discovery_json_path.exists():
        import shutil
        shutil.copy2(discovery_json_path, run_log_dir / "discovery.json")

    # 3–5. Run compliance tests, risk assessment, and write pipeline_report.json
    report = _run_tests_and_assess(test_file, run_log_dir, args.speed)
    if report is None:
        return None

    # Patch in pipeline-run-specific fields not present in the menu path
    report["discovery_json"] = str(run_log_dir / "discovery.json") if (run_log_dir / "discovery.json").exists() else None
    report["component_assessment"] = str(run_log_dir / "component_assessment.json") if (run_log_dir / "component_assessment.json").exists() else None

    # Re-write report with the extra fields
    report_path = run_log_dir / "pipeline_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if getattr(args, "report_dir", None) is not None:
        copy_report = args.report_dir if args.report_dir.is_absolute() else root / args.report_dir
        copy_report.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(report_path, copy_report / f"pipeline_report_{run_timestamp}.json")

    return report


if __name__ == "__main__":
    main()
