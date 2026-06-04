"""Multi-string UI submission: N prompts per page/session in sequence."""

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from browser_bot.browser.human_behavior import human_mouse_wander
from browser_bot.config import EVASION_REQUEST_DELAY_S, FETCH_METHOD, get_posts_batches
from browser_bot.sites import get_storage_state_path, get_submission_config

from browser_bot.live_preview import emit_preview_layout, live_preview_context
from browser_bot.page_blockers import PageBlockedError, check_login_wall_before_submit, ensure_page_ready_for_submit
from browser_bot.submit.common import (
    NonSuccessResponseError,
    SubmissionProgressTracker,
    _do_one_submit_step,
    _write_run_log,
    append_test_prompt_delimiter,
    begin_run_log_session,
    format_ui_run_label,
    log_evasion,
    parallel_fetchers_for_ui,
    run_with_evasion_retry,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


async def do_ui_submit_sequence_with_page(
    page: "Page",
    start_url: str,
    inputs: list[dict],
    submit_selector: str,
    texts: list[str],
    *,
    response_selector: str = "",
    response_within_selector: str = "",
    response_text_within_selector: str = "",
    submit_via: str = "click",
    response_wait_ms: int = 5000,
    human_behavior: bool = False,
    progress_tracker: "SubmissionProgressTracker | None" = None,
    site: str = "",
    component: str = "",
    blockers: list[dict] | None = None,
    preview_slot: int = 0,
    run_label: str = "",
) -> list[tuple[str, str | None]]:
    """Run a sequence of UI submissions on the same page. Returns list of (text, response_text)."""
    async with live_preview_context(page, slot=preview_slot):
        await asyncio.sleep(0.1 + time.perf_counter() % 0.15)
        await page.goto(start_url, wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(0.25)
        await ensure_page_ready_for_submit(
            page,
            site=site,
            component=component,
            inputs=inputs,
            submit_selector=submit_selector,
            start_url=start_url,
            blockers=blockers,
            run_label=run_label,
        )
        if human_behavior:
            await human_mouse_wander(page, count=1)

        results: list[tuple[str, str | None]] = []
        for turn_i, text in enumerate(texts):
            turn_label = (
                f"{run_label} · turn {turn_i + 1}/{len(texts)}"
                if run_label
                else f"turn {turn_i + 1}/{len(texts)}"
            )
            await check_login_wall_before_submit(
                page,
                site=site,
                component=component,
                start_url=start_url,
                blockers=blockers,
                run_label=turn_label,
            )
            text_out, response_out, _full_content = await _do_one_submit_step(
                page,
                inputs,
                submit_selector,
                text,
                response_selector=response_selector,
                response_within_selector=response_within_selector,
                response_text_within_selector=response_text_within_selector,
                submit_via=submit_via,
                response_wait_ms=response_wait_ms,
                multi_turn=True,
            )
            results.append((text_out, response_out))
            if progress_tracker is not None:
                progress_tracker.record_completed(1)
        return results


async def run_ui_submission_multi(
    site: str,
    component: str,
    *,
    pool_fetcher=None,
    cluster_fetcher=None,
    human_fetcher=None,
    suite_path=None,
) -> tuple[list[tuple[str, str | None]], Path | None]:
    """
    Run UI submission for each batch in posts.json (array of arrays).
    Each batch runs in one page/session. Returns (flattened list of (input_string, response_text) tuples, log_path or None).
    """
    sub = get_submission_config(site, component)
    if not sub:
        return [], None

    batches = get_posts_batches(suite_path=suite_path)
    if not batches:
        return [], None
    batches = [[append_test_prompt_delimiter(t) for t in batch] for batch in batches]

    storage_path = get_storage_state_path(site)
    if not storage_path:
        return [], None

    start_url = sub["start_url"]
    inputs: list[dict] = sub["inputs"]
    submit_selector = sub["submit_selector"]
    response_selector = sub.get("response_selector") or ""
    response_within_selector = sub.get("response_within_selector") or ""
    response_text_within_selector = sub.get("response_text_within_selector") or ""
    submit_via = sub.get("submit_via", "click")
    response_wait_ms = int(sub.get("response_wait_ms", 5000) or 5000)
    blockers = sub.get("blockers") or []

    fetchers_to_try: list[tuple] = []
    if pool_fetcher:
        fetchers_to_try.append((pool_fetcher, False))
    if cluster_fetcher:
        fetchers_to_try.append((cluster_fetcher, False))
    if human_fetcher:
        fetchers_to_try.append((human_fetcher, True))

    if not fetchers_to_try:
        return [], None

    all_results: list[tuple[str, str | None]] = []
    storage_str = str(storage_path)

    total_turns = sum(len(b) for b in batches)
    begin_run_log_session(site, component)
    tracker = SubmissionProgressTracker("multi", total_turns)
    tracker.emit_run_start()

    batch_prompt_starts: list[int] = []
    _offset = 0
    for _batch in batches:
        batch_prompt_starts.append(_offset)
        _offset += len(_batch)

    def _batch_label(i: int, batch: list[str], *, retry: bool = False, parallel: bool) -> str:
        return format_ui_run_label(
            batch_index=i,
            batch_count=len(batches),
            turn_count=len(batch),
            prompt_start=batch_prompt_starts[i] + 1,
            prompt_end=batch_prompt_starts[i] + len(batch),
            prompt_total=total_turns,
            browser_slot=i if parallel else None,
            retry=retry,
        )

    ui_kwargs = dict(
        site=site,
        component=component,
        blockers=blockers,
        response_selector=response_selector,
        response_within_selector=response_within_selector,
        response_text_within_selector=response_text_within_selector,
        submit_via=submit_via,
        response_wait_ms=response_wait_ms,
    )

    method = FETCH_METHOD.lower()
    parallel_fetchers = []
    if len(batches) > 1:
        parallel_fetchers = parallel_fetchers_for_ui(method, pool_fetcher, cluster_fetcher)

    if parallel_fetchers:
        emit_preview_layout(len(batches))

        async def _run_batch_with_human(
            batch: list[str],
            *,
            preview_slot: int = 0,
            run_label: str = "",
        ):
            if human_fetcher is None:
                return None

            async def _cb(page, b=batch, ps=preview_slot, rl=run_label):
                return await do_ui_submit_sequence_with_page(
                    page,
                    start_url,
                    inputs,
                    submit_selector,
                    b,
                    human_behavior=True,
                    progress_tracker=None,
                    preview_slot=ps,
                    run_label=rl,
                    **ui_kwargs,
                )

            try:
                return await run_with_evasion_retry(
                    lambda f=_cb: human_fetcher.with_page(f, storage_path=storage_str)
                )
            except PageBlockedError:
                raise
            except NonSuccessResponseError:
                return None

        async def _run_batch(
            batch: list[str],
            fetcher,
            *,
            preview_slot: int = 0,
            run_label: str = "",
        ):
            async def _cb(page, b=batch, ps=preview_slot, rl=run_label):
                return await do_ui_submit_sequence_with_page(
                    page,
                    start_url,
                    inputs,
                    submit_selector,
                    b,
                    human_behavior=False,
                    progress_tracker=None,
                    preview_slot=ps,
                    run_label=rl,
                    **ui_kwargs,
                )
            try:
                return await run_with_evasion_retry(
                    lambda f=_cb, fet=fetcher: fet.with_page(f, storage_path=storage_str)
                )
            except PageBlockedError:
                raise

        async def _retry_fast_then_human(
            batch: list[str],
            *,
            preview_slot: int = 0,
            run_label: str = "",
        ):
            for fetcher in parallel_fetchers[1:]:
                try:
                    retry_result = await _run_batch(
                        batch, fetcher, preview_slot=preview_slot, run_label=run_label
                    )
                except PageBlockedError:
                    raise
                except Exception:
                    retry_result = None
                if retry_result and all(resp for _, resp in retry_result):
                    return retry_result
            return await _run_batch_with_human(
                batch, preview_slot=preview_slot, run_label=run_label
            )

        async def _finalize_batch_result(
            batch: list[str],
            r,
            *,
            preview_slot: int,
            batch_index: int,
        ) -> list[tuple[str, str | None]]:
            retry_label = _batch_label(batch_index, batch, retry=True, parallel=True)
            if isinstance(r, PageBlockedError):
                print(f"[!] {r}", flush=True)
                raise r
            if isinstance(r, Exception):
                fallback = await _retry_fast_then_human(
                    batch, preview_slot=preview_slot, run_label=retry_label
                )
                if fallback and all(resp for _, resp in fallback):
                    return list(fallback)
                return [(t, None) for t in batch]
            if r is not None:
                if all(resp for _, resp in r):
                    return list(r)
                fallback = await _retry_fast_then_human(
                    batch, preview_slot=preview_slot, run_label=retry_label
                )
                if fallback and all(resp for _, resp in fallback):
                    return list(fallback)
                return list(r)
            fallback = await _retry_fast_then_human(
                batch, preview_slot=preview_slot, run_label=retry_label
            )
            if fallback and all(resp for _, resp in fallback):
                return list(fallback)
            return [(t, None) for t in batch]

        async def _run_and_finalize_batch(i: int, batch: list[str]) -> list[tuple[str, str | None]]:
            run_label = _batch_label(i, batch, parallel=True)
            r = await _run_batch(
                batch, parallel_fetchers[0], preview_slot=i, run_label=run_label
            )
            rows = await _finalize_batch_result(batch, r, preview_slot=i, batch_index=i)
            tracker.record_completed(len(batch))
            return rows

        batch_rows = await asyncio.gather(
            *[_run_and_finalize_batch(i, batch) for i, batch in enumerate(batches)],
            return_exceptions=True,
        )
        for item in batch_rows:
            if isinstance(item, PageBlockedError):
                raise item
            if isinstance(item, BaseException):
                raise item
            all_results.extend(item)
    else:
        for i, batch in enumerate(batches):
            if i > 0:
                log_evasion(
                    "sequential_burst_pause",
                    sleep_s=EVASION_REQUEST_DELAY_S,
                    detail="Pause between sequential batches to reduce burst-rate detection",
                )
                await asyncio.sleep(EVASION_REQUEST_DELAY_S)
            batch_results = None
            seq_label = _batch_label(i, batch, parallel=False)
            for fetcher, human_behavior in fetchers_to_try:
                async def _cb(page, b=batch, hb=human_behavior, rl=seq_label):
                    return await do_ui_submit_sequence_with_page(
                        page,
                        start_url,
                        inputs,
                        submit_selector,
                        b,
                        human_behavior=hb,
                        progress_tracker=tracker,
                        run_label=rl,
                        **ui_kwargs,
                    )

                try:
                    batch_results = await run_with_evasion_retry(
                        lambda f=_cb, fet=fetcher: fet.with_page(f, storage_path=storage_str)
                    )
                except PageBlockedError as exc:
                    print(f"[!] {exc}", flush=True)
                    raise
                except NonSuccessResponseError:
                    batch_results = None
                if batch_results is not None and all(resp for _, resp in batch_results):
                    break
            if batch_results is not None:
                all_results.extend(batch_results)
            else:
                all_results.extend((t, None) for t in batch)

    log_path = (
        _write_run_log(site, component, all_results, multi_batches=batches)
        if all_results
        else None
    )
    tracker.emit_run_done()
    return all_results, log_path
