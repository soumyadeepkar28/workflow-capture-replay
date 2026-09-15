from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

SessionPurpose = Literal["capture", "replay"]


class InvalidSessionTicketError(ValueError):
    pass


@dataclass(frozen=True)
class SessionTicketGrant:
    purpose: SessionPurpose
    sandbox_generation: int


def _connect(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_control_database(database: Path, installation_id: str) -> None:
    if not installation_id:
        raise ValueError("installation_id cannot be empty")

    with _connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sandbox_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                installation_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                recovery_required INTEGER NOT NULL DEFAULT 0
                    CHECK (recovery_required IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS demo_session_ticket (
                digest TEXT PRIMARY KEY,
                purpose TEXT NOT NULL CHECK (purpose IN ('capture', 'replay')),
                sandbox_generation INTEGER NOT NULL CHECK (sandbox_generation >= 1),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER
            );
            """
        )
        existing = connection.execute(
            "SELECT installation_id FROM sandbox_state WHERE singleton = 1"
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO sandbox_state(singleton, installation_id, generation) "
                "VALUES (1, ?, 1)",
                (installation_id,),
            )
        elif existing["installation_id"] != installation_id:
            raise ValueError("control database belongs to a different installation")


def current_sandbox_generation(database: Path) -> int:
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT generation FROM sandbox_state WHERE singleton = 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("control database is not initialized")
    return int(row["generation"])


def _digest_ticket(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def issue_session_ticket(
    database: Path,
    purpose: SessionPurpose,
    *,
    ttl_seconds: int = 60,
    now: int | None = None,
    token_factory: Callable[[], str] | None = None,
) -> str:
    if purpose not in {"capture", "replay"}:
        raise ValueError("unsupported session purpose")
    if not 1 <= ttl_seconds <= 300:
        raise ValueError("session ticket lifetime must be between 1 and 300 seconds")

    issued_at = int(time.time()) if now is None else now
    ticket = (token_factory or (lambda: secrets.token_urlsafe(32)))()
    if len(ticket) < 32:
        raise ValueError("session ticket does not contain enough entropy")

    with _connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT generation FROM sandbox_state "
            "WHERE singleton = 1 AND recovery_required = 0"
        ).fetchone()
        if row is None:
            connection.rollback()
            raise RuntimeError("sandbox is in recovery or control database is not initialized")
        connection.execute(
            """
            INSERT INTO demo_session_ticket(
                digest, purpose, sandbox_generation, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _digest_ticket(ticket),
                purpose,
                int(row["generation"]),
                issued_at,
                issued_at + ttl_seconds,
            ),
        )
        connection.commit()
    return ticket


def consume_session_ticket(
    database: Path,
    ticket: str,
    *,
    now: int | None = None,
) -> SessionTicketGrant:
    consumed_at = int(time.time()) if now is None else now
    with _connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT ticket.purpose, ticket.sandbox_generation
            FROM demo_session_ticket AS ticket
            JOIN sandbox_state AS state ON state.singleton = 1
            WHERE ticket.digest = ?
              AND ticket.consumed_at IS NULL
              AND ticket.expires_at >= ?
              AND ticket.sandbox_generation = state.generation
              AND state.recovery_required = 0
            """,
            (_digest_ticket(ticket), consumed_at),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise InvalidSessionTicketError("session ticket is invalid or expired")
        updated = connection.execute(
            "UPDATE demo_session_ticket SET consumed_at = ? "
            "WHERE digest = ? AND consumed_at IS NULL",
            (consumed_at, _digest_ticket(ticket)),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise InvalidSessionTicketError("session ticket is invalid or expired")
        connection.commit()

    return SessionTicketGrant(
        purpose=row["purpose"],
        sandbox_generation=int(row["sandbox_generation"]),
    )


def advance_sandbox_generation(database: Path) -> int:
    with _connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE sandbox_state SET generation = generation + 1 WHERE singleton = 1"
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise RuntimeError("control database is not initialized")
        connection.execute("DELETE FROM demo_session_ticket WHERE consumed_at IS NULL")
        row = connection.execute(
            "SELECT generation FROM sandbox_state WHERE singleton = 1"
        ).fetchone()
        connection.commit()
    return int(row["generation"])


def begin_sandbox_reset(database: Path) -> int:
    with _connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE sandbox_state
            SET generation = generation + 1, recovery_required = 1
            WHERE singleton = 1 AND recovery_required = 0
            """
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise RuntimeError("sandbox is already in recovery or is not initialized")
        connection.execute("DELETE FROM demo_session_ticket WHERE consumed_at IS NULL")
        row = connection.execute(
            "SELECT generation FROM sandbox_state WHERE singleton = 1"
        ).fetchone()
        connection.commit()
    return int(row["generation"])


def complete_sandbox_reset(database: Path) -> None:
    with _connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE sandbox_state SET recovery_required = 0 "
            "WHERE singleton = 1 AND recovery_required = 1"
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise RuntimeError("sandbox reset is not awaiting completion")
        connection.commit()