from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import signal
import subprocess
import sys
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import httpx

from workflow_companion.client import CompanionClient
from workflow_companion.replay import ReplayExecutor, TargetRuntime
from workflow_companion.store import CompanionStore
from workflow_protocol import OutcomeRunEvent, PhaseRunEvent
from workflow_target_state import issue_session_ticket
from workflow_target_state.config import LocalTargetPaths

COMPANION_VERSION = "0.1.0"


def project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def state_paths(root: Path) -> tuple[Path, Path, Path]:
    state = Path(
        os.environ.get("WORKFLOW_COMPANION_STATE_DIR", root / ".local" / "state")
    ).resolve()
    return state / "companion.sqlite3", state / "companion.pid", state / "companion.lock"


def api_origin() -> str:
    return os.environ.get("WORKFLOW_PUBLIC_ORIGIN", "http://127.0.0.1:8000").rstrip("/")


def open_in_chrome(url: str) -> None:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["open", "-a", "Google Chrome", url],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
    if not webbrowser.open(url):
        raise RuntimeError("could not open the browser; open the displayed URL manually")


@contextmanager
def single_instance(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another companion daemon is already running") from error
        yield


async def pair(root: Path) -> None:
    database_path, _, _ = state_paths(root)
    client = CompanionClient(api_origin(), CompanionStore(database_path))
    launch = await client.initiate_pairing("Local replay runner", COMPANION_VERSION)
    open_in_chrome(launch.pairing_url)
    print("Confirm the pending runner in the opened Chrome tab.")
    while int(time.time()) <= launch.expires_at:
        state = await client.refresh_pairing()
        if state == "connected":
            await client.heartbeat("ready", COMPANION_VERSION)
            print("Runner connected and ready.")
            return
        if state == "expired":
            break
        await asyncio.sleep(2)
    raise RuntimeError("pairing expired before Connect was confirmed")


async def request_prepare_capture(root: Path) -> None:
    database_path, _, _ = state_paths(root)
    store = CompanionStore(database_path)
    store.initialize()
    if store.association() is None:
        raise RuntimeError("pair the companion before preparing capture")
    if store.unfinished_runs():
        raise RuntimeError("cannot prepare capture while a run is unfinished")
    _, pid_path, _ = state_paths(root)
    if not process_is_running(pid_path):
        raise RuntimeError("start the companion before preparing capture")
    operation = store.request_operation("prepare_capture", int(time.time()))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        current = store.operation(operation.operation_id)
        if current is None:
            raise RuntimeError("capture preparation operation disappeared")
        if current.status == "completed":
            print("Capture target reset and opened in Chrome. Keep exactly one demo ticket tab open.")
            return
        if current.status == "failed":
            raise RuntimeError(current.error_message or "capture preparation failed")
        await asyncio.sleep(0.1)
    raise RuntimeError("capture preparation did not finish within 45 seconds")


async def handle_local_operation(
    root: Path,
    client: CompanionClient,
    store: CompanionStore,
) -> bool:
    operation = store.claim_operation(int(time.time()))
    if operation is None:
        return False
    try:
        await client.heartbeat("busy", COMPANION_VERSION)
        if operation.kind == "prepare_capture":
            runtime = TargetRuntime(root)
            await runtime.prepare()
            ticket = issue_session_ticket(runtime.paths.control_database, "capture")
            open_in_chrome(f"http://127.0.0.1:8765/_workflow/session/#ticket={ticket}")
        await client.heartbeat("ready", COMPANION_VERSION)
        store.finish_operation(
            operation.operation_id,
            error_message=None,
            finished_at=int(time.time()),
        )
    except BaseException as error:
        try:
            await client.heartbeat("recovery_required", COMPANION_VERSION)
        finally:
            store.finish_operation(
                operation.operation_id,
                error_message=f"{type(error).__name__}: {str(error)[:400]}",
                finished_at=int(time.time()),
            )
        return True
    return True


async def reconcile_unfinished(client: CompanionClient, store: CompanionStore) -> None:
    for local_run in store.unfinished_runs():
        if local_run.snapshot.workflow.workflow_mode == "verified_preset":
            await client.record_event(
                local_run.run_id,
                OutcomeRunEvent(
                    kind="outcome",
                    outcome="uncertain",
                    summary="The companion restarted after accepting this run; browser mutations were not resumed.",
                    observed_at=datetime.now(timezone.utc),
                ),
            )
        await client.record_event(
            local_run.run_id,
            PhaseRunEvent(
                kind="phase",
                phase="interrupted",
                observed_at=datetime.now(timezone.utc),
            ),
        )


async def daemon(root: Path) -> None:
    database_path, pid_path, lock_path = state_paths(root)
    store = CompanionStore(database_path)
    store.initialize()
    if store.association() is None:
        raise RuntimeError("pair the companion before starting it")
    with single_instance(lock_path):
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        stop_requested = asyncio.Event()
        loop = asyncio.get_running_loop()
        for handled_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(handled_signal, stop_requested.set)
        client = CompanionClient(api_origin(), store)
        try:
            await reconcile_unfinished(client, store)
            backoff = 2
            while not stop_requested.is_set():
                try:
                    if await handle_local_operation(root, client, store):
                        backoff = 2
                        continue
                    await client.heartbeat("ready", COMPANION_VERSION)
                    snapshot = await client.claim_run()
                    if snapshot is not None:
                        await ReplayExecutor(client, root, headless=False).execute(snapshot)
                    backoff = 2
                except httpx.HTTPError:
                    backoff = min(backoff * 2, 30)
                try:
                    await asyncio.wait_for(stop_requested.wait(), timeout=backoff)
                except TimeoutError:
                    pass
        finally:
            pid_path.unlink(missing_ok=True)


def process_is_running(pid_path: Path) -> bool:
    if not pid_path.exists():
        return False
    try:
        process_id = int(pid_path.read_text(encoding="utf-8").strip())
        try:
            waited_pid, _ = os.waitpid(process_id, os.WNOHANG)
            if waited_pid == process_id:
                pid_path.unlink(missing_ok=True)
                return False
        except ChildProcessError:
            pass
        os.kill(process_id, 0)
        return True
    except (ValueError, ProcessLookupError):
        pid_path.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True


def start(root: Path) -> None:
    _, pid_path, _ = state_paths(root)
    if process_is_running(pid_path):
        raise RuntimeError("companion is already running")
    log_path = root / ".local" / "state" / "companion.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "workflow_companion.service", "daemon"],
            cwd=root,
            env=os.environ.copy(),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"companion exited during startup; inspect {log_path}")
        if process_is_running(pid_path):
            print(f"Companion started as PID {pid_path.read_text(encoding='utf-8').strip()}.")
            return
        time.sleep(0.05)
    process.terminate()
    raise RuntimeError("companion did not report startup within five seconds")


def stop(root: Path) -> None:
    _, pid_path, _ = state_paths(root)
    if process_is_running(pid_path):
        process_id = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(process_id, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not process_is_running(pid_path):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("companion did not stop at a safe boundary within ten seconds")
    target_paths = LocalTargetPaths.from_project_root(root)
    if target_paths.pid_file.exists():
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root / "packages" / "target-state" / "src")
        subprocess.run(
            [sys.executable, str(root / "scripts" / "target.py"), "stop"],
            cwd=root,
            env=environment,
            check=True,
        )
    print("Companion and managed target stopped.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("pair", "prepare-capture", "start", "stop", "daemon", "status"),
    )
    args = parser.parse_args()
    root = project_root()
    _, pid_path, _ = state_paths(root)
    if args.command == "pair":
        asyncio.run(pair(root))
    elif args.command == "prepare-capture":
        asyncio.run(request_prepare_capture(root))
    elif args.command == "start":
        start(root)
    elif args.command == "stop":
        stop(root)
    elif args.command == "daemon":
        asyncio.run(daemon(root))
    elif args.command == "status":
        print("running" if process_is_running(pid_path) else "stopped")


if __name__ == "__main__":
    main()