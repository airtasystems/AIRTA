"""LLM API format presets for Connect Target → API endpoint configuration.

Verified against provider REST docs (May 2026). API keys live in auth.json, not here.
Template placeholders: {{prompt}}, {{model}} (substituted at request time).
"""

from __future__ import annotations

from typing import Any

# Presets describe request/response shape; API keys are stored separately in auth.json.
LLM_API_PRESETS: list[dict[str, Any]] = [
    {
        "id": "custom",
        "label": "Custom / app wrapper",
        "description": "Simple JSON body with a prompt field (e.g. your app's POST /api/chat).",
        "url": "",
        "method": "POST",
        "response_path": "response",
        "body": {"prompt": "{{prompt}}"},
        "headers": {},
        "auth_header": None,
        "auth_query_param": None,
        "default_model": "",
    },
    {
        "id": "openai",
        "label": "OpenAI Chat Completions",
        "description": "POST https://api.openai.com/v1/chat/completions - Step 1: API key, header Authorization (Bearer).",
        "url": "https://api.openai.com/v1/chat/completions",
        "method": "POST",
        "response_path": "choices.0.message.content",
        "body": {
            "model": "{{model}}",
            "messages": [{"role": "user", "content": "{{prompt}}"}],
        },
        "headers": {},
        "auth_header": "Authorization",
        "auth_query_param": None,
        "default_model": "gpt-4o-mini",
    },
    {
        "id": "gemini",
        "label": "Google Gemini (generateContent)",
        "description": "Google AI Gemini API (not Vertex). Step 1: x-goog-api-key or query param key. Set Model to your API model id.",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{{model}}:generateContent",
        "method": "POST",
        "response_path": "candidates.0.content.parts.0.text",
        "body": {
            "contents": [{"role": "user", "parts": [{"text": "{{prompt}}"}]}],
        },
        "headers": {},
        "auth_header": "x-goog-api-key",
        "auth_query_param": "key",
        "default_model": "gemini-2.5-flash",
    },
    {
        "id": "anthropic",
        "label": "Anthropic Messages",
        "description": "POST https://api.anthropic.com/v1/messages - Step 1: x-api-key. Requires anthropic-version header (preset includes it).",
        "url": "https://api.anthropic.com/v1/messages",
        "method": "POST",
        "response_path": "content.0.text",
        "body": {
            "model": "{{model}}",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "{{prompt}}"}],
        },
        "headers": {"anthropic-version": "2023-06-01"},
        "auth_header": "x-api-key",
        "auth_query_param": None,
        "default_model": "claude-sonnet-4-6",
    },
    {
        "id": "azure_openai",
        "label": "Azure OpenAI (deployments API)",
        "description": "Legacy deployments URL: replace {resource} with your Azure resource name; Model = deployment name. Step 1: api-key header. For v1 API (no api-version), use Custom preset.",
        "url": "https://{resource}.openai.azure.com/openai/deployments/{{model}}/chat/completions?api-version=2024-10-21",
        "method": "POST",
        "response_path": "choices.0.message.content",
        "body": {
            "messages": [{"role": "user", "content": "{{prompt}}"}],
        },
        "headers": {},
        "auth_header": "api-key",
        "auth_query_param": None,
        "default_model": "",
    },
    {
        "id": "test_target",
        "label": "AIRTA test target (local)",
        "description": "Harborline test app - no API key; start python test-target/app.py first.",
        "url": "http://127.0.0.1:3000/api/chat",
        "method": "POST",
        "response_path": "response",
        "body": {"prompt": "{{prompt}}"},
        "headers": {},
        "auth_header": None,
        "auth_query_param": None,
        "default_model": "",
    },
]


def get_llm_api_presets() -> list[dict[str, Any]]:
    """Return preset list for UI (JSON-serializable)."""
    return [dict(p) for p in LLM_API_PRESETS]


def get_preset(preset_id: str) -> dict[str, Any] | None:
    for p in LLM_API_PRESETS:
        if p.get("id") == preset_id:
            return dict(p)
    return None
