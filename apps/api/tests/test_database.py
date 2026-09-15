from __future__ import annotations

import sqlite3
from pathlib import Path

from workflow_api.database import Database, MIGRATIONS, SCHEMA_VERSION


def test_migration_seven_backfills_existing_workflow_categories(tmp_path: Path) -> None:
    database_path = tmp_path / "api.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migration (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for version in range(1, 7):
            connection.executescript(MIGRATIONS[version])
            connection.execute(
                "INSERT INTO schema_migration(version) VALUES (?)",
                (version,),
            )
        for workflow_id, mode, category in (
            ("wf_existingpreset01", "verified_preset", "Linked-ticket resolution"),
            ("wf_existinggeneral1", "general", "Ticket intake"),
        ):
            connection.execute(
                """
                INSERT INTO workflow(
                    id, name, description, target_alias, action_count,
                    content_hash, definition_json, created_at, workflow_mode, category
                ) VALUES (?, ?, '', 'helpdesk-demo', 1, ?, '{}', 100, ?, ?)
                """,
                (workflow_id, workflow_id, "0" * 64, mode, category),
            )
        connection.commit()

    Database(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute(
            "SELECT MAX(version) AS version FROM schema_migration"
        ).fetchone()["version"]
        categories = connection.execute(
            "SELECT * FROM workflow_category ORDER BY built_in DESC, name"
        ).fetchall()
        workflows = connection.execute(
            "SELECT workflow_mode, category, category_id FROM workflow ORDER BY id"
        ).fetchall()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert version == SCHEMA_VERSION == 7
    assert [row["name"] for row in categories] == [
        "Linked-ticket resolution",
        "Ticket intake",
    ]
    assert categories[0]["id"] == "cat_linked_tickets_v1"
    assert categories[0]["verification_profile_json"] is not None
    assert categories[1]["verification_profile_json"] is None
    assert {row["category_id"] for row in workflows} == {
        categories[0]["id"],
        categories[1]["id"],
    }
    assert foreign_key_errors == []