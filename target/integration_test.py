from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import django

from workflow_target_state import initialize_control_database, issue_session_ticket


def csrf_post(client, ticket: str, *, origin: str):
    landing = client.get("/_workflow/session/", HTTP_HOST="127.0.0.1:8765")
    assert landing.status_code == 200
    assert "_auth_user_id" not in client.session
    csrf_token = landing.cookies["workflow_helpdesk_csrftoken"].value
    return client.post(
        "/_workflow/session/consume/",
        {"ticket": ticket, "csrfmiddlewaretoken": csrf_token},
        HTTP_HOST="127.0.0.1:8765",
        HTTP_ORIGIN=origin,
    )


def main() -> None:
    from django.conf import settings
    from django.test import Client

    baseline = Path(os.environ["WORKFLOW_TARGET_BASELINE"])
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        database = temporary / "target.sqlite3"
        control = temporary / "control.sqlite3"
        shutil.copy2(baseline, database)
        os.environ["WORKFLOW_TARGET_DATABASE"] = str(database)
        os.environ["WORKFLOW_TARGET_CONTROL_DATABASE"] = str(control)
        django.setup()

        initialize_control_database(control, "integration-test")
        capture_ticket = issue_session_ticket(control, "capture")
        replay_ticket = issue_session_ticket(control, "replay")

        capture_client = Client(enforce_csrf_checks=True)
        capture_response = csrf_post(
            capture_client,
            capture_ticket,
            origin=settings.WORKFLOW_TARGET_ORIGIN,
        )
        assert capture_response.status_code == 302
        assert capture_response.headers["Location"].startswith("/tickets/")
        assert capture_client.session["workflow_session_purpose"] == "capture"

        from helpdesk.models import Queue

        queue = Queue.objects.get(slug="WF")
        csrf_token = capture_client.cookies[settings.CSRF_COOKIE_NAME].value
        created = capture_client.post(
            "/tickets/submit/",
            {
                "csrfmiddlewaretoken": csrf_token,
                "queue": str(queue.id),
                "title": "Integration-created ticket",
                "body": "Created by the bounded staff workflow integration test.",
                "priority": "3",
                "submitter_email": "requester@example.test",
                "assigned_to": "",
                "due_date": "",
            },
            HTTP_HOST="127.0.0.1:8765",
            HTTP_ORIGIN=settings.WORKFLOW_TARGET_ORIGIN,
        )
        assert created.status_code == 302
        assert created.headers["Location"].startswith("/tickets/")
        created_page = capture_client.get(
            created.headers["Location"],
            HTTP_HOST="127.0.0.1:8765",
        )
        assert created_page.status_code == 200
        assert b"Integration-created ticket" in created_page.content

        reused_client = Client(enforce_csrf_checks=True)
        assert csrf_post(
            reused_client,
            capture_ticket,
            origin=settings.WORKFLOW_TARGET_ORIGIN,
        ).status_code == 403

        replay_client = Client(enforce_csrf_checks=True)
        replay_response = csrf_post(
            replay_client,
            replay_ticket,
            origin=settings.WORKFLOW_TARGET_ORIGIN,
        )
        assert replay_response.status_code == 302
        assert replay_client.session["workflow_session_purpose"] == "replay"
        assert (
            capture_client.cookies[settings.SESSION_COOKIE_NAME].value
            != replay_client.cookies[settings.SESSION_COOKIE_NAME].value
        )

        wrong_origin_ticket = issue_session_ticket(control, "capture")
        wrong_origin_client = Client(enforce_csrf_checks=True)
        assert csrf_post(
            wrong_origin_client,
            wrong_origin_ticket,
            origin="https://example.invalid",
        ).status_code == 403
        assert csrf_post(
            wrong_origin_client,
            wrong_origin_ticket,
            origin=settings.WORKFLOW_TARGET_ORIGIN,
        ).status_code == 302

    print(
        "PASS: GET grants nothing; CSRF/origin, single-use, separate sessions, "
        "and least-privilege ticket creation verified."
    )


if __name__ == "__main__":
    main()