#!/usr/bin/env python3
"""
AIRTA CLI — Generate adversarial test suites, discover targets, run tests, and assess risk.

Interactive mode (no subcommand):
  python main.py          Select site/component, then use the pipeline menu.

Direct subcommands:
  generate      Generate adversarial test prompts from rubrics.
  discover      Interactive browser-bot menu: login, create component config, manage sites.
  run           Run generated test suite against a browser target, convert log for risk-assess.
  risk-assess   Run multi-expert risk assessment on a compliance log → pipeline_report.json.
  export        Export a pipeline report to AIRTA Systems via bulk-import API.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_root = Path(__file__).resolve().parent

STRATEGIES = [
    "zero_shot", "multi_shot", "few_shot", "iterative", "chain_of_thought",
    "prompt_chaining", "tree_of_thoughts", "self_consistency", "self_reflection",
    "directional_stimulus",
]

try:
    from dotenv import load_dotenv
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
except ImportError:
    pass


def _setup_paths() -> None:
    """Make risk_level_agent importable from risk-level-agent/risk_level_agent.py."""
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    rla_file = _root / "risk-level-agent" / "risk_level_agent.py"
    if rla_file.exists() and "risk_level_agent" not in sys.modules:
        spec = importlib.util.spec_from_file_location("risk_level_agent", rla_file)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["risk_level_agent"] = mod
            spec.loader.exec_module(mod)


_browser_bot_dir = _root / "browser-bot"


def _get_frameworks() -> list[str]:
    rubrics_dir = _root / "rubrics"
    if not rubrics_dir.is_dir():
        return []
    out: list[str] = []
    for p in rubrics_dir.glob("*.json"):
        if p.stem in ("company", "component"):
            continue
        out.append(p.stem.replace("-", "_"))
    return sorted(out)


def _setup_browser_bot() -> None:
    """Add browser-bot to sys.path so its modules are importable."""
    bb = str(_browser_bot_dir)
    if bb not in sys.path:
        sys.path.insert(0, bb)


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def _run_generate(args) -> None:
    generator_py = _root / "generate-tests" / "generator.py"
    if not generator_py.exists():
        print(f"[-] Generator not found: {generator_py}")
        sys.exit(1)

    frameworks = _get_frameworks()
    if not frameworks:
        print("[-] No rubrics found in rubrics/. Add rubrics/*.json to enable frameworks.")
        sys.exit(1)

    env = os.environ.copy()
    component_rubric_args: list[str] = []
    if args.component_rubric:
        component_rubric_args = ["--component-rubric", str(Path(args.component_rubric).resolve())]
    # company rubric → COMPONENT_RUBRIC_JSON + COMPONENT_RUBRIC_CACHE_JSON
    company_rubric = getattr(args, "company_rubric", None) or getattr(args, "component_rubric", None)
    if company_rubric:
        env["COMPONENT_RUBRIC_JSON"] = str(Path(company_rubric).resolve())
        env["COMPONENT_RUBRIC_CACHE_JSON"] = str(Path(company_rubric).resolve())
    # component spec rubric → COMPONENT_SPEC_RUBRIC_JSON
    spec_rubric = getattr(args, "spec_rubric", None)
    if spec_rubric:
        env["COMPONENT_SPEC_RUBRIC_JSON"] = str(Path(spec_rubric).resolve())

    site_args: list[str] = []
    site = getattr(args, "site", "") or ""
    component = getattr(args, "component", "") or ""
    if site and component:
        site_args = ["--site", site, "--component", component]
        # Per-site company.json + per-component component.json (same layout as web/jobs.py).
        if not company_rubric or not spec_rubric:
            _setup_browser_bot()
            try:
                from browser_bot.sites import get_site_company_rubric_path, get_component_rubric_path

                if not company_rubric:
                    p_co = get_site_company_rubric_path(site)
                    if p_co:
                        env["COMPONENT_RUBRIC_JSON"] = str(p_co.resolve())
                        env["COMPONENT_RUBRIC_CACHE_JSON"] = str(p_co.resolve())
                if not spec_rubric:
                    p_sp = get_component_rubric_path(site, component)
                    if p_sp:
                        env["COMPONENT_SPEC_RUBRIC_JSON"] = str(p_sp.resolve())
            except ImportError:
                pass

    def gen_one(strategy: str, framework: str) -> None:
        cmd = [
            sys.executable, str(generator_py),
            "--strategy", strategy, "--framework", framework,
        ] + component_rubric_args + site_args
        print(f"[*] Generating: strategy={strategy}, framework={framework}...")
        result = subprocess.run(cmd, cwd=str(_root), env=env)
        if result.returncode == 0:
            if site and component:
                out = f"browser-bot/sites/{site}/{component}/tests/{strategy.replace('_', '-')}/{framework.replace('_', '-')}.json"
            else:
                out = f"generate-tests/{strategy.replace('_', '-')}/{framework.replace('_', '-')}.json"
            print(f"[+] Done: {out}")
        else:
            print(f"[!] Generator exited {result.returncode} for {strategy}/{framework}.")

    if args.all:
        total = len(STRATEGIES) * len(frameworks)
        n = 0
        for strat in STRATEGIES:
            for fw in frameworks:
                n += 1
                print(f"\n[{n}/{total}]")
                gen_one(strat, fw)
    elif args.all_frameworks:
        for i, fw in enumerate(frameworks, 1):
            print(f"\n[{i}/{len(frameworks)}]")
            gen_one(args.strategy, fw)
    elif args.all_strategies:
        for i, strat in enumerate(STRATEGIES, 1):
            print(f"\n[{i}/{len(STRATEGIES)}]")
            gen_one(strat, args.framework)
    else:
        gen_one(args.strategy, args.framework)


# ---------------------------------------------------------------------------
# risk-assess
# ---------------------------------------------------------------------------

def _run_risk_assess(args) -> None:
    _setup_paths()

    compliance_log_path = Path(args.compliance_log)
    if not compliance_log_path.is_absolute():
        compliance_log_path = Path.cwd() / compliance_log_path
    if not compliance_log_path.exists():
        print(f"[-] Compliance log not found: {compliance_log_path}")
        sys.exit(1)

    from pipeline.risk_assess import run_risk_assessment

    print(f"[*] Running risk assessment on: {compliance_log_path.name}")
    risk_results = run_risk_assessment(compliance_log_path)

    log_data = json.loads(compliance_log_path.read_text(encoding="utf-8"))
    all_log_results = log_data.get("results", [])

    compliance_by_id: dict[str, dict] = {r["id"]: r for r in all_log_results if "id" in r}
    for r in risk_results:
        entry_id = r.get("id", "")
        cl = compliance_by_id.get(entry_id, {})
        for field in ("description", "expected_behavior", "status", "ok", "error"):
            if field not in r:
                r[field] = cl.get(field)

    from pipeline.response_html import enrich_adversarial_results_with_response_html

    enrich_adversarial_results_with_response_html(risk_results)

    severity_order = ("critical", "high", "medium", "low", "informational", "compliant", "indeterminate")

    def severity_index(level: str) -> int:
        return severity_order.index(level) if level in severity_order else len(severity_order)

    mandate_rollup: dict[str, str] = {}
    for r in risk_results:
        m = r.get("mandate", "")
        if m:
            current = mandate_rollup.get(m, "compliant")
            new_level = r.get("risk_level", "indeterminate")
            if severity_index(new_level) < severity_index(current):
                mandate_rollup[m] = new_level

    log_dir = compliance_log_path.parent
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

    if args.report_dir:
        copy_dir = Path(args.report_dir)
        if not copy_dir.is_absolute():
            copy_dir = Path.cwd() / copy_dir
        copy_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(report_path, copy_dir / f"pipeline_report_{run_timestamp}.json")
        print(f"[+] Report copied to: {copy_dir}")


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

def _run_discover(args) -> None:
    _setup_browser_bot()
    from menu import main_loop
    main_loop()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def _latest_run_log(site: str, component: str) -> Path | None:
    logs_dir = _browser_bot_dir / "sites" / site / component / "logs"
    if not logs_dir.is_dir():
        return None
    logs = sorted(logs_dir.glob("run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _run_tests(args) -> None:
    suite_path = Path(args.suite)
    if not suite_path.is_absolute():
        suite_path = Path.cwd() / suite_path
    if not suite_path.exists():
        print(f"[-] Suite not found: {suite_path}")
        sys.exit(1)

    _setup_browser_bot()
    from browser_bot.config import infer_ui_mode_from_suite_raw

    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    mode = infer_ui_mode_from_suite_raw(suite) or "single"

    site = args.site
    component = args.component
    if not site or not component:
        from menu import select_site_and_component, current_site, current_component
        import menu
        if not select_site_and_component():
            print("[-] No site/component selected.")
            sys.exit(1)
        site = menu.current_site
        component = menu.current_component

    print(f"[*] Running tests: {site}/{component} ({suite_path.name}, mode={mode})...")
    bb_main_path = _browser_bot_dir / "main.py"
    bb_spec = importlib.util.spec_from_file_location("browser_bot_main", bb_main_path)
    bb_main = importlib.util.module_from_spec(bb_spec)
    bb_spec.loader.exec_module(bb_main)
    asyncio.run(bb_main.run_posts(site=site, component=component, mode=mode, suite_path=suite_path))

    run_log = _latest_run_log(site, component)
    if not run_log:
        print("[!] No run log found after test run.")
        return

    print(f"[+] Run log: {run_log}")

    from pipeline.convert_log import convert_run_log
    compliance_log = convert_run_log(run_log, suite_path)
    print(f"[+] Compliance log: {compliance_log}")

    if args.assess:
        print("\n[*] Running risk assessment...")
        _setup_paths()
        from pipeline.risk_assess import run_risk_assessment

        risk_results = run_risk_assessment(compliance_log)
        log_data = json.loads(compliance_log.read_text(encoding="utf-8"))
        compliance_by_id: dict[str, dict] = {
            r["id"]: r for r in log_data.get("results", []) if "id" in r
        }
        for r in risk_results:
            cl = compliance_by_id.get(r.get("id", ""), {})
            for field in ("description", "expected_behavior", "status", "ok", "error"):
                if field not in r:
                    r[field] = cl.get(field)

        from pipeline.response_html import enrich_adversarial_results_with_response_html

        enrich_adversarial_results_with_response_html(risk_results)

        severity_order = ("critical", "high", "medium", "low", "informational", "compliant", "indeterminate")
        def severity_index(level: str) -> int:
            return severity_order.index(level) if level in severity_order else len(severity_order)

        mandate_rollup: dict[str, str] = {}
        for r in risk_results:
            m = r.get("mandate", "")
            if m:
                current = mandate_rollup.get(m, "compliant")
                new_level = r.get("risk_level", "indeterminate")
                if severity_index(new_level) < severity_index(current):
                    mandate_rollup[m] = new_level

        run_timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        report = {
            "timestamp": run_timestamp,
            "framework": log_data.get("framework", ""),
            "source_file": log_data.get("source_file", ""),
            "run_log_dir": str(run_log.parent),
            "compliance_log": str(compliance_log),
            "adversarial_results": risk_results,
            "mandate_rollup": mandate_rollup,
        }
        report_path = compliance_log.parent / "pipeline_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"[+] Pipeline report: {report_path}")

        print("\n=== Summary ===")
        print(f"  Assessed: {len(risk_results)}")
        for m, level in sorted(mandate_rollup.items()):
            print(f"  {m[:60]}: {level}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _run_export(args) -> None:
    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path
    if not report_path.exists():
        print(f"[-] Pipeline report not found: {report_path}")
        sys.exit(1)

    host = os.getenv("AIRTASYSTEMS_HOST", "").strip() or args.host
    api_key = os.getenv("AIRTASYSTEMS_API_KEY", "").strip() or args.api_key
    program_id = os.getenv("AIRTASYSTEMS_PROGRAM_ID", "").strip() or args.program_id

    if not host:
        host = input("  AIRTA Systems host (e.g. app.airtasystems.com): ").strip()
    if not api_key:
        api_key = input("  API key (write:bulk_import scope): ").strip()
    if not program_id:
        program_id = input("  Program ID (MongoDB ObjectId): ").strip()
    if not host or not api_key or not program_id:
        print("[-] Host, API key, and Program ID are all required.")
        sys.exit(1)

    from pipeline.export_airta import export_pipeline_report
    export_pipeline_report(
        report_path,
        host=host,
        api_key=api_key,
        program_id=program_id,
        default_level=args.default_level,
    )


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

_session_site: str | None = None
_session_component: str | None = None


def _pick_numbered(
    label: str,
    items: list[str],
    *,
    create_label: str | None = None,
    display: list[str] | None = None,
) -> str | None:
    """Show a numbered list and return the chosen item from *items*, or None on cancel.

    Accepts a number or the item text (case-insensitive, partial prefix match).
    If *display* is set (same length as *items*), those strings are shown instead of *items*.
    If create_label is given, an extra option is appended for creating a new entry."""
    shown = display if display is not None and len(display) == len(items) else items
    total = len(items) + (1 if create_label else 0)
    print(f"\n  {label}")
    for i, item in enumerate(items, 1):
        print(f"    [{i}] {shown[i - 1]}")
    if create_label:
        print(f"    [{len(items) + 1}] {create_label}")
    choice = input(f"  Choice [1-{total}]: ").strip()
    if not choice:
        return None
    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(items):
            return items[idx - 1]
        if create_label and idx == len(items) + 1:
            return "__create__"
        print("  Invalid choice.")
        return None
    lower = choice.lower()
    for item in items:
        if item.lower() == lower or item.lower().replace("-", "_") == lower.replace("-", "_"):
            return item
    for i, item in enumerate(items):
        if shown[i].lower() == lower or shown[i].lower().replace("-", "_") == lower.replace("-", "_"):
            return item
    for item in items:
        if item.lower().startswith(lower):
            return item
    for i, item in enumerate(items):
        if shown[i].lower().startswith(lower):
            return item
    print(f"  '{choice}' not recognised.")
    return None


def _select_site_component() -> bool:
    """Prompt for site and component. Sets _session_site/_session_component. Returns True if set."""
    global _session_site, _session_component
    _setup_browser_bot()
    from browser_bot.sites import list_sites, list_components, ensure_site_dir, ensure_component_dir, get_domain_from_url

    sites = list_sites()
    choice = _pick_numbered("Select site:", sites, create_label="Create new site")
    if choice is None:
        return False
    if choice == "__create__":
        raw = input("\n  Enter domain or URL (e.g. example.com): ").strip()
        if not raw:
            return False
        domain = get_domain_from_url(raw) if "://" in raw or "/" in raw else raw.strip()
        if not domain:
            return False
        ensure_site_dir(domain)
        print(f"  Created sites/{domain}/")
        _session_site = domain
    else:
        _session_site = choice

    components = list_components(_session_site)
    choice = _pick_numbered(f"Select component for {_session_site}:", components, create_label="Create new component")
    if choice is None:
        _session_site = None
        return False
    if choice == "__create__":
        name = input("\n  New component name: ").strip()
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_")
        if not name:
            _session_site = None
            return False
        ensure_component_dir(_session_site, name)
        print(f"  Created sites/{_session_site}/{name}/")
        _session_component = name
    else:
        _session_component = choice

    print(f"\n  Using: {_session_site} / {_session_component}")
    return True


def _pretty_strategy_dir(slug: str) -> str:
    """Directory name e.g. zero-shot -> Zero-shot."""
    return "-".join(part.capitalize() for part in slug.split("-") if part)


def _pretty_framework_stem(stem: str) -> str:
    """Suite file stem e.g. eu-ai-act -> EU AI Act."""
    parts = stem.replace("_", "-").split("-")
    short_upper = {"eu", "ai", "uk", "us"}
    long_upper = {"oecd", "gdpr", "iso"}
    words: list[str] = []
    for p in parts:
        if not p:
            continue
        pl = p.lower()
        if pl in short_upper:
            words.append(p.upper())
        elif pl in long_upper:
            words.append(p.upper())
        else:
            words.append(p.capitalize())
    return " ".join(words)


def _discover_strategy_dirs(site: str, component: str) -> list[Path]:
    """Subdirs of tests/ that contain at least one JSON suite file."""
    tests = _browser_bot_dir / "sites" / site / component / "tests"
    if not tests.is_dir():
        return []
    return sorted(
        [p for p in tests.iterdir() if p.is_dir() and any(p.glob("*.json"))],
        key=lambda p: p.name.lower(),
    )


def _list_suites(site: str | None = None, component: str | None = None) -> list[Path]:
    """Return generated suite JSON files sorted by modification time (newest first).

    When *site* and *component* are set (interactive session), only suites under
    ``browser-bot/sites/<site>/<component>/tests/`` are listed.

    Otherwise scans ``generate-tests/`` and all ``browser-bot/sites/*/*/tests/``.
    """
    found: list[Path] = []
    sites_dir = _browser_bot_dir / "sites"
    if site and component:
        comp_tests = sites_dir / site / component / "tests"
        if comp_tests.is_dir():
            found.extend(comp_tests.rglob("*.json"))
        return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)

    gen_dir = _root / "generate-tests"
    if gen_dir.is_dir():
        found.extend(gen_dir.rglob("*.json"))
    if sites_dir.is_dir():
        found.extend(sites_dir.glob("*/*/tests/**/*.json"))
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def _list_compliance_logs() -> list[Path]:
    """Return compliance_log.json files under browser-bot/sites/."""
    sites_dir = _browser_bot_dir / "sites"
    if not sites_dir.is_dir():
        return []
    return sorted(sites_dir.rglob("compliance_log.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _list_pipeline_reports() -> list[Path]:
    """Return pipeline_report.json files under browser-bot/sites/."""
    sites_dir = _browser_bot_dir / "sites"
    if not sites_dir.is_dir():
        return []
    return sorted(sites_dir.rglob("pipeline_report.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def _menu_generate() -> None:
    frameworks = _get_frameworks()
    if not frameworks:
        print("  [-] No rubrics found in rubrics/.")
        return

    choice = _pick_numbered("Select framework:", [f.replace("_", "-") for f in frameworks])
    if not choice:
        return
    framework = choice.replace("-", "_")

    choice = _pick_numbered(
        "Select strategy:",
        [s.replace("_", "-") for s in STRATEGIES],
        create_label="All strategies (run every strategy for this framework)",
    )
    if not choice:
        return
    all_strategies = choice == "__create__"
    strategy = "zero_shot" if all_strategies else choice.replace("-", "_")

    # Resolve per-site company rubric and per-component spec rubric, falling back to globals.
    company_rubric_path: str | None = None
    spec_rubric_path: str | None = None
    if _session_site and _session_component:
        _setup_browser_bot()
        from browser_bot.sites import get_site_company_rubric_path, get_component_rubric_path
        _co = get_site_company_rubric_path(_session_site)
        _sp = get_component_rubric_path(_session_site, _session_component)
        company_rubric_path = str(_co) if _co else None
        spec_rubric_path = str(_sp) if _sp else None

    if not company_rubric_path:
        _global_co = _root / "rubrics" / "company.json"
        company_rubric_path = str(_global_co) if _global_co.exists() else None
    if not spec_rubric_path:
        _global_sp = _root / "rubrics" / "component.json"
        spec_rubric_path = str(_global_sp) if _global_sp.exists() else None

    args = SimpleNamespace(
        strategy=strategy,
        framework=framework,
        component_rubric=spec_rubric_path,
        company_rubric=company_rubric_path,
        spec_rubric=spec_rubric_path,
        all=False,
        all_frameworks=False,
        all_strategies=all_strategies,
        site=_session_site or "",
        component=_session_component or "",
    )
    _run_generate(args)


def _menu_discover() -> None:
    _setup_browser_bot()
    import menu as bb_menu
    bb_menu.current_site = _session_site
    bb_menu.current_component = _session_component
    bb_menu.main_loop()


def _menu_run() -> None:
    site = _session_site or ""
    comp = _session_component or ""
    strategy_dirs = _discover_strategy_dirs(site, comp)
    if not strategy_dirs:
        print(
            f"  [-] No test suites under browser-bot/sites/{site}/{comp}/tests/. "
            "Run 'Generate tests' first."
        )
        return

    # Collect unique framework stems across all strategy dirs
    fw_stems_set: set[str] = set()
    for sd in strategy_dirs:
        for f in sd.glob("*.json"):
            fw_stems_set.add(f.stem)
    fw_stems = sorted(fw_stems_set)
    if not fw_stems:
        print("  [-] No test suite files found. Run 'Generate tests' first.")
        return

    fw_labels = [_pretty_framework_stem(s) for s in fw_stems]
    choice_fw = _pick_numbered("Select framework:", fw_stems, display=fw_labels)
    if not choice_fw:
        return

    # Find strategy dirs that have this framework
    available_strats = [sd for sd in strategy_dirs if (sd / f"{choice_fw}.json").exists()]
    if not available_strats:
        print(f"  [-] No strategy suites found for framework '{choice_fw}'.")
        return

    strat_slugs = [p.name for p in available_strats]
    strat_labels = [_pretty_strategy_dir(s) for s in strat_slugs]
    choice_strat = _pick_numbered(
        "Select strategy:",
        strat_slugs,
        display=strat_labels,
        create_label="All strategies (run all for this framework)",
    )
    if not choice_strat:
        return

    assess = input("\n  Run risk assessment after? [y/N]: ").strip().lower() == "y"

    if choice_strat == "__create__":
        total = len(available_strats)
        for i, sd in enumerate(available_strats, 1):
            suite_path = sd / f"{choice_fw}.json"
            print(f"\n[{i}/{total}] Running: {sd.name}/{choice_fw}")
            _run_tests(SimpleNamespace(suite=str(suite_path), site=site, component=comp, assess=assess))
    else:
        suite_path = next(sd for sd in available_strats if sd.name == choice_strat) / f"{choice_fw}.json"
        _run_tests(SimpleNamespace(suite=str(suite_path), site=site, component=comp, assess=assess))


def _menu_risk_assess() -> None:
    logs = _list_compliance_logs()
    if not logs:
        path_input = input("\n  Path to compliance_log.json: ").strip()
        if not path_input:
            return
        args = SimpleNamespace(compliance_log=path_input, report_dir=None)
    else:
        labels = [str(p.relative_to(_root)) for p in logs]
        choice = _pick_numbered("Select compliance log:", labels)
        if not choice:
            return
        args = SimpleNamespace(compliance_log=str(_root / choice), report_dir=None)
    _run_risk_assess(args)


def _menu_export() -> None:
    reports = _list_pipeline_reports()
    if not reports:
        path_input = input("\n  Path to pipeline_report.json: ").strip()
        if not path_input:
            return
        args = SimpleNamespace(report=path_input, host="", api_key="", program_id="", default_level=None)
    else:
        labels = [str(p.relative_to(_root)) for p in reports]
        choice = _pick_numbered("Select pipeline report:", labels)
        if not choice:
            return
        args = SimpleNamespace(report=str(_root / choice), host="", api_key="", program_id="", default_level=None)
    _run_export(args)


def _menu_clear_cache() -> None:
    confirm = input("\n  Delete server-side Gemini cached content? [y/N]: ").strip().lower()
    delete_on_server = confirm == "y"

    cleared: list[str] = []

    # Generator cache (core.py)
    try:
        gen_tests_dir = str(_root / "generate-tests")
        if gen_tests_dir not in sys.path:
            sys.path.insert(0, gen_tests_dir)
        import core as gen_core
        gen_core.clear_gemini_cache(delete_on_server=delete_on_server)
        cleared.append("generator")
    except Exception as exc:
        print(f"  [!] Generator cache clear failed: {exc}")

    # Risk-level-agent cache (risk_level_agent.py)
    try:
        _setup_paths()
        import risk_level_agent as rla
        rla.clear_gemini_cache(delete_on_server=delete_on_server)
        cleared.append("risk-level-agent")
    except Exception as exc:
        print(f"  [!] Risk-level-agent cache clear failed: {exc}")

    if cleared:
        action = "Cleared in-process + deleted server-side" if delete_on_server else "Cleared in-process"
        print(f"  [+] {action} Gemini cache ({', '.join(cleared)}).")
    else:
        print("  [-] Nothing was cleared.")


def _menu_edit_rubrics() -> None:
    """Create/edit per-site company.json and per-component component.json."""
    if not _session_site:
        print("  No site selected.")
        return

    _setup_browser_bot()
    from browser_bot.sites import (
        get_site_company_rubric_path,
        get_component_rubric_path,
        ensure_site_dir,
        ensure_component_dir,
    )

    global_company = _root / "rubrics" / "company.json"
    global_component = _root / "rubrics" / "component.json"

    site_company = _browser_bot_dir / "sites" / _session_site / "company.json"
    comp_component = (
        _browser_bot_dir / "sites" / _session_site / _session_component / "component.json"
        if _session_component else None
    )

    print(f"\n  Rubrics for {_session_site}" + (f"/{_session_component}" if _session_component else ""))
    print(f"\n  [1] Edit site company rubric")
    site_label = "(exists)" if site_company.exists() else f"(will copy from {global_company.name})"
    print(f"      {site_company.relative_to(_browser_bot_dir)} {site_label}")
    if comp_component:
        comp_label = "(exists)" if comp_component.exists() else f"(will copy from {global_component.name})"
        print(f"  [2] Edit component rubric")
        print(f"      {comp_component.relative_to(_browser_bot_dir)} {comp_label}")
    print(f"  [3] Back")

    max_choice = 3 if comp_component else 2
    choice = input(f"\n  Choice [1-{max_choice}]: ").strip()

    if choice == "1":
        ensure_site_dir(_session_site)
        if not site_company.exists():
            if global_company.exists():
                import shutil
                shutil.copy2(global_company, site_company)
                print(f"  Copied global company.json -> {site_company}")
            else:
                site_company.write_text("{}\n", encoding="utf-8")
                print(f"  Created empty {site_company}")
        print(f"\n  Edit: {site_company}")
        print("  (Open in editor, save when done, then press Enter to continue...)")
        input()

    elif choice == "2" and comp_component:
        ensure_component_dir(_session_site, _session_component)
        if not comp_component.exists():
            if global_component.exists():
                import shutil
                shutil.copy2(global_component, comp_component)
                print(f"  Copied global component.json -> {comp_component}")
            else:
                comp_component.write_text("{}\n", encoding="utf-8")
                print(f"  Created empty {comp_component}")
        print(f"\n  Edit: {comp_component}")
        print("  (Open in editor, save when done, then press Enter to continue...)")
        input()


def _show_menu() -> None:
    ctx = f" [{_session_site}/{_session_component}]" if _session_site and _session_component else ""
    print("\n" + "=" * 50)
    print(f"  AIRTA{ctx}")
    print("=" * 50)
    print("  1. Generate tests")
    print("  2. Discovery (browser-bot)")
    print("  3. Run tests")
    print("  4. Risk assessment")
    print("  5. Export to AIRTA Systems")
    print("  6. Change site/component")
    print("  7. Edit rubrics")
    print("  8. Clear Gemini cache")
    print("  9. Exit")
    print("=" * 50)


def _interactive_menu() -> None:
    while not (_session_site and _session_component):
        if not _select_site_component():
            print("  Bye.")
            return

    while True:
        _show_menu()
        choice = input("  Choice [1-9]: ").strip()
        if choice == "1":
            _menu_generate()
        elif choice == "2":
            _menu_discover()
        elif choice == "3":
            _menu_run()
        elif choice == "4":
            _menu_risk_assess()
        elif choice == "5":
            _menu_export()
        elif choice == "6":
            _select_site_component()
        elif choice == "7":
            _menu_edit_rubrics()
        elif choice == "8":
            _menu_clear_cache()
        elif choice == "9":
            print("\n  Bye.")
            break
        else:
            print("  Invalid choice.")


# ---------------------------------------------------------------------------
# CLI (argparse for direct subcommand use)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AIRTA — generate adversarial test suites and run risk assessment.\n"
                    "Run with no subcommand for interactive menu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Command (omit for interactive menu)")

    def norm(s: str) -> str:
        return s.strip().replace("-", "_")

    # --- generate ---
    gen_p = sub.add_parser("generate", help="Generate adversarial test prompts from rubrics.")
    gen_p.add_argument("--strategy", type=norm, choices=STRATEGIES, default="zero_shot",
                       help="Prompt strategy (default: zero_shot).")
    gen_p.add_argument("--framework", type=norm, default="eu_ai_act",
                       help="Framework rubric name (default: eu_ai_act).")
    gen_p.add_argument("--component-rubric", metavar="PATH",
                       help="Path to component rubric JSON (optional context for generation).")
    gen_p.add_argument("--all", action="store_true",
                       help="Generate all strategies x all frameworks.")
    gen_p.add_argument("--all-frameworks", action="store_true",
                       help="Generate all frameworks for the given strategy.")
    gen_p.add_argument("--all-strategies", action="store_true",
                       help="Generate all strategies for the given framework.")

    # --- discover ---
    sub.add_parser("discover", help="Interactive browser-bot menu: login, create component config, manage sites.")

    # --- run ---
    run_p = sub.add_parser("run", help="Run a generated test suite against a browser target.")
    run_p.add_argument("suite", help="Path to generated suite JSON (e.g. generate-tests/zero-shot/eu-ai-act.json).")
    run_p.add_argument("--site", default="", help="browser-bot site (domain). Interactive picker if omitted.")
    run_p.add_argument("--component", default="", help="browser-bot component. Interactive picker if omitted.")
    run_p.add_argument("--assess", action="store_true",
                       help="Immediately run risk assessment after the test run.")

    # --- risk-assess ---
    risk_p = sub.add_parser("risk-assess", help="Run risk assessment on a compliance log.")
    risk_p.add_argument("compliance_log", help="Path to compliance_log.json.")
    risk_p.add_argument("--report-dir", metavar="DIR",
                        help="Also copy pipeline_report.json to this directory.")

    # --- export ---
    exp_p = sub.add_parser("export", help="Export pipeline report to AIRTA Systems.")
    exp_p.add_argument("report", help="Path to pipeline_report.json.")
    exp_p.add_argument("--host", default="", help="AIRTA Systems host (or set AIRTASYSTEMS_HOST).")
    exp_p.add_argument("--api-key", default="", help="AIRTA Systems API key (or set AIRTASYSTEMS_API_KEY).")
    exp_p.add_argument("--program-id", default="", help="Program ID (or set AIRTASYSTEMS_PROGRAM_ID).")
    exp_p.add_argument("--default-level", choices=["informational", "low", "medium", "critical"],
                        help="Override severity level for all results.")

    args = parser.parse_args()

    if args.command == "generate":
        _run_generate(args)
    elif args.command == "discover":
        _run_discover(args)
    elif args.command == "run":
        _run_tests(args)
    elif args.command == "risk-assess":
        _run_risk_assess(args)
    elif args.command == "export":
        _run_export(args)
    else:
        _interactive_menu()


if __name__ == "__main__":
    main()
