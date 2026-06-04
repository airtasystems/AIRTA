#!/usr/bin/env python3
"""Bootstrap venv, install dependencies, and launch the AIRTA web UI."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "airta-venv"
REQUIREMENTS = ROOT / "requirements.txt"
WEB_APP = ROOT / "web" / "app.py"
PLAYWRIGHT_MARKER = ROOT / ".playwright-chromium-installed"
PLAYWRIGHT_HOST_PLATFORM_FALLBACKS = {
    "26.": "ubuntu24.04-x64",
}


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> bool:
    """Create the virtual environment if missing. Returns True when newly created."""
    if VENV_DIR.exists():
        return False
    print(f"Creating virtual environment at {VENV_DIR} ...")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    return True


def install_requirements(python: Path) -> None:
    if not REQUIREMENTS.is_file():
        raise SystemExit(f"Missing requirements file: {REQUIREMENTS}")
    print("Installing requirements ...")
    subprocess.check_call([str(python), "-m", "pip", "install", "-U", "pip"])
    subprocess.check_call([str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)])


def _ubuntu_version_id() -> str | None:
    if sys.platform != "linux":
        return None
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION_ID="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def playwright_subprocess_env() -> dict[str, str]:
    """Env for `python -m playwright` with unsupported OS fallbacks applied."""
    env = os.environ.copy()
    if env.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"):
        return env
    version_id = _ubuntu_version_id()
    for prefix, host_platform in PLAYWRIGHT_HOST_PLATFORM_FALLBACKS.items():
        if version_id and version_id.startswith(prefix):
            env["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = host_platform
            break
    return env


def apply_playwright_runtime_env() -> None:
    """Make browser install and runtime resolve the same Playwright browser build."""
    env = playwright_subprocess_env()
    override = env.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE")
    if override:
        os.environ["PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"] = override


def playwright_browsers_installed() -> bool:
    return PLAYWRIGHT_MARKER.is_file()


def install_playwright_browsers(python: Path) -> None:
    env = playwright_subprocess_env()
    if env.get("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE"):
        print(
            "Note: Playwright has no ubuntu26.04 build yet; "
            f"using {env['PLAYWRIGHT_HOST_PLATFORM_OVERRIDE']} browser binaries."
        )
    print("Installing Playwright Chromium browser ...")
    subprocess.check_call(
        [str(python), "-m", "playwright", "install", "chromium"],
        env=env,
    )
    PLAYWRIGHT_MARKER.touch()
    version_id = _ubuntu_version_id()
    if version_id and version_id.startswith("26."):
        print(
            "Ubuntu 26.04: if browser automation fails with missing .so libraries, run:\n"
            f"  PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 "
            f"{python} -m playwright install-deps chromium"
        )


def launch_ui(python: Path) -> None:
    os.chdir(ROOT)
    os.execv(str(python), [str(python), str(WEB_APP)])


def main() -> None:
    apply_playwright_runtime_env()
    created = ensure_venv()
    python = venv_python()
    if not python.is_file():
        raise SystemExit(f"Virtual environment python not found: {python}")

    install_requirements(python)
    if created or not playwright_browsers_installed():
        install_playwright_browsers(python)

    print("Starting AIRTA web UI ...")
    launch_ui(python)


if __name__ == "__main__":
    main()
