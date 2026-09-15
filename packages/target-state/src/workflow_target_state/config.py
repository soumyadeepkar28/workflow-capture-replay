from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

TARGET_REPOSITORY = "https://github.com/django-helpdesk/django-helpdesk.git"
TARGET_TAG = "v2.4.0"
TARGET_REVISION = "7698042564408aa48c481005650bf223744949a2"
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8765


@dataclass(frozen=True)
class LocalTargetPaths:
    project_root: Path
    local_root: Path
    source: Path
    environment: Path
    data: Path
    database: Path
    baseline: Path
    manifest: Path
    control_database: Path
    secret_key: Path
    pid_file: Path
    integration: Path
    target_state_source: Path

    @classmethod
    def from_project_root(cls, project_root: Path) -> LocalTargetPaths:
        project_root = project_root.resolve()
        local_root = project_root / ".local" / "target"
        data = local_root / "data"
        return cls(
            project_root=project_root,
            local_root=local_root,
            source=local_root / "source",
            environment=local_root / "venv",
            data=data,
            database=data / "db.sqlite3",
            baseline=data / "baseline.sqlite3",
            manifest=data / "installation.json",
            control_database=project_root / ".local" / "state" / "companion.sqlite3",
            secret_key=data / "django-secret-key",
            pid_file=local_root / "target-server.pid",
            integration=project_root / "target" / "integration",
            target_state_source=project_root / "packages" / "target-state" / "src",
        )


def verify_target_source(paths: LocalTargetPaths) -> None:
    if not (paths.source / ".git").is_dir():
        raise RuntimeError(f"missing target checkout: {paths.source}")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=paths.source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != TARGET_REVISION:
        raise RuntimeError(f"unexpected target revision: {revision}")
    changed = subprocess.run(
        ["git", "status", "--short", "--untracked-files=no"],
        cwd=paths.source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if changed:
        raise RuntimeError("target checkout has tracked modifications")


def target_environment(
    paths: LocalTargetPaths,
    *,
    database: Path | None = None,
    control_database: Path | None = None,
) -> dict[str, str]:
    python_paths = [
        paths.integration,
        paths.target_state_source,
        paths.source,
        paths.source / "src",
    ]
    existing_python_path = os.environ.get("PYTHONPATH")
    if existing_python_path:
        python_paths.append(Path(existing_python_path))

    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "workflow_target.settings",
            "PYTHONPATH": os.pathsep.join(str(path) for path in python_paths),
            "WORKFLOW_TARGET_CONTROL_DATABASE": str(
                (control_database or paths.control_database).resolve()
            ),
            "WORKFLOW_TARGET_DATABASE": str((database or paths.database).resolve()),
            "WORKFLOW_TARGET_DATA_ROOT": str(paths.data.resolve()),
            "WORKFLOW_TARGET_EXPECTED_HOST": f"{TARGET_HOST}:{TARGET_PORT}",
            "WORKFLOW_TARGET_ORIGIN": f"http://{TARGET_HOST}:{TARGET_PORT}",
            "WORKFLOW_TARGET_SECRET_KEY_FILE": str(paths.secret_key.resolve()),
        }
    )
    return environment