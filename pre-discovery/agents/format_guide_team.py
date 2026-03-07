"""
Agent team to analyze format/ data and produce a definitive JSON guide
for LLM GET/POST paths and formats (sending and response).
"""
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph

from .format_loader import DEFAULT_FORMAT_DIR, load_format_data
from ..methods.trace_parser import extract_ui_hints

# Load config from project root (AIRTA)
_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".config")
load_dotenv(_ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable is not set.")

# request_timeout in seconds; prevents indefinite hang on slow/blocked API
LLM_TIMEOUT = int(os.getenv("PRE_DISCOVERY_LLM_TIMEOUT", "120"))

llm = ChatGoogleGenerativeAI(
    model=GEMINI_MODEL,
    api_key=GEMINI_API_KEY,
    temperature=0.1,
    request_timeout=LLM_TIMEOUT,
)


def _response_to_text(resp) -> str:
    """Extract plain text from LLM response (handles string or list of content blocks)."""
    raw = resp.content
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            elif hasattr(block, "text"):
                parts.append(str(getattr(block, "text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts).strip()
    return str(raw).strip()


def _extract_json(text: str) -> dict | None:
    """Extract JSON from LLM response (handles markdown fences, trailing commas, embedded content blocks)."""
    if not text or not text.strip():
        return None
    parse_text = text.strip()

    # Handle Python repr of content blocks: [{'type': 'text', 'text': '{"app_url":...}', 'extras':...}]
    if parse_text.startswith("[{") and "'text'" in parse_text:
        # Find 'text': ' - the JSON starts with { right after the opening quote
        match = re.search(r"'text'\s*:\s*'", parse_text)
        if match:
            after_prefix = match.end()
            if after_prefix < len(parse_text) and parse_text[after_prefix] == "{":
                extracted = _extract_brace_balanced(parse_text[after_prefix:])
                if extracted:
                    parse_text = extracted.replace("\\n", "\n").replace("\\t", "\t")

    if "```" in parse_text:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", parse_text)
        if match:
            parse_text = match.group(1).strip()
    if not parse_text.strip().startswith("{"):
        start = parse_text.find("{")
        end = parse_text.rfind("}")
        if start != -1 and end != -1 and end > start:
            parse_text = parse_text[start : end + 1]

    # Fix common LLM JSON issues
    parse_text = re.sub(r",\s*([}\]])", r"\1", parse_text)
    parse_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", parse_text)  # strip control chars

    for candidate in [parse_text, _extract_brace_balanced(parse_text)]:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _extract_prompt_structure_from_trace(
    trace_data: dict | None,
) -> list[dict] | None:
    """
    Extract the message/prompt structure from full_trace POST requests.
    Finds chat POSTs (matching target path or with messages in post_data), parses
    post_data, returns the messages array. If single-turn (user only), augments
    with a canonical assistant message to show the full format.
    """
    if not trace_data:
        return None
    requests = trace_data.get("requests") or []
    best_messages: list[dict] | None = None
    for r in requests:
        if r.get("method") != "POST":
            continue
        post_data = r.get("post_data")
        if not post_data or not isinstance(post_data, str):
            continue
        if "messages" not in post_data.lower():
            continue
        try:
            body = json.loads(post_data)
        except json.JSONDecodeError:
            continue
        messages = None
        if isinstance(body, dict) and "messages" in body:
            messages = body["messages"]
        elif isinstance(body, list):
            messages = body
        if not isinstance(messages, list) or not messages:
            continue
        # Prefer multi-turn (has assistant)
        has_assistant = any(
            (m.get("role") or "").lower() == "assistant" for m in messages if isinstance(m, dict)
        )
        if has_assistant or best_messages is None:
            best_messages = messages
        if has_assistant:
            break

    if not best_messages:
        return None
    # If single-turn (user only), augment with assistant example
    has_assistant = any(
        (m.get("role") or "").lower() == "assistant" for m in best_messages if isinstance(m, dict)
    )
    if not has_assistant and best_messages:
        user_content = ""
        for m in best_messages:
            if isinstance(m, dict) and (m.get("role") or "").lower() == "user":
                user_content = m.get("content") or ""
                break
        best_messages = [
            {"role": "user", "content": user_content or "Hello"},
            {"role": "assistant", "content": "I am an AI assistant. How can I help you?"},
        ]
    return best_messages


def _extract_brace_balanced(s: str) -> str | None:
    """Extract first complete {...} from string using brace balancing."""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    quote = None
    for i, c in enumerate(s[start:], start):
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
            elif c in ('"', "'"):
                in_string = True
                quote = c
        elif c == quote:
            in_string = False
    return None


# =========================
# Agents
# =========================


def endpoint_agent(state: dict) -> dict:
    """
    Analyze discovered_api.json: extract GET/POST endpoints, payload formats,
    required headers, query params. Output structured analysis.
    """
    data = state.get("format_data", {})
    discovered = data.get("discovered_api")
    if not discovered:
        return {"endpoint_analysis": None, "format_data": data}

    system = """You are an API endpoint analyst. Analyze the discovered_api JSON and produce a concise structured analysis.

CRITICAL: Use ONLY the actual URLs, hosts, paths, and field names from the input. Never substitute example.com or placeholder values.

Focus on:
1. Primary LLM chat endpoint (target_api_url) - include the exact URL and path pattern, method, required headers
2. All POST endpoints: exact path, payload_format (fields, field_types) from the data, headers needed
3. All GET endpoints: exact path, query_params, purpose (session, conversations, messages, etc.)
4. Path templates: identify dynamic segments (e.g. {conversation_id}, {message_id}) using the actual path structure

Output a clear JSON object with keys: primary_endpoint, post_endpoints, get_endpoints, path_templates.
Include the actual host/domain from the URLs in your output.
Respond with ONLY the raw JSON object. No markdown, no code fences."""

    user = f"Analyze this discovered API data:\n\n{json.dumps(discovered, indent=2)}"

    target = discovered.get("target_api_url", "?")
    n_post = len(discovered.get("endpoints", {}).get("post", []))
    n_get = len(discovered.get("endpoints", {}).get("get", []))
    target_short = (target[:55] + "...") if len(target) > 55 else target
    print(f"[*] Endpoint agent: analyzing discovered_api.json (target: {target_short}, {n_post} POST, {n_get} GET)")
    print(f"[*] Endpoint agent: calling {GEMINI_MODEL} (timeout={LLM_TIMEOUT}s)...")
    try:
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        text = _response_to_text(resp)
        print(f"[+] Endpoint agent done ({len(text)} chars)")
        return {"endpoint_analysis": text, "format_data": data}
    except Exception as e:
        print(f"[!] endpoint_agent: {e}")
        return {"endpoint_analysis": None, "format_data": data}


def trace_agent(state: dict) -> dict:
    """
    Analyze full_trace.json: find LLM-related requests, extract request/response examples,
    identify flow order (which GETs before which POSTs).
    """
    data = state.get("format_data", {})
    trace = data.get("full_trace")
    if not trace:
        return {"trace_analysis": None, "format_data": data}

    # Filter to LLM-relevant requests (chat, conversation, message, api, chat-api)
    requests = trace.get("requests", [])
    llm_requests = [
        r
        for r in requests
        if any(
            x in (r.get("path") or "").lower() or x in (r.get("url") or "").lower()
            for x in ["chat", "conversation", "message", "api", "/api/"]
        )
    ]
    # Limit size for context
    subset = llm_requests[:30] if len(llm_requests) > 30 else llm_requests
    trace_subset = {"app_url": trace.get("app_url"), "request_count": len(llm_requests), "requests": subset}

    system = """You are a request trace analyst. Analyze the full_trace JSON (LLM-related requests only) and produce:

CRITICAL: Preserve the exact app_url, URLs, paths, and domains from the trace. Never substitute example.com or placeholder values.

1. app_url: use the exact app_url from the trace (the chat UI URL)
2. base_url: scheme + host from the API requests (e.g. https://www.example.com)
3. Request flow order: which endpoints are called in sequence (e.g. session check -> create conversation -> send message)
4. For each POST: exact path, example request body (post_data), response_status
5. For each GET: exact path, query_params used, response_status
6. Required headers observed (content-type, session-id, referer, etc.)

Output a clear JSON object with keys: app_url, base_url, flow_order, post_examples, get_examples, required_headers.
Respond with ONLY the raw JSON object. No markdown, no code fences."""

    user = f"Analyze this trace data:\n\n{json.dumps(trace_subset, indent=2)}"

    app_url = trace.get("app_url", "unknown")
    print(f"[*] Trace agent: analyzing full_trace.json (app_url: {app_url}, {len(llm_requests)} LLM-relevant requests)")
    print(f"[*] Trace agent: calling {GEMINI_MODEL} (timeout={LLM_TIMEOUT}s)...")
    try:
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        text = _response_to_text(resp)
        print(f"[+] Trace agent done ({len(text)} chars)")
        return {"trace_analysis": text, "format_data": data}
    except Exception as e:
        print(f"[!] trace_agent: {e}")
        return {"trace_analysis": None, "format_data": data}


def synthesizer_agent(state: dict) -> dict:
    """
    Transform discovered_api + full_trace into the definitive llm_api_guide JSON.
    The raw data is the source of truth; analyses provide flow/context.
    """
    endpoint = state.get("endpoint_analysis")
    trace_analysis = state.get("trace_analysis")
    format_data = state.get("format_data") or {}
    discovered = format_data.get("discovered_api")
    trace_data = format_data.get("full_trace")

    system = """You produce the definitive LLM API integration guide by transforming the discovered_api and full_trace data.

Your output MUST be a direct transformation of the input data. Every path, URL, field, and header must come from the input. Never use example.com, placeholder domains, or invented paths.

TASK: Convert discovered_api.json into the guide format below. Use app_url from full_trace. Map each endpoint in discovered_api.endpoints.post and discovered_api.endpoints.get into the guide's post and get objects. For path_template, replace UUID segments with {conversation_id} or {message_id} as appropriate.

Output structure (fill from the actual data):
{
  "app_url": "<from full_trace.app_url>",
  "base_url": "<scheme://host from target_api_url or first API URL>",
  "overview": "<1-2 sentence summary of this app's chat API>",
  "flow": ["<step 1>", "<step 2>", ...],
  "get": { "<id>": { "path", "path_template", "description", "query_params", "required_headers", "response_status", "example_url" } },
  "post": { "<id>": { "path", "path_template", "description", "request_format": { "fields", "field_types", "example", "prompt_structure" }, "required_headers", "response_status", "example_request" } },
  "primary_chat_endpoint": "<key of the main send-message POST endpoint>"
}

For request_format: use payload_format from discovered_api. For the primary chat POST, request_format MUST include "prompt_structure": an array showing the message format with both user and assistant roles, e.g. [{"role":"user","content":"..."},{"role":"assistant","content":"..."}]. Use post_data from full_trace when available; if only single-turn, add an assistant example. For required_headers, use headers from discovered_api. example_url = base_url + path.

Respond with ONLY the raw JSON. No markdown, no code fences."""

    user_parts = []
    if discovered:
        user_parts.append(f"discovered_api.json:\n{json.dumps(discovered, indent=2)}")
    if trace_data:
        all_requests = trace_data.get("requests") or []
        # Include chat POSTs (with post_data) — they may be late in the trace
        chat_posts = [r for r in all_requests if r.get("method") == "POST" and r.get("post_data")][:5]
        seen_urls = {r.get("url") for r in chat_posts}
        head = [r for r in all_requests[:15] if r.get("url") not in seen_urls]
        trace_subset = chat_posts + head
        user_parts.append(
            f"\nfull_trace (app_url, requests including chat POSTs with post_data):\n{json.dumps({'app_url': trace_data.get('app_url'), 'requests': trace_subset}, indent=2)}"
        )
    if endpoint:
        user_parts.append(f"\nEndpoint analysis:\n{endpoint}")
    if trace_analysis:
        user_parts.append(f"\nTrace analysis:\n{trace_analysis}")

    user = "\n".join(user_parts) if user_parts else "No data."

    has_discovered = bool(discovered)
    has_trace_data = bool(trace_data)
    has_endpoint = bool(endpoint)
    has_trace = bool(trace_analysis)
    print(f"[*] Synthesizer: discovered_api={has_discovered}, full_trace={has_trace_data}, endpoint_analysis={has_endpoint}, trace_analysis={has_trace}")
    if not has_discovered:
        print("[!] WARNING: Synthesizer has no discovered_api - format_data may not have propagated through graph")
    print(f"[*] Synthesizer: calling {GEMINI_MODEL} (timeout={LLM_TIMEOUT}s)...")
    try:
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        text = _response_to_text(resp)
        print(f"[+] Synthesizer done ({len(text)} chars)")
        guide = _extract_json(text)
        if guide is None and text:
            # Save raw output for debugging (always in format dir)
            fmt_dir = state.get("_format_dir") or DEFAULT_FORMAT_DIR
            debug_path = Path(fmt_dir) / "llm_api_guide_raw.txt"
            debug_path.write_text(text, encoding="utf-8")
            print(f"[!] Synthesizer output did not parse as JSON. Raw output saved to {debug_path}")
        return {"guide": guide if isinstance(guide, dict) else None}
    except Exception as e:
        print(f"[!] synthesizer: {e}")
        return {"guide": None}


# =========================
# Graph
# =========================


def run_format_guide_team(
    format_dir: Path | None = None,
    output_path: Path | None = None,
    component_name: str | None = None,
) -> dict | None:
    """
    Run the agent team to analyze format/ and produce llm_api_guide.json.
    If component_name is provided, it is written as the first JSON entry.
    Returns the guide dict, or None on failure.
    """
    format_dir = format_dir or DEFAULT_FORMAT_DIR
    output_path = output_path or format_dir / "llm_api_guide.json"

    print(f"[*] Loading format data from {format_dir}...")
    format_data = load_format_data(format_dir)
    if not format_data.get("discovered_api"):
        print("[-] No discovered_api.json in format/. Run pre-discovery first.")
        return None
    disc = format_data["discovered_api"]
    n_post = len(disc.get("endpoints", {}).get("post", []))
    n_get = len(disc.get("endpoints", {}).get("get", []))
    target = disc.get("target_api_url", "?")[:50]
    print(f"[+] discovered_api.json loaded ({n_post} POST, {n_get} GET, target: {target}...)")
    if format_data.get("full_trace"):
        trace = format_data["full_trace"]
        n = len(trace.get("requests", []))
        app = trace.get("app_url", "?")
        print(f"[+] full_trace.json loaded ({n} requests, app_url: {app})")
    else:
        print(f"[*] full_trace.json not found (trace agent will skip)")
    if format_data.get("playwright_available"):
        print(f"[+] Playwright trace dir present")
    print(f"[*] Output path: {output_path}")
    print("[*] Pipeline: endpoint agent -> trace agent -> synthesizer")
    # Build graph: endpoint_agent -> trace_agent -> synthesizer
    workflow = StateGraph(dict)

    workflow.add_node("endpoint", endpoint_agent)
    workflow.add_node("trace", trace_agent)
    workflow.add_node("synthesizer", synthesizer_agent)

    workflow.add_edge(START, "endpoint")
    workflow.add_edge("endpoint", "trace")
    workflow.add_edge("trace", "synthesizer")
    workflow.add_edge("synthesizer", END)

    app = workflow.compile()
    initial = {
        "format_data": format_data,
        "endpoint_analysis": None,
        "trace_analysis": None,
        "guide": None,
        "_format_dir": format_dir,
    }
    result = app.invoke(initial)

    guide = result.get("guide")

    # Post-process: add prompt_structure from full_trace if missing
    if guide and format_data.get("full_trace"):
        prompt_structure = _extract_prompt_structure_from_trace(format_data["full_trace"])
        if prompt_structure:
            primary_key = guide.get("primary_chat_endpoint")
            post_endpoints = guide.get("post") or {}
            if primary_key and primary_key in post_endpoints:
                rf = post_endpoints[primary_key].get("request_format") or {}
                if "prompt_structure" not in rf:
                    rf["prompt_structure"] = prompt_structure
                    post_endpoints[primary_key]["request_format"] = rf
                    print(f"[+] Added prompt_structure from trace ({len(prompt_structure)} messages)")

    # Post-process: merge UI hints from Playwright trace into guide
    if guide:
        fmt_dir = Path(result.get("_format_dir") or format_dir)
        trace_path = fmt_dir / "playwright" / "trace.trace"
        hints = extract_ui_hints(trace_path) if trace_path.exists() else None
        if hints:
            guide["ui"] = {
                "chat_input_selectors": hints.get("chat_input_selectors", []),
                "response_container": hints.get("response_container"),
                "response_extraction": "text_after_prompt" if hints.get("response_container") else "selectors",
                "consent_selectors": hints.get("consent_selectors", []),
            }
        else:
            guide["ui"] = {
                "chat_input_selectors": [],
                "response_container": None,
                "response_extraction": "selectors",
                "consent_selectors": [],
            }

    if guide and guide.get("app_url") and (guide.get("get") or guide.get("post")):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        to_write = guide
        if component_name:
            to_write = {"component_name": component_name, **guide}
        output_path.write_text(json.dumps(to_write, indent=2), encoding="utf-8")
        n_get = len(guide.get("get", {}))
        n_post = len(guide.get("post", {}))
        app_url = guide.get("app_url", "")
        primary = guide.get("primary_chat_endpoint", "")
        print(f"[+] Guide written to {output_path}")
        print(f"    app_url: {app_url}")
        print(f"    base_url: {guide.get('base_url', '')}")
        print(f"    primary_chat_endpoint: {primary}")
        print(f"    endpoints: {n_get} GET, {n_post} POST")
        return guide

    if guide and (not guide.get("app_url") or (not guide.get("get") and not guide.get("post"))):
        missing = []
        if not guide.get("app_url"):
            missing.append("app_url")
        if not guide.get("get") and not guide.get("post"):
            missing.append("endpoints (get/post)")
        print(f"[-] Synthesizer produced incomplete guide (missing: {', '.join(missing)})")
        print("[-] Check agent prompts and input data in format/")
    else:
        print("[-] Failed to produce guide (synthesizer output did not parse as valid JSON)")
    return None
