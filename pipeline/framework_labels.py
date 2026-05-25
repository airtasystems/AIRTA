"""Framework slug → human-readable labels from rubrics/*.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_RUBRICS_DIR = _ROOT / "rubrics"


@lru_cache(maxsize=64)
def _load_rubric_framework(stem: str) -> str | None:
    normalized = stem.replace("-", "_").strip().lower()
    if not normalized:
        return None
    path = _RUBRICS_DIR / f"{normalized}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = (data.get("framework") or "").strip()
    return name or None


def _pretty_slug(slug: str) -> str:
    short = {"eu", "ai", "uk", "us"}
    long_ = {"oecd", "gdpr", "iso"}
    parts = slug.replace("_", "-").split("-")
    words: list[str] = []
    for p in parts:
        if not p:
            continue
        pl = p.lower()
        if pl in short or pl in long_:
            words.append(p.upper())
        else:
            words.append(p.capitalize())
    return " ".join(words)


def framework_label(stem: str, *, suite_framework: str | None = None) -> str:
    """Return display label for a framework stem or test-file stem."""
    from_rubric = _load_rubric_framework(stem)
    if from_rubric:
        return from_rubric
    if suite_framework and str(suite_framework).strip():
        return str(suite_framework).strip()
    return _pretty_slug(stem)


def list_framework_slugs() -> list[str]:
    if not _RUBRICS_DIR.is_dir():
        return []
    out: list[str] = []
    for p in sorted(_RUBRICS_DIR.glob("*.json")):
        if p.stem in ("company", "component"):
            continue
        out.append(p.stem.replace("-", "_"))
    return out


def list_framework_options() -> list[dict[str, str]]:
    """Return [{slug, label}, ...] for UI dropdowns."""
    return [
        {"slug": stem, "label": framework_label(stem)}
        for stem in list_framework_slugs()
    ]
