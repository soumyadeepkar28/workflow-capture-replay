from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

from smoke_capture import (
    API_ORIGIN,
    EXTENSION_ID,
    PROJECT_ROOT,
    TARGET_ORIGIN,
    PRODUCT_PYTHONPATH,
    extension_worker,
    start_api,
    start_target,
    target_command,
    wait_for_capture,
    wait_for_http,
)

import sys

sys.path[:0] = PRODUCT_PYTHONPATH.split(":")

from workflow_companion import CompanionClient, CompanionStore, ReplayExecutor  # noqa: E402
from workflow_target_state import issue_session_ticket  # noqa: E402
from workflow_target_state.config import LocalTargetPaths  # noqa: E402


async def run_smoke(*, headed: bool) -> None:
    paths = LocalTargetPaths.from_project_root(PROJECT_ROOT)
    extension = PROJECT_ROOT / "apps" / "extension" / "dist"
    artifacts = PROJECT_ROOT / ".local" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    if not (extension / "manifest.json").is_file():
        raise RuntimeError("extension build is missing")
    if not (PROJECT_ROOT / "apps" / "portal" / "dist" / "index.html").is_file():
        raise RuntimeError("portal build is missing")

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise RuntimeError("port 8000 is already in use")

    target_command("reset", quiet=True)
    target_server = None
    api_server = None
    try:
        with tempfile.TemporaryDirectory(prefix="workflow-general-") as temporary_directory:
            temporary = Path(temporary_directory)
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
            pairing = await companion.initiate_pairing("General capture smoke", "0.1.0")
            session_ticket = issue_session_ticket(paths.control_database, "capture")

            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(temporary / "browser-profile"),
                    headless=not headed,
                    args=[
                        f"--disable-extensions-except={extension}",
                        f"--load-extension={extension}",
                    ],
                    viewport={"width": 1440, "height": 950},
                )
                try:
                    worker = await extension_worker(context)
                    if worker.url.split("/")[2] != EXTENSION_ID:
                        raise AssertionError("general smoke loaded the wrong extension")
                    target_page = context.pages[0] if context.pages else await context.new_page()
                    await target_page.goto(
                        f"{TARGET_ORIGIN}/_workflow/session/#ticket={session_ticket}"
                    )
                    await target_page.wait_for_url("**/tickets/1/")

                    portal_page = await context.new_page()
                    await portal_page.goto(pairing.pairing_url)
                    await portal_page.get_by_role("button", name="Connect runner").click()
                    await expect(
                        portal_page.get_by_role("heading", name="Runner connected")
                    ).to_be_visible()
                    if await companion.refresh_pairing() != "connected":
                        raise AssertionError("companion did not reconcile browser pairing")
                    await companion.heartbeat("ready", "0.1.0")
                    await portal_page.get_by_role("link", name="Open workflow library").click()
                    await portal_page.get_by_role("button", name="Capture workflow").click()
                    await expect(portal_page.get_by_text("Recording Helpdesk workflow")).to_be_visible()
                    await wait_for_capture(
                        worker,
                        lambda state: state["nextSequence"] >= 1,
                        "capture startup",
                    )

                    title = "Printer access request"
                    await target_page.bring_to_front()
                    await target_page.locator('a[href="/tickets/submit/"]').click()
                    await target_page.wait_for_url("**/tickets/submit/")
                    await target_page.get_by_label(
                        re.compile(r"^\s*Summary\s+of\s+the\s+problem\s*$")
                    ).fill(title)
                    await target_page.get_by_label(
                        re.compile(r"^\s*Description\s+of\s+your\s+issue\s*$")
                    ).fill(
                        "Grant printer access for the fictional demo requester."
                    )
                    await target_page.get_by_role(
                        "button", name=re.compile("Submit Ticket")
                    ).click()
                    await target_page.wait_for_url(re.compile(r"/tickets/\d+/$"))
                    await target_page.locator('a[href="/tickets/"]').first.click()
                    await target_page.wait_for_url("**/tickets/")
                    await target_page.get_by_role("textbox", name="Search", exact=True).fill(title)
                    await target_page.get_by_role("button", name=re.compile(r"\bGo\b")).click()
                    await target_page.wait_for_url(re.compile(r"/tickets/\?.*q="))
                    await expect(
                        target_page.get_by_role(
                            "link",
                            name=re.compile(rf"^\d+\.\s+{re.escape(title)}$"),
                        )
                    ).to_be_visible(timeout=10_000)
                    await wait_for_capture(
                        worker,
                        lambda state: "q=Printer" in state.get("lastPage", {}).get("query", ""),
                        "searched ticket-list document",
                    )

                    await portal_page.bring_to_front()
                    await portal_page.get_by_role("button", name="Stop capture").click()
                    await expect(
                        portal_page.get_by_role("heading", name="Review recorded actions")
                    ).to_be_visible(timeout=15_000)
                    await portal_page.get_by_label("Workflow category").select_option(
                        label="Create new category…"
                    )
                    await portal_page.get_by_label("New category name").fill("Ticket intake")
                    await expect(portal_page.get_by_text("Execution evidence only")).to_be_visible()
                    await expect(
                        portal_page.get_by_text("New category · Unverified")
                    ).to_be_visible()
                    await portal_page.set_viewport_size({"width": 390, "height": 844})
                    review_dimensions = await portal_page.evaluate(
                        "() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth })"
                    )
                    if review_dimensions["width"] > review_dimensions["viewport"]:
                        raise AssertionError(
                            f"mobile category review overflows horizontally: {review_dimensions}"
                        )
                    await portal_page.screenshot(
                        path=artifacts / "general-category-review-mobile.png",
                        full_page=True,
                    )
                    await portal_page.set_viewport_size({"width": 1440, "height": 950})
                    workflow_name = "Create and find a Helpdesk ticket"
                    await portal_page.get_by_label("Workflow name").fill(workflow_name)
                    await portal_page.get_by_role("button", name="Save workflow").click()
                    await expect(
                        portal_page.get_by_role("heading", name=workflow_name)
                    ).to_be_visible()

                    async with httpx.AsyncClient(base_url=API_ORIGIN) as client:
                        workflow_summary = (await client.get("/api/workflows")).json()["workflows"][0]
                        categories = (await client.get("/api/workflow-categories")).json()["categories"]
                        workflow = (
                            await client.get(
                                f"/api/workflows/{workflow_summary['workflow_id']}"
                            )
                        ).json()["workflow"]
                    assert workflow["workflow_mode"] == "general"
                    assert workflow["category"] == "Ticket intake"
                    assert workflow["verification_profile"] is None
                    assert any(
                        category["category_id"] == workflow["category_id"]
                        and category["name"] == "Ticket intake"
                        and category["verification_profile"] is None
                        for category in categories
                    )
                    serialized = json.dumps(workflow).lower()
                    if "csrf" in serialized or "password" in serialized:
                        raise AssertionError("general workflow persisted sensitive browser data")
                    assert any(
                        action["kind"] == "navigate"
                        and action["destination"].get("query", "").find("q=Printer") >= 0
                        for action in workflow["actions"]
                    )

                    await target_page.close()
                    await companion.heartbeat("ready", "0.1.0")
                    await portal_page.get_by_role("button", name="Replay").click()
                    snapshot = await companion.claim_run()
                    if snapshot is None:
                        raise AssertionError("companion did not claim general replay")
                    outcome = await ReplayExecutor(
                        companion,
                        PROJECT_ROOT,
                        headless=not headed,
                        step_delay_seconds=0,
                        final_hold_seconds=0,
                    ).execute(snapshot)
                    if outcome is not None:
                        raise AssertionError(f"general replay invented a business outcome: {outcome}")

                    async with httpx.AsyncClient(base_url=API_ORIGIN) as client:
                        run = (await client.get(f"/api/runs/{snapshot.run_id}")).json()
                    (artifacts / "last-general-run-detail.json").write_text(
                        json.dumps(run, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    if run["run"]["phase"] != "finished":
                        failures = [
                            event["event"]
                            for event in run["events"]
                            if event["event"]["kind"] == "action"
                            and event["event"].get("status") in {"failed", "uncertain"}
                        ]
                        raise AssertionError(
                            f"general replay phase was {run['run']['phase']}: "
                            f"{json.dumps(failures, sort_keys=True)}"
                        )
                    assert run["run"]["outcome"] is None
                    assert run["run"]["workflow_mode"] == "general"
                    assert not any(event["event"]["kind"] == "check" for event in run["events"])
                    results = [
                        event["event"]
                        for event in run["events"]
                        if event["event"]["kind"] == "action"
                        and event["event"]["stage"] == "result"
                    ]
                    assert results and all(result["status"] == "completed" for result in results)
                    assert {artifact["kind"] for artifact in run["artifacts"]} == {
                        "baseline",
                        "final",
                    }
                    await companion.heartbeat("ready", "0.1.0")
                    await expect(
                        portal_page.get_by_text("finished · unverified", exact=True)
                    ).to_be_visible(timeout=12_000)
                    await portal_page.get_by_role(
                        "button", name=f"Inspect {workflow_name} run"
                    ).click()
                    await expect(
                        portal_page.get_by_role("heading", name="Business verification")
                    ).to_be_visible()
                    await portal_page.screenshot(
                        path=artifacts / "general-run-detail.png",
                        full_page=True,
                    )
                finally:
                    await context.close()

            print(
                "PASS: general Helpdesk capture created and searched for a ticket; "
                "replay finished with execution evidence and no business outcome claim."
            )
    finally:
        if api_server is not None and api_server.poll() is None:
            api_server.terminate()
            api_server.wait(timeout=5)
        if target_server is not None and target_server.poll() is None:
            target_command("stop", quiet=True)
            target_server.wait(timeout=5)
        target_command("reset", quiet=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    asyncio.run(run_smoke(headed=args.headed))


if __name__ == "__main__":
    main()