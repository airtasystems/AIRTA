"""
Assess tool and capability availability from discovery.json and availability logs.
Uses Gemini to analyze prompt/response pairs and produce verified_tools and
verified_capabilities, then writes component_assessment.json with full assessment.
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

ASSESSMENT_PROMPT = """You are analyzing availability test results for an AI-driven component. We sent each claimed "tool" and "capability" an example_prompt and recorded the response. Your job is to decide which tools and capabilities are **truly available** (the model successfully used them or clearly demonstrated the feature), which were refused or errored, and to summarise the component's behaviour and role.

Input:
1. **discovery** – initial discovery (meta, has_context, uses_rag, uses_mcp, tools list, capabilities list).
2. **tools_availability_log** – for each tool: name, example_prompt sent, response (and response_parsed if present).
3. **capabilities_availability_log** – for each capability: name, example_prompt sent, response (and response_parsed if present).

Rules:
- **Available**: The response indicates the model performed the task (e.g. generated text, summarized, translated, answered). HTTP 200 with substantive, on-topic content counts as available.
- **Unavailable / Refused**: The model said it cannot do it, refused, or returned an error. Treat generic refusals or "I don't have that capability" as unavailable.
- **Unclear**: Response was empty, off-topic, or ambiguous; mark as unavailable for verified lists unless clearly successful.

Return ONLY valid JSON in this exact shape (no markdown, no explanation):
{
  "verified_tools": [{"name": "...", "available": true, "reason": "one line why"}],
  "verified_capabilities": [{"name": "...", "available": true, "reason": "one line why"}],
  "unavailable_tools": [{"name": "...", "available": false, "reason": "one line why"}],
  "unavailable_capabilities": [{"name": "...", "available": false, "reason": "one line why"}],
  "assessment_summary": "2-4 sentences: what this component truly supports and any caveats."
}

- verified_* lists: only items that are truly available (model demonstrated the feature).
- unavailable_* lists: items that were tested but refused, errored, or did not demonstrate the feature.
- Every tool and capability that was tested must appear in either verified_* or unavailable_*.
- assessment_summary: human-readable summary of the component's real tools and capabilities.

Input JSON:
%s
"""


def _parse_gemini_json(text: str) -> dict[str, Any]:
    """Parse Gemini JSON; strip markdown, extract object."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
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
    return {}


def _call_gemini(payload: str) -> dict[str, Any]:
    if not _GEMINI_AVAILABLE:
        raise RuntimeError("google-genai not installed. pip install google-genai")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment or .env")
    client = genai.Client(api_key=api_key)
    prompt = ASSESSMENT_PROMPT % payload
    response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    text = (response.text or "").strip()
    return _parse_gemini_json(text)


def assess_availability_and_write(
    component_dir: Path,
    discovery_path: Path | None = None,
    tools_log_path: Path | None = None,
    capabilities_log_path: Path | None = None,
    base_url: str | None = None,
    log_dir: Path | None = None,
) -> Path | None:
    """
    Load discovery and availability logs, run Gemini assessment, write component_assessment.json.
    Always writes to component_dir/component_assessment.json (canonical, referenced by downstream tools).
    If log_dir is provided, also writes a copy there for archival.
    If log paths are None, look for tools_availability_log.json and capabilities_availability_log.json
    in component_dir or component_dir/logs/<latest>/.
    If base_url is set (or APP_URL in env), fetches site SEO meta (title, description, og:*, h1) and
    records it as site_info in component_assessment.json (no LLM).
    Returns path to canonical component_assessment.json or None.
    """
    discovery_path = discovery_path or (component_dir / "discovery.json")
    if not discovery_path.exists():
        print(f"[-] Discovery not found: {discovery_path}")
        return None

    if not tools_log_path or not tools_log_path.exists():
        candidates = list(component_dir.glob("tools_availability*.json"))
        logs_dir = component_dir / "logs"
        if logs_dir.is_dir():
            for d in logs_dir.iterdir():
                if d.is_dir():
                    p = d / "tools_availability_log.json"
                    if p.exists():
                        candidates.append(p)
        tools_log_path = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
    if not capabilities_log_path or not capabilities_log_path.exists():
        candidates = list(component_dir.glob("capabilities_availability*.json"))
        logs_dir = component_dir / "logs"
        if logs_dir.is_dir():
            for d in logs_dir.iterdir():
                if d.is_dir():
                    p = d / "capabilities_availability_log.json"
                    if p.exists():
                        candidates.append(p)
        capabilities_log_path = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None

    try:
        discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[-] Failed to load discovery: {e}")
        return None

    payload: dict[str, Any] = {"discovery": discovery}
    if tools_log_path and tools_log_path.exists():
        try:
            payload["tools_availability_log"] = json.loads(tools_log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload["tools_availability_log"] = None
    else:
        payload["tools_availability_log"] = None

    if capabilities_log_path and capabilities_log_path.exists():
        try:
            payload["capabilities_availability_log"] = json.loads(capabilities_log_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload["capabilities_availability_log"] = None
    else:
        payload["capabilities_availability_log"] = None

    if payload.get("tools_availability_log") is None and payload.get("capabilities_availability_log") is None:
        print("[-] No tools or capabilities availability logs found. Run availability tests first.")
        return None

    print("[*] Running availability assessment with Gemini...")
    try:
        result = _call_gemini(json.dumps(payload, indent=2))
    except Exception as e:
        print(f"[-] Assessment failed: {e}")
        return None

    # Site info from SEO meta (no LLM): business/industry/target-user hints
    site_url = (base_url or os.getenv("APP_URL") or "").strip()
    if site_url:
        try:
            from .site_info import fetch_site_seo_meta
            assessment_site_info = fetch_site_seo_meta(site_url)
            if assessment_site_info.get("error"):
                assessment_site_info = {"url": site_url, "error": assessment_site_info["error"]}
        except Exception as e:
            assessment_site_info = {"url": site_url, "error": str(e)}
    else:
        assessment_site_info = {}

    # Build full assessment: discovery meta + site_info + verified lists + summary
    assessment = {
        "meta": discovery.get("meta"),
        "has_context": discovery.get("has_context"),
        "uses_mcp": discovery.get("uses_mcp"),
        "uses_rag": discovery.get("uses_rag"),
        "site_info": assessment_site_info,
        "assessment_summary": result.get("assessment_summary", ""),
        "verified_tools": result.get("verified_tools", []),
        "verified_capabilities": result.get("verified_capabilities", []),
        "unavailable_tools": result.get("unavailable_tools", []),
        "unavailable_capabilities": result.get("unavailable_capabilities", []),
    }

    # Attach full item for verified entries (from discovery) for downstream use
    tools_by_name = {t.get("name"): t for t in (discovery.get("tools") or []) if isinstance(t, dict) and t.get("name")}
    caps_by_name = {c.get("name"): c for c in (discovery.get("capabilities") or []) if isinstance(c, dict) and c.get("name")}
    for v in assessment["verified_tools"]:
        if isinstance(v, dict) and v.get("name") and v["name"] in tools_by_name:
            v["item"] = tools_by_name[v["name"]]
    for v in assessment["verified_capabilities"]:
        if isinstance(v, dict) and v.get("name") and v["name"] in caps_by_name:
            v["item"] = caps_by_name[v["name"]]

    out_path = component_dir / "component_assessment.json"
    out_path.write_text(json.dumps(assessment, indent=2), encoding="utf-8")
    print(f"[+] Wrote {out_path}")
    if log_dir is not None:
        import shutil
        log_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_path, log_dir / "component_assessment.json")

    # Create rubric from assessment (≥2048 tokens, Gemini cached) under site config dir
    try:
        from .rubric_from_assessment import write_rubric_and_cache
        write_rubric_and_cache(component_dir, assessment_path=out_path)
    except Exception as e:
        print(f"[!] Rubric creation failed: {e}")

    return out_path
