#!/usr/bin/env python3
"""
Unified AIRTA pipeline entry point.
Run from project root: python main.py [options]

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
    """Sub-menu: select strategy and framework, then run generator.py.
    If discovery_config is provided and a component rubric file exists (e.g. chat/chat_rubric.json),
    sets COMPONENT_RUBRIC_CACHE_JSON so the generator uses its cache_name.
    """
    frameworks = _get_generate_tests_frameworks(root)
    if not frameworks:
        print("[-] No rubrics found in rubrics/. Add rubrics/*.json to enable frameworks.")
        return
    print()
    print("  Generate tests — strategy")
    print()
    for i, name in enumerate(GENERATE_TESTS_STRATEGIES, 1):
        print(f"  {i:2}) {name}")
    print(f"  {len(GENERATE_TESTS_STRATEGIES) + 1:2}) Back")
    print()
    try:
        s = input(f"  Strategy [1-{len(GENERATE_TESTS_STRATEGIES) + 1}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not s:
        return
    try:
        idx = int(s)
    except ValueError:
        print("  Invalid choice.")
        return
    if idx == len(GENERATE_TESTS_STRATEGIES) + 1:
        return
    if idx < 1 or idx > len(GENERATE_TESTS_STRATEGIES):
        print("  Invalid choice.")
        return
    strategy = GENERATE_TESTS_STRATEGIES[idx - 1]

    print()
    print("  Generate tests — framework")
    print()
    for i, name in enumerate(frameworks, 1):
        print(f"  {i:2}) {name}")
    print(f"  {len(frameworks) + 1:2}) Back")
    print()
    try:
        f = input(f"  Framework [1-{len(frameworks) + 1}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not f:
        return
    try:
        fidx = int(f)
    except ValueError:
        print("  Invalid choice.")
        return
    if fidx == len(frameworks) + 1:
        return
    if fidx < 1 or fidx > len(frameworks):
        print("  Invalid choice.")
        return
    framework = frameworks[fidx - 1]

    generator_py = root / "generate-tests" / "generator.py"
    if not generator_py.exists():
        print(f"[-] Generator not found: {generator_py}")
        return
    env = os.environ.copy()
    generator_args = [sys.executable, str(generator_py), "--strategy", strategy, "--framework", framework]
    if discovery_config is not None:
        cache_path = discovery_config.SITE_STATE_DIR / f"{discovery_config.SITE_STATE_DIR.name}_rubric.json"
        if cache_path.exists():
            env["COMPONENT_RUBRIC_CACHE_JSON"] = str(cache_path)
            generator_args.extend(["--component-rubric", str(cache_path)])
            print(f"[*] Using component rubric cache: {cache_path.name} (tools + capabilities will append after framework)")
    print(f"[*] Generating tests: strategy={strategy}, framework={framework}...")
    result = subprocess.run(
        generator_args,
        cwd=str(root),
        env=env,
    )
    if result.returncode == 0:
        framework_output = f"generate-tests/{strategy}/{framework.replace('_', '-')}.json"
        print(f"[+] Done. Output: {framework_output}")
    else:
        print(f"[!] Generator exited with code {result.returncode}.")


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
        print("  7) Exit")
        print()
        try:
            choice = input("  Choice [1-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice:
            continue
        if choice == "7" or choice.lower() == "q":
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
                )
                if (discovery_config.SITE_STATE_DIR / "component_assessment.json").exists():
                    import shutil
                    shutil.copy2(
                        discovery_config.SITE_STATE_DIR / "component_assessment.json",
                        log_dir / "component_assessment.json",
                    )
                print("[+] Tools & caps done.")
                print("[*] Full configuration: 4) Refresh...")
                from component_discovery import auth
                ok = asyncio.run(auth.refresh_session())
                print("[+] Session refreshed." if ok else "[-] Refresh failed.")
                print(f"[+] Full configuration complete. Logs: {log_dir}")
            except Exception as e:
                print(f"[!] Full configuration failed: {e}")
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
                )
                assessment_path = discovery_config.SITE_STATE_DIR / "component_assessment.json"
                if assessment_path.exists():
                    import shutil
                    shutil.copy2(assessment_path, log_dir / "component_assessment.json")
                    print(f"[+] component_assessment.json written ({assessment_path}; copy in {log_dir})")
                else:
                    print("[+] Availability logs written; assessment may have been skipped (e.g. no Gemini key).")
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
        else:
            print("  Invalid choice.")
    print("  Bye.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified AIRTA pipeline: menu (discovery/diagnostics) or full run (discovery, diagnostics, tests, risk assessment).",
    )
    sub = parser.add_subparsers(dest="command", help="Command")
    run_p = sub.add_parser("run", help="Run full pipeline (discovery, diagnostics, compliance tests, risk assessment).")
    menu_p = sub.add_parser("menu", help="Interactive menu: run Discovery or Diagnostics only (default if no command).")

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
    # Summary
    print("\n=== Summary ===")
    print(f"  Adversarial assessed: {len(report.get('adversarial_results', []))}")
    mandate_rollup = report.get("mandate_rollup", {})
    if mandate_rollup:
        for m, level in sorted(mandate_rollup.items(), key=lambda x: x[0]):
            print(f"  {m[:50]}: {level}")
    print(f"  Calibration: {report['calibration_ok_count']}/{report['calibration_count']} OK")


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
            )
            assessment_path = discovery_config.SITE_STATE_DIR / "component_assessment.json"
            if assessment_path.exists():
                import shutil
                shutil.copy2(assessment_path, run_log_dir / "component_assessment.json")
                print(f"[+] component_assessment.json written to run log.")
        except Exception as e:
            print(f"[!] Availability/assessment failed: {e}")

    # 3. Run compliance tests
    from pipeline.run_tests import run_compliance_tests

    compliance_log_path = asyncio.run(run_compliance_tests(test_file, log_dir=run_log_dir, verbose=True, speed=args.speed))
    if compliance_log_path is None:
        print("[-] Compliance test run produced no log. Exiting.")
        return None

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
        "component_assessment": str(run_log_dir / "component_assessment.json") if (run_log_dir / "component_assessment.json").exists() else None,
        "adversarial_results": risk_results,
        "mandate_rollup": mandate_rollup,
        "calibration_count": len(calibration_results),
        "calibration_ok_count": sum(1 for r in calibration_results if r.get("ok")),
    }
    report_path = run_log_dir / "pipeline_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] Pipeline report: {report_path}")
    if getattr(args, "report_dir", None) is not None:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        copy_report = args.report_dir if args.report_dir.is_absolute() else root / args.report_dir
        copy_report.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(report_path, copy_report / f"pipeline_report_{run_timestamp}.json")

    return report


if __name__ == "__main__":
    main()
