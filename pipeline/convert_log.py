"""
Convert browser-bot run logs into compliance_log.json for risk-assess.

browser-bot run logs have shape:
  Single: { site, component, timestamp, mode: "single", entries: [{ input, response }] }
  Multi:  { site, component, timestamp, mode: "multi",  batches: [{ turns: [{ input, response }] }] }

Generated suite JSON has shape:
  { framework, mandates: [{ mandate, prompts: [{ id, description, prompt } | { id, description, prompts: [str] }] }] }

Multi-turn suites (multi_shot, tree_of_thoughts, iterative, prompt_chaining) produce one compliance
result per test case. All turns are preserved in run_log.json; compliance_log uses the full prompt
transcript but only the final model response for ok/response and risk assessment.
browser-bot appends UI_PROMPT_PREFIX (suffix) and may truncate via UI_PROMPT_MAX_CHARS, so matching strips
the known wrapper and compares the body portion. Legacy run logs with a prepended wrapper are still handled.
"""
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _detect_suite_mode(suite: dict) -> str:
    """Return 'multi' if the first prompt entry uses `prompts` (array), else 'single'."""
    for m in suite.get("mandates") or []:
        for p in m.get("prompts") or []:
            if isinstance(p.get("prompts"), list):
                return "multi"
            if isinstance(p.get("prompt"), str):
                return "single"
    return "single"


def _build_single_index(suite: dict) -> list[dict]:
    """Flat list of { id, mandate, description, prompt } from the suite (single-shot order)."""
    out: list[dict] = []
    for m in suite.get("mandates") or []:
        mandate_name = m.get("mandate", "")
        for p in m.get("prompts") or []:
            out.append({
                "id": p.get("id", ""),
                "mandate": mandate_name,
                "description": p.get("description", ""),
                "prompt": p.get("prompt", ""),
            })
    return out


def _build_multi_index(suite: dict) -> list[dict]:
    """Flat list of { id, mandate, description, prompts: [str] } from the suite (multi-shot order)."""
    out: list[dict] = []
    for m in suite.get("mandates") or []:
        mandate_name = m.get("mandate", "")
        for p in m.get("prompts") or []:
            out.append({
                "id": p.get("id", ""),
                "mandate": mandate_name,
                "description": p.get("description", ""),
                "prompts": p.get("prompts", []),
            })
    return out


@lru_cache(maxsize=1)
def _ui_prompt_wrapper_parts() -> tuple[str | None, str | None]:
    """Return (legacy_prepend_head, append_tail) from browser_bot.config, or (None, None)."""
    try:
        root = Path(__file__).resolve().parent.parent
        bb = root / "browser-bot"
        if bb.is_dir() and str(bb) not in sys.path:
            sys.path.insert(0, str(bb))
        from browser_bot.config import UI_PROMPT_PREFIX, UI_PROMPT_PREFIX_SEPARATOR

        p = UI_PROMPT_PREFIX or ""
        sep = UI_PROMPT_PREFIX_SEPARATOR or ""
        if not p:
            return (None, None)
        head = f"{p}{sep}"
        tail = f"{sep}{p}"
        return (head, tail)
    except Exception:
        return (None, None)


def _strip_ui_prefix(submitted: str) -> str:
    """Remove UI prompt wrapper (appended suffix or legacy prepended head) to recover the original body."""
    s = submitted.strip()
    head, tail = _ui_prompt_wrapper_parts()
    if tail and s.endswith(tail):
        s = s[: -len(tail)].rstrip()
    elif head and s.startswith(head):
        s = s[len(head) :].strip()
    elif s.startswith("["):
        bracket_end = s.find("]")
        if bracket_end != -1:
            s = s[bracket_end + 1 :].lstrip("\n")
    return s.strip()


def _prompt_matches(original: str, submitted_body: str) -> bool:
    """Check if submitted_body (possibly truncated) matches the original prompt."""
    orig = original.strip()
    sub = submitted_body.strip()
    if not sub:
        return False
    # Exact match
    if orig == sub:
        return True
    # Truncated: original starts with submitted body
    if orig.startswith(sub) and len(sub) >= min(80, len(orig)):
        return True
    return False


def _convert_single(
    run_log: dict,
    suite: dict,
    suite_path: str,
) -> dict:
    entries = run_log.get("entries") or []
    index = _build_single_index(suite)
    framework = suite.get("framework", "")
    results: list[dict] = []

    for i, entry in enumerate(entries):
        submitted = entry.get("input", "")
        response = entry.get("response")
        body = _strip_ui_prefix(submitted)

        matched: dict[str, Any] | None = None
        # Positional match first (most reliable when counts align)
        if i < len(index):
            if _prompt_matches(index[i]["prompt"], body):
                matched = index[i]
        # Fallback: scan for body match
        if matched is None:
            for idx_entry in index:
                if _prompt_matches(idx_entry["prompt"], body):
                    matched = idx_entry
                    break

        results.append({
            "id": matched["id"] if matched else f"entry-{i + 1}",
            "mandate": matched["mandate"] if matched else "",
            "description": matched["description"] if matched else "",
            "prompt": matched["prompt"] if matched else submitted,
            "response": response or "",
            "ok": bool(response and str(response).strip()),
        })

    return {
        "framework": framework,
        "source_file": suite_path,
        "results": results,
    }


def _format_multi_prompt(prompts: list[str]) -> str:
    """Format all user turns as a transcript for risk assessment context."""
    lines: list[str] = []
    for i, text in enumerate(prompts, 1):
        if text:
            lines.append(f"[Turn {i}] {text}")
    return "\n\n".join(lines)


def _convert_multi(
    run_log: dict,
    suite: dict,
    suite_path: str,
) -> dict:
    batches = run_log.get("batches") or []
    index = _build_multi_index(suite)
    framework = suite.get("framework", "")
    results: list[dict] = []

    for batch_i, batch in enumerate(batches):
        turns = batch.get("turns") or []
        if not turns:
            continue

        matched: dict[str, Any] | None = None
        if batch_i < len(index):
            matched = index[batch_i]

        if not matched and turns:
            first_body = _strip_ui_prefix(turns[0].get("input", ""))
            for idx_entry in index:
                if idx_entry["prompts"] and _prompt_matches(idx_entry["prompts"][0], first_body):
                    matched = idx_entry
                    break

        suite_prompts = matched.get("prompts", []) if matched else []
        last_turn = turns[-1]
        last_response = last_turn.get("response")

        # Use suite prompts when available; fall back to submitted inputs from the run log.
        if suite_prompts and len(suite_prompts) == len(turns):
            prompt_text = _format_multi_prompt(suite_prompts)
            final_prompt = suite_prompts[-1]
        else:
            submitted_prompts = [_strip_ui_prefix(t.get("input", "")) for t in turns]
            prompt_text = _format_multi_prompt(submitted_prompts)
            final_prompt = submitted_prompts[-1] if submitted_prompts else last_turn.get("input", "")

        turn_details = []
        for turn_i, turn in enumerate(turns):
            original_prompt = ""
            if matched and turn_i < len(suite_prompts):
                original_prompt = suite_prompts[turn_i]
            else:
                original_prompt = _strip_ui_prefix(turn.get("input", ""))
            turn_details.append({
                "turn": turn_i + 1,
                "prompt": original_prompt,
                "response": turn.get("response") or "",
            })

        results.append({
            "id": matched["id"] if matched else f"batch-{batch_i + 1}",
            "mandate": matched["mandate"] if matched else "",
            "description": matched["description"] if matched else "",
            "prompt": prompt_text or final_prompt,
            "response": last_response or "",
            "ok": bool(last_response and str(last_response).strip()),
            "turns": turn_details,
        })

    return {
        "framework": framework,
        "source_file": suite_path,
        "results": results,
    }


def convert_run_log(
    run_log_path: Path,
    suite_path: Path,
    output_path: Path | None = None,
) -> Path:
    """
    Convert a browser-bot run log + the original generated suite into a compliance_log.json
    that risk-assess can consume.

    Returns the path to the written compliance log.
    """
    run_log = _load_json(run_log_path)
    suite = _load_json(suite_path)
    suite_rel = str(suite_path)

    mode = run_log.get("mode", "single")
    if mode == "multi":
        compliance_log = _convert_multi(run_log, suite, suite_rel)
    else:
        compliance_log = _convert_single(run_log, suite, suite_rel)

    if output_path is None:
        output_path = run_log_path.parent / "compliance_log.json"

    output_path.write_text(json.dumps(compliance_log, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path
