"""HTTP helpers for API-based component submission."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def apply_prompt_template(obj: Any, prompt: str, *, model: str = "") -> Any:
    """Replace ``{{prompt}}`` and ``{{model}}`` in strings inside dict/list structures."""
    if isinstance(obj, str):
        out = obj.replace("{{prompt}}", prompt)
        if model:
            out = out.replace("{{model}}", model)
        return out
    if isinstance(obj, dict):
        return {k: apply_prompt_template(v, prompt, model=model) for k, v in obj.items()}
    if isinstance(obj, list):
        return [apply_prompt_template(v, prompt, model=model) for v in obj]
    return obj


def extract_json_path(data: Any, path: str) -> Any:
    """Extract a dotted path from parsed JSON (supports dict keys and list indices)."""
    path = (path or "").strip()
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        else:
            return None
    return cur


def _merge_url_query(url: str, extra: dict[str, str]) -> str:
    """Append query parameters to *url* (does not overwrite existing keys)."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    if not extra:
        return url
    parsed = urlparse(url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for k, v in extra.items():
        if k not in existing:
            existing[k] = v
    new_query = urlencode(existing)
    return urlunparse(parsed._replace(query=new_query))


def _normalize_provider_auth(headers: dict[str, str], url: str, query_params: dict[str, str]) -> dict[str, str]:
    """Map saved auth to provider-specific headers (e.g. Gemini rejects Authorization)."""
    out = dict(headers)
    host = (url or "").lower()
    if "generativelanguage.googleapis.com" not in host:
        return out

    if out.get("x-goog-api-key"):
        return out

    key = (query_params or {}).get("key", "").strip()
    if not key:
        auth = (out.get("Authorization") or out.get("authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        if auth:
            key = auth

    if key:
        out["x-goog-api-key"] = key
        out.pop("Authorization", None)
        out.pop("authorization", None)
    return out


def auth_headers_for_site(site: str | None, *, url: str = "") -> dict[str, str]:
    """Merge saved auth headers and cookies for API requests."""
    if not site:
        return {}
    from browser_bot.auth_state import load_auth_config

    cfg = load_auth_config(site) or {}
    headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
    cookies = cfg.get("cookies") or []
    if cookies:
        parts = []
        for cookie in cookies:
            if isinstance(cookie, dict) and cookie.get("name") is not None:
                parts.append(f"{cookie['name']}={cookie.get('value', '')}")
        if parts:
            headers.setdefault("Cookie", "; ".join(parts))
    qparams = {str(k): str(v) for k, v in (cfg.get("query_params") or {}).items()}
    return _normalize_provider_auth(headers, url, qparams)


def auth_query_params_for_site(site: str | None) -> dict[str, str]:
    """Query parameters from auth.json (e.g. Gemini ``?key=``)."""
    if not site:
        return {}
    from browser_bot.auth_state import load_auth_config

    cfg = load_auth_config(site) or {}
    raw = cfg.get("query_params") or {}
    return {str(k): str(v) for k, v in raw.items() if v is not None and str(v).strip()}


def resolve_api_url(sub: dict[str, Any], *, site: str | None = None) -> tuple[str | None, str | None]:
    """Resolve final request URL (model + auth query). Returns ``(url, error)``."""
    url = (sub.get("api_url") or "").strip()
    model = (sub.get("api_model") or "").strip()
    if "{{model}}" in url and not model:
        return None, "Model is required: URL contains {{model}} — set the Model field before connecting."
    if model and "{{model}}" in url:
        url = url.replace("{{model}}", model)
    url = _merge_url_query(url, auth_query_params_for_site(site))
    return url, None


def do_api_request(
    sub: dict[str, Any],
    prompt: str,
    *,
    site: str | None = None,
    timeout: float = 120.0,
) -> tuple[int, str | None, str | None]:
    """Send one API submission. Returns ``(status_code, response_text, error)``."""
    url, url_err = resolve_api_url(sub, site=site)
    if url_err:
        return 0, None, url_err

    method = (sub.get("api_method") or "POST").upper()
    headers = {"Accept": "application/json", **dict(sub.get("api_headers") or {})}
    headers.update(auth_headers_for_site(site, url=url or ""))

    model = (sub.get("api_model") or "").strip()
    body_obj = apply_prompt_template(
        sub.get("api_body") or {"prompt": "{{prompt}}"},
        prompt,
        model=model,
    )

    if "generativelanguage.googleapis.com" in (url or "").lower():
        if not headers.get("x-goog-api-key") and "key=" not in (url or ""):
            return 0, None, (
                "Gemini API key missing. In Connect Target → Step 1 choose API key, "
                "header x-goog-api-key (or query param key), then save your Google AI key."
            )
    data: bytes | None = None
    if method in {"POST", "PUT", "PATCH"}:
        if "Content-Type" not in headers and "content-type" not in headers:
            headers["Content-Type"] = "application/json"
        data = json.dumps(body_obj).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(exc)
        parsed = _parse_response_text(raw, sub.get("api_response_path") or "response")
        if parsed:
            return status, parsed, None
        return status, None, f"HTTP {status}: {raw[:500]}"
    except Exception as exc:
        return 0, None, str(exc)

    return status, _parse_response_text(raw, sub.get("api_response_path") or "response"), None


def _parse_response_text(raw: str, response_path: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    extracted = extract_json_path(parsed, response_path)
    if extracted is None:
        return None
    if isinstance(extracted, str):
        return extracted.strip() or None
    return json.dumps(extracted, ensure_ascii=False)
