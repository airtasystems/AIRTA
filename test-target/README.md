# AIRTA Test Target

Harborline AI - a fictional regulated fintech playground for local browser-bot automation. The chat UI calls a **server-side Gemini** integration (with mock fallback) via `POST /api/chat`.

## Start

From the AIRTA repo root (with venv active or after `python start.py` has installed deps):

```bash
python test-target/app.py
```

Open http://localhost:3000/playground

Environment overrides:

- `TEST_TARGET_HOST` - default `127.0.0.1`
- `TEST_TARGET_PORT` - default `3000` (uses next free port if busy)
- `GEMINI_API_KEY` - required for real LLM replies (loaded from repo `.config` / `.env`)
- `GEMINI_MODEL` - default from `.config` (e.g. `gemini-3.1-flash-lite`)

On startup the server prints whether Gemini is configured or mock fallback is active.

## API

### `POST /api/chat`

Send a prompt to Harborline Advisor and receive a JSON response. Supports single-turn (`prompt`) or multi-turn API tests (`messages`).

**Request (single-turn)**

```json
{ "prompt": "What is the capital of England?" }
```

**Request (multi-turn, OpenAI-style)**

```json
{
  "messages": [
    { "role": "system", "content": "You are Harborline Advisor." },
    { "role": "user", "content": "What is your refund policy?" },
    { "role": "assistant", "content": "Refunds within 30 days are automatic." },
    { "role": "user", "content": "What about after 60 days?" }
  ]
}
```

**Response** (`200`)

```json
{
  "prompt": "What is the capital of England?",
  "response": "London is the capital of England.",
  "model": "gemini-3.1-flash-lite",
  "source": "gemini"
}
```

When `GEMINI_API_KEY` is unset, `source` is `"mock"` and keyword-based canned replies are returned instead.

**Errors**

- `400` - empty or invalid prompt
- `502` - Gemini request failed

Example:

```bash
curl -s -X POST http://localhost:3000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"capital of england"}' | jq
```

### `GET /health`

```json
{
  "ok": true,
  "llm": { "configured": true, "model": "gemini-3.1-flash-lite" },
  "messages_api": true
}
```

For multi-turn Run Tests via API, set `submission.api_context_mode: messages` and `api_body.messages: "{{messages}}"` in component `config.yaml`.

## Run AIRTA against it

1. Start this app on port 3000.
2. Configure `GEMINI_API_KEY` in `.config` or `.env` for real LLM responses.
3. From repo root:

```bash
python main.py run browser-bot/sites/localhost:3000/main/tests/zero-shot/eu-ai-act.json \
  --site localhost:3000 \
  --component main
```

No login is required. Site auth in `browser-bot/sites/localhost:3000/auth.json` can use `auth_mode: none`.

## Selectors

| Element | Selector |
|---------|----------|
| Prompt | `#test-prompt` |
| Submit | `#test-submit` |
| Chat section | `section[aria-labelledby="test-chat-heading"]` |
| Assistant reply | `div[data-testid="assistant-message"]` |
| Response text | `> p` (under the bubble) |

Browser-bot config: `browser-bot/sites/localhost:3000/main/config.yaml`

The playground ships with a welcome bubble (`data-testid="assistant-welcome"`) so AIRTA can establish a response baseline before the first submit.

## Mock fallback

When Gemini is unavailable, keyword-based replies are used:

- **intern + private** → workplace conduct warning
- **credit score / personal data / suitability** → high-risk compliance note
- **refund** / **60 days** → refund policy lines (multi-turn demo)
- **default** → `Echo: …` prefix with truncated prompt

Status text (`Generating response…`) appears outside the captured bubble selectors.

## Endpoints

| Route | Purpose |
|-------|---------|
| `/` | Landing page |
| `/playground` | Chat UI under test |
| `/about` | Fictional company about page (Harborline AI) |
| `/health` | Health + LLM configuration status |
| `/api/chat` | Custom app API for LLM prompts (POST) |
