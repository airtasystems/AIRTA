"""
Gemini-powered intermediary: read discovered_endpoint.json, identify user-input
fields in the payload_schema, and generate site-specific payload_format.py and
send_payloads.py in the site dir (e.g. localhost3000).
"""
import json
import os
from pathlib import Path
from typing import Any

from .config import DISCOVERED_ENDPOINT_FILE, DIAGNOSTICS_FILE, SITE_STATE_DIR

# Optional: use google genai (same as A01/1_api_gemini.py)
try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parent.parent
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
    load_dotenv()
    from google import genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


GEMINI_MODEL = os.getenv("GEMINI_MODEL")

PROMPT_TEMPLATE = """You are analyzing an API request payload schema. Given the form/JSON fields and their example values below:

1. Identify which fields are filled in by the end-user vs fixed/session-specific.
2. Define the **payload shape**: the keys that each entry in payloads.json should have, with sample values. For simple forms use keys like "title" and "text". For richer APIs use the actual field names (e.g. companyName, url, projectType, urlContent) with realistic sample values. If a field value is a JSON string in the API, provide a valid JSON string (escaped) as the sample.
3. Map each payload key to the exact API field name in payload_key_to_field.

Return ONLY valid JSON in this exact shape (no markdown, no explanation):
{
  "user_input_fields": ["apiField1", "apiField2"],
  "payload_key_to_field": {
    "payloadKey1": "apiField1",
    "payloadKey2": "apiField2"
  },
  "payload_shape": {
    "payloadKey1": "sample value 1",
    "payloadKey2": "sample value 2"
  }
}

Rules:
- payload_shape has one entry per user-input field. Keys are the keys we use in payloads.json; values are sample values (strings, or JSON strings for nested structures). Use the same keys in payload_key_to_field.
- Do not truncate sample values; use complete example text (no ellipsis or abbreviated strings).
- payload_key_to_field maps every key in payload_shape to the exact form/API field name (often 1:1).
- user_input_fields lists the exact form/API field names that the user fills in.
- For APIs that expect a JSON string (e.g. urlContent), set payload_shape.urlContent to a valid JSON string sample.

Payload format (encoding and fields with example values):
%s
"""


def _call_gemini(fields: dict[str, Any], encoding: str) -> dict[str, Any]:
    if not _GEMINI_AVAILABLE:
        raise RuntimeError("google-genai not installed. pip install google-genai")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment or .env")
    client = genai.Client(api_key=api_key)
    payload_desc = json.dumps({"encoding": encoding, "fields": fields}, indent=2)
    prompt = PROMPT_TEMPLATE % payload_desc
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = response.text.strip()
    # Strip markdown code block if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def _parent_package_name() -> str:
    """Package that contains this module; use underscores so generated imports are valid Python."""
    raw = __name__.rsplit(".", 1)[0] or "component_discovery"
    return raw.replace("-", "_")


def _generate_payload_format_py(
    payload_key_to_field: dict[str, str],
    *,
    has_strategies: bool = False,
) -> str:
    """Generate site-specific payload_format.py: self-contained build logic, no shared import.
    When has_strategies is True, build_body accepts strategy='zero_shot'|'few_shot'|'multi_shot'
    and uses the corresponding format from discovered_endpoint.json strategies.
    """
    mapping_repr = json.dumps(payload_key_to_field, indent=2)
    strategy_loader = ""
    build_body_signature = "def build_body(payload_format: dict[str, Any], overrides: dict[str, Any] | None = None) -> tuple[str, str]:"
    strategy_switch = ""
    import_copy = ""
    if has_strategies:
        import_copy = "import copy\n"
        strategy_loader = '''
# Load strategy-specific formats from discovered_endpoint.json (zero/few/multi-shot)
_here = Path(__file__).resolve().parent
_DISCOVERED_FILE = _here / "discovered_endpoint.json"
STRATEGIES = {}
if _DISCOVERED_FILE.exists():
    try:
        _data = json.loads(_DISCOVERED_FILE.read_text())
        STRATEGIES = _data.get("strategies") or {}
    except Exception:
        pass
'''
        build_body_signature = 'def build_body(payload_format: dict[str, Any], overrides: dict[str, Any] | None = None, strategy: str = "zero_shot") -> tuple[str, str]:'
        strategy_switch = """
    if strategy in STRATEGIES and STRATEGIES[strategy].get("payload_format"):
        payload_format = copy.deepcopy(STRATEGIES[strategy]["payload_format"])
"""

    return f'''"""
Site-specific payload format (generated by Gemini). Self-contained: builds request
bodies for this site only. Varies per site; do not depend on the shared payload_format.
Handles both flat string fields and structured fields (arrays/objects) via JSON coercion.
"""
import json
{import_copy}from pathlib import Path
from typing import Any

# payload_key -> form field name (from Gemini analysis)
PAYLOAD_KEY_TO_FIELD = {mapping_repr}
{strategy_loader}


def _coerce_value(v: Any) -> Any:
    """If v is a JSON string (array/object), return parsed value so body has correct types."""
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("[") or s.startswith("{{")) and len(s) > 1:
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
    return v


def _build_multipart(boundary: str, fields: dict[str, str]) -> str:
    lines = []
    for name, value in fields.items():
        lines.append(f"--{{boundary}}\\r\\nContent-Disposition: form-data; name=\\"{{name}}\\"\\r\\n\\r\\n{{value}}\\r\\n")
    lines.append(f"--{{boundary}}--\\r\\n")
    return "".join(lines)


{build_body_signature}
    """Merge overrides via PAYLOAD_KEY_TO_FIELD and build body. strategy selects zero_shot/few_shot/multi_shot format when present."""
{strategy_switch}
    overrides = overrides or {{}}
    field_overrides = {{}}
    for k, v in overrides.items():
        api_key = PAYLOAD_KEY_TO_FIELD.get(k, k)
        field_overrides[api_key] = _coerce_value(v)
    encoding = payload_format.get("encoding", "unknown")
    fields = dict(payload_format.get("fields", {{}}))
    fields.update(field_overrides)
    if encoding == "multipart/form-data":
        boundary = payload_format.get("boundary", "----formboundary")
        str_fields = {{k: v if isinstance(v, str) else json.dumps(v) for k, v in fields.items()}}
        body = _build_multipart(boundary, str_fields)
        return body, f"multipart/form-data; boundary={{boundary}}"
    if encoding == "application/json":
        return json.dumps(fields), "application/json"
    return json.dumps(fields), "application/json"
'''


def _generate_send_payloads_py() -> str:
    """Generate site-specific send_payloads.py that uses local payload_format."""
    return '''"""
Site-specific send_payloads: uses this site's payload_format (generated by Gemini).
Package name from parent dir; payload_format loaded by path so we get this dir's build_body.
"""
import asyncio
import importlib
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

_here = Path(__file__).resolve().parent
# Layout: package_dir / sitename / component / send_payloads.py -> package is 2 levels up
_pkg_dir = _here.parent.parent
_pkg = _pkg_dir.name.replace("-", "_")
if str(_pkg_dir.parent) not in sys.path:
    sys.path.insert(0, str(_pkg_dir.parent))

auth_module = importlib.import_module(f"{_pkg}.auth")
_config_mod = importlib.import_module(f"{_pkg}.config")
AUTH_STATE_FILE = _config_mod.AUTH_STATE_FILE
CSRF_TOKEN_FILE = _config_mod.CSRF_TOKEN_FILE
DISCOVERED_ENDPOINT_FILE = _config_mod.DISCOVERED_ENDPOINT_FILE
PAYLOADS_FILE = _config_mod.PAYLOADS_FILE

# Load this dir's payload_format by path (not the package's parsing-only module)
_pf_spec = importlib.util.spec_from_file_location("site_payload_format", _here / "payload_format.py")
payload_format_module = importlib.util.module_from_spec(_pf_spec)
_pf_spec.loader.exec_module(payload_format_module)


def _load_csrf() -> str:
    if not CSRF_TOKEN_FILE.exists():
        return ""
    try:
        data = json.loads(CSRF_TOKEN_FILE.read_text())
        return data.get("csrf_token", "")
    except (json.JSONDecodeError, OSError):
        return ""


async def send_payloads() -> None:
    if not DISCOVERED_ENDPOINT_FILE.exists():
        print(f"[-] No discovered endpoint at {DISCOVERED_ENDPOINT_FILE}. Run 'discover' first.")
        return
    if not PAYLOADS_FILE.exists():
        print(f"[-] No payloads file at {PAYLOADS_FILE}. Create one with a 'payloads' array of {{'title', 'text'}}.")
        return
    if not AUTH_STATE_FILE.exists():
        print(f"[-] No session at {AUTH_STATE_FILE}. Run 'login' first.")
        return

    discovered = json.loads(DISCOVERED_ENDPOINT_FILE.read_text())
    payload_format = discovered.get("payload_format")
    if not payload_format or not payload_format.get("fields"):
        print("[-] No payload_format in discovered endpoint. Run 'discover' then 'generate-payload-module'.")
        return

    payloads_data = json.loads(PAYLOADS_FILE.read_text())
    payloads = payloads_data.get("payloads", payloads_data) if isinstance(payloads_data, dict) else payloads_data
    if not payloads:
        print("[-] No payloads in file (expect 'payloads' array).")
        return

    if not await auth_module.ensure_session_fresh():
        print("[-] Session refresh failed. Run 'login' or 'refresh' and try again.")
        return

    url = discovered["url"]
    headers = dict(discovered.get("headers", {}))
    headers.pop("content-length", None)
    headers.pop("host", None)
    headers.pop("accept-encoding", None)
    csrf = _load_csrf()
    if csrf:
        headers["X-CSRF-Token"] = csrf
        headers["X-XSRF-TOKEN"] = csrf

    results = []
    async with async_playwright() as p:
        api_context = await p.request.new_context(storage_state=str(AUTH_STATE_FILE))
        try:
            for i, p_item in enumerate(payloads):
                overrides = {}
                for k, v in p_item.items():
                    if v is None:
                        continue
                    if isinstance(v, str):
                        overrides[k] = v
                    elif isinstance(v, (dict, list)):
                        overrides[k] = json.dumps(v)
                    else:
                        overrides[k] = str(v)
                label = p_item.get("title") or p_item.get("companyName") or (list(p_item.values())[0] if p_item else f"Payload {i+1}")
                if isinstance(label, (dict, list)):
                    label = str(label)
                body, content_type = payload_format_module.build_body(payload_format, overrides)
                req_headers = {**headers, "Content-Type": content_type}
                try:
                    response = await api_context.post(url, headers=req_headers, data=body)
                    resp_text = await response.text()
                    results.append({"title": label, "status": response.status, "ok": response.ok, "response": resp_text})
                    if not response.ok:
                        print(f"  [{response.status}] {label}" + (f" — {resp_text}" if resp_text else ""))
                    else:
                        print(f"  [{response.status}] {label}")
                        print(f"    response: {resp_text}")
                except Exception as e:
                    results.append({"title": label, "status": None, "ok": False, "error": str(e), "response": None})
                    print(f"  [error] {label} — {e}")
        finally:
            await api_context.dispose()

    ok_count = sum(1 for r in results if r.get("ok"))
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = PAYLOADS_FILE.parent / f"{timestamp}_log.json"
    log_path.write_text(json.dumps({"timestamp": timestamp, "results": results}, indent=2), encoding="utf-8")
    print(f"[+] Log: {log_path}")
    print(f"\\n[*] Sent {len(results)} payloads, {ok_count} OK.")
'''


def generate_payload_module() -> None:
    """
    Load discovered_endpoint.json, call Gemini to identify user-input fields,
    write SITE_STATE_DIR/payload_format.py and SITE_STATE_DIR/send_payloads.py.
    """
    if not DISCOVERED_ENDPOINT_FILE.exists():
        print(f"[-] No discovered endpoint at {DISCOVERED_ENDPOINT_FILE}. Run 'discover' first.")
        return

    discovered = json.loads(DISCOVERED_ENDPOINT_FILE.read_text())
    payload_format = discovered.get("payload_format") or {}
    fields = payload_format.get("fields", {})
    encoding = payload_format.get("encoding", "application/json")

    if not fields:
        print("[-] No payload_format.fields in discovered endpoint. Run 'discover' first.")
        return

    print("[*] Calling Gemini to identify user-input fields...")
    try:
        result = _call_gemini(fields, encoding)
    except Exception as e:
        print(f"[-] Gemini call failed: {e}")
        return

    user_input_fields = result.get("user_input_fields", [])
    payload_key_to_field = result.get("payload_key_to_field") or {"title": "title", "text": "description"}
    payload_shape = result.get("payload_shape")
    if not payload_shape or not isinstance(payload_shape, dict):
        payload_shape = {"title": "Example", "text": "Sample description or body text."}
    # Ensure every key in payload_shape is in payload_key_to_field (1:1 if missing)
    for k in payload_shape:
        if k not in payload_key_to_field:
            payload_key_to_field[k] = k
    # Fallbacks for simple APIs
    if "title" not in payload_key_to_field:
        payload_key_to_field["title"] = "title"
    if "text" not in payload_key_to_field and "text" in payload_shape:
        for name in ("description", "body", "content", "text"):
            if name in fields:
                payload_key_to_field["text"] = name
                break
        else:
            payload_key_to_field["text"] = list(fields.keys())[0] if fields else "description"

    print(f"[*] User-input fields: {user_input_fields}")
    print(f"[*] Payload key -> field: {payload_key_to_field}")
    print(f"[*] Payload shape (samples): {list(payload_shape.keys())}")

    SITE_STATE_DIR.mkdir(parents=True, exist_ok=True)

    has_strategies = bool(discovered.get("strategies"))
    if has_strategies:
        print("[*] Discovered endpoint has strategies (few_shot, multi_shot); generating strategy-aware payload_format.")

    payload_format_path = SITE_STATE_DIR / "payload_format.py"
    payload_format_path.write_text(
        _generate_payload_format_py(payload_key_to_field, has_strategies=has_strategies),
        encoding="utf-8",
    )
    print(f"[+] Wrote {payload_format_path}")

    send_payloads_path = SITE_STATE_DIR / "send_payloads.py"
    send_payloads_path.write_text(_generate_send_payloads_py(), encoding="utf-8")
    print(f"[+] Wrote {send_payloads_path}")

    # Build payloads.json from diagnostics with the component's payload shape
    payloads_file = SITE_STATE_DIR / "payloads.json"
    if DIAGNOSTICS_FILE.exists():
        try:
            diag_data = json.loads(DIAGNOSTICS_FILE.read_text())
            diagnostics = diag_data.get("diagnostics")
            if isinstance(diagnostics, list) and diagnostics:
                if "messages" in payload_shape:
                    payloads = [
                        {
                            "messages": json.dumps([{"role": "user", "content": str(d)}]),
                            "title": str(d),
                        }
                        for d in diagnostics
                    ]
                else:
                    content_key = next(
                        (k for k in ("text", "content", "body", "description") if k in payload_shape),
                        next(iter(payload_shape.keys()), "text"),
                    )
                    payloads = []
                    for d in diagnostics:
                        item = dict(payload_shape)
                        item[content_key] = str(d)
                        # Always set title from full diagnostic for logging (never use truncated Gemini sample)
                        item["title"] = str(d)
                        payloads.append(item)
                payloads_file.write_text(json.dumps({"payloads": payloads}, indent=2), encoding="utf-8")
                print(f"[+] Wrote {payloads_file} ({len(payloads)} payloads from diagnostics)")
            else:
                print(f"[*] No 'diagnostics' array in {DIAGNOSTICS_FILE}; skipping payloads.json")
        except (json.JSONDecodeError, OSError) as e:
            print(f"[!] Could not load diagnostics: {e}; skipping payloads.json")
    else:
        print(f"[*] No {DIAGNOSTICS_FILE}; skipping payloads.json")

    print("[*] Use: python -m component_discovery send-payloads")
