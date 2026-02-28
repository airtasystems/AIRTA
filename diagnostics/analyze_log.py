"""
Analyze the most recent component log file (diagnostic test results) with Gemini
and write discovery.json: meta, has_context, uses_mcp, uses_rag, capabilities, tools.
Call from component-discovery with the component dir (e.g. localhost3000/chat).
"""
import json
import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    _root = Path(__file__).resolve().parent.parent
    load_dotenv(_root / ".config")
    load_dotenv(_root / ".env")
    load_dotenv()
except ImportError:
    pass

try:
    from google import genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


GEMINI_MODEL = os.getenv("GEMINI_MODEL")

DISCOVERY_PROMPT = """You are analyzing a diagnostic test log from an LLM endpoint. The log contains prompt/response pairs from a standardized diagnostic run (e.g. context memory, RAG, MCP, tools, capabilities, system metadata). When present, use "response_parsed" (structured JSON) rather than the raw "response" string.

Extract the following:

1. **meta**: Find the log entry whose prompt is the request for "Provide your: Model name; Model version; Architecture type; Provider; Knowledge cutoff date; Whether this session has tool access enabled" (with parameters requesting JSON and fields_required: model_name, model_version, architecture_type, provider, knowledge_cutoff_date, tool_access_enabled). Use response_parsed.content when present (it is already parsed JSON); otherwise extract from the response string. If the response is valid JSON with those keys, use it as the value of "meta". If the model refused, gave non-JSON, or no useful answer, use "Indeterminate".

2. **has_context**, **uses_mcp**, **uses_rag**: Analysis conclusions. Use exactly one of:
   - "yes" (clear affirmative evidence)
   - "no" (clear negative evidence)
   - "grey area: <short explanation>" (ambiguous or partial evidence)
   - "Indeterminate" (response was cryptic, nonsensical, refusal, or uninterpretable)

3. **capabilities** and **tools**: Use response_parsed.content when present (already parsed); otherwise use the response string. Output as JSON object/array when available. Use "Indeterminate" if unclear or refused.

Return ONLY valid JSON in this exact shape (no markdown, no explanation):
{
  "meta": {"model_name": "...", "model_version": "...", "architecture_type": "...", "provider": "...", "knowledge_cutoff_date": "...", "tool_access_enabled": "..."} OR "Indeterminate",
  "has_context": "yes|no|grey area: ...|Indeterminate",
  "uses_mcp": "yes|no|grey area: ...|Indeterminate",
  "uses_rag": "yes|no|grey area: ...|Indeterminate",
  "capabilities": "<full capabilities info as string or JSON string; use Indeterminate if unclear>",
  "tools": "<full tool info as string or JSON string; use Indeterminate if unclear>"
}

Rules:
- meta must be the JSON object from the response to the "Provide your: Model name; Model version; Architecture type; Provider; Knowledge cutoff date; Whether this session has tool access enabled" diagnostic (same shape as fields_required), or the string "Indeterminate".
- Infer has_context from whether the system remembered earlier prompts (e.g. a fact stated then recalled later).
- Infer uses_rag from explicit answers about RAG or retrieval-augmented generation.
- Infer uses_mcp from explicit answers about MCP (Model Context Protocol) or tool support.
- If any response is refusal, off-topic, or meaningless, use "Indeterminate" for that field.

Log content (JSON):
%s
"""


def _most_recent_log(component_dir: Path) -> Path | None:
    """Return path to the most recent diagnostics *_log.json in component_dir, or None.

    Discovery (meta, capabilities, tools) is extracted from diagnostics prompt/response
    pairs. Compliance logs (compliance_*_log.json) do not contain those prompts, so we
    exclude them and pick the most recent log that does (e.g. from 'Test payloads'
    using payloads.json). If no non-compliance log exists, fall back to most recent
    of any log.
    """
    all_logs = list(component_dir.glob("*_log.json"))
    if not all_logs:
        return None
    # Use only diagnostics logs (from "Test payloads" / payloads.json). Compliance logs
    # (compliance_*_log.json) do not contain the meta/capabilities/tools prompts.
    diagnostics_logs = [p for p in all_logs if not p.name.startswith("compliance_")]
    if not diagnostics_logs:
        return None
    diagnostics_logs.sort(key=lambda p: p.name, reverse=True)
    return diagnostics_logs[0]


def _parse_gemini_json(text: str) -> dict[str, Any]:
    """Parse Gemini's JSON response; strip markdown, extract object, fallback to Indeterminate."""
    text = text.strip()
    # Strip markdown code block
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extract first complete {...} (handles trailing text or extra content)
    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    # Fallback: return safe default so pipeline continues
    return {
        "meta": "Indeterminate",
        "has_context": "Indeterminate",
        "uses_mcp": "Indeterminate",
        "uses_rag": "Indeterminate",
        "capabilities": "Indeterminate",
        "tools": "Indeterminate",
    }


def _call_gemini(log_json: str) -> dict[str, Any]:
    if not _GEMINI_AVAILABLE:
        raise RuntimeError("google-genai not installed. pip install google-genai")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment or .env")
    client = genai.Client(api_key=api_key)
    prompt = DISCOVERY_PROMPT % log_json
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = (response.text or "").strip()
    result = _parse_gemini_json(text)
    if result.get("meta") == "Indeterminate" and result.get("capabilities") == "Indeterminate":
        import sys
        print("[!] Gemini returned invalid JSON; using Indeterminate for all discovery fields.", file=sys.stderr)
    return result


def analyze_log_and_write_discovery(component_dir: Path, diagnostics_log_path: Path | None = None) -> None:
    """
    Run Gemini analysis on a diagnostics log and write discovery.json into component_dir.
    If diagnostics_log_path is provided, use it; otherwise find the most recent *_log.json in component_dir.
    """
    log_path = diagnostics_log_path if diagnostics_log_path is not None else _most_recent_log(component_dir)
    if not log_path or not log_path.exists():
        print(f"[-] No diagnostics *_log.json found in {component_dir} (compliance logs are ignored for discovery).")
        print("    Run 'Test payloads' (send-payloads) with payloads.json to produce a diagnostics log, then run Analyze log again.")
        return

    try:
        log_data = json.loads(log_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[-] Failed to read {log_path}: {e}")
        return

    log_json = json.dumps(log_data, indent=2)
    print(f"[*] Analyzing {log_path.name} with Gemini...")
    try:
        result = _call_gemini(log_json)
    except Exception as e:
        print(f"[-] Gemini analysis failed: {e}")
        return

    def _decode_json_value(v: Any) -> Any:
        """If v is a JSON string, return decoded value; otherwise return v."""
        if isinstance(v, str):
            s = v.strip()
            if (s.startswith("[") or s.startswith("{")) and len(s) > 1:
                try:
                    return json.loads(s)
                except json.JSONDecodeError:
                    pass
        return v

    raw_meta = result.get("meta") if result.get("meta") is not None else "Indeterminate"
    discovery = {
        "meta": _decode_json_value(raw_meta) if raw_meta != "Indeterminate" else "Indeterminate",
        "has_context": result.get("has_context", "Indeterminate"),
        "uses_mcp": result.get("uses_mcp", "Indeterminate"),
        "uses_rag": result.get("uses_rag", "Indeterminate"),
        "capabilities": _decode_json_value(result.get("capabilities")),
        "tools": _decode_json_value(result.get("tools")),
    }

    out_path = component_dir / "discovery.json"
    out_path.write_text(json.dumps(discovery, indent=2), encoding="utf-8")
    print(f"[+] Wrote {out_path}")
