"""Bootstrap Moon Sisters through Playwright, then hand off to HTTP collector."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


def play_endpoint(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["gsc"] = ["play"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))


async def capture(url: str, headed: bool, timeout: float) -> tuple[str, str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise SystemExit(
            "Bootstrap needs Python Playwright: python -m pip install playwright "
            "then python -m playwright install chromium"
        ) from error

    token: str | None = None
    observed_url: str | None = None
    executable = os.environ.get("MOON_CHROME_PATH")
    if not executable:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        executable = next((item for item in candidates if Path(item).exists()), None)

    async with async_playwright() as playwright:
        launch = {"headless": not headed}
        if executable:
            launch["executable_path"] = executable
        browser = await playwright.chromium.launch(**launch)
        page = await browser.new_page()

        async def on_request(request) -> None:
            nonlocal token, observed_url
            if "gsc=login" not in request.url:
                return
            observed_url = play_endpoint(request.url)
            try:
                payload = json.loads(request.post_data or "{}")
            except json.JSONDecodeError:
                return
            value = payload.get("token")
            if isinstance(value, str) and value:
                token = value

        page.on("request", on_request)
        await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        deadline = asyncio.get_running_loop().time() + timeout
        while token is None and asyncio.get_running_loop().time() < deadline:
            await page.wait_for_timeout(250)
        await browser.close()

    if not token or not observed_url:
        raise RuntimeError("Could not capture token from the game's login request.")
    return observed_url, token


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Moon Sisters via browser, then collect over HTTP.")
    parser.add_argument("--launch-url", required=True, help="Game launch URL opened by Playwright")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--run-name", default="moon-bootstrap-smoke")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=float, default=45.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url, token = asyncio.run(capture(args.launch_url, args.headed, args.timeout))
    collector = Path(__file__).with_name("collect_moon_http.py")
    command = [
        sys.executable, str(collector), "--url", url, "--token", token,
        "--workers", str(args.workers), "--rounds", str(args.rounds),
        "--delay", str(args.delay), "--progress-every", str(args.progress_every),
        "--run-name", args.run_name,
    ]
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
