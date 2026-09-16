import subprocess
from pathlib import Path

import pytest

from workflow_companion import CompanionStore
from workflow_companion.service import start, state_paths


def test_local_operation_has_single_daemon_owner_and_terminal_result(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    requested = store.request_operation("prepare_capture", 100)
    with pytest.raises(RuntimeError, match="another local operation"):
        store.request_operation("prepare_capture", 101)

    claimed = store.claim_operation(102)
    assert claimed is not None
    assert claimed.operation_id == requested.operation_id
    assert claimed.status == "running"
    assert store.claim_operation(103) is None

    store.finish_operation(requested.operation_id, error_message=None, finished_at=104)
    assert store.operation(requested.operation_id).status == "completed"  # type: ignore[union-attr]

    failed = store.request_operation("prepare_capture", 105)
    assert store.claim_operation(106) is not None
    store.finish_operation(failed.operation_id, error_message="target unavailable", finished_at=107)
    result = store.operation(failed.operation_id)
    assert result is not None and result.status == "failed"
    assert result.error_message == "target unavailable"


def test_detached_daemon_does_not_inherit_terminal_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process_checks = iter((False, True))
    captured: dict[str, object] = {}
    _, pid_path, _ = state_paths(tmp_path)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("123\n", encoding="utf-8")

    class FakeProcess:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            raise AssertionError("daemon startup unexpectedly timed out")

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "workflow_companion.service.process_is_running",
        lambda _path: next(process_checks),
    )
    monkeypatch.setattr("workflow_companion.service.subprocess.Popen", fake_popen)

    start(tmp_path)

    assert captured["stdin"] is subprocess.DEVNULL
