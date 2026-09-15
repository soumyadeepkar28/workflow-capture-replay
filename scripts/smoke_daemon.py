from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from smoke_capture import API_ORIGIN, PROJECT_ROOT, CompanionClient, CompanionStore, start_api, wait_for_http
from workflow_companion.service import process_is_running, start, state_paths, stop


async def run_smoke() -> None:
    api_server = None
    previous_state = os.environ.get("WORKFLOW_COMPANION_STATE_DIR")
    try:
        with tempfile.TemporaryDirectory(prefix="workflow-daemon-") as directory:
            temporary = Path(directory)
            os.environ["WORKFLOW_COMPANION_STATE_DIR"] = str(temporary / "state")
            os.environ["WORKFLOW_PUBLIC_ORIGIN"] = API_ORIGIN
            api_server = start_api(temporary / "api.sqlite3")
            await wait_for_http(f"{API_ORIGIN}/api/health")

            database_path, pid_path, _ = state_paths(PROJECT_ROOT)
            companion = CompanionClient(API_ORIGIN, CompanionStore(database_path))
            pairing = await companion.initiate_pairing("Daemon smoke runner", "0.1.0")
            ticket = parse_qs(urlsplit(pairing.pairing_url).fragment)["ticket"][0]
            async with httpx.AsyncClient(base_url=API_ORIGIN) as portal:
                session = (await portal.get("/api/session")).json()
                headers = {
                    "Origin": API_ORIGIN,
                    "X-CSRF-Token": session["csrf_token"],
                }
                connected = await portal.post(
                    "/api/pairings/connect",
                    headers=headers,
                    json={"ticket": ticket},
                )
                if connected.status_code != 200:
                    raise AssertionError(connected.text)
                if await companion.refresh_pairing() != "connected":
                    raise AssertionError("companion did not persist the association")

                start(PROJECT_ROOT)
                if not process_is_running(pid_path):
                    raise AssertionError("daemon PID did not become live")
                try:
                    start(PROJECT_ROOT)
                except RuntimeError as error:
                    if "already running" not in str(error):
                        raise
                else:
                    raise AssertionError("second daemon start unexpectedly succeeded")

                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = (await portal.get("/api/session")).json()
                    if current["runner"] and current["runner"]["readiness"] == "ready":
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("daemon heartbeat did not become visible")

                stop(PROJECT_ROOT)
                if process_is_running(pid_path):
                    raise AssertionError("daemon remained live after stop")
            print("PASS: detached daemon paired, heartbeated, refused duplication, and stopped.")
    finally:
        if api_server is not None and api_server.poll() is None:
            api_server.terminate()
            api_server.wait(timeout=5)
        if previous_state is None:
            os.environ.pop("WORKFLOW_COMPANION_STATE_DIR", None)
        else:
            os.environ["WORKFLOW_COMPANION_STATE_DIR"] = previous_state


if __name__ == "__main__":
    asyncio.run(run_smoke())