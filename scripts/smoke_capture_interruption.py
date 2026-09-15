from __future__ import annotations

import asyncio
import socket
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

from smoke_capture import (
    API_ORIGIN,
    PROJECT_ROOT,
    TARGET_ORIGIN,
    CompanionClient,
    CompanionStore,
    LocalTargetPaths,
    extension_worker,
    issue_session_ticket,
    start_api,
    start_target,
    target_command,
    wait_for_capture,
    wait_for_http,
)


async def run_smoke() -> None:
    paths = LocalTargetPaths.from_project_root(PROJECT_ROOT)
    extension = PROJECT_ROOT / "apps" / "extension" / "dist"
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise RuntimeError("port 8000 is already in use")

    target_command("reset", quiet=True)
    target_server = None
    api_server = None
    try:
        with tempfile.TemporaryDirectory(prefix="workflow-interruption-") as directory:
            temporary = Path(directory)
            target_server = start_target()
            api_server = start_api(temporary / "api.sqlite3")
            await asyncio.gather(
                wait_for_http(f"{TARGET_ORIGIN}/_workflow/session/"),
                wait_for_http(f"{API_ORIGIN}/api/health"),
            )
            companion = CompanionClient(
                API_ORIGIN,
                CompanionStore(temporary / "companion.sqlite3"),
            )
            pairing = await companion.initiate_pairing("Interruption smoke runner", "0.1.0")
            session_ticket = issue_session_ticket(paths.control_database, "capture")

            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(temporary / "browser-profile"),
                    headless=False,
                    args=[
                        f"--disable-extensions-except={extension}",
                        f"--load-extension={extension}",
                    ],
                    viewport={"width": 1280, "height": 850},
                )
                try:
                    worker = await extension_worker(context)
                    target_page = context.pages[0] if context.pages else await context.new_page()
                    await target_page.goto(
                        f"{TARGET_ORIGIN}/_workflow/session/#ticket={session_ticket}"
                    )
                    await target_page.wait_for_url("**/tickets/1/")
                    portal_page = await context.new_page()
                    await portal_page.goto(pairing.pairing_url)
                    await portal_page.get_by_role("button", name="Connect runner").click()
                    if await companion.refresh_pairing() != "connected":
                        raise AssertionError("companion did not connect")
                    await companion.heartbeat("ready", "0.1.0")
                    await portal_page.get_by_role("link", name="Open workflow library").click()
                    await portal_page.get_by_role("button", name="Capture workflow").click()
                    active = await wait_for_capture(
                        worker,
                        lambda state: state["nextSequence"] >= 1,
                        "active initial capture",
                    )
                    draft_id = active["draftId"]

                    await target_page.goto(f"{TARGET_ORIGIN}/dashboard/")
                    await wait_for_capture(
                        worker,
                        lambda state: bool(state.get("captureError")),
                        "durable capture interruption",
                    )
                    await portal_page.bring_to_front()
                    await portal_page.get_by_role("button", name="Stop capture").click()
                    await expect(
                        portal_page.get_by_role("alert")
                    ).to_contain_text("Capture interrupted:")
                    await expect(
                        portal_page.get_by_text("Recording Helpdesk tab")
                    ).to_have_count(0)

                    review = await portal_page.evaluate(
                        """async (draftId) => {
                          const response = await fetch(`/api/captures/${encodeURIComponent(draftId)}`);
                          return await response.json();
                        }""",
                        draft_id,
                    )
                    if review["status"] != "interrupted" or "left the supported" not in review["error_message"]:
                        raise AssertionError(f"central interruption was not inspectable: {review}")
                    async with httpx.AsyncClient(base_url=API_ORIGIN) as client:
                        workflows = (await client.get("/api/workflows")).json()["workflows"]
                    if workflows:
                        raise AssertionError("interrupted capture created a workflow")
                finally:
                    await context.close()
        print("PASS: out-of-scope navigation produced an inspectable non-replayable draft.")
    finally:
        if api_server is not None and api_server.poll() is None:
            api_server.terminate()
            api_server.wait(timeout=5)
        if target_server is not None and target_server.poll() is None:
            target_command("stop", quiet=True)
            target_server.wait(timeout=5)
        target_command("reset", quiet=True)


if __name__ == "__main__":
    asyncio.run(run_smoke())