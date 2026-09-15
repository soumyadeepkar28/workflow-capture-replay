from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "target-state" / "src"))

from workflow_target_state.config import (  # noqa: E402
    TARGET_REPOSITORY,
    TARGET_REVISION,
    TARGET_TAG,
    LocalTargetPaths,
    verify_target_source,
)


def run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=TARGET_REPOSITORY)
    args = parser.parse_args()
    paths = LocalTargetPaths.from_project_root(PROJECT_ROOT)

    if not paths.source.exists():
        paths.local_root.mkdir(parents=True, exist_ok=True)
        run(
            [
                "git",
                "clone",
                "--branch",
                TARGET_TAG,
                "--depth",
                "1",
                args.repository,
                str(paths.source),
            ]
        )
    verify_target_source(paths)

    if not (paths.environment / "bin" / "python").exists():
        run(["python3.13", "-m", "venv", str(paths.environment)])
    run(
        [
            str(paths.environment / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--retries",
            "20",
            "--timeout",
            "120",
            "--requirement",
            str(PROJECT_ROOT / "target" / "requirements.lock"),
        ]
    )

    required_asset = (
        paths.source
        / "src"
        / "helpdesk"
        / "static"
        / "helpdesk"
        / "vendor"
        / "bootstrap"
        / "js"
        / "bootstrap.bundle.min.js"
    )
    if not required_asset.is_file():
        run(
            ["npx", "--yes", "corepack@0.34.0", "yarn", "install", "--immutable"],
            cwd=paths.source,
        )
        run(["make", "static-vendor"], cwd=paths.source)

    verify_target_source(paths)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=paths.source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != TARGET_REVISION:
        raise RuntimeError("target revision changed during setup")
    print(f"Target dependencies and assets are ready at {paths.local_root}")


if __name__ == "__main__":
    main()