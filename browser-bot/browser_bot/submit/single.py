"""Single-string UI submission: one prompt per page/session."""

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from browser_bot.browser.human_behavior import human_mouse_wander
from browser_bot.config import EVASION_REQUEST_DELAY_S, FETCH_METHOD, get_posts_strings
from browser_bot.sites import get_storage_state_path, get_submission_config

from browser_bot.live_preview import emit_preview_layout, live_preview_context
from browser_bot.page_blockers import PageBlockedError, ensure_page_ready_for_submit
from browser_bot.submit.common import (
    NonSuccessResponseError,
    SubmissionProgressTracker,
    _do_one_submit_step,
    _write_run_log,
    append_test_prompt_delimiter,
    log_evasion,
    parallel_fetchers_for_ui,
    run_with_evasion_retry,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


async def do_ui_submit_with_page(
    page: "Page",
    start_url: str,
    inputs: list[dict],
    submit_selector: str,
    text: str,
    *,
    response_selector: str = "",
    response_within_selector: str = "",
    response_text_within_selector: str = "",
    submit_via: str = "click",
    response_wait_ms: int = 5000,
    human_behavior: bool = False,
    site: str = "",
    component: str = "",
    blockers: list[dict] | None = None,
    preview_slot: int = 0,
) -> tuple[str, str | None]:
    """Run a single UI submission with the given page. Returns (text, response_text)."""
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
        )
        if human_behavior:
            await human_mouse_wander(page, count=1)

        text_out, response_out, _ = await _do_one_submit_step(
            page,
            inputs,
            submit_selector,
            text,
            response_selector=response_selector,
            response_within_selector=response_within_selector,
            response_text_within_selector=response_text_within_selector,
            submit_via=submit_via,
            response_wait_ms=response_wait_ms,
        )
        return (text_out, response_out)


async def run_ui_submission_single(
    site: str,
    component: str,
    *,
    pool_fetcher=None,
    cluster_fetcher=None,
    human_fetcher=None,
    suite_path=None,
) -> tuple[list[tuple[str, str | None]], Path | None]:
    """
    Run UI submission for each string in posts.json (flat list).
    Uses fetchers in order (Pool → Cluster → Human). Returns (list of (input_string, response_text) tuples, log_path or None).
    """
    sub = get_submission_config(site, component)
    if not sub:
        return [], None

    posts = get_posts_strings(suite_path=suite_path)
    if not posts:
        return [], None
    posts = [append_test_prompt_delimiter(p) for p in posts]

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

    results: list[tuple[str, str | None]] = []
    storage_str = str(storage_path)

    tracker = SubmissionProgressTracker("single", len(posts))
    tracker.emit_run_start()

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
    if len(posts) > 1:
        parallel_fetchers = parallel_fetchers_for_ui(method, pool_fetcher, cluster_fetcher)

    if parallel_fetchers:
        emit_preview_layout(len(posts))

        async def _run_one_with_human(text: str, *, preview_slot: int = 0):
            if human_fetcher is None:
                return None

            async def _cb(page, t=text, ps=preview_slot):
                return await do_ui_submit_with_page(
                    page,
                    start_url,
                    inputs,
                    submit_selector,
                    t,
                    human_behavior=True,
                    preview_slot=ps,
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

        async def _run_one(text: str, fetcher, *, record_progress: bool = False, preview_slot: int = 0):
            async def _cb(page, t=text, ps=preview_slot):
                return await do_ui_submit_with_page(
                    page,
                    start_url,
                    inputs,
                    submit_selector,
                    t,
                    human_behavior=False,
                    preview_slot=ps,
                    **ui_kwargs,
                )
            try:
                return await run_with_evasion_retry(
                    lambda f=_cb, fet=fetcher: fet.with_page(f, storage_path=storage_str)
                )
            except PageBlockedError:
                raise
            finally:
                if record_progress:
                    tracker.record_completed(1)

        try:
            parallel_results = await asyncio.gather(
                *[
                    _run_one(text, parallel_fetchers[0], record_progress=True, preview_slot=i)
                    for i, text in enumerate(posts)
                ],
                return_exceptions=True,
            )
        except PageBlockedError:
            raise

        async def _retry_fast_then_human(text: str, *, preview_slot: int = 0):
            for fetcher in parallel_fetchers[1:]:
                try:
                    retry_result = await _run_one(text, fetcher, preview_slot=preview_slot)
                except PageBlockedError:
                    raise
                except Exception:
                    retry_result = None
                if retry_result and retry_result[1]:
                    return retry_result
            return await _run_one_with_human(text, preview_slot=preview_slot)

        for i, (text, r) in enumerate(zip(posts, parallel_results)):
            if isinstance(r, PageBlockedError):
                print(f"[!] {r}", flush=True)
                raise r
            if isinstance(r, Exception):
                fallback = await _retry_fast_then_human(text, preview_slot=i)
                results.append(fallback if fallback and fallback[1] else (text, None))
            elif r is not None:
                if r[1]:
                    results.append(r)
                else:
                    fallback = await _retry_fast_then_human(text, preview_slot=i)
                    results.append(fallback if fallback and fallback[1] else r)
            else:
                fallback = await _retry_fast_then_human(text, preview_slot=i)
                results.append(fallback if fallback and fallback[1] else (text, None))
    else:
        for i, text in enumerate(posts):
            if i > 0:
                log_evasion(
                    "sequential_burst_pause",
                    sleep_s=EVASION_REQUEST_DELAY_S,
                    detail="Pause between sequential prompts to reduce burst-rate detection",
                )
                await asyncio.sleep(EVASION_REQUEST_DELAY_S)
            result = None
            for fetcher, human_behavior in fetchers_to_try:
                async def _cb(page, t=text, hb=human_behavior):
                    return await do_ui_submit_with_page(
                        page,
                        start_url,
                        inputs,
                        submit_selector,
                        t,
                        human_behavior=hb,
                        **ui_kwargs,
                    )

                try:
                    result = await run_with_evasion_retry(
                        lambda f=_cb, fet=fetcher: fet.with_page(f, storage_path=storage_str)
                    )
                except PageBlockedError as exc:
                    print(f"[!] {exc}", flush=True)
                    raise
                except NonSuccessResponseError:
                    result = None
                if result is not None and result[1]:
                    break
            results.append(result if result is not None else (text, None))
            tracker.record_completed(1)

    tracker.emit_run_done()
    log_path = _write_run_log(site, component, results) if results else None
    return results, log_path
