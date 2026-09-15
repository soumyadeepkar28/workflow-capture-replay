from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
from pathlib import Path


class TargetResetError(RuntimeError):
    pass


def validate_sqlite_integrity(database: Path) -> None:
    if not database.is_file():
        raise TargetResetError(f"missing SQLite database: {database}")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise TargetResetError(f"cannot read SQLite database: {database}") from error
    if result is None or result[0] != "ok":
        detail = "no result" if result is None else result[0]
        raise TargetResetError(f"SQLite integrity check failed: {detail}")


def _pid_is_running(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        process_id = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(process_id, 0)
    except (ProcessLookupError, ValueError):
        pid_file.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True
    return True


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def assert_target_stopped(pid_file: Path, host: str, port: int) -> None:
    if _pid_is_running(pid_file) or _port_is_open(host, port):
        raise TargetResetError("refusing reset while the target server is running")


def restore_sqlite_database(baseline: Path, target: Path) -> None:
    validate_sqlite_integrity(baseline)

    journals = [
        Path(f"{target}-journal"),
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
    ]
    present = [path.name for path in journals if path.exists()]
    if present:
        raise TargetResetError(
            "refusing reset while SQLite journal files exist: " + ", ".join(present)
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".workflow-restore-",
        suffix=".sqlite3",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with sqlite3.connect(f"file:{baseline}?mode=ro", uri=True) as source:
            with sqlite3.connect(temporary) as destination:
                source.backup(destination)
        validate_sqlite_integrity(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)