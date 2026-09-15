from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "target-state" / "src"))

from workflow_target_state import (  # noqa: E402
    begin_sandbox_reset,
    complete_sandbox_reset,
    issue_session_ticket,
    restore_sqlite_database,
)
from workflow_target_state.config import (  # noqa: E402
    TARGET_HOST,
    TARGET_PORT,
    TARGET_REVISION,
    LocalTargetPaths,
    target_environment,
    verify_target_source,
)
from workflow_target_state.reset import assert_target_stopped  # noqa: E402


def _write_secret(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(secrets.token_urlsafe(48))


def _run_target(
    paths: LocalTargetPaths,
    arguments: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(paths.environment / "bin" / "python"), *arguments],
        cwd=paths.source,
        env=target_environment(paths),
        check=check,
        capture_output=capture_output,
        text=True,
    )


def initialize(paths: LocalTargetPaths) -> None:
    verify_target_source(paths)
    managed_paths = [
        paths.database,
        paths.baseline,
        paths.manifest,
        paths.control_database,
        paths.secret_key,
    ]
    existing = [str(path) for path in managed_paths if path.exists()]
    if existing:
        raise RuntimeError("refusing to overwrite initialized target state: " + ", ".join(existing))
    if not (paths.environment / "bin" / "python").is_file():
        raise RuntimeError("target environment is missing; run target setup first")

    paths.data.mkdir(parents=True, exist_ok=True)
    _write_secret(paths.secret_key)
    installation_id = f"inst_{secrets.token_urlsafe(18)}"
    _run_target(paths, [str(paths.source / "manage.py"), "migrate", "--noinput"])
    _run_target(
        paths,
        [
            "-m",
            "workflow_target_state.django_cli",
            "initialize",
            "--baseline",
            str(paths.baseline),
            "--manifest",
            str(paths.manifest),
            "--installation-id",
            installation_id,
            "--source-revision",
            TARGET_REVISION,
        ],
    )
    print(f"Initialized local target {installation_id}")


def probe(paths: LocalTargetPaths, expect_baseline: bool) -> None:
    arguments = ["-m", "workflow_target_state.django_cli", "probe"]
    if expect_baseline:
        arguments.append("--expect-baseline")
    result = _run_target(paths, arguments, capture_output=True)
    print(result.stdout, end="")


def reset(paths: LocalTargetPaths) -> None:
    assert_target_stopped(paths.pid_file, TARGET_HOST, TARGET_PORT)
    begin_sandbox_reset(paths.control_database)
    restore_sqlite_database(paths.baseline, paths.database)
    probe(paths, expect_baseline=True)
    complete_sandbox_reset(paths.control_database)
    print("Target reset completed; prior demo sessions are invalidated.")


def issue_session(paths: LocalTargetPaths, purpose: str, open_browser: bool) -> None:
    ticket = issue_session_ticket(paths.control_database, purpose)  # type: ignore[arg-type]
    url = f"http://{TARGET_HOST}:{TARGET_PORT}/_workflow/session/#ticket={ticket}"
    if open_browser:
        if not webbrowser.open(url):
            raise RuntimeError("could not open the demo-session link")
        print(f"Opened a one-use {purpose} session link in the default browser.")
    else:
        print(url)


def serve(paths: LocalTargetPaths) -> None:
    verify_target_source(paths)
    if paths.pid_file.exists():
        raise RuntimeError("target PID file already exists")
    assert_target_stopped(paths.pid_file, TARGET_HOST, TARGET_PORT)
    process = subprocess.Popen(
        [
            str(paths.environment / "bin" / "python"),
            str(paths.source / "manage.py"),
            "runserver",
            f"{TARGET_HOST}:{TARGET_PORT}",
            "--noreload",
        ],
        cwd=paths.source,
        env=target_environment(paths),
        text=True,
    )
    paths.pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
    try:
        return_code = process.wait()
        if return_code not in {0, -signal.SIGTERM}:
            raise SystemExit(return_code)
    finally:
        paths.pid_file.unlink(missing_ok=True)


def stop(paths: LocalTargetPaths) -> None:
    if not paths.pid_file.exists():
        print("Target is not recorded as running.")
        return
    try:
        process_id = int(paths.pid_file.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        print("Target was already stopped.")
        return
    try:
        os.kill(process_id, signal.SIGTERM)
    except ProcessLookupError:
        paths.pid_file.unlink(missing_ok=True)
        print("Target was already stopped.")
        return
    for _ in range(50):
        if not paths.pid_file.exists():
            print("Target stopped.")
            return
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            paths.pid_file.unlink(missing_ok=True)
            print("Target stopped.")
            return
        time.sleep(0.1)
    raise RuntimeError("target did not stop within five seconds")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--expect-baseline", action="store_true")
    subparsers.add_parser("reset")
    subparsers.add_parser("serve")
    subparsers.add_parser("stop")
    session_parser = subparsers.add_parser("issue-session")
    session_parser.add_argument("--purpose", choices=("capture", "replay"), required=True)
    session_parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    paths = LocalTargetPaths.from_project_root(PROJECT_ROOT)

    if args.command == "init":
        initialize(paths)
    elif args.command == "probe":
        probe(paths, args.expect_baseline)
    elif args.command == "reset":
        reset(paths)
    elif args.command == "serve":
        serve(paths)
    elif args.command == "stop":
        stop(paths)
    elif args.command == "issue-session":
        issue_session(paths, args.purpose, args.open)


if __name__ == "__main__":
    main()