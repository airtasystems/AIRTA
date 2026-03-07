"""
Agent that reads llm_api_guide.json and generates a Playwright headless script
to ask a question (e.g. "what is the capital of america") via the chat UI.
Always uses template + trace-derived UI hints when available; generic selectors when no trace.
"""
import json
from pathlib import Path

from .playwright_script_template import render_playwright_script
from ..methods.trace_parser import extract_ui_hints

_AGENTS_DIR = Path(__file__).resolve().parent
_PRE_DISCOVERY = _AGENTS_DIR.parent

DEFAULT_GUIDE_PATH = _PRE_DISCOVERY / "format" / "llm_api_guide.json"
DEFAULT_PROMPT = "what is the capital of america"
DEFAULT_OUTPUT = _PRE_DISCOVERY / "format" / "ask_capital_script.py"


def generate_playwright_script(
    guide_path: Path | None = None,
    prompt: str = DEFAULT_PROMPT,
    output_path: Path | None = None,
    format_dir: Path | None = None,
) -> Path | None:
    """
    Read llm_api_guide.json and generate a Playwright headless script that asks the prompt.
    When format_dir is provided and trace exists, uses template + UI hints (no LLM).
    Otherwise falls back to LLM generation.
    Returns path to the generated script, or None on failure.
    """
    format_dir = format_dir or (guide_path.parent if guide_path else _PRE_DISCOVERY / "format")
    guide_path = guide_path or Path(format_dir) / "llm_api_guide.json"
    output_path = output_path or Path(format_dir) / "ask_capital_script.py"

    print(f"[*] Loading guide from {guide_path}...")
    if not guide_path.exists():
        print(f"[-] Guide not found: {guide_path}")
        return None

    try:
        guide = json.loads(guide_path.read_text(encoding="utf-8"))
        app_url = guide.get("app_url") or guide.get("base_url") or "?"
        n_post = len(guide.get("post", {}))
        n_get = len(guide.get("get", {}))
        print(f"[+] Guide loaded (app_url: {app_url}, {n_post} POST, {n_get} GET endpoints)")
    except json.JSONDecodeError as e:
        print(f"[-] Invalid JSON in guide: {e}")
        return None

    base_url = guide.get("app_url") or guide.get("base_url")
    chat_page = guide.get("get", {}).get("chat_page", {})
    if chat_page.get("example_url"):
        app_url = chat_page["example_url"]
    elif chat_page.get("path"):
        app_url = f"{base_url.rstrip('/')}{chat_page['path']}"
    else:
        app_url = base_url
    print(f"[*] Using chat page URL: {app_url}")

    # Resolve UI hints: from guide["ui"] or from trace (never LLM - always template)
    ui = guide.get("ui")
    trace_path = Path(format_dir) / "playwright" / "trace.trace"
    if (not ui or (not ui.get("chat_input_selectors") and not ui.get("response_container"))) and trace_path.exists():
        hints = extract_ui_hints(trace_path)
        if hints:
            ui = {
                "chat_input_selectors": hints.get("chat_input_selectors", []),
                "response_container": hints.get("response_container"),
                "response_extraction": "text_after_prompt" if hints.get("response_container") else "selectors",
                "consent_selectors": hints.get("consent_selectors", []),
            }
            print(f"[+] UI hints from trace: {len(ui.get('chat_input_selectors', []))} input, container={ui.get('response_container')}")
        elif trace_path.exists():
            print(f"[!] Trace parse failed or empty; using generic selectors only")
    if not ui:
        ui = {
            "chat_input_selectors": [],
            "response_container": None,
            "response_extraction": "selectors",
            "consent_selectors": [],
        }
    if not trace_path.exists():
        print(f"[*] No Playwright trace; using generic selectors (script may fail for custom DOM)")

    # Always use template (deterministic); app-specific selectors when available, else generic
    input_selectors = ui.get("chat_input_selectors", [])
    response_container = ui.get("response_container")
    response_extraction = ui.get("response_extraction", "text_after_prompt" if response_container else "selectors")
    consent_selectors = ui.get("consent_selectors", [])
    response_selectors = [response_container] if response_container else []
    if response_extraction == "text_after_prompt" and response_container:
        response_selectors = [response_container]

    if input_selectors or response_container:
        print(f"[*] Using template (app-specific + generic selectors)")
    else:
        print(f"[*] Using template (generic selectors only)")

    text = render_playwright_script(
        app_url=app_url,
        prompt=prompt,
        input_selectors=input_selectors,
        response_selectors=response_selectors,
        response_extraction=response_extraction,
        consent_selectors=consent_selectors,
        use_stealth=True,
        use_evasion=True,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    n_lines = len(text.splitlines())
    print(f"[+] Script written to {output_path} ({n_lines} lines)")
    print(f"[*] Run with: python {output_path}")
    return output_path
