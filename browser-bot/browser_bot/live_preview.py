"""Live browser screenshot preview during AIRTA web UI test runs."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from browser_bot.submit.common import (
    log_airta_progress,
    map_preview_display_slot,
    preview_ui_slot_count,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

PREVIEW_INTERVAL_S = 1.0
_slot_locks: dict[int, asyncio.Lock] = {}
_compliance_screenshot_seq = 0
_compliance_screenshot_seq_lock = threading.Lock()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def get_live_preview_path(job_id: str, display_slot: int = 0) -> Path:
    """Ephemeral latest frame for the web UI (one file per display pane 0..7)."""
    preview_dir = _project_root() / "web" / "tmp" / "previews" / job_id
    preview_dir.mkdir(parents=True, exist_ok=True)
    return preview_dir / f"{display_slot}.png"


def reset_compliance_screenshot_sequence() -> None:
    """Reset per-run counter when a new logs/{timestamp}/ session starts."""
    global _compliance_screenshot_seq
    with _compliance_screenshot_seq_lock:
        _compliance_screenshot_seq = 0


def allocate_compliance_screenshot_path(
    *,
    logical_slot: int,
    display_slot: int,
) -> Path | None:
    """New timestamped file under logs/{run}/screenshots/ (never overwritten)."""
    run_dir = os.environ.get("AIRTA_RUN_LOG_DIR", "").strip()
    if not run_dir:
        return None
    shots_dir = Path(run_dir) / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    captured_at = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
    global _compliance_screenshot_seq
    with _compliance_screenshot_seq_lock:
        _compliance_screenshot_seq += 1
        seq = _compliance_screenshot_seq
    name = (
        f"{captured_at}_{seq:06d}"
        f"_pane{display_slot + 1:02d}_browser{logical_slot + 1:02d}.png"
    )
    return shots_dir / name


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


def emit_preview_layout(parallel_count: int) -> None:
    """Tell the web UI how many parallel browser preview panes to show (max 8)."""
    job_id = os.environ.get("AIRTA_JOB_ID", "").strip()
    if not job_id or parallel_count <= 1:
        return
    ui_slots = preview_ui_slot_count(parallel_count)
    log_airta_progress(
        {
            "type": "preview_layout",
            "job_id": job_id,
            "slots": ui_slots,
            "parallel_count": int(parallel_count),
        }
    )


def _lock_for_slot(display_slot: int) -> asyncio.Lock:
    if display_slot not in _slot_locks:
        _slot_locks[display_slot] = asyncio.Lock()
    return _slot_locks[display_slot]


@asynccontextmanager
async def live_preview_context(page: Page, *, slot: int = 0):
    """Capture a screenshot every 1s; max 8 UI panes, round-robin by logical slot."""
    job_id = os.environ.get("AIRTA_JOB_ID", "").strip()
    logical_slot = int(slot)
    display_slot = map_preview_display_slot(logical_slot)
    if not job_id:
        yield
        return

    tmp_path = get_live_preview_path(job_id, display_slot)
    stop = asyncio.Event()
    slot_lock = _lock_for_slot(display_slot)

    async def _capture() -> None:
        async with slot_lock:
            try:
                await page.screenshot(path=str(tmp_path), type="png")
                persist_path = allocate_compliance_screenshot_path(
                    logical_slot=logical_slot,
                    display_slot=display_slot,
                )
                if persist_path is not None:
                    shutil.copy2(tmp_path, persist_path)
                log_airta_progress(
                    {
                        "type": "screenshot",
                        "job_id": job_id,
                        "slot": display_slot,
                        "logical_slot": logical_slot,
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
