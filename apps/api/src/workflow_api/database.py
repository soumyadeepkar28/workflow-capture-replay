from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 7

SCHEMA_V1 = """
CREATE TABLE portal_session (
    id TEXT PRIMARY KEY,
    token_digest TEXT NOT NULL UNIQUE,
    csrf_digest TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE pairing (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    ticket_digest TEXT NOT NULL UNIQUE,
    pending_proof_digest TEXT NOT NULL UNIQUE,
    runner_credential_digest TEXT NOT NULL UNIQUE,
    companion_version TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    source_ip TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    controller_session_id TEXT REFERENCES portal_session(id),
    runner_id TEXT
);

CREATE TABLE runner (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    credential_digest TEXT NOT NULL UNIQUE,
    controller_session_id TEXT NOT NULL REFERENCES portal_session(id),
    companion_version TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    last_seen_at INTEGER,
    readiness TEXT NOT NULL DEFAULT 'offline'
        CHECK (readiness IN ('offline', 'ready', 'busy', 'recovery_required'))
);

CREATE UNIQUE INDEX one_active_runner_per_session
ON runner(controller_session_id)
WHERE revoked_at IS NULL;

CREATE INDEX pairing_source_created
ON pairing(source_ip, created_at);

CREATE INDEX runner_credential_lookup
ON runner(credential_digest);
"""

SCHEMA_V2 = """
CREATE TABLE workflow (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    target_alias TEXT NOT NULL,
    action_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE capture_draft (
    id TEXT PRIMARY KEY,
    controller_session_id TEXT NOT NULL REFERENCES portal_session(id),
    runner_id TEXT NOT NULL REFERENCES runner(id),
    capability_digest TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'review', 'saved', 'interrupted')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    finalized_at INTEGER,
    capture_json TEXT,
    expected_batch_count INTEGER,
    expected_action_count INTEGER,
    workflow_id TEXT REFERENCES workflow(id)
);

CREATE TABLE capture_batch (
    draft_id TEXT NOT NULL REFERENCES capture_draft(id) ON DELETE CASCADE,
    batch_index INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    action_count INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    PRIMARY KEY (draft_id, batch_index),
    UNIQUE (draft_id, batch_id)
);

CREATE INDEX capture_draft_session
ON capture_draft(controller_session_id, created_at DESC);

CREATE INDEX workflow_created
ON workflow(created_at DESC);
"""

SCHEMA_V3 = """
CREATE TABLE workflow_run (
    id TEXT PRIMARY KEY,
    controller_session_id TEXT NOT NULL REFERENCES portal_session(id),
    runner_id TEXT NOT NULL REFERENCES runner(id),
    workflow_id TEXT NOT NULL REFERENCES workflow(id),
    client_request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN (
        'pending', 'claimed', 'accepted', 'preparing', 'running', 'verifying',
        'finished', 'interrupted', 'cancelled', 'rejected', 'expired'
    )),
    outcome TEXT CHECK (outcome IN (
        'succeeded', 'partially_succeeded', 'failed', 'uncertain'
    )),
    outcome_summary TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    claim_id TEXT UNIQUE,
    reporting_capability_digest TEXT UNIQUE,
    claimed_at INTEGER,
    accepted_at INTEGER,
    started_at INTEGER,
    finished_at INTEGER,
    last_event_sequence INTEGER NOT NULL DEFAULT -1,
    UNIQUE (controller_session_id, client_request_key)
);

CREATE TABLE run_event (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_hash TEXT NOT NULL,
    event_json TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    UNIQUE (run_id, sequence)
);

CREATE INDEX workflow_run_runner_phase
ON workflow_run(runner_id, phase, created_at);

CREATE INDEX workflow_run_workflow_created
ON workflow_run(workflow_id, created_at DESC);
"""

SCHEMA_V4 = """
CREATE TABLE run_artifact (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_run(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'baseline', 'review_checkpoint', 'final', 'failure'
    )),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 2097152),
    content_type TEXT NOT NULL CHECK (content_type = 'image/png'),
    relative_path TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, kind, artifact_id)
);

CREATE INDEX run_artifact_run
ON run_artifact(run_id, created_at);
"""

SCHEMA_V5 = """
ALTER TABLE capture_draft ADD COLUMN error_message TEXT;
"""

SCHEMA_V6 = """
ALTER TABLE capture_draft ADD COLUMN workflow_mode TEXT NOT NULL DEFAULT 'verified_preset'
    CHECK (workflow_mode IN ('verified_preset', 'general'));
ALTER TABLE capture_draft ADD COLUMN category TEXT NOT NULL DEFAULT 'Linked-ticket resolution';
ALTER TABLE workflow ADD COLUMN workflow_mode TEXT NOT NULL DEFAULT 'verified_preset'
    CHECK (workflow_mode IN ('verified_preset', 'general'));
ALTER TABLE workflow ADD COLUMN category TEXT NOT NULL DEFAULT 'Linked-ticket resolution';
"""

SCHEMA_V7 = """
CREATE TABLE workflow_category (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    verification_profile_json TEXT,
    built_in INTEGER NOT NULL DEFAULT 0 CHECK (built_in IN (0, 1)),
    created_at INTEGER NOT NULL
);

INSERT INTO workflow_category(
    id, name, verification_profile_json, built_in, created_at
) VALUES (
    'cat_linked_tickets_v1', 'Linked-ticket resolution', NULL, 1, 0
);

INSERT OR IGNORE INTO workflow_category(
    id, name, verification_profile_json, built_in, created_at
)
SELECT
    'cat_legacy_' || lower(hex(randomblob(12))),
    category,
    NULL,
    0,
    MIN(created_at)
FROM workflow
WHERE workflow_mode = 'general'
GROUP BY category COLLATE NOCASE;

ALTER TABLE workflow ADD COLUMN category_id TEXT REFERENCES workflow_category(id);

UPDATE workflow
SET category_id = 'cat_linked_tickets_v1'
WHERE workflow_mode = 'verified_preset';

UPDATE workflow
SET category_id = (
    SELECT workflow_category.id
    FROM workflow_category
    WHERE workflow_category.name = workflow.category COLLATE NOCASE
)
WHERE workflow_mode = 'general';

CREATE INDEX workflow_category_created
ON workflow_category(created_at, id);
"""

MIGRATIONS = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
    6: SCHEMA_V6,
    7: SCHEMA_V7,
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migration"
            ).fetchone()
            version = int(row["version"])
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {version} is newer than this API")

            for next_version in range(version + 1, SCHEMA_VERSION + 1):
                migration = MIGRATIONS[next_version]
                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + migration
                        + f"\nINSERT INTO schema_migration(version) VALUES ({next_version});\n"
                        + "COMMIT;"
                    )
                except BaseException:
                    connection.rollback()
                    raise

            from workflow_api.workflows import verification_profile

            connection.execute(
                """
                UPDATE workflow_category
                SET verification_profile_json = ?
                WHERE id = 'cat_linked_tickets_v1' AND built_in = 1
                """,
                (verification_profile().model_dump_json(),),
            )