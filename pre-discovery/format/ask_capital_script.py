#!/usr/bin/env python3
"""Playwright script to ask questions via chat UI (generated from format guide)."""
import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow importing pipeline.evasion from project root
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from playwright.async_api import async_playwright

APP_URL = 'https://deepai.org/chat'
DEFAULT_PROMPT = 'what is the capital of america'
DEFAULT_PROMPTS_PATH = Path(__file__).resolve().parent.parent / "prompts" / "prompts.json"

INPUT_SELECTORS = [
            '#persistentChatbox',
            'textarea[placeholder*="Message" i]',
            'textarea[placeholder*="message" i], textarea[placeholder*="chat" i], textarea[placeholder*="ask" i]',
            'textarea[aria-label*="message" i], textarea[aria-label*="chat" i]',
            '[contenteditable="true"][role="textbox"]',
            'input[type="text"][placeholder*="message" i], input[type="text"][placeholder*="ask" i]',
            'textarea',
        ]

RESPONSE_SELECTORS = [
            '.chat-message',
            "[class*='message']",
            "[class*='response']",
            "[class*='assistant']",
            "[class*='completion']",
            "[class*='answer']",
            "[class*='output']",
            "[data-role='assistant']",
            "[role='article']",
            '.bot-message',
            'article',
        ]


def _extract_response_after_prompt(body_text: str, current_prompt: str, prompts: list[str], current_index: int) -> str:
    """Extract response after current_prompt using generic boundaries (next prompt, ©, Copyright). No site-specific values."""
    if not body_text or not current_prompt:
        return body_text
    body_lower = body_text.lower()
    prompt_lower = current_prompt.lower()
    idx = body_lower.find(prompt_lower)
    if idx < 0:
        return body_text
    start = idx + len(current_prompt)
    after = body_text[start:].strip()
    if not after:
        return body_text
    end_pos = len(after)
    if current_index + 1 < len(prompts):
        next_prompt = prompts[current_index + 1]
        next_idx = after.lower().find(next_prompt.lower())
        if 0 <= next_idx < end_pos:
            end_pos = next_idx
    for marker in ("©", "copyright", "all rights reserved"):
        mi = after.lower().find(marker)
        if 0 <= mi < end_pos:
            end_pos = mi
    after = after[:end_pos].strip()
    blocks = [b.strip() for b in after.split("\n\n") if b.strip()]
    result_parts = []
    for b in blocks:
        if len(b) < 25 and not any(c in b for c in ".!?"):
            break
        result_parts.append(b)
    out = "\n\n".join(result_parts) if result_parts else after
    return out.strip() or body_text


def _load_prompts(prompts_path: Path | None) -> list[str]:
    """Load prompts from JSON file. Expects array of objects with 'prompt' key, or array of strings."""
    path = prompts_path or DEFAULT_PROMPTS_PATH
    if not path.exists():
        return [DEFAULT_PROMPT]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return [DEFAULT_PROMPT]
        out = []
        for item in data:
            if isinstance(item, dict) and "prompt" in item:
                out.append(str(item["prompt"]))
            elif isinstance(item, str):
                out.append(item)
        return out if out else [DEFAULT_PROMPT]
    except (json.JSONDecodeError, OSError):
        return [DEFAULT_PROMPT]


async def main(*, prompts: list[str], log_path: Path | None = None, headless: bool = True, stealth: bool = True) -> None:

    try:
        from pipeline import evasion
        _evasion = evasion if stealth else None
    except ImportError:
        _evasion = None

    print(f"[*] {len(prompts)} prompt(s) to run", flush=True)
    log_file = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
        print(f"[*] Log file: {log_path}", flush=True)
    print("[*] Launching browser...", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        if _evasion:
            print("[*] Applying stealth...", flush=True)
            await _evasion.apply_stealth(page)

        print("[*] Navigating to app...", flush=True)
        await page.goto(APP_URL, wait_until="domcontentloaded", timeout=30000)
        print("[*] Page loaded, waiting for content...", flush=True)
        await page.wait_for_timeout(5000)

        # Dismiss consent banner
        print("[*] Checking for consent banner...", flush=True)
        consent_selectors = [
            '#cmpwrapper button:has-text("Accept")',
            '#cmpwrapper button:has-text("Allow")',
            '#cmpwrapper button:has-text("Agree")',
            '#cmpbox button:has-text("Accept")',
            'button:has-text("Accept all")',
            'button:has-text("Accept")',
            'button:has-text("Allow all")',
            '[aria-label*="accept" i]',
        ]
        for sel in consent_selectors:
            try:
                loc = page.locator(sel)
                n = await asyncio.wait_for(loc.count(), timeout=2)
                if n > 0:
                    await loc.first.click(timeout=2000)
                    await page.wait_for_timeout(1500)
                    print("[*] Consent dismissed.", flush=True)
                    break
            except (asyncio.TimeoutError, Exception):
                continue

        print("[*] Looking for chat input...", flush=True)
        chat_input = None
        for sel in INPUT_SELECTORS:
            try:
                chat_input = await page.wait_for_selector(sel, timeout=5000, state="visible")
                if chat_input:
                    print(f"[*] Found input.", flush=True)
                    break
            except Exception:
                continue

        if not chat_input:
            print("Could not find chat input field.")
            debug_path = Path(__file__).parent / "ask_capital_debug.png"
            try:
                await page.screenshot(path=str(debug_path))
                print(f"Debug screenshot saved to {debug_path}")
            except Exception:
                pass
            body = await page.evaluate("() => document.body?.innerText ?? ''")
            if any(x in body.lower() for x in ["cloudflare", "verify you are human", "security verification", "performing security"]):
                print("")
                print("Cloudflare or similar bot protection detected. Headless browsers are often blocked.")
                print("Try running with --no-headless:  python script.py --no-headless")
                print("")
            await browser.close()
            return

        for i, current_prompt in enumerate(prompts):
            print(f"\n--- Prompt {i+1}/{len(prompts)}: {current_prompt[:60]}{'...' if len(current_prompt) > 60 else ''} ---", flush=True)
            await chat_input.fill(current_prompt)
            await chat_input.press("Enter")

            print("[*] Waiting for response (6s)...", flush=True)
            await page.wait_for_timeout(6000)
            print("[*] Extracting response...", flush=True)

            seen: set[str] = set()
            response_parts: list[str] = []
            for sel in RESPONSE_SELECTORS:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        txt = (await el.inner_text()).strip()
                        if not txt or txt in seen:
                            continue
                        if current_prompt.lower() not in txt.lower() and len(txt) > 10:
                            seen.add(txt)
                            response_parts.append(txt)
                except Exception:
                    continue
                if response_parts:
                    break

            if response_parts:
                full_response = "\n\n".join(response_parts)
                print("LLM Response:")
                print(full_response)
            else:
                body_text = await page.evaluate("() => document.body.innerText")
                full_response = _extract_response_after_prompt(body_text, current_prompt, prompts, i)
                if full_response != body_text:
                    print("LLM Response (prompt-anchored fallback):")
                else:
                    print("Fallback (full page text):")
                print(full_response)

            if log_file:
                log_file.write(f"\n=== Prompt {i+1}/{len(prompts)}: {current_prompt}\n")
                log_file.write(full_response)
                log_file.write("\n\n")
                log_file.flush()

            if i < len(prompts) - 1:
                await page.wait_for_timeout(2000)
                chat_input = None
                for sel in INPUT_SELECTORS:
                    try:
                        chat_input = await page.wait_for_selector(sel, timeout=5000, state="visible")
                        if chat_input:
                            break
                    except Exception:
                        continue
                if not chat_input:
                    print("[!] Could not find input for next prompt. Stopping.")
                    break

        await browser.close()
    if log_file:
        log_file.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ask questions via chat UI. Loads prompts from prompts.json by default."
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=None,
        help="Path to prompts JSON file (default: pre-discovery/prompts/prompts.json)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Path to log file for responses (default: format/chat_responses_YYYY-MM-DDTHH-MM-SS.log)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser visible. Use when Cloudflare/bot protection blocks headless.",
    )
    parser.add_argument("--no-stealth", action="store_true", help="Skip playwright-stealth")
    args = parser.parse_args()
    prompts = _load_prompts(args.prompts)
    fmt_dir = Path(__file__).resolve().parent
    log_path = args.log or (fmt_dir / f"chat_responses_{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')}.log")
    asyncio.run(main(prompts=prompts, log_path=log_path, headless=not args.no_headless, stealth=not args.no_stealth))
