from __future__ import annotations

import json
from pathlib import Path

from workflow_protocol import WorkflowDefinition


def remove_discriminator_annotations(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: remove_discriminator_annotations(child)
            for key, child in value.items()
            if key != "discriminator"
        }
    if isinstance(value, list):
        return [remove_discriminator_annotations(child) for child in value]
    return value


def main() -> None:
    package_root = Path(__file__).resolve().parents[1]
    output_path = package_root / "schema" / "workflow-definition.schema.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    generated = remove_discriminator_annotations(
        WorkflowDefinition.model_json_schema(mode="validation")
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://workflow-capture-replay.example/schemas/workflow-definition-v1.json",
        **generated,
    }
    output_path.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()