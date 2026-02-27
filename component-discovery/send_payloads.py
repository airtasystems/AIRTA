"""
Send payloads from payloads.json to the discovered endpoint. Prefers the site's
payload_format.py (e.g. localhost3000/payload_format.py) when present; otherwise
uses a minimal fallback (title -> title, text -> description).
"""
import asyncio
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from . import auth as auth_module
from . import payload_format as payload_format_shared
from .config import (
    AUTH_STATE_FILE,
    CSRF_TOKEN_FILE,
    DISCOVERED_ENDPOINT_FILE,
    PAYLOADS_FILE,
    SITE_STATE_DIR,
)
from . import evasion


def _get_site_payload_format_module():
    """Load site payload_format.py if present (e.g. localhost3000/payload_format.py)."""
    site_py = SITE_STATE_DIR / "payload_format.py"
    if not site_py.exists():
        return None
    spec = importlib.util.spec_from_file_location("site_payload_format", site_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_body_fallback(payload_format: dict[str, Any], overrides: dict[str, str] | None) -> tuple[str, str]:
    """Minimal build when no site payload_format: overrides are field names (e.g. title, description)."""
    overrides = overrides or {}
    encoding = payload_format.get("encoding", "unknown")
    fields = dict(payload_format.get("fields", {}))
    fields.update(overrides)
    if encoding == "multipart/form-data":
        boundary = payload_format.get("boundary", "----formboundary")
        lines = []
        for name, value in fields.items():
            lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
        lines.append(f"--{boundary}--\r\n")
        return "".join(lines), f"multipart/form-data; boundary={boundary}"
    return json.dumps(fields), "application/json"


def _load_csrf() -> str:
    if not CSRF_TOKEN_FILE.exists():
        return ""
    try:
        data = json.loads(CSRF_TOKEN_FILE.read_text())
        return data.get("csrf_token", "")
    except (json.JSONDecodeError, OSError):
        return ""


async def send_payloads_from_list(
    payloads_list: list[dict],
    *,
    verbose: bool = True,
) -> list[dict]:
    """
    POST each payload in payloads_list to the discovered endpoint.

    For sites with a site-specific payload_format.py (e.g. chat components),
    each payload dict is passed through as overrides directly to build_body()
    so the site module can interpret keys like "messages", "title", etc.

    For sites without a site-specific payload_format, payloads_list items
    should have at least "title" and "text"; these are mapped via the shared
    fallback format (title -> title, text -> description).

    Returns list of {title, status, ok, response, error?} for the caller to log.
    """
    if not DISCOVERED_ENDPOINT_FILE.exists():
        if verbose:
            print(f"[-] No discovered endpoint at {DISCOVERED_ENDPOINT_FILE}. Run 'discover' first.")
        return []
    if not AUTH_STATE_FILE.exists():
        if verbose:
            print(f"[-] No session at {AUTH_STATE_FILE}. Run 'login' first.")
        return []
    discovered = json.loads(DISCOVERED_ENDPOINT_FILE.read_text())
    payload_format = discovered.get("payload_format")
    if not payload_format:
        payload_format = payload_format_shared.parse_payload_from_request(
            discovered.get("headers", {}),
            discovered.get("payload_schema"),
        )
        if not payload_format.get("fields"):
            if verbose:
                print("[-] Could not derive payload format from discovered endpoint.")
            return []
    site_payload = _get_site_payload_format_module()
    if site_payload is not None:
        build_body_fn = site_payload.build_body
        use_raw_overrides = True
        override_keys = None
    else:
        build_body_fn = _build_body_fallback
        use_raw_overrides = False
        override_keys = ("title", "description")
    if not await auth_module.ensure_session_fresh():
        if verbose:
            print("[-] Session refresh failed. Run 'login' or 'refresh' and try again.")
        return []
    url = discovered["url"]
    headers = dict(discovered.get("headers", {}))
    headers.pop("content-length", None)
    headers.pop("host", None)
    headers.pop("accept-encoding", None)
    csrf = _load_csrf()
    if csrf:
        headers["X-CSRF-Token"] = csrf
        headers["X-XSRF-TOKEN"] = csrf
    results: list[dict] = []
    proxy = evasion.get_playwright_proxy()
    if verbose:
        print("[*] Evasion: throttle, retry on 429, header rotation" + ("; proxy=" + proxy["server"] if proxy else "") + ".")
    async with async_playwright() as p:
        api_context = await p.request.new_context(
            storage_state=str(AUTH_STATE_FILE),
            proxy=proxy,
        )
        try:
            for i, p_item in enumerate(payloads_list):
                if i > 0:
                    await asyncio.sleep(evasion.THROTTLE_BETWEEN_PAYLOADS_SEC)

                title = p_item.get("title", f"Payload {i+1}")

                if use_raw_overrides:
                    # Site-specific payload_format: pass through all keys so the
                    # site module can map them via PAYLOAD_KEY_TO_FIELD.
                    overrides = {k: v for k, v in p_item.items() if v is not None}
                else:
                    text = p_item.get("text", "")
                    overrides = {override_keys[0]: title, override_keys[1]: text}  # type: ignore[index]

                body, content_type = build_body_fn(payload_format, overrides)
                req_headers = {**headers, "Content-Type": content_type, **evasion.rotated_headers()}
                try:
                    response = await evasion.post_with_retry_429(
                        api_context, url, req_headers, body
                    )
                    resp_text = await response.text()
                    results.append({"title": title, "status": response.status, "ok": response.ok, "response": resp_text})
                    if verbose:
                        if not response.ok:
                            print(f"  [{response.status}] {title}" + (f" — {resp_text}" if resp_text else ""))
                        else:
                            print(f"  [{response.status}] {title}")
                except evasion.RateLimit429:
                    results.append({"title": title, "status": 429, "ok": False, "response": None})
                    if verbose:
                        print(f"  [429] {title} (max retries exceeded)")
                except Exception as e:
                    results.append({"title": title, "status": None, "ok": False, "error": str(e), "response": None})
                    if verbose:
                        print(f"  [error] {title} — {e}")
        finally:
            await api_context.dispose()
    return results


async def send_payloads() -> None:
    """
    Load discovered endpoint (with payload_format), load payloads.json, ensure
    session fresh, then POST each payload with overrides title + description (from text).
    """
    if not PAYLOADS_FILE.exists():
        print(f"[-] No payloads file at {PAYLOADS_FILE}. Create one with a 'payloads' array of {{'title', 'text'}}.")
        return
    payloads_data = json.loads(PAYLOADS_FILE.read_text())
    payloads = payloads_data.get("payloads", payloads_data) if isinstance(payloads_data, dict) else payloads_data
    if not payloads:
        print("[-] No payloads in file (expect 'payloads' array).")
        return
    results = await send_payloads_from_list(payloads, verbose=True)
    if not results:
        return
    ok_count = sum(1 for r in results if r.get("ok"))
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_path = SITE_STATE_DIR / f"{timestamp}_log.json"
    log_path.write_text(json.dumps({"timestamp": timestamp, "results": results}, indent=2), encoding="utf-8")
    print(f"[+] Log: {log_path}")
    print(f"\n[*] Sent {len(results)} payloads, {ok_count} OK.")


async def run_refresh_every(minutes: float = 4) -> None:
    """
    Run the refresh logic (session + CSRF re-extraction) every `minutes` minutes,
    in a loop. Stop with Ctrl+C.
    """
    if not AUTH_STATE_FILE.exists():
        print(f"[-] No session at {AUTH_STATE_FILE}. Run 'login' first.")
        return
    print(f"[*] Refreshing every {minutes} minutes (Ctrl+C to stop).")
    while True:
        ok = await auth_module.refresh_session()
        if not ok:
            print("[-] Refresh failed; will retry next interval.")
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            break
