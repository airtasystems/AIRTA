"""
Auth state: full capture and load (cookies, localStorage, sessionStorage, headers).
"""

import json
from pathlib import Path
from typing import Any

AUTH_FILE = "auth.json"
STORAGE_STATE_FILE = "storage_state.json"  # Legacy

PUBLIC_AUTH_TEMPLATE: dict[str, Any] = {
    "cookies": [],
    "origins": [],
    "headers": {},
    "auth_mode": "none",
}

AUTH_MODE_NONE = "none"
AUTH_MODE_API_KEY = "api_key"
AUTH_MODE_SESSION = "session"


def is_no_login_auth_mode(mode: str | None) -> bool:
    """True for targets that skip browser login capture (public or API key only)."""
    return mode in (AUTH_MODE_NONE, AUTH_MODE_API_KEY)


def get_auth_path(domain: str) -> Path:
    """Path to auth config dir for domain."""
    from browser_bot.sites import _domain_to_site_dir

    return _domain_to_site_dir(domain)


def get_auth_config_path(domain: str) -> Path:
    """Path to auth.json (preferred) or storage_state.json (legacy)."""
    site_dir = get_auth_path(domain)
    auth_json = site_dir / AUTH_FILE
    storage_json = site_dir / STORAGE_STATE_FILE
    return auth_json if auth_json.exists() else storage_json


def auth_config_exists(domain: str) -> bool:
    """True if auth config exists for domain."""
    site_dir = get_auth_path(domain)
    return (site_dir / AUTH_FILE).exists() or (site_dir / STORAGE_STATE_FILE).exists()


def is_auth_configured(domain: str) -> bool:
    """True when auth is ready for browser runs (session capture or explicit public/no-login)."""
    config = load_auth_config(domain)
    if not config:
        return False
    if config.get("auth_mode") in (AUTH_MODE_NONE, AUTH_MODE_API_KEY):
        return True
    if config.get("cookies"):
        return True
    return any(
        o.get("localStorage") or o.get("sessionStorage")
        for o in config.get("origins", [])
    )


def auth_mode_for_domain(domain: str) -> str | None:
    """Return ``none``, ``session``, or None when auth is not configured."""
    if not is_auth_configured(domain):
        return None
    config = load_auth_config(domain) or {}
    mode = config.get("auth_mode")
    if mode in (AUTH_MODE_NONE, AUTH_MODE_API_KEY):
        return mode
    return AUTH_MODE_SESSION


def save_public_auth(domain: str) -> Path:
    """Save a no-login auth stub for public targets."""
    return save_auth_config(domain, dict(PUBLIC_AUTH_TEMPLATE))


def _api_key_header_value(header_name: str, key: str, *, use_bearer: bool | None = None) -> str:
    """Format API key for the given header (Bearer only for Authorization by default)."""
    header = (header_name or "Authorization").strip() or "Authorization"
    if key.lower().startswith("bearer "):
        return key
    if use_bearer is None:
        use_bearer = header.lower() == "authorization"
    if use_bearer:
        return f"Bearer {key}"
    return key


def save_api_key_auth(
    domain: str,
    api_key: str,
    *,
    header_name: str = "Authorization",
    use_bearer: bool | None = None,
    query_param_name: str | None = None,
) -> Path:
    """Save API-key auth: header and/or query param on API transport requests."""
    key = (api_key or "").strip()
    if not key:
        raise ValueError("API key is required")
    header = (header_name or "").strip()
    query_param = (query_param_name or "").strip()
    if not header and not query_param:
        header = "Authorization"

    headers: dict[str, str] = {}
    if header:
        headers[header] = _api_key_header_value(header, key, use_bearer=use_bearer)

    query_params: dict[str, str] = {}
    if query_param:
        query_params[query_param] = key

    config: dict[str, Any] = {
        "cookies": [],
        "origins": [],
        "headers": headers,
        "query_params": query_params,
        "auth_mode": AUTH_MODE_API_KEY,
    }
    return save_auth_config(domain, config)


def load_auth_config(domain: str) -> dict[str, Any] | None:
    """Load auth config. Returns dict or None if not found."""
    path = get_auth_config_path(domain)
    if not path.exists():
        return None
    with open(path) as f:
        data = json.load(f)
    return _normalize_auth_config(data)


def _normalize_auth_config(data: dict) -> dict:
    """Normalize legacy storage_state or full auth.json to unified format."""
    # Ensure each origin has sessionStorage
    for origin in data.get("origins", []):
        if "sessionStorage" not in origin:
            origin["sessionStorage"] = []
    if "headers" not in data:
        data["headers"] = {}
    if "query_params" not in data:
        data["query_params"] = {}
    return data


def save_auth_config(domain: str, config: dict[str, Any]) -> Path:
    """Save auth config to auth.json."""
    from browser_bot.sites import ensure_site_dir

    site_dir = ensure_site_dir(domain)
    path = site_dir / AUTH_FILE
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    return path


def clean_auth_storage(domain: str, max_value_len: int | None = None) -> tuple[bool, str]:
    """
    Strip localStorage/sessionStorage items with value length > max_value_len.
    Re-saves auth.json. Returns (success, message).
    """
    from browser_bot.config import LOCALSTORAGE_MAX_VALUE_LEN
    from browser_bot.sites import ensure_site_dir

    max_len = max_value_len if max_value_len is not None else LOCALSTORAGE_MAX_VALUE_LEN
    config = load_auth_config(domain)
    if not config:
        return False, f"No auth.json for {domain}"

    def _filter(items: list) -> list:
        return [i for i in items if len(str(i.get("value", ""))) <= max_len]

    removed = 0
    for origin in config.get("origins", []):
        ls = origin.get("localStorage", [])
        ss = origin.get("sessionStorage", [])
        new_ls = _filter(ls)
        new_ss = _filter(ss)
        removed += (len(ls) - len(new_ls)) + (len(ss) - len(new_ss))
        origin["localStorage"] = new_ls
        origin["sessionStorage"] = new_ss

    if removed == 0:
        return True, f"No items over {max_len} chars to remove."
    save_auth_config(domain, config)
    return True, f"Removed {removed} items (value > {max_len} chars)."
