from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

QUEUE_TITLE = "Workflow Feasibility"
QUEUE_SLUG = "WF"
ACTOR_USERNAME = "workflow_actor"
ACTOR_EMAIL = "workflow.actor@example.test"
REVIEW_TITLE = "Review access request"
COMPLETION_TITLE = "Complete access request"


def _write_json_atomically(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ticket_payload(ticket: Any) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "title": ticket.title,
        "status": ticket.status,
        "status_display": ticket.get_status_display(),
        "owner_id": ticket.assigned_to_id,
        "owner": ticket.assigned_to.get_username() if ticket.assigned_to else None,
        "follow_up_count": ticket.followup_set.count(),
        "follow_ups": [
            {
                "id": follow_up.id,
                "comment": follow_up.comment,
                "new_status": follow_up.new_status,
                "user": follow_up.user.get_username() if follow_up.user else None,
            }
            for follow_up in ticket.followup_set.order_by("id")
        ],
    }


def collect_fixture_state() -> dict[str, Any]:
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.models import Session
    from helpdesk.models import Queue, Ticket, TicketDependency

    queue = Queue.objects.get(slug=QUEUE_SLUG)
    actor = get_user_model().objects.get(username=ACTOR_USERNAME)
    review = Ticket.objects.get(queue=queue, title=REVIEW_TITLE)
    completion = Ticket.objects.get(queue=queue, title=COMPLETION_TITLE)
    dependencies = list(
        TicketDependency.objects.filter(ticket__queue=queue)
        .order_by("id")
        .values("id", "ticket_id", "depends_on_id")
    )
    return {
        "queue": {"id": queue.id, "slug": queue.slug, "title": queue.title},
        "actor": {
            "id": actor.id,
            "username": actor.username,
            "is_active": actor.is_active,
            "is_staff": actor.is_staff,
            "is_superuser": actor.is_superuser,
            "has_usable_password": actor.has_usable_password(),
            "permissions": sorted(actor.user_permissions.values_list("codename", flat=True)),
        },
        "review_ticket": _ticket_payload(review),
        "completion_ticket": _ticket_payload(completion),
        "dependencies": dependencies,
        "fixture_ticket_count": Ticket.objects.filter(queue=queue).count(),
        "target_session_count": Session.objects.count(),
    }


def assert_baseline_state(state: dict[str, Any]) -> None:
    from helpdesk.models import Ticket

    review = state["review_ticket"]
    completion = state["completion_ticket"]
    actor = state["actor"]
    assert state["fixture_ticket_count"] == 2
    assert actor["is_active"] and actor["is_staff"] and not actor["is_superuser"]
    assert actor["has_usable_password"] is False
    assert actor["permissions"] == [
        "add_ticket",
        "change_ticket",
        "view_queue",
        "view_ticket",
    ]
    assert review["status"] == Ticket.OPEN_STATUS
    assert completion["status"] == Ticket.OPEN_STATUS
    assert review["owner"] is None and completion["owner"] is None
    assert review["follow_up_count"] == 0 and completion["follow_up_count"] == 0
    assert state["dependencies"] == [
        {
            "id": state["dependencies"][0]["id"],
            "ticket_id": completion["id"],
            "depends_on_id": review["id"],
        }
    ]


def _snapshot_database(database: Path, baseline: Path) -> str:
    baseline.parent.mkdir(parents=True, exist_ok=True)
    temporary = baseline.with_suffix(".sqlite3.tmp")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source:
        with sqlite3.connect(temporary) as destination:
            source.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise RuntimeError("baseline snapshot failed its integrity check")
    os.replace(temporary, baseline)
    return hashlib.sha256(baseline.read_bytes()).hexdigest()


def initialize_fixture(
    *,
    database: Path,
    baseline: Path,
    manifest: Path,
    installation_id: str,
    source_revision: str,
) -> dict[str, Any]:
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Permission
    from django.db import connections, transaction
    from helpdesk.models import Queue, Ticket, TicketDependency

    User = get_user_model()
    with transaction.atomic():
        Queue.objects.filter(slug=QUEUE_SLUG).delete()
        User.objects.filter(username=ACTOR_USERNAME).delete()

        actor = User.objects.create_user(
            username=ACTOR_USERNAME,
            email=ACTOR_EMAIL,
            password=None,
            is_active=True,
            is_staff=True,
            is_superuser=False,
        )
        permissions = Permission.objects.filter(
            content_type__app_label="helpdesk",
            codename__in=("add_ticket", "view_ticket", "change_ticket", "view_queue"),
        )
        if permissions.count() != 4:
            raise RuntimeError("expected Helpdesk permissions are not installed")
        actor.user_permissions.set(permissions)

        queue = Queue.objects.create(
            title=QUEUE_TITLE,
            slug=QUEUE_SLUG,
            email_address="workflow-feasibility@example.test",
            locale="en",
            allow_public_submission=False,
            allow_email_submission=False,
            enable_notifications_on_email_events=False,
        )
        review = Ticket.objects.create(
            title=REVIEW_TITLE,
            queue=queue,
            submitter_email="requester@example.test",
            status=Ticket.OPEN_STATUS,
            description="Fictional review prerequisite for the workflow demo.",
            priority=3,
        )
        completion = Ticket.objects.create(
            title=COMPLETION_TITLE,
            queue=queue,
            submitter_email="requester@example.test",
            status=Ticket.OPEN_STATUS,
            description="Fictional completion task blocked by the review ticket.",
            priority=3,
        )
        TicketDependency.objects.create(ticket=completion, depends_on=review)

    state = collect_fixture_state()
    assert_baseline_state(state)
    connections.close_all()
    baseline_hash = _snapshot_database(database, baseline)
    installation = {
        "schema_version": 1,
        "installation_id": installation_id,
        "source_revision": source_revision,
        "baseline_sha256": baseline_hash,
        "logical_records": {
            "actor": actor.id,
            "review": review.id,
            "completion": completion.id,
        },
    }
    _write_json_atomically(manifest, installation)
    return {"installation": installation, "state": state}