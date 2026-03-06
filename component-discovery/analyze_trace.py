"""
LLM-powered analysis of raw network traces.

Takes raw_trace.json (from capture_site_trace) and produces site_profile.json
that drives the adaptive replay engine.
"""
import json
import os
import re
from pathlib import Path
from typing import Any

from . import config as config_module

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

SITE_PROFILE_SCHEMA = """\
{
  "site_name": "<hostname>",
  "api_base": "<base URL for API calls>",
  "chat_page_url": "<optional: URL of chat page for browser-based extraction>",
  "auth": {
    "csrf_header": "<header name or null>",
    "csrf_cookie": "<cookie name or null>",
    "extra_headers": {<headers required: Content-Type, trpc-accept, x-trpc-source, etc.>}
  },
  "message_flow": {
    "steps": [
      {
        "id": "<unique step id>",
        "when": "<first_message_only|subsequent_messages|always>",
        "url": "<relative or absolute URL>",
        "method": "POST",
        "headers": {<step-specific headers>},
        "body_template": <flat JSON object: {"field": "{{VAR}}", "messages": {{CHAT_HISTORY}}} — NEVER array of {name,value}>,
        "extract": {<variable>: "regex:<pattern>" or "jsonpath:$.path">,
        "fallback_extract": {<variable>: "regex:<pattern>"} optional,
        "response_format": "<json|sse|sse_patches|text|null>",
        "response_source": "<api|sse|ws|dom>",
        "extract_response_from_dom": {"selector": "...", "timeout_ms": 20000} when response_source=dom
      }
    ]
  },
  "response_parsing": {"<format>": {"content_path": "...", "accumulate": "concatenate|last|first"}},
  "dynamic_values": {
    "<VAR>": {"source": "step:<id>|generate|input|cookie:<name>|header:<name>|static", "type": "uuid_from_response|uuidv4|regex|static|json_array"}
  },
  "security": {"has_waf": false, "waf_type": "none", "requires_browser_fetch": false} — only if WAF detected
}
Use CHAT_HISTORY (source: input, type: json_array) for conversation messages. Use USER_TEXT (source: input) for the current message.
"""

ANALYZE_PROMPT = """\
You are converting a network trace into a repeatable site_profile.json for an AI chat component.

**You have full request and response bodies.** Extract the exact structure. Use placeholders ({{VAR}}) only for dynamic values: chatId, sessionId, conversationId, and the user message. The body_template must be derivable from the actual request — no guessing.

**Trace structure**: "entries" contains requests and responses in order. Auth/AI entries have full "body". "auth_summary" has headers_observed and cookie_names. "sections" (if present) marks auth_entry_indices and ai_entry_indices.

**Zero-shot** = first user message. **Multi-shot** = subsequent messages with conversation history. Identify the flow: which call creates the chat? Which sends the message? Which returns the AI response?

**For each step**:
- id: unique step id (use snake_case, e.g. create_new_chat, stream_first_message)
- url: relative path (e.g. /api/chat)
- body_template: flat JSON from the request, with {{CHAT_ID}}, {{CHAT_HISTORY}}, {{USER_TEXT}} etc. for dynamic parts
- extract: {VAR_NAME: "jsonpath:$.path"} for each ID you extract from the response
- response_source: api (body has AI text), sse (streaming), ws (WebSocket), dom (AI text in page)
- when: first_message_only | subsequent_messages | always

**Dynamic values**: Use source "step:<step_id>" where step_id is the EXACT "id" of a step in message_flow.steps that has that variable in its extract. Never use descriptive names or extract paths in the step reference—only the step id. Example: if step id is "create_new_chat" and it extracts CHAT_ID, use {"CHAT_ID": {"source": "step:create_new_chat", "type": "uuid_from_response"}}.

Return ONLY valid JSON (no markdown, no code fences):

%s

## Trace

```json
%s
```
"""


def _normalize_profile(profile: dict) -> None:
    """Post-process profile: canonicalize CHAT_HISTORY, ensure body_template is dict, fix step refs."""
    flow = profile.get("message_flow", {})
    steps = flow.get("steps", [])
    dv = profile.setdefault("dynamic_values", {})
    step_ids = {s.get("id") for s in steps if s.get("id")}

    # Build map: var_name -> step_id for each step that extracts that variable
    var_to_step: dict[str, str] = {}
    for step in steps:
        sid = step.get("id")
        if not sid:
            continue
        for var in step.get("extract", {}).keys():
            var_to_step[var] = sid

    # Fix dynamic_values with invalid step references: infer correct step from extract
    for name, spec in list(dv.items()):
        source = spec.get("source", "")
        if not source.startswith("step:"):
            continue
        ref = source.split(":", 1)[1]
        if ref in step_ids:
            continue
        # LLM may have used step:wrong_id or step:wrong_id:extractKey
        step_id_candidate = ref.split(":")[0] if ":" in ref else ref
        if step_id_candidate in step_ids:
            spec["source"] = f"step:{step_id_candidate}"
            continue
        # Infer from extract: which step produces this variable?
        if name in var_to_step:
            spec["source"] = f"step:{var_to_step[name]}"
            continue

    chat_history_aliases = ("CHAT_HISTORY_JSON", "CHAT_HISTORY_JSON_ARRAY", "MESSAGES", "messages")

    def _replace_in_value(val: Any) -> Any:
        if isinstance(val, str):
            for alias in chat_history_aliases:
                val = val.replace(f"{{{{{alias}}}}}", "{{CHAT_HISTORY}}")
            return val
        if isinstance(val, dict):
            return {k: _replace_in_value(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_replace_in_value(x) for x in val]
        return val

    for step in steps:
        body = step.get("body_template")
        if body is not None:
            step["body_template"] = _replace_in_value(body)
        # Convert array body_template to dict if it slipped through
        body = step.get("body_template")
        if isinstance(body, list):
            _d: dict[str, Any] = {}
            for item in body:
                if isinstance(item, dict) and "name" in item:
                    _d[item["name"]] = item.get("value", "")
            step["body_template"] = _d

    for alias in chat_history_aliases:
        if alias in dv and "CHAT_HISTORY" not in dv:
            dv["CHAT_HISTORY"] = dv.pop(alias)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _validate_profile(profile: dict) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors = []
    flow = profile.get("message_flow")
    if not flow or not flow.get("steps"):
        errors.append("message_flow.steps is missing or empty")
        return errors

    steps = flow["steps"]
    step_ids = {s.get("id") for s in steps}

    all_placeholders: set[str] = set()
    for step in steps:
        if not step.get("url"):
            errors.append(f"Step '{step.get('id')}' has no url")
        if not step.get("method"):
            errors.append(f"Step '{step.get('id')}' has no method")
        body_str = json.dumps(step.get("body_template", {}))
        placeholders = set(re.findall(r"\{\{(\w+)\}\}", body_str))
        all_placeholders.update(placeholders)

        # response_source must be one of api|sse|ws|dom
        rs = step.get("response_source", "api")
        if rs not in ("api", "sse", "ws", "dom"):
            errors.append(f"Step '{step.get('id')}' has invalid response_source '{rs}' (use api|sse|ws|dom)")
        if rs == "dom":
            dom_extract = step.get("extract_response_from_dom")
            if not dom_extract or not isinstance(dom_extract, dict) or not dom_extract.get("selector"):
                errors.append(f"Step '{step.get('id')}' has response_source=dom but missing extract_response_from_dom.selector")

    dv = profile.get("dynamic_values", {})
    for ph in all_placeholders:
        if ph not in dv:
            errors.append(f"Placeholder {{{{{ph}}}}} has no entry in dynamic_values")

    for name, spec in dv.items():
        source = spec.get("source", "")
        if source.startswith("step:"):
            ref = source.split(":", 1)[1]
            if ref not in step_ids:
                errors.append(f"dynamic_values.{name} references unknown step '{ref}'")
        # Allow: input, generate, static, step:<id>, cookie:<name>, header:<name>
        if source and source not in ("input", "generate", "static") and not source.startswith("step:") and not source.startswith("cookie:") and not source.startswith("header:"):
            errors.append(f"dynamic_values.{name} uses unsupported source '{source}' (use input, generate, static, step:<id>, cookie:<name>, header:<name>)")

    return errors


def analyze_trace(
    trace_path: Path | None = None,
    *,
    max_retries: int = 2,  # Includes LLM repair attempts when validation fails
) -> Path | None:
    """
    Analyze raw_trace.json with Gemini and produce site_profile.json.

    Returns the path to site_profile.json, or None on failure.
    """
    if not _GEMINI_AVAILABLE:
        raise RuntimeError("google-genai not installed. pip install google-genai")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment or .env")

    if trace_path is None:
        trace_path = config_module.SITE_STATE_DIR / "raw_trace.json"
    if not trace_path.exists():
        print(f"[-] No raw trace at {trace_path}. Run capture first.")
        return None

    raw = json.loads(trace_path.read_text(encoding="utf-8"))
    entries = raw.get("entries", [])
    if not entries:
        print("[-] Trace has no entries.")
        return None

    sections = raw.get("sections", {})
    ai_indices = set(sections.get("ai_entry_indices", []))
    auth_indices = set(sections.get("auth_entry_indices", []))

    # AI-aware trim: full bodies for auth/AI, drop or collapse noise
    trace_for_llm = _prepare_trace_for_llm(entries, ai_indices, auth_indices, raw)
    trace_json = json.dumps(trace_for_llm, indent=2)

    client = genai.Client(api_key=api_key)

    for attempt in range(1 + max_retries):
        prompt = ANALYZE_PROMPT % (SITE_PROFILE_SCHEMA, trace_json)
        if attempt > 0:
            prompt += "\n\nPrevious attempt had these validation errors:\n"
            prompt += "\n".join(f"- {e}" for e in last_errors)
            prompt += "\n\nThe profile has these errors. Return a corrected site_profile.json that fixes them. Use only supported dynamic value sources: input, generate, step:<id>, cookie:<name>, header:<name>. Return valid JSON only."

        print(f"[*] Calling Gemini to analyze trace (attempt {attempt + 1}) ...")
        try:
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        except Exception as exc:
            print(f"[!] Gemini call failed: {exc}")
            return None

        text = _strip_markdown_fences(response.text)
        try:
            profile = json.loads(text)
            _normalize_profile(profile)
        except json.JSONDecodeError as exc:
            print(f"[!] Gemini returned invalid JSON: {exc}")
            if attempt < max_retries:
                last_errors = [f"Invalid JSON: {exc}"]
                continue
            return None

        last_errors = _validate_profile(profile)
        if not last_errors:
            break
        print(f"[!] Profile validation errors: {last_errors}")
        if attempt >= max_retries:
            print("[!] Max retries reached; saving best-effort profile.")

    out_path = trace_path.parent / "site_profile.json"
    out_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    print(f"[+] Site profile saved to {out_path}")

    step_ids = [s.get("id", "?") for s in profile.get("message_flow", {}).get("steps", [])]
    print(f"    Flow steps: {step_ids}")
    sec = profile.get("security", {})
    if sec.get("has_waf"):
        print(f"    WAF detected: {sec.get('waf_type', 'unknown')}")
    if sec.get("requires_browser_fetch"):
        print("    Requires browser-based fetch (in-page)")

    return out_path


_AI_PATH_KEYWORDS = ("/api/", "/chat", "/completions", "/message", "/converse", "/generate", "/inference", "/trpc", "/graphql")
_AUTH_PATH_KEYWORDS = ("/login", "/signin", "/auth", "/csrf", "/session")
_WS_NOISE_PATTERNS = ("webpack-hmr", "/_next/", "hot-update", "sockjs", "turbopack")

_BODY_LIMIT_AI_REQUEST = 32768   # 32KB for AI request bodies
_BODY_LIMIT_AI_RESPONSE = 65536  # 64KB for AI response bodies
_BODY_LIMIT_AUTH = 16384         # 16KB for auth
_BODY_LIMIT_OTHER = 2048        # 2KB for other


def _is_ai_or_auth_entry(entry: dict, index: int, ai_indices: set[int], auth_indices: set[int]) -> tuple[bool, bool]:
    """Return (is_ai, is_auth). Fallback to URL heuristics when sections missing."""
    if index in ai_indices:
        return True, False
    if index in auth_indices:
        return False, True
    url = (entry.get("url") or "").lower()
    if any(p in url for p in _AI_PATH_KEYWORDS):
        return True, False
    if any(p in url for p in _AUTH_PATH_KEYWORDS):
        return False, True
    return False, False


def _prepare_trace_for_llm(
    entries: list[dict],
    ai_indices: set[int],
    auth_indices: set[int],
    raw: dict,
) -> dict:
    """
    AI-aware trim: full bodies for auth/AI entries, drop or collapse noise.
    Returns a dict with entries + metadata for the LLM.
    """
    SKIP_HEADERS = {
        "accept-encoding", "connection", "sec-ch-ua", "sec-ch-ua-mobile",
        "sec-ch-ua-platform", "sec-fetch-dest", "sec-fetch-mode",
        "sec-fetch-site", "sec-fetch-user", "upgrade-insecure-requests",
        "dnt", "pragma", "cache-control",
    }

    # Deduplicate: collapse repeated noise URLs (e.g. many webpack frames)
    noise_url_counts: dict[str, int] = {}
    trimmed: list[dict] = []

    for i, entry in enumerate(entries):
        direction = entry.get("direction", "")
        url = entry.get("url") or ""

        is_ai, is_auth = _is_ai_or_auth_entry(entry, i, ai_indices, auth_indices)
        is_relevant = is_ai or is_auth

        # Drop WebSocket noise that slipped through
        if direction in ("ws_sent", "ws_received") and any(p in url.lower() for p in _WS_NOISE_PATTERNS):
            noise_url_counts[url] = noise_url_counts.get(url, 0) + 1
            continue

        # For non-relevant HTTP, keep only if it might be auth (e.g. same-origin POST)
        if not is_relevant and direction == "request":
            path_lower = url.lower()
            if any(p in path_lower for p in _WS_NOISE_PATTERNS) or "/_next" in path_lower or "__nextjs" in path_lower:
                continue

        e: dict[str, Any] = {
            "direction": direction,
            "url": url,
            "method": entry.get("method"),
            "status": entry.get("status"),
        }
        if entry.get("query_params"):
            e["query_params"] = entry["query_params"]
        if entry.get("content_type"):
            e["content_type"] = entry["content_type"]
        if entry.get("is_sse"):
            e["is_sse"] = entry["is_sse"]
        if entry.get("paired_request_id") is not None:
            e["paired_request_id"] = entry["paired_request_id"]
        if entry.get("id") is not None:
            e["id"] = entry["id"]

        headers = entry.get("headers", {})
        if headers:
            e["headers"] = {k: v for k, v in headers.items() if k.lower() not in SKIP_HEADERS}

        body = entry.get("body") or entry.get("payload", "")
        if body:
            if is_ai:
                limit = _BODY_LIMIT_AI_RESPONSE if direction == "response" else _BODY_LIMIT_AI_REQUEST
            elif is_auth:
                limit = _BODY_LIMIT_AUTH
            else:
                limit = _BODY_LIMIT_OTHER
            e["body"] = body[:limit]
        elif entry.get("body_preview"):
            e["body_preview"] = entry["body_preview"]

        trimmed.append(e)

    result: dict[str, Any] = {
        "base_url": raw.get("base_url"),
        "auth_summary": raw.get("auth_summary"),
        "entries": trimmed,
    }
    if noise_url_counts:
        result["omitted_noise"] = {k: v for k, v in noise_url_counts.items() if v > 1}
    return result
