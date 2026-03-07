"""
Playwright trace recording: capture the entire LLM interaction flow.
Extracts contents to output_dir/playwright/ for programmatic access.
The .zip is deleted after extraction.
Image and CSS files in resources/ are removed to reduce size.
"""
import zipfile
from pathlib import Path

from ..heuristics import IMAGE_EXTENSIONS

TRACE_FILENAME = "playwright_trace.zip"
PLAYWRIGHT_EXTRACT_DIR = "playwright"
RESOURCES_DIR = "resources"


async def start_playwright_trace(context, *, screenshots: bool = True, snapshots: bool = True, sources: bool = True) -> None:
    """
    Start Playwright trace recording on the given browser context.
    Captures screenshots, DOM snapshots, network activity, and source locations.
    """
    await context.tracing.start(
        screenshots=screenshots,
        snapshots=snapshots,
        sources=sources,
        title="LLM interaction flow",
    )


async def stop_playwright_trace(context, output_dir: Path) -> Path:
    """
    Stop Playwright trace recording and save to output_dir/playwright_trace.zip.
    Extracts contents to output_dir/playwright/ for programmatic access.
    Returns path to the trace zip file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / TRACE_FILENAME
    await context.tracing.stop(path=str(trace_path))

    extract_dir = output_dir / PLAYWRIGHT_EXTRACT_DIR
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(trace_path, "r") as zf:
        zf.extractall(extract_dir)

    trace_path.unlink(missing_ok=True)

    _clean_resources(extract_dir / RESOURCES_DIR)

    return extract_dir


# Additional extensions to remove from resources (beyond IMAGE_EXTENSIONS)
_RESOURCES_EXCLUDE = (".css", ".gif", ".woff2")


def _clean_resources(resources_dir: Path) -> None:
    """Remove image, CSS, font, and similar files from format/playwright/resources."""
    if not resources_dir.is_dir():
        return
    for f in resources_dir.iterdir():
        if not f.is_file():
            continue
        suffix = f.suffix.lower()
        if suffix in IMAGE_EXTENSIONS or suffix in _RESOURCES_EXCLUDE:
            f.unlink(missing_ok=True)
