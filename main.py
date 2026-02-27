#!/usr/bin/env python3
"""
Unified AIRTA pipeline entry point.
Run from project root: python main.py [options]

Orchestrates: optional discovery -> diagnostics (send payloads + analyze_log -> discovery.json) -> compliance tests -> risk assessment -> report.

discovery.json (meta, capabilities, tools, has_context, uses_rag, uses_mcp) is produced before tests so future runs can use it to decide whether to run multishot, agent, or capabilities tests.
"""
import argparse
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from datetime import datetime


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

    load_order = ["config", "evasion", "payload_format", "auth", "discover", "send_payloads", "run_diagnostics", "generate_site_payload"]
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

    # Diagnostics: ensure diagnostics.analyze_log is loadable
    diag_dir = root / "diagnostics"
    if (diag_dir / "analyze_log.py").exists():
        diag_pkg = types.ModuleType("diagnostics")
        diag_pkg.__path__ = [str(diag_dir)]
        sys.modules["diagnostics"] = diag_pkg
        spec = importlib.util.spec_from_file_location(
            "diagnostics.analyze_log",
            diag_dir / "analyze_log.py",
            submodule_search_locations=[str(diag_dir)],
        )
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            setattr(diag_pkg, "analyze_log", mod)
            sys.modules["diagnostics.analyze_log"] = mod
            spec.loader.exec_module(mod)


async def _run_discovery() -> bool:
    """Run login, discover, generate-payload-module. Returns True if successful."""
    from component_discovery import auth, discover, generate_site_payload

    print("[*] Running login (browser will open)...")
    await auth.capture_login_and_csrf(headless=False)
    await discover.discover_endpoint(headless=False)
    generate_site_payload.generate_payload_module()
    return True


async def _run_diagnostics_send(discovery_config, diagnostics_path: Path, log_dir: Path | None = None) -> Path | None:
    """Run diagnostics-only flow: adapt format from discovered endpoint, send, write log. Does not use payloads.json."""
    from component_discovery import run_diagnostics
    return await run_diagnostics.run_diagnostics_send(discovery_config, diagnostics_path, log_dir=log_dir, verbose=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified AIRTA pipeline: discovery (optional), compliance tests, diagnostics (optional), risk assessment.",
    )
    parser.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip discovery; assume endpoint and auth already exist.",
    )
    parser.add_argument(
        "--skip-diagnostics",
        action="store_true",
        help="Skip running diagnostics (send diagnostics payloads + analyze_log) before compliance tests.",
    )
    parser.add_argument(
        "--test-file",
        type=Path,
        default=Path("tests/eu_ai_act_compliance_prompts.json"),
        help="Path to test prompts JSON (default: tests/eu_ai_act_compliance_prompts.json).",
    )
    parser.add_argument(
        "--component",
        type=str,
        default=os.getenv("COMPONENT", "default"),
        help="Component name for discovery state (default: COMPONENT env or 'default').",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Optional: also copy pipeline_report.json here. Run logs are in sitename/component/logs/{timestamp}/.",
    )
    parser.add_argument(
        "--force-discovery",
        action="store_true",
        help="Run discovery even if endpoint and auth exist.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    os.chdir(root)
    _setup_paths(root)

    # Resolve paths relative to root
    test_file = args.test_file if args.test_file.is_absolute() else root / args.test_file

    # Set COMPONENT for config
    os.environ["COMPONENT"] = args.component

    from component_discovery import config as discovery_config

    # Run log dir: sitename/component/logs/{timestamp}/ for this round
    run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    run_log_dir = discovery_config.SITE_STATE_DIR / "logs" / run_timestamp
    run_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[*] Run logs: {run_log_dir}")

    # 1. Optional discovery
    if not args.skip_discovery and not args.force_discovery:
        if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists() or not discovery_config.AUTH_STATE_FILE.exists():
            print("[*] Discovery state missing; running discovery (login + discover + generate-payload)...")
            asyncio.run(_run_discovery())
    elif args.force_discovery:
        print("[*] Force discovery...")
        asyncio.run(_run_discovery())

    if not discovery_config.DISCOVERED_ENDPOINT_FILE.exists():
        print("[-] No discovered endpoint. Run without --skip-discovery or run discovery first.")
        sys.exit(1)
    if not discovery_config.AUTH_STATE_FILE.exists():
        print("[-] No auth state. Run without --skip-discovery or run login first.")
        sys.exit(1)

    # 2. Diagnostics (before tests): send diagnostics from diagnostics.json (format adapted to endpoint), then analyze_log -> discovery.json
    # discovery.json is used later to decide whether to run multishot, agent, or capabilities tests.
    discovery_json_path = discovery_config.SITE_STATE_DIR / "discovery.json"
    diagnostics_path = root / "diagnostics" / "diagnostics.json"
    if not args.skip_diagnostics:
        print("[*] Running diagnostics (format-adapted send + analyze_log) before compliance tests...")
        try:
            diag_log_path = asyncio.run(_run_diagnostics_send(discovery_config, diagnostics_path, log_dir=run_log_dir))
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

    # 3. Run compliance tests
    from pipeline.run_tests import run_compliance_tests

    compliance_log_path = asyncio.run(run_compliance_tests(test_file, log_dir=run_log_dir, verbose=True))
    if compliance_log_path is None:
        print("[-] Compliance test run produced no log. Exiting.")
        sys.exit(1)

    # 4. Risk assessment
    from pipeline.risk_assess import run_risk_assessment

    print("[*] Running risk assessment on compliance log...")
    risk_results = run_risk_assessment(compliance_log_path)

    # 5. Report
    log_data = json.loads(compliance_log_path.read_text(encoding="utf-8"))
    calibration_results = [r for r in log_data.get("results", []) if r.get("mandate") == "calibration"]
    severity_order = ("critical", "high", "medium", "low", "informational", "compliant", "indeterminate")

    def _severity_index(level: str) -> int:
        return severity_order.index(level) if level in severity_order else len(severity_order)

    mandate_rollup: dict[str, str] = {}
    for r in risk_results:
        m = r.get("mandate", "")
        if m and m != "calibration":
            current = mandate_rollup.get(m, "compliant")
            new_level = r.get("risk_level", "indeterminate")
            if _severity_index(new_level) < _severity_index(current):
                mandate_rollup[m] = new_level

    # Copy discovery.json into run log dir so this round has everything in one place
    if discovery_json_path.exists():
        import shutil
        shutil.copy2(discovery_json_path, run_log_dir / "discovery.json")

    report = {
        "timestamp": run_timestamp,
        "run_log_dir": str(run_log_dir),
        "compliance_log": str(compliance_log_path),
        "test_file": str(test_file),
        "discovery_json": str(run_log_dir / "discovery.json") if (run_log_dir / "discovery.json").exists() else None,
        "adversarial_results": risk_results,
        "mandate_rollup": mandate_rollup,
        "calibration_count": len(calibration_results),
        "calibration_ok_count": sum(1 for r in calibration_results if r.get("ok")),
    }
    report_path = run_log_dir / "pipeline_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] Pipeline report: {report_path}")
    if args.report_dir is not None:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        copy_report = args.report_dir if args.report_dir.is_absolute() else root / args.report_dir
        copy_report.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(report_path, copy_report / f"pipeline_report_{run_timestamp}.json")

    # Summary
    print("\n=== Summary ===")
    print(f"  Adversarial assessed: {len(risk_results)}")
    if mandate_rollup:
        for m, level in sorted(mandate_rollup.items(), key=lambda x: x[0]):
            print(f"  {m[:50]}: {level}")
    print(f"  Calibration: {report['calibration_ok_count']}/{report['calibration_count']} OK")


if __name__ == "__main__":
    main()
