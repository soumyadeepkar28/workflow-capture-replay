from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import django

from workflow_target_state.control import initialize_control_database
from workflow_target_state.fixture import (
    assert_baseline_state,
    collect_fixture_state,
    initialize_fixture,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize_parser = subparsers.add_parser("initialize")
    initialize_parser.add_argument("--baseline", type=Path, required=True)
    initialize_parser.add_argument("--manifest", type=Path, required=True)
    initialize_parser.add_argument("--installation-id", required=True)
    initialize_parser.add_argument("--source-revision", required=True)

    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--expect-baseline", action="store_true")

    args = parser.parse_args()
    django.setup()

    if args.command == "initialize":
        control_database = Path(os.environ["WORKFLOW_TARGET_CONTROL_DATABASE"])
        initialize_control_database(control_database, args.installation_id)
        result = initialize_fixture(
            database=Path(os.environ["WORKFLOW_TARGET_DATABASE"]),
            baseline=args.baseline,
            manifest=args.manifest,
            installation_id=args.installation_id,
            source_revision=args.source_revision,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    state = collect_fixture_state()
    if args.expect_baseline:
        assert_baseline_state(state)
    print(json.dumps(state, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()