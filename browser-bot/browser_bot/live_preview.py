"""Live browser screenshot preview during AIRTA web UI test runs."""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from browser_bot.submit.common import log_airta_progress

if TYPE_CHECKING:
    from playwright.async_api import Page

PREVIEW_INTERVAL_S = 1.0
_slot_locks: dict[int, asyncio.Lock] = {}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_live_preview_path(job_id: str, slot: int = 0) -> Path:
    """Path for one preview slot (pool/cluster parallel tasks use slot 0..N-1)."""
    preview_dir = _project_root() / "web" / "tmp" / "previews" / job_id
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir / f"{slot}.png"


def cleanup_live_preview(job_id: str) -> None:
    preview_root = _project_root() / "web" / "tmp" / "previews"
    slot_dir = preview_root / job_id
    if slot_dir.is_dir():
        try:
            shutil.rmtree(slot_dir)
        except OSError:
            pass
    legacy = preview_root / f"{job_id}.png"
    if legacy.is_file():
        try:
            legacy.unlink()
        except OSError:
            pass


def emit_preview_layout(slot_count: int) -> None:
    """Tell the web UI how many parallel browser preview panes to show."""
    job_id = os.environ.get("AIRTA_JOB_ID", "").strip()
    if not job_id or slot_count <= 1:
        return
    log_airta_progress(
        {
            "type": "preview_layout",
            "job_id": job_id,
            "slots": int(slot_count),
        }
    )


def _lock_for_slot(slot: int) -> asyncio.Lock:
    if slot not in _slot_locks:
        _slot_locks[slot] = asyncio.Lock()
    return _slot_locks[slot]


@asynccontextmanager
async def live_preview_context(page: Page, *, slot: int = 0):
    """Capture a screenshot every 1s into previews/{job_id}/{slot}.png."""
    job_id = os.environ.get("AIRTA_JOB_ID", "").strip()
    if not job_id:
        yield
        return

    path = get_live_preview_path(job_id, slot)
    stop = asyncio.Event()
    slot_lock = _lock_for_slot(slot)

    async def _capture() -> None:
        async with slot_lock:
            try:
                await page.screenshot(path=str(path), type="png")
                log_airta_progress(
                    {
                        "type": "screenshot",
                        "job_id": job_id,
                        "slot": slot,
                    }
                )
            except Exception:
                pass

    async def _loop() -> None:
        await _capture()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=PREVIEW_INTERVAL_S)
                break
            except asyncio.TimeoutError:
                await _capture()

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
