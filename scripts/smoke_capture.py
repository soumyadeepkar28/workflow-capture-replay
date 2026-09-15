from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import BrowserContext, Page, Worker, async_playwright, expect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_PYTHONPATH = os.pathsep.join(
    str(PROJECT_ROOT / path)
    for path in (
        "apps/api/src",
        "apps/companion/src",
        "packages/protocol/src",
        "packages/target-state/src",
    )
)
sys.path[:0] = PRODUCT_PYTHONPATH.split(os.pathsep)

from workflow_companion import CompanionClient, CompanionStore, ReplayExecutor  # noqa: E402
from workflow_target_state import issue_session_ticket  # noqa: E402
from workflow_target_state.config import LocalTargetPaths  # noqa: E402

API_ORIGIN = "http://127.0.0.1:8000"
TARGET_ORIGIN = "http://127.0.0.1:8765"
EXTENSION_ID = "ooocecppjnccilieepgobfjnlhhmebdk"


async def wait_for_http(url: str) -> None:
    deadline = time.monotonic() + 15
    async with httpx.AsyncClient(timeout=1) as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(url)).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError(f"service did not become ready: {url}")


def target_command(command: str, *, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "packages" / "target-state" / "src")
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "target.py"), command],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )


def start_target() -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "packages" / "target-state" / "src")
    return subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "target.py"), "serve"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_api(database: Path) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": PRODUCT_PYTHONPATH,
            "WORKFLOW_API_DATABASE": str(database),
                "WORKFLOW_API_ARTIFACTS": str(database.parent / "artifacts"),
            "WORKFLOW_PUBLIC_ORIGIN": API_ORIGIN,
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "workflow_api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--log-level",
            "warning",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def extension_worker(context: BrowserContext) -> Worker:
    workers = context.service_workers
    worker = workers[0] if workers else await context.wait_for_event("serviceworker")
    actual_id = worker.url.split("/")[2]
    if actual_id != EXTENSION_ID:
        raise AssertionError(f"unexpected extension ID: {actual_id}")
    return worker


async def capture_state(worker: Worker) -> dict[str, Any] | None:
    result = await worker.evaluate(
        "async () => (await chrome.storage.local.get('activeCapture')).activeCapture ?? null"
    )
    return result


async def wait_for_capture(
    worker: Worker,
    predicate,
    description: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    last_state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_state = await capture_state(worker)
        if last_state is not None and predicate(last_state):
            return last_state
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"capture state did not reach {description}: "
        f"{json.dumps(last_state, sort_keys=True)}"
    )


async def update_review(page: Page) -> None:
    await page.get_by_role("tab", name="Respond", exact=True).click()
    await page.get_by_label("Comment / Resolution", exact=True).fill("Demo review completed.")
    await page.get_by_role("radio", name="Resolved", exact=True).check()
    await page.get_by_role("button", name="Update This Ticket", exact=True).click()
    await page.wait_for_load_state("networkidle")


async def update_completion(page: Page, *, resolve: bool) -> None:
    await page.locator('select[name="owner"]').filter(visible=True).select_option(
        label="workflow_actor"
    )
    await page.get_by_role("button", name="Save ticket assignment", exact=True).click()
    await page.wait_for_load_state("networkidle")
    await page.get_by_role("tab", name="Respond", exact=True).click()
    await page.get_by_label("Comment / Resolution", exact=True).fill("Demo completion recorded.")
    if resolve:
        await page.get_by_role("radio", name="Resolved", exact=True).check()
    await page.get_by_role("button", name="Update This Ticket", exact=True).click()
    await page.wait_for_load_state("networkidle")


async def run_smoke(*, headed: bool, scenario: str) -> None:
    paths = LocalTargetPaths.from_project_root(PROJECT_ROOT)
    artifacts = PROJECT_ROOT / ".local" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    extension = PROJECT_ROOT / "apps" / "extension" / "dist"
    if not (extension / "manifest.json").is_file():
        raise RuntimeError("extension build is missing")
    if not (PROJECT_ROOT / "apps" / "portal" / "dist" / "index.html").is_file():
        raise RuntimeError("portal build is missing")

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8000)) == 0:
            raise RuntimeError("port 8000 is already in use")

    target_command("reset", quiet=True)
    target_server: subprocess.Popen[str] | None = None
    api_server: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="workflow-capture-") as temporary_directory:
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
            pairing = await companion.initiate_pairing("Capture smoke runner", "0.1.0")
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
                    target_page = context.pages[0] if context.pages else await context.new_page()
                    await target_page.goto(
                        f"{TARGET_ORIGIN}/_workflow/session/#ticket={session_ticket}"
                    )
                    await target_page.wait_for_url("**/tickets/1/")
                    await expect(
                        target_page.get_by_role(
                            "heading",
                            name=re.compile(r"^Review access request #\d+$"),
                        )
                    ).to_be_visible()

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
                    await expect(
                        portal_page.get_by_role("button", name="Capture workflow")
                    ).to_be_enabled()

                    await portal_page.get_by_role("button", name="Capture workflow").click()
                    await expect(
                        portal_page.get_by_text("Recording Helpdesk workflow")
                    ).to_be_visible()
                    await wait_for_capture(
                        worker,
                        lambda state: state["nextSequence"] >= 1,
                        "initial navigation",
                    )

                    await target_page.bring_to_front()
                    if scenario != "failed":
                        await update_review(target_page)
                    await target_page.goto(f"{TARGET_ORIGIN}/tickets/2/")
                    await expect(
                        target_page.get_by_role(
                            "heading",
                            name=re.compile(r"^Complete access request #\d+$"),
                        )
                    ).to_be_visible()
                    await wait_for_capture(
                        worker,
                        lambda state: state.get("lastPage", {}).get("record_ref") == "completion",
                        "completion ticket document",
                    )
                    if scenario != "failed":
                        await update_completion(target_page, resolve=scenario == "success")
                    else:
                        await target_page.get_by_role("tab", name="Respond", exact=True).click()

                    await portal_page.bring_to_front()
                    await portal_page.get_by_role("button", name="Stop capture").click()
                    await expect(
                        portal_page.get_by_role("heading", name="Review recorded actions")
                    ).to_be_visible(timeout=15_000)
                    await portal_page.get_by_label("Workflow category").select_option(
                        label="Linked-ticket resolution · Verified"
                    )
                    action_rows = portal_page.locator(".action-list li")
                    action_count = await action_rows.count()
                    minimum_actions = 10 if scenario in {"success", "partial"} else 3
                    if action_count < minimum_actions:
                        raise AssertionError(f"captured too few actions: {action_count}")
                    await portal_page.screenshot(
                        path=artifacts / "capture-review.png",
                        full_page=True,
                    )
                    workflow_name = {
                        "success": "Resolve linked Helpdesk tickets",
                        "partial": "Save linked ticket work without completion",
                        "failed": "Inspect Completion before Review",
                    }[scenario]
                    await portal_page.get_by_label("Workflow name").fill(workflow_name)
                    await portal_page.get_by_role("button", name="Save workflow").click()
                    await expect(
                        portal_page.get_by_role("heading", name=workflow_name)
                    ).to_be_visible()

                    async with httpx.AsyncClient(base_url=API_ORIGIN) as client:
                        workflows = (await client.get("/api/workflows")).json()["workflows"]
                        if len(workflows) != 1:
                            raise AssertionError(f"expected one saved workflow, found {len(workflows)}")
                        detail = (
                            await client.get(f"/api/workflows/{workflows[0]['workflow_id']}")
                        ).json()["workflow"]
                    actions = detail["actions"]
                    if len(actions) != action_count:
                        raise AssertionError("saved workflow differs from reviewed action count")
                    serialized = json.dumps(actions)
                    for forbidden in ("password", "_workflow/session", "csrf"):
                        if forbidden in serialized.lower():
                            raise AssertionError(f"captured forbidden data: {forbidden}")
                    if [
                        action.get("destination", {}).get("record_ref")
                        for action in actions
                        if action["kind"] == "navigate"
                    ] != ["review", "completion"]:
                        raise AssertionError("capture did not preserve both logical ticket navigations")
                    expected_fills = (
                        ["Demo review completed.", "Demo completion recorded."]
                        if scenario != "failed"
                        else []
                    )
                    if [action.get("value") for action in actions if action["kind"] == "fill"] != expected_fills:
                        raise AssertionError("capture did not preserve the demonstrated note values")
                    (artifacts / "last-captured-workflow.json").write_text(
                        json.dumps(detail, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                    await target_page.close()
                    await companion.heartbeat("ready", "0.1.0")
                    await portal_page.get_by_role("button", name="Replay").click()
                    await expect(portal_page.get_by_text("pending", exact=True)).to_be_visible()
                    snapshot = await companion.claim_run()
                    if snapshot is None:
                        raise AssertionError("companion did not claim the requested replay")
                    if snapshot.workflow.workflow_id != workflows[0]["workflow_id"]:
                        raise AssertionError("claimed replay did not freeze the captured workflow")

                    executor = ReplayExecutor(
                        companion,
                        PROJECT_ROOT,
                        headless=not headed,
                        step_delay_seconds=0,
                        final_hold_seconds=0,
                    )
                    outcome = await executor.execute(snapshot)
                    async with httpx.AsyncClient(base_url=API_ORIGIN) as client:
                        execution_detail = (
                            await client.get(f"/api/runs/{snapshot.run_id}")
                        ).json()
                    (artifacts / "last-run-detail.json").write_text(
                        json.dumps(execution_detail, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    expected_outcome = {
                        "success": "succeeded",
                        "partial": "partially_succeeded",
                        "failed": "failed",
                    }[scenario]
                    if outcome != expected_outcome:
                        raise AssertionError(
                            f"captured workflow replay outcome was {outcome}, expected {expected_outcome}"
                        )
                    await companion.heartbeat("ready", "0.1.0")
                    await expect(
                        portal_page.get_by_text(expected_outcome.replace("_", " "), exact=True)
                    ).to_be_visible(timeout=12_000)
                    await portal_page.get_by_role(
                        "button",
                        name=f"Inspect {workflow_name} run",
                    ).click()
                    await expect(
                        portal_page.get_by_role("heading", name="Outcome checks")
                    ).to_be_visible()
                    await expect(
                        portal_page.get_by_role("heading", name="Executed actions")
                    ).to_be_visible()
                    evidence_images = portal_page.locator(".evidence-grid img")
                    expected_image_count = 3 if scenario != "failed" else 2
                    if await evidence_images.count() != expected_image_count:
                        raise AssertionError(
                            "portal did not show all expected synchronized checkpoints"
                        )
                    for index in range(expected_image_count):
                        await evidence_images.nth(index).wait_for(state="visible")
                        decoded = await evidence_images.nth(index).evaluate(
                            "image => image.complete && image.naturalWidth > 0"
                        )
                        if not decoded:
                            raise AssertionError(f"evidence image {index} did not decode")
                    await portal_page.get_by_role("button", name="Replay history").click()
                    await companion.heartbeat("ready", "0.1.0")
                    await portal_page.get_by_role("button", name="Refresh runner status").click()
                    await expect(portal_page.get_by_role("button", name="Replay")).to_be_enabled()
                    await portal_page.get_by_role("button", name="Replay").click()
                    second_snapshot = await companion.claim_run()
                    if second_snapshot is None:
                        raise AssertionError("companion did not claim the comparison replay")
                    second_outcome = await ReplayExecutor(
                        companion,
                        PROJECT_ROOT,
                        headless=not headed,
                        step_delay_seconds=0,
                        final_hold_seconds=0,
                    ).execute(second_snapshot)
                    if second_outcome != expected_outcome:
                        raise AssertionError(f"comparison replay outcome was {second_outcome}")
                    await companion.heartbeat("ready", "0.1.0")
                    await expect(
                        portal_page.get_by_role("button", name="Compare runs")
                    ).to_be_visible(timeout=12_000)
                    await portal_page.get_by_role("button", name="Compare runs").click()
                    await expect(
                        portal_page.get_by_role("heading", name="Outcome checks")
                    ).to_be_visible()
                    await expect(
                        portal_page.get_by_role("heading", name="Action results")
                    ).to_be_visible()
                    comparison_images = portal_page.locator(".comparison-image img")
                    if await comparison_images.count() != 2:
                        raise AssertionError("comparison did not show both final images")
                    for index in range(2):
                        await comparison_images.nth(index).wait_for(state="visible")
                        if not await comparison_images.nth(index).evaluate(
                            "image => image.complete && image.naturalWidth > 0"
                        ):
                            raise AssertionError(f"comparison image {index} did not decode")
                    await portal_page.set_viewport_size({"width": 390, "height": 844})
                    comparison_dimensions = await portal_page.evaluate(
                        "() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth })"
                    )
                    if comparison_dimensions["width"] > comparison_dimensions["viewport"]:
                        raise AssertionError(
                            f"mobile comparison overflows horizontally: {comparison_dimensions}"
                        )
                    async with httpx.AsyncClient(base_url=API_ORIGIN) as client:
                        runs = (await client.get("/api/runs")).json()["runs"]
                        run_detail = (
                            await client.get(f"/api/runs/{snapshot.run_id}")
                        ).json()
                    if len(runs) != 2 or any(run["outcome"] != expected_outcome for run in runs):
                        raise AssertionError("portal history did not materialize two expected outcomes")
                    check_ids = {
                        envelope["event"]["check_id"]
                        for envelope in run_detail["events"]
                        if envelope["event"]["kind"] == "check"
                    }
                    required_checks = {
                        "baseline_dependency",
                        "final_completion_status",
                        "final_completion_owner",
                        "final_dependency",
                    }
                    if scenario != "failed":
                        required_checks.add("review_persisted_after_save")
                    if scenario == "success":
                        required_checks.add("review_prerequisite_before_completion")
                    if not required_checks.issubset(check_ids):
                        raise AssertionError(
                            f"replay history is missing checks: {sorted(required_checks - check_ids)}"
                        )
                finally:
                    await context.close()

            print(
                f"PASS: real {scenario} capture drove two visible replays, "
                f"{expected_outcome} history, evidence, and comparison."
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
    parser.add_argument(
        "--scenario",
        choices=("success", "partial", "failed"),
        default="success",
    )
    args = parser.parse_args()
    asyncio.run(run_smoke(headed=args.headed, scenario=args.scenario))


if __name__ == "__main__":
    main()