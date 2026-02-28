"""
AIRTA Streamlit UI. Run from project root:

  streamlit run ui/streamlit_app.py

Features: config, evasion, payload_format, auth, discover, send_payloads,
run_diagnostics, generate_site_payload (see main.py load_order).
"""
import sys
sys.dont_write_bytecode = True

import asyncio
import io
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import streamlit as st

# Project root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Load .config for sidebar defaults (COMPONENT, APP_URL). .env (e.g. GEMINI_API_KEY) is loaded by component-discovery when needed.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".config")
except ImportError:
    pass

# Lazy import main after cwd is set
def _ensure_paths():
    import main
    main._setup_paths(ROOT)


def _get_discovery_status(component_name: str):
    """Return (app_url, discovery_done, status_message). Uses .config and component-discovery paths (no cached config)."""
    from urllib.parse import urlparse
    app_url = os.getenv("APP_URL") or "—"
    sitename = (urlparse(app_url).netloc.replace(":", "") if app_url != "—" else "") or "default"
    cd = ROOT / "component-discovery"
    auth_file = cd / sitename / "site_config" / "auth_state.json"
    endpoint_file = cd / sitename / component_name / "discovered_endpoint.json"
    has_auth = auth_file.exists()
    has_endpoint = endpoint_file.exists()
    # If current component has no endpoint, check if any component under this site does (e.g. ran with "default")
    if not has_endpoint and has_auth:
        site_dir = cd / sitename
        if site_dir.exists():
            for d in site_dir.iterdir():
                if d.is_dir() and d.name != "site_config":
                    if (d / "discovered_endpoint.json").exists():
                        has_endpoint = True
                        break
    discovery_done = has_auth and has_endpoint
    if discovery_done:
        msg = "Discovery complete"
    elif has_auth:
        msg = "Auth saved. Run Discover endpoint (step 2) for this component to finish."
    else:
        msg = "Discovery not run"
    return app_url, discovery_done, msg


st.set_page_config(page_title="AIRTA", page_icon="🔬", layout="wide")

# Sidebar: global options (component and app URL default from .config)
_env_component = (os.getenv("COMPONENT") or "default").strip() or "default"
_env_app_url = (os.getenv("APP_URL") or "").strip()

with st.sidebar:
    st.header("AIRTA")
    component = st.text_input(
        "Component",
        value=_env_component,
        help="Discovery state component name (from .config COMPONENT when set)",
    )
    strategy = st.selectbox(
        "Strategy",
        ["zero_shot", "multi_shot", "few_shot", "iterative", "chain_of_thought", "prompt_chaining",
         "tree_of_thoughts", "self_consistency", "self_reflection", "directional_stimulus"],
        index=0,
    )
    framework = st.text_input("Framework", value="eu_ai_act", help="e.g. eu_ai_act, fria_core, owasp_llm")
    skip_discovery = st.checkbox("Skip discovery", value=False, help="Assume endpoint and auth exist")
    skip_diagnostics = st.checkbox("Skip diagnostics", value=False, help="Skip diagnostics before compliance tests")
    force_discovery = st.checkbox("Force discovery", value=False, help="Run discovery even if state exists")
    api_url = st.text_input(
        "Pipeline API URL (optional)",
        value="",
        placeholder="http://localhost:8000",
        help="If set, Run pipeline uses this API instead of running in-process",
    )
    st.divider()
    # Discovery status for current component (paths from .config + component name)
    app_url, discovery_done, status_msg = _get_discovery_status(component)
    st.caption(f"**App URL:** {app_url}")
    if discovery_done:
        st.success(status_msg)
    elif status_msg.startswith("Auth saved"):
        st.info(status_msg)
    else:
        st.warning(status_msg)

# Persist component for this session
os.environ["COMPONENT"] = component

# Auth confirmation flow (used by Discovery and Pipeline)
if "auth_login_event" not in st.session_state:
    st.session_state.auth_login_event = None
if "auth_thread" not in st.session_state:
    st.session_state.auth_thread = None
if "auth_waiting_login" not in st.session_state:
    st.session_state.auth_waiting_login = False
if "auth_result" not in st.session_state:
    st.session_state.auth_result = None
if "run_all_after_confirm" not in st.session_state:
    st.session_state.run_all_after_confirm = False
if "pipeline_after_confirm" not in st.session_state:
    st.session_state.pipeline_after_confirm = False
if "pipeline_args" not in st.session_state:
    st.session_state.pipeline_args = None

page = st.sidebar.radio(
    "Go to",
    ["Discovery", "Pipeline", "Diagnostics", "Config", "Reports"],
    index=0,
)


def _auth_waiter(event: threading.Event):
    async def wait():
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, event.wait)
    return wait


def _run_auth_with_confirm():
    event = threading.Event()
    st.session_state.auth_login_event = event
    st.session_state.auth_result = None

    def target():
        try:
            _ensure_paths()
            from component_discovery import auth
            asyncio.run(auth.capture_login_and_csrf(
                headless=False,
                wait_for_login=_auth_waiter(event),
                position_right_half=True,
            ))
            st.session_state.auth_result = {"done": True, "error": None}
        except Exception as e:
            st.session_state.auth_result = {"done": True, "error": str(e)}

    t = threading.Thread(target=target, daemon=True)
    t.start()
    st.session_state.auth_thread = t
    st.session_state.auth_waiting_login = True


if page == "Config":
    st.title("Config")
    _ensure_paths()
    try:
        from component_discovery import config as discovery_config
        st.json({
            "component": discovery_config.COMPONENT,
            "base_url": discovery_config.BASE_URL,
            "login_url": discovery_config.LOGIN_URL,
            "site_state_dir": str(discovery_config.SITE_STATE_DIR),
            "discovered_endpoint_file": str(discovery_config.DISCOVERED_ENDPOINT_FILE),
            "auth_state_file": str(discovery_config.AUTH_STATE_FILE),
            "has_discovered_endpoint": discovery_config.DISCOVERED_ENDPOINT_FILE.exists(),
            "has_auth_state": discovery_config.AUTH_STATE_FILE.exists(),
        })
    except Exception as e:
        st.error(str(e))

elif page == "Discovery":
    st.title("Discovery")
    st.caption("Auth (capture login + CSRF) → Discover endpoint → Generate site payload")

    # Always-visible instructions at top
    st.info(
        "**Step 1:** Click **\"1. Capture login & CSRF\"** → a browser opens on the **right half** of your screen. "
        "Log in (and MFA if needed) in that browser. When finished, come back here and click **\"Confirm login\"**. "
        "**Step 2:** Click **\"2. Discover endpoint\"** and make one LLM request in the app. "
        "**Step 3:** Click **\"3. Generate site payload\"** to generate the payload module."
    )
    with st.expander("Detailed instructions"):
        st.markdown("""
        1. **Capture login & CSRF** — A browser window opens on the **right half** of your screen so this UI stays visible.  
           Log in (and complete MFA if required). When you are fully logged in and the app is loaded, click **Confirm login** below.

        2. **Discover endpoint** — With auth saved, the browser opens again; make **one** request to the LLM API (e.g. send a message).  
           We intercept that request and save the endpoint URL, method, headers, and payload schema.

        3. **Generate site payload** — Generates site-specific `payload_format.py` and `send_payloads.py` from the discovered schema.
        """)

    _ensure_paths()

    col1, col2, col3 = st.columns(3)
    with col1:
        run_auth = st.button("1. Capture login & CSRF")
    with col2:
        run_discover = st.button("2. Discover endpoint")
    with col3:
        run_generate = st.button("3. Generate site payload")
    run_all = st.button("Run all (1 → 2 → 3)")

    # Confirm login block — show when we're waiting for user to finish login
    if st.session_state.auth_waiting_login:
        st.warning("Browser is open on the **right half** of the screen. Complete login (and MFA if needed) in the browser, then click the button below.")
        if st.button("Confirm login", type="primary", key="discovery_confirm_login"):
            if st.session_state.auth_login_event:
                st.session_state.auth_login_event.set()
            with st.spinner("Saving session and extracting CSRF..."):
                if st.session_state.auth_thread:
                    st.session_state.auth_thread.join(timeout=60)
                st.session_state.auth_waiting_login = False
                st.session_state.auth_thread = None
                st.session_state.auth_login_event = None
                res = st.session_state.auth_result
                st.session_state.auth_result = None
                if res and res.get("done"):
                    if res.get("error"):
                        st.error(res["error"])
                    else:
                        st.success("Login and CSRF captured.")
                        # If "Run all" was used, continue with discover + generate
                        if st.session_state.get("run_all_after_confirm"):
                            st.session_state.run_all_after_confirm = False
                            try:
                                with st.spinner("Discovering endpoint..."):
                                    from component_discovery import discover
                                    asyncio.run(discover.discover_endpoint(headless=False, position_right_half=True))
                                with st.spinner("Generating payload module..."):
                                    from component_discovery import generate_site_payload
                                    generate_site_payload.generate_payload_module()
                                st.success("Discovery complete (auth + discover + generate).")
                            except Exception as e:
                                st.error(str(e))
                        elif st.session_state.get("pipeline_after_confirm") and st.session_state.get("pipeline_args"):
                            st.session_state.pipeline_after_confirm = False
                            args = st.session_state.pipeline_args
                            st.session_state.pipeline_args = None
                            try:
                                import main
                                with st.spinner("Running pipeline (diagnostics, tests, risk assessment)..."):
                                    report = main.run_pipeline(args)
                                if report:
                                    st.success("Pipeline complete.")
                                    st.json(report)
                                else:
                                    st.error("Pipeline failed.")
                            except Exception as e:
                                st.error(str(e))
            st.rerun()

    if run_auth and not st.session_state.auth_waiting_login:
        _run_auth_with_confirm()
        st.rerun()

    if run_discover:
        with st.spinner("Discovering endpoint (browser will open on right half)..."):
            try:
                _ensure_paths()
                from component_discovery import discover
                asyncio.run(discover.discover_endpoint(headless=False, position_right_half=True))
                st.success("Endpoint discovered.")
            except Exception as e:
                st.error(str(e))

    if run_generate:
        with st.spinner("Generating payload module..."):
            try:
                _ensure_paths()
                from component_discovery import generate_site_payload
                generate_site_payload.generate_payload_module()
                st.success("Site payload module generated.")
            except Exception as e:
                st.error(str(e))

    if run_all and not st.session_state.auth_waiting_login:
        st.session_state.run_all_after_confirm = True
        _run_auth_with_confirm()
        st.info("Complete login in the browser (right half), then click **Confirm login** above. After that, discovery and payload generation will run automatically.")
        st.rerun()

elif page == "Diagnostics":
    st.title("Diagnostics")
    st.caption("Send diagnostics payloads and run analyze_log → discovery.json")
    _ensure_paths()
    if st.button("Run diagnostics"):
        with st.spinner("Running diagnostics..."):
            try:
                import main
                from component_discovery import config as discovery_config
                from component_discovery import run_diagnostics
                diagnostics_path = ROOT / "diagnostics" / "diagnostics.json"
                if not diagnostics_path.exists():
                    st.warning("diagnostics/diagnostics.json not found.")
                else:
                    run_log_dir = discovery_config.SITE_STATE_DIR / "logs"
                    from datetime import datetime
                    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
                    log_dir = run_log_dir / ts
                    log_dir.mkdir(parents=True, exist_ok=True)
                    diag_log = asyncio.run(run_diagnostics.run_diagnostics_send(
                        discovery_config, diagnostics_path, log_dir=log_dir, verbose=True
                    ))
                    if diag_log:
                        from diagnostics import analyze_log
                        analyze_log.analyze_log_and_write_discovery(
                            discovery_config.SITE_STATE_DIR, diagnostics_log_path=diag_log
                        )
                        st.success(f"Diagnostics done. Log: {diag_log}")
                    else:
                        st.info("No diagnostics log produced.")
            except Exception as e:
                st.error(str(e))

elif page == "Pipeline":
    st.title("Run pipeline")
    st.caption("Discovery (optional) → Diagnostics → Compliance tests → Risk assessment → Report")

    # When discovery was started from Pipeline, show same confirm flow (browser on right half)
    if st.session_state.auth_waiting_login:
        st.warning("Browser is open on the **right half** of the screen. Complete login (and MFA if needed) in the browser, then click the button below.")
        if st.button("Confirm login", type="primary", key="pipeline_confirm_login"):
            if st.session_state.auth_login_event:
                st.session_state.auth_login_event.set()
            with st.spinner("Saving session and extracting CSRF..."):
                if st.session_state.auth_thread:
                    st.session_state.auth_thread.join(timeout=60)
                st.session_state.auth_waiting_login = False
                st.session_state.auth_thread = None
                st.session_state.auth_login_event = None
                res = st.session_state.auth_result
                st.session_state.auth_result = None
                if res and res.get("done"):
                    if res.get("error"):
                        st.error(res["error"])
                    elif st.session_state.get("pipeline_after_confirm") and st.session_state.get("pipeline_args"):
                        st.session_state.pipeline_after_confirm = False
                        args = st.session_state.pipeline_args
                        st.session_state.pipeline_args = None
                        try:
                            import main
                            with st.spinner("Running pipeline (diagnostics, tests, risk assessment)..."):
                                report = main.run_pipeline(args)
                            if report:
                                st.success("Pipeline complete.")
                                st.json(report)
                            else:
                                st.error("Pipeline failed.")
                        except Exception as e:
                            st.error(str(e))
                elif res and not res.get("error"):
                    st.success("Login and CSRF captured.")
            st.rerun()

    elif st.button("Run pipeline"):
        if api_url and api_url.strip():
            import urllib.request
            import json
            url = f"{api_url.rstrip('/')}/run/pipeline"
            data = json.dumps({
                "component": component,
                "strategy": strategy,
                "framework": framework,
                "skip_discovery": skip_discovery,
                "skip_diagnostics": skip_diagnostics,
                "force_discovery": force_discovery,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
            with st.spinner("Running pipeline via API (may take several minutes)..."):
                try:
                    with urllib.request.urlopen(req, timeout=3600) as resp:
                        report = json.loads(resp.read().decode())
                    st.success("Pipeline complete.")
                    st.json(report)
                except Exception as e:
                    st.error(str(e))
        else:
            _ensure_paths()
            from component_discovery import config as discovery_config
            need_discovery = not skip_discovery and (
                force_discovery
                or not discovery_config.DISCOVERED_ENDPOINT_FILE.exists()
                or not discovery_config.AUTH_STATE_FILE.exists()
            )
            if need_discovery:
                # Use UI flow: browser on right half + Confirm login, then run pipeline with skip_discovery
                st.session_state.pipeline_after_confirm = True
                st.session_state.pipeline_args = SimpleNamespace(
                    component=component,
                    strategy=strategy,
                    framework=framework,
                    test_file=None,
                    report_dir=None,
                    skip_discovery=True,
                    skip_diagnostics=skip_diagnostics,
                    force_discovery=False,
                )
                _run_auth_with_confirm()
                st.rerun()
            buf = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                import main
                args = SimpleNamespace(
                    component=component,
                    strategy=strategy,
                    framework=framework,
                    test_file=None,
                    report_dir=None,
                    skip_discovery=skip_discovery,
                    skip_diagnostics=skip_diagnostics,
                    force_discovery=force_discovery,
                )
                with st.spinner("Running pipeline (discovery, diagnostics, tests, risk assessment)..."):
                    report = main.run_pipeline(args)
                sys.stdout = old_stdout
                out = buf.getvalue()
                if out:
                    with st.expander("Log", expanded=False):
                        st.code(out)
                if report:
                    st.success("Pipeline complete.")
                    st.json(report)
                else:
                    st.error("Pipeline failed. Check log above.")
            except Exception as e:
                sys.stdout = old_stdout
                st.error(str(e))
                if buf.getvalue():
                    st.code(buf.getvalue())

elif page == "Reports":
    st.title("Reports")
    _ensure_paths()
    cd = ROOT / "component-discovery"
    reports = []
    if cd.exists():
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
                            import json
                            reports.append((run_dir, json.loads(rp.read_text(encoding="utf-8"))))
                        except Exception:
                            pass
    if not reports:
        st.info("No pipeline reports yet. Run the pipeline first.")
    else:
        for run_dir, report in reports[:20]:
            with st.expander(f"{report.get('timestamp', '')} — {run_dir}"):
                st.json(report)
                if st.button("Open run dir", key=str(run_dir)):
                    st.code(str(run_dir))
