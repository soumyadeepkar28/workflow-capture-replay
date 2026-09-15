from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from helpdesk.models import Queue, Ticket
from workflow_target_state import InvalidSessionTicketError, consume_session_ticket
from workflow_target_state.fixture import (
    ACTOR_USERNAME,
    QUEUE_SLUG,
    REVIEW_TITLE,
)


def _protected_response(response: HttpResponse) -> HttpResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response


@require_GET
@ensure_csrf_cookie
def session_landing(request: HttpRequest) -> HttpResponse:
    return _protected_response(render(request, "workflow_target/session_landing.html"))


@require_POST
def consume_session(request: HttpRequest) -> HttpResponse:
    if request.get_host() != settings.WORKFLOW_TARGET_EXPECTED_HOST:
        return HttpResponseForbidden("Session request rejected.")
    if request.headers.get("Origin") != settings.WORKFLOW_TARGET_ORIGIN:
        return HttpResponseForbidden("Session request rejected.")

    ticket = request.POST.get("ticket", "")
    try:
        grant = consume_session_ticket(
            Path(settings.WORKFLOW_TARGET_CONTROL_DATABASE),
            ticket,
        )
    except InvalidSessionTicketError:
        return HttpResponseForbidden("Session ticket is invalid or expired.")

    actor = get_user_model().objects.get(username=ACTOR_USERNAME)
    if not actor.is_active or not actor.is_staff or actor.is_superuser:
        return HttpResponseForbidden("Demo actor is not available.")

    queue = Queue.objects.get(slug=QUEUE_SLUG)
    review = Ticket.objects.get(queue=queue, title=REVIEW_TITLE)
    login(request, actor, backend="django.contrib.auth.backends.ModelBackend")
    request.session["workflow_session_purpose"] = grant.purpose
    request.session["workflow_sandbox_generation"] = grant.sandbox_generation

    return _protected_response(
        redirect(reverse("helpdesk:view", kwargs={"ticket_id": review.id}))
    )