"""
Configuration for the LLM endpoint discovery app.
Loads from environment and defines paths for auth state and discovered endpoint.

Layout:
- site_config (shared per site): auth_state.json, csrf_token.json, last_refresh.txt, discovered_refresh_url.txt
  e.g. localhost3000/site_config/
- per-component dir: discovered_endpoint.json, payload_format.py, send_payloads.py, payloads.json
  e.g. localhost3000/submissions/, localhost3000/chat/
"""
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load project-root .config (non-sensitive) then .env (secrets) so COMPONENT etc. work when run from any cwd
_config_dir = Path(__file__).resolve().parent
_root = _config_dir.parent
load_dotenv(_root / ".config")
load_dotenv(_root / ".env")
load_dotenv()  # then cwd so local overrides apply

# App under test (ensure str; dotenv can sometimes yield bytes)
_base = os.getenv("APP_URL")
BASE_URL = _base.decode("utf-8", errors="replace") if isinstance(_base, bytes) else (_base or "")
LOGIN_URL = f"{BASE_URL}/login"
# Optional: when set, only intercept requests whose path exactly matches this endpoint
def _get_target_api_url() -> str | None:
    url = (
        os.getenv("TARGET_API_URL")
        or os.getenv("LOCAL_API_URL")
        or os.getenv("API_URL")
    )
    if url:
        return url
    # Fallback: pre-discovery output (python -m pre-discovery)
    # Path: pre-discovery/<sitename>/<component>/format/discovered_api.json
    _netloc = urlparse(BASE_URL or "").netloc
    if isinstance(_netloc, bytes):
        _netloc = _netloc.decode("utf-8", errors="replace")
    _sitename = (_netloc or "").replace(":", "") or "default"
    _comp = (os.getenv("COMPONENT") or "default").strip() or "default"
    _pre_disc = _config_dir.parent / "pre-discovery" / _sitename / _comp / "format" / "discovered_api.json"
    _legacy = _config_dir.parent / "pre-discovery" / "format" / "discovered_api.json"
    for _path in (_pre_disc, _legacy):
        if _path.exists():
            try:
                import json
                data = json.loads(_path.read_text())
                return data.get("target_api_url") or None
            except Exception:
                pass
    return None


TARGET_API_URL = _get_target_api_url()
# Session refresh (site requires refresh every 14 minutes)
REFRESH_URL = os.getenv("REFRESH_URL")
REFRESH_MAX_AGE_SECONDS = 14 * 60  # 14 minutes

# Component name (e.g. submissions, chat); each component has its own state dir under sitename/COMPONENT
COMPONENT = (os.getenv("COMPONENT") or "default").strip() or "default"

# Paths
_COMPONENT_DIR = Path(__file__).resolve().parent
_netloc = urlparse(BASE_URL or "").netloc
if isinstance(_netloc, bytes):
    _netloc = _netloc.decode("utf-8", errors="replace")
_sitename = (_netloc or "").replace(":", "") or "default"

# Shared per-site auth/session (all components under this site use this)
SITE_CONFIG_DIR = _COMPONENT_DIR / _sitename / "site_config"
SITE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
AUTH_STATE_FILE = SITE_CONFIG_DIR / "auth_state.json"
CSRF_TOKEN_FILE = SITE_CONFIG_DIR / "csrf_token.json"
LAST_REFRESH_FILE = SITE_CONFIG_DIR / "last_refresh.txt"
DISCOVERED_REFRESH_URL_FILE = SITE_CONFIG_DIR / "discovered_refresh_url.txt"

# Per-component state (discovered endpoint, generated payload module, payloads)
SITE_STATE_DIR = _COMPONENT_DIR / _sitename / COMPONENT
SITE_STATE_DIR.mkdir(parents=True, exist_ok=True)
DISCOVERED_ENDPOINT_FILE = SITE_STATE_DIR / "discovered_endpoint.json"
DISCOVERED_MULTI_FILE = SITE_STATE_DIR / "discovered_multi_endpoint.json"
PAYLOADS_FILE = SITE_STATE_DIR / "payloads.json"

# Optional: diagnostics/diagnostics.json (project root); when present, chat component can use it as payload source
DIAGNOSTICS_FILE = _config_dir.parent / "diagnostics" / "diagnostics.json"

# One-time migration: if site_config has no auth yet, copy from any existing component dir
if not AUTH_STATE_FILE.exists():
    _site_root = _COMPONENT_DIR / _sitename
    for _d in _site_root.iterdir():
        if _d.is_dir() and _d.name != "site_config":
            _legacy_auth = _d / "auth_state.json"
            if _legacy_auth.exists():
                import shutil
                for _f in ("auth_state.json", "csrf_token.json", "last_refresh.txt", "discovered_refresh_url.txt"):
                    _legacy = _d / _f
                    if _legacy.exists():
                        shutil.copy2(_legacy, SITE_CONFIG_DIR / _f)
                break


def get_refresh_url() -> str | None:
    """REFRESH_URL from env, or discovered URL saved during login (Playwright)."""
    if REFRESH_URL:
        return REFRESH_URL
    if DISCOVERED_REFRESH_URL_FILE.exists():
        try:
            return DISCOVERED_REFRESH_URL_FILE.read_text().strip() or None
        except OSError:
            pass
    return None
