from pathlib import Path

import pytest

from workflow_target_state import (
    InvalidSessionTicketError,
    advance_sandbox_generation,
    begin_sandbox_reset,
    complete_sandbox_reset,
    consume_session_ticket,
    current_sandbox_generation,
    initialize_control_database,
    issue_session_ticket,
)

TEST_TICKET = "session_ticket_value_with_at_least_32_characters"


def initialize(database: Path) -> None:
    initialize_control_database(database, "installation-test")


def test_session_ticket_is_scoped_and_single_use(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    initialize(database)
    ticket = issue_session_ticket(
        database,
        "capture",
        now=1_000,
        token_factory=lambda: TEST_TICKET,
    )

    grant = consume_session_ticket(database, ticket, now=1_030)

    assert grant.purpose == "capture"
    assert grant.sandbox_generation == 1
    with pytest.raises(InvalidSessionTicketError):
        consume_session_ticket(database, ticket, now=1_031)


def test_expired_and_prior_generation_tickets_are_rejected(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    initialize(database)
    expired = issue_session_ticket(
        database,
        "capture",
        ttl_seconds=60,
        now=1_000,
        token_factory=lambda: TEST_TICKET,
    )
    with pytest.raises(InvalidSessionTicketError):
        consume_session_ticket(database, expired, now=1_061)

    old_generation = issue_session_ticket(
        database,
        "replay",
        now=2_000,
        token_factory=lambda: TEST_TICKET + "_new",
    )
    assert advance_sandbox_generation(database) == 2
    assert current_sandbox_generation(database) == 2
    with pytest.raises(InvalidSessionTicketError):
        consume_session_ticket(database, old_generation, now=2_001)


def test_control_database_cannot_change_installation_identity(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    initialize(database)

    with pytest.raises(ValueError, match="different installation"):
        initialize_control_database(database, "installation-other")


def test_reset_recovery_blocks_tickets_until_completion(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    initialize(database)

    assert begin_sandbox_reset(database) == 2
    with pytest.raises(RuntimeError, match="sandbox is in recovery"):
        issue_session_ticket(database, "capture")
    with pytest.raises(RuntimeError, match="already in recovery"):
        begin_sandbox_reset(database)

    complete_sandbox_reset(database)
    ticket = issue_session_ticket(
        database,
        "replay",
        now=2_000,
        token_factory=lambda: TEST_TICKET,
    )
    assert consume_session_ticket(database, ticket, now=2_001).sandbox_generation == 2