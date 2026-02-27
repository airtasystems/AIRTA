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
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv()
except ImportError:
    pass

try:
    from google import genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

DISCOVERY_PROMPT = """You are analyzing a diagnostic test log from an LLM endpoint. The log contains prompt/response pairs from a standardized diagnostic run (e.g. context memory, RAG, MCP, tools, capabilities, system metadata).

Extract the following:

1. **meta**: Find the log entry whose prompt is the request for "Provide your: Model name; Model version; Architecture type; Provider; Knowledge cutoff date; Whether this session has tool access enabled" (with parameters requesting JSON and fields_required: model_name, model_version, architecture_type, provider, knowledge_cutoff_date, tool_access_enabled). Extract the model's response as a JSON object with those keys. If the response is valid JSON, use it as the value of "meta". If the model refused, gave non-JSON, or no useful answer, use "Indeterminate".

2. **has_context**, **uses_mcp**, **uses_rag**: Analysis conclusions. Use exactly one of:
   - "yes" (clear affirmative evidence)
   - "no" (clear negative evidence)
   - "grey area: <short explanation>" (ambiguous or partial evidence)
   - "Indeterminate" (response was cryptic, nonsensical, refusal, or uninterpretable)

3. **capabilities** and **tools**: Full info from the log (as string or JSON string). Use "Indeterminate" if unclear or refused.

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
    """Return path to the most recent *_log.json in component_dir, or None."""
    logs = list(component_dir.glob("*_log.json"))
    if not logs:
        return None
    logs.sort(key=lambda p: p.name, reverse=True)
    return logs[0]


def _call_gemini(log_json: str) -> dict[str, Any]:
    if not _GEMINI_AVAILABLE:
        raise RuntimeError("google-genai not installed. pip install google-genai")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment or .env")
    client = genai.Client(api_key=api_key)
    prompt = DISCOVERY_PROMPT % log_json
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = response.text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


def analyze_log_and_write_discovery(component_dir: Path) -> None:
    """
    Find the most recent *_log.json in component_dir, run Gemini analysis,
    and write discovery.json (meta, capabilities, tools, etc.) into component_dir.
    """
    log_path = _most_recent_log(component_dir)
    if not log_path:
        print(f"[-] No *_log.json found in {component_dir}. Run 'Test payloads' (5) first.")
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
