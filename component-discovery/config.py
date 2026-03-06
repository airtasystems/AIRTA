"""
Configuration for the LLM endpoint discovery app.
Loads from environment and defines paths for auth state and discovered endpoint.

Layout:
- site_config (shared per site): auth_state.json, csrf_token.json
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

# App under test
BASE_URL = os.getenv("APP_URL") or ""
LOGIN_URL = f"{BASE_URL}/login" if BASE_URL else ""
# Optional: when set, only intercept requests whose path exactly matches this endpoint
TARGET_API_URL = (
    os.getenv("TARGET_API_URL")
    or os.getenv("LOCAL_API_URL")
    or os.getenv("API_URL")
)
# Component name (e.g. submissions, chat); each component has its own state dir under sitename/COMPONENT
COMPONENT = (os.getenv("COMPONENT") or "default").strip() or "default"

# Paths
_COMPONENT_DIR = Path(__file__).resolve().parent
_base = BASE_URL or ""
_sitename = (urlparse(_base).netloc or "").replace(":", "") or "default"

# Shared per-site auth/session (all components under this site use this)
SITE_CONFIG_DIR = _COMPONENT_DIR / _sitename / "site_config"
if _sitename != "default":
    SITE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
AUTH_STATE_FILE = SITE_CONFIG_DIR / "auth_state.json"
CSRF_TOKEN_FILE = SITE_CONFIG_DIR / "csrf_token.json"
LAST_REFRESH_FILE = SITE_CONFIG_DIR / "last_refresh.txt"
DISCOVERED_REFRESH_URL_FILE = SITE_CONFIG_DIR / "discovered_refresh_url.txt"

# Per-component state (discovered endpoint, generated payload module, payloads)
SITE_STATE_DIR = _COMPONENT_DIR / _sitename / COMPONENT
if _sitename != "default" and COMPONENT != "default":
    SITE_STATE_DIR.mkdir(parents=True, exist_ok=True)
DISCOVERED_ENDPOINT_FILE = SITE_STATE_DIR / "discovered_endpoint.json"
DISCOVERED_MULTI_FILE = SITE_STATE_DIR / "discovered_multi_endpoint.json"
PAYLOADS_FILE = SITE_STATE_DIR / "payloads.json"

# Optional: diagnostics/diagnostics.json (project root); when present, chat component can use it as payload source
DIAGNOSTICS_FILE = _config_dir.parent / "diagnostics" / "diagnostics.json"

# One-time migration: if site_config has no auth yet, copy from any existing component dir
if not AUTH_STATE_FILE.exists() and _sitename != "default":
    _site_root = _COMPONENT_DIR / _sitename
    for _d in (_site_root.iterdir() if _site_root.is_dir() else []):
        if _d.is_dir() and _d.name != "site_config":
            _legacy_auth = _d / "auth_state.json"
            if _legacy_auth.exists():
                import shutil
                for _f in ("auth_state.json", "csrf_token.json"):
                    _legacy = _d / _f
                    if _legacy.exists():
                        shutil.copy2(_legacy, SITE_CONFIG_DIR / _f)
                break
