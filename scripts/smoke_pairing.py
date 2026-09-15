from __future__ import annotations

import argparse
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(PROJECT_ROOT / "apps" / "companion" / "src"),
    str(PROJECT_ROOT / "packages" / "protocol" / "src"),
]

from workflow_companion import CompanionClient, CompanionStore  # noqa: E402


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def wait_for_api(origin: str) -> None:
    deadline = time.monotonic() + 10
    async with httpx.AsyncClient(base_url=origin, timeout=1) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get("/api/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError("temporary API did not become ready")


async def run_smoke(*, headed: bool) -> None:
    port = available_port()
    origin = f"http://127.0.0.1:{port}"
    artifacts = PROJECT_ROOT / ".local" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="workflow-pairing-") as temporary_directory:
        temporary = Path(temporary_directory)
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(PROJECT_ROOT / "apps" / "api" / "src"),
                        str(PROJECT_ROOT / "apps" / "companion" / "src"),
                        str(PROJECT_ROOT / "packages" / "protocol" / "src"),
                        str(PROJECT_ROOT / "packages" / "target-state" / "src"),
                    ]
                ),
                "WORKFLOW_API_DATABASE": str(temporary / "api.sqlite3"),
                "WORKFLOW_PUBLIC_ORIGIN": origin,
            }
        )
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "workflow_api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await wait_for_api(origin)
            store = CompanionStore(temporary / "companion.sqlite3")
            companion = CompanionClient(origin, store)
            launch = await companion.initiate_pairing("Browser smoke runner", "0.1.0")
            pending = store.pending()
            if pending is None:
                raise RuntimeError("companion did not persist pending pairing")

            requested_urls: list[str] = []
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=not headed)
                context = await browser.new_context(viewport={"width": 1440, "height": 900})
                page = await context.new_page()
                page.on("request", lambda request: requested_urls.append(request.url))

                await page.goto(launch.pairing_url)
                await expect(page).to_have_title("Workflow Replay")
                await expect(
                    page.get_by_role("heading", name="Connect replay runner")
                ).to_be_visible()
                if page.url != f"{origin}/pair":
                    raise AssertionError("pairing fragment was not removed from browser history")
                if await companion.refresh_pairing() != "pending":
                    raise AssertionError("page load unexpectedly granted runner authority")

                await page.get_by_role("button", name="Connect runner").click()
                await expect(page.get_by_role("heading", name="Runner connected")).to_be_visible()
                if await companion.refresh_pairing() != "connected":
                    raise AssertionError("companion did not observe pairing completion")
                await companion.heartbeat("ready", "0.1.0")

                await page.get_by_role("link", name="Open workflow library").click()
                await expect(page.get_by_text("Ready", exact=True)).to_be_visible()
                await expect(page.get_by_role("button", name="Capture workflow")).to_be_enabled()
                await page.screenshot(path=artifacts / "portal-paired-desktop.png", full_page=True)

                await page.set_viewport_size({"width": 390, "height": 844})
                await page.reload()
                await expect(page.get_by_role("heading", name="Workflow library")).to_be_visible()
                dimensions = await page.evaluate(
                    "() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth })"
                )
                if dimensions["width"] > dimensions["viewport"]:
                    raise AssertionError(f"mobile portal overflows horizontally: {dimensions}")
                await page.screenshot(path=artifacts / "portal-paired-mobile.png", full_page=True)
                await browser.close()

            if any(pending.pending_proof in url or pending.runner_credential in url for url in requested_urls):
                raise AssertionError("companion authority appeared in a browser request URL")
            print("PASS: explicit browser pairing, reconciliation, readiness, and responsive portal.")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_smoke(headed=args.headed))


if __name__ == "__main__":
    main()