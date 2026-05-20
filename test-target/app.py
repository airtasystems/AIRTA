"""AIRTA local test target — Harborline AI playground for browser-bot automation."""

from __future__ import annotations

import os
import socket
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from llm import ChatResult, gemini_configured, gemini_model, generate_reply

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
HOME = STATIC / "home.html"
PLAYGROUND = STATIC / "playground.html"
ABOUT = STATIC / "about.html"

app = FastAPI(title="AIRTA Test Target", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)


class ChatResponse(BaseModel):
    prompt: str
    response: str
    model: str
    source: str


def _chat_result_to_response(result: ChatResult) -> ChatResponse:
    return ChatResponse(
        prompt=result.prompt,
        response=result.response,
        model=result.model,
        source=result.source,
    )


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "llm": {
                "configured": gemini_configured(),
                "model": gemini_model(),
            },
        }
    )


@app.post("/api/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    """Send a prompt to Harborline Advisor and receive an LLM response."""
    try:
        return _chat_result_to_response(generate_reply(body.prompt))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/")
def home() -> FileResponse:
    return FileResponse(HOME)


@app.get("/playground")
def playground() -> FileResponse:
    return FileResponse(PLAYGROUND)


@app.get("/about")
def about() -> FileResponse:
    return FileResponse(ABOUT)


def _next_available_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    return preferred


def main() -> None:
    import uvicorn

    host = os.getenv("TEST_TARGET_HOST", "127.0.0.1")
    preferred = int(os.getenv("TEST_TARGET_PORT", "3000"))
    port = _next_available_port(host, preferred)
    if port != preferred:
        print(f"Port {preferred} is in use; starting test target on {port} instead.")
    llm_status = "Gemini" if gemini_configured() else "mock fallback (set GEMINI_API_KEY)"
    print(f"AIRTA test target: http://{host}:{port}/playground  [{llm_status}]")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
