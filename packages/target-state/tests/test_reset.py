import sqlite3
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from workflow_target_state import TargetResetError, restore_sqlite_database
from scripts import target as target_script


def create_database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker(value) VALUES (?)", (value,))


def read_marker(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT value FROM marker").fetchone()
    assert row is not None
    return row[0]


def test_restore_replaces_target_with_integrity_checked_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.sqlite3"
    target = tmp_path / "target.sqlite3"
    create_database(baseline, "baseline")
    create_database(target, "changed")

    restore_sqlite_database(baseline, target)

    assert read_marker(target) == "baseline"


def test_restore_refuses_existing_journal_state(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.sqlite3"
    target = tmp_path / "target.sqlite3"
    create_database(baseline, "baseline")
    Path(f"{target}-wal").touch()

    with pytest.raises(TargetResetError, match="journal files exist"):
        restore_sqlite_database(baseline, target)


def test_target_stop_accepts_process_that_already_exited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "target.pid"
    pid_file.write_text("1234\n", encoding="utf-8")

    def process_gone(process_id: int, sent_signal: int) -> None:
        assert (process_id, sent_signal) == (1234, signal.SIGTERM)
        raise ProcessLookupError

    monkeypatch.setattr(target_script.os, "kill", process_gone)

    target_script.stop(SimpleNamespace(pid_file=pid_file))  # type: ignore[arg-type]

    assert not pid_file.exists()


def test_target_stop_accepts_pid_file_removed_before_read() -> None:
    class DisappearingPidFile:
        def exists(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise FileNotFoundError

    target_script.stop(  # type: ignore[arg-type]
        SimpleNamespace(pid_file=DisappearingPidFile())
    )


def test_target_stop_accepts_owner_reaping_and_removing_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "target.pid"
    pid_file.write_text("1234\n", encoding="utf-8")

    def reap_after_signal(process_id: int, sent_signal: int) -> None:
        assert (process_id, sent_signal) == (1234, signal.SIGTERM)
        pid_file.unlink()

    monkeypatch.setattr(target_script.os, "kill", reap_after_signal)

    target_script.stop(SimpleNamespace(pid_file=pid_file))  # type: ignore[arg-type]

    assert not pid_file.exists()