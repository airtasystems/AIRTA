# Component discovery

Finds and records the auth and request shape for a **black-box, auth-only LLM API** inside an LLM-driven app. You log in once in a browser; then you trigger one manual request to the LLM in the app; this tool captures that request and saves the endpoint URL, headers (including CSRF), and payload schema.

## How it works

1. **Login** – Opens the app’s login page. You sign in (and complete MFA if required). Session and CSRF are saved to `<sitename>/site_config/` and **shared by all components** on that site. Run once per site.
2. **Discover** – Opens the app using the saved session and listens for the POST that matches the API URL in `.env`. You perform one action in the app that calls the LLM (e.g. send a chat message). That request is intercepted and saved to `<sitename>/<COMPONENT>/discovered_endpoint.json` with **payload_format** and **payload_schema**. Set `COMPONENT` (e.g. `chat`, `submissions`) before each discover/generate.
3. **Generate payload module (optional)** – Run `generate-payload-module` to call the Gemini API: it analyzes the payload schema, identifies which fields are user-input vs static, and writes a site-specific `payload_format.py` and `send_payloads.py` in the site dir (e.g. `localhost3000/`). Requires `GEMINI_API_KEY` in `.env` and `pip install google-genai`.
4. **Send payloads** – POSTs each entry from `payloads.json` to the discovered endpoint. If the site dir contains `send_payloads.py`, that script is used (with the site’s `payload_format.py`); otherwise the default sender runs and maps `title`/`text` to the endpoint’s fields using the shared payload format.

## Setup

- Python 3.10+
- Install deps (from repo root or `C06`): `pip install playwright python-dotenv`
- For `generate-payload-module`: `pip install google-genai`
- For WAF evasion (stealth) and human-like browser behaviour: `pip install playwright-stealth`
- For rate-limit evasion (retry on 429 with Retry-After): `pip install tenacity`
- Install browsers: `playwright install chromium`
- Create a `.env` in `C06/` with:
  - `APP_URL` – base URL of the app (e.g. `http://localhost:3000`)
  - `API_URL` – full URL of the auth-only API to intercept (e.g. `http://localhost:3000/api/v2/submissions`)
  - `COMPONENT` – AI component name (e.g. `submissions`). State is stored under `<sitename>/<COMPONENT>/`. Defaults to `default` if unset.
  - `GEMINI_API_KEY` – (optional) for `generate-payload-module` to identify user-input fields via Gemini
  - `REFRESH_URL` – (optional) override session refresh URL; default is `{APP_URL}/api/v2/auth/refresh`
  - `PROXY_LIST` – (optional) send traffic via a proxy (e.g. Burp Suite: `PROXY_LIST=http://127.0.0.1:8080`). Comma-separated; first URL is used for `send-payloads`.

## Usage

From the `C06` directory (so that `A04_component_discovery` is on the path and `.env` is found):

```bash
python -m A04_component_discovery <command>
```

| Command | Description |
|--------|-------------|
| `run` | Interactive menu: login, discover, generate, refresh; auth refresh every 4 min in background. |
| `login` | Capture session + CSRF only. |
| `discover` | Intercept one request to the API URL; save endpoint + payload format to `<sitename>/<COMPONENT>/discovered_endpoint.json`. |
| `generate-payload-module` | Use Gemini to analyze payload schema and write site `payload_format.py` and `send_payloads.py`. |
| `refresh` | Refresh session and re-extract CSRF once. |
| `refresh-interval` | Run refresh every 4 min (or `--minutes N`). Ctrl+C to stop. |
| `send-payloads` | POST each entry in `payloads.json` (throttled; retries on 429). Uses site `send_payloads.py` if present. |

```bash
# One flow: login then discover (browser opens twice)
python -m A04_component_discovery run
```

Or run the steps separately:

```bash
# 1. Capture session and CSRF (browser opens; log in and complete MFA, then press Enter)
python -m A04_component_discovery login

# 2. Discover the LLM endpoint (browser opens with your session; trigger one LLM request in the app)
python -m A04_component_discovery discover

# 3. (Optional) Generate site-specific payload module using Gemini (needs GEMINI_API_KEY and google-genai)
python -m A04_component_discovery generate-payload-module

# Refresh auth tokens (required every 14 min; discover/send-payloads refresh automatically when needed)
python -m A04_component_discovery refresh

# Run refresh every 4 minutes in a loop (keeps session + CSRF valid; Ctrl+C to stop)
python -m A04_component_discovery refresh-interval
python -m A04_component_discovery refresh-interval --minutes 6   # custom interval

# POST each payload from payloads.json to the discovered endpoint (throttle + 429 retry)
python -m A04_component_discovery send-payloads
```

State is stored under `<sitename>/` (from `APP_URL`, e.g. `localhost3000/`):

**Shared per site** (`<sitename>/site_config/`) — one login for all components on that site:
- `auth_state.json` – Playwright storage state (cookies, etc.)
- `csrf_token.json` – Extracted CSRF token (if present)
- `last_refresh.txt` – Timestamp of last token refresh (14‑minute refresh rule)
- `discovered_refresh_url.txt` – (optional) Refresh URL discovered during login

**Per component** (`<sitename>/<COMPONENT>/`, e.g. `localhost3000/submissions/`, `localhost3000/chat/`) — set `COMPONENT` in `.env`:
- `discovered_endpoint.json` – URL, method, headers (CSRF/cookie omitted), **payload_format**, **payload_schema**
- `payload_format.py` – (after `generate-payload-module`) Mapping from payload keys to API field names
- `send_payloads.py` – (after `generate-payload-module`) Component-specific sender; used by `send-payloads` when present
- `payloads.json` – Payloads for this component (array of `{title, text}` or schema-defined keys)

**Token refresh:** This site requires refreshing auth tokens every 14 minutes. Before `discover` and `send-payloads`, the app refreshes the session automatically if it’s older than 14 minutes. After each refresh, the CSRF token is re-extracted from the app and saved so subsequent requests don’t get `invalid_csrf_token`. You can run `refresh` manually, or run `refresh-interval` in a separate terminal to refresh every 4 minutes (default; use `--minutes N` to change). Override the refresh URL in `.env` with `REFRESH_URL` if it differs from `{APP_URL}/api/v2/auth/refresh`.

**Payloads file:** Put `payloads.json` in the component dir (e.g. `localhost3000/submissions/payloads.json`) with a `payloads` array of objects that have `title` and `text`. Example:

```json
{"payloads": [{"title": "Example", "text": "Hello world"}]}
```

Use `--headless` only if you don’t need to see the browser (e.g. for scripting); login and discover are easier with the browser visible.

**Evasion (A03 lessons):** The app integrates **tenacity** (retry on 429 with Retry-After), **playwright-stealth** (login/discover pages), **human simulation** (viewport, delays, scroll), **header rotation** (User-Agent, Accept-Language per request in send-payloads), and **optional proxy** (e.g. `PROXY_LIST=http://127.0.0.1:8080` for Burp Suite).
