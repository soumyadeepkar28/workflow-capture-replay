from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from workflow_companion.client import CompanionClient
from workflow_protocol import (
    Action,
    ActionRunEvent,
    BusinessOutcome,
    CheckRunEvent,
    ClickAction,
    FillAction,
    HelpdeskVerificationProfile,
    LabelLocator,
    NavigateAction,
    OutcomeRunEvent,
    PageIdentity,
    PhaseRunEvent,
    ReloadAction,
    RoleLocator,
    RunRequestSnapshot,
    SelectAction,
    SetCheckedAction,
)
from workflow_target_state import issue_session_ticket
from workflow_target_state.config import LocalTargetPaths
from workflow_target_state.fixture import ACTOR_USERNAME

TARGET_ORIGIN = "http://127.0.0.1:8765"
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8765
SAFE_FINGERPRINT_VALUE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ReplayExecutionError(RuntimeError):
    def __init__(self, message: str, *, possible_side_effect: bool = False) -> None:
        super().__init__(message)
        self.possible_side_effect = possible_side_effect


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    status: Literal["passed", "failed", "unknown", "not_evaluated"]
    expected: str
    actual: str
    critical: bool
    milestone: bool = False


@dataclass(frozen=True)
class TicketObservation:
    record_ref: Literal["review", "completion"]
    ticket_id: int
    title: str
    open_checked: bool
    resolved_checked: bool
    owner_label: str
    review_note_visible: bool
    completion_note_visible: bool
    dependency_visible: bool | None


def classify_outcome(
    checks: list[CheckResult],
    *,
    outcome_determining_uncertainty: bool = False,
) -> BusinessOutcome:
    required_final_checks = {
        "final_review",
        "final_completion_status",
        "final_completion_note",
        "final_completion_owner",
        "final_dependency",
    }
    if any(check.critical and check.status == "failed" for check in checks):
        return "failed"
    if outcome_determining_uncertainty or any(
        check.critical and check.status == "unknown" for check in checks
    ):
        return "uncertain"
    final_checks = {
        check.check_id: check
        for check in checks
        if check.check_id in required_final_checks
    }
    if required_final_checks == final_checks.keys() and all(
        check.status == "passed" for check in final_checks.values()
    ):
        return "succeeded"
    if any(check.milestone and check.status == "passed" for check in checks):
        return "partially_succeeded"
    return "failed"


def resolve_page_path(identity: PageIdentity, logical_records: dict[str, int]) -> str:
    if identity.target_alias != "helpdesk-demo":
        raise ReplayExecutionError("unsupported target alias")
    if identity.record_ref is not None:
        ticket_id = logical_records.get(identity.record_ref)
        if not isinstance(ticket_id, int):
            raise ReplayExecutionError(
                f"missing fixture mapping for {identity.record_ref}"
            )
        return f"/tickets/{ticket_id}/"
    if not (
        identity.path in {"/tickets/", "/tickets/submit/"}
        or re.fullmatch(r"/tickets/\d+/", identity.path)
    ):
        raise ReplayExecutionError("unsupported target route")
    return identity.path


def resolve_page_url(identity: PageIdentity, logical_records: dict[str, int]) -> str:
    path = resolve_page_path(identity, logical_records)
    return f"{path}?{identity.query}" if identity.query else path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def accessible_name_pattern(name: str) -> re.Pattern[str]:
    words = name.split()
    if not words:
        raise ReplayExecutionError("recorded accessible name is blank")
    decorative = r"[\s\ue000-\uf8ff]*"
    return re.compile(
        r"^" + decorative + r"\s+".join(re.escape(word) for word in words) + decorative + r"$"
    )


def select_option_label(
    action: SelectAction,
    profile: HelpdeskVerificationProfile | None,
) -> str:
    if action.fixture_ref == "actor":
        return profile.actor_username if profile is not None else ACTOR_USERNAME
    return action.option_label


class TargetRuntime:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.paths = LocalTargetPaths.from_project_root(self.project_root)
        self.process: subprocess.Popen[str] | None = None

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(
            self.project_root / "packages" / "target-state" / "src"
        )
        return environment

    def _command(self, command: str) -> None:
        result = subprocess.run(
            [sys.executable, str(self.project_root / "scripts" / "target.py"), command],
            cwd=self.project_root,
            env=self._environment(),
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip())[-1_200:]
            suffix = f": {detail}" if detail else ""
            raise ReplayExecutionError(
                f"target {command} failed with exit code {result.returncode}{suffix}"
            )

    async def prepare(self) -> None:
        await asyncio.to_thread(self._command, "stop")
        await asyncio.to_thread(self._command, "reset")
        log_path = self.project_root / ".local" / "target" / "replay-server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(self.project_root / "scripts" / "target.py"), "serve"],
            cwd=self.project_root,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 15
        async with httpx.AsyncClient(timeout=1) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ReplayExecutionError("local target exited during preparation")
                try:
                    response = await client.get(f"{TARGET_ORIGIN}/_workflow/session/")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        raise ReplayExecutionError("local target did not become ready")

    async def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            await asyncio.to_thread(self._command, "stop")
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 5)

    def logical_records(self) -> dict[str, int]:
        manifest = json.loads(self.paths.manifest.read_text(encoding="utf-8"))
        records = manifest.get("logical_records")
        if not isinstance(records, dict):
            raise ReplayExecutionError("target installation manifest is invalid")
        return {str(key): int(value) for key, value in records.items()}


class HelpdeskBrowser:
    def __init__(
        self,
        context: BrowserContext,
        page: Page,
        snapshot: RunRequestSnapshot,
        logical_records: dict[str, int],
        blocked_requests: list[str],
    ) -> None:
        self.context = context
        self.page = page
        self.snapshot = snapshot
        self.logical_records = logical_records
        self.blocked_requests = blocked_requests

    def ticket_id(self, record_ref: Literal["review", "completion"]) -> int:
        value = self.logical_records.get(record_ref)
        if not isinstance(value, int):
            raise ReplayExecutionError(f"missing target mapping for {record_ref}")
        return value

    async def assert_page_identity(self, identity: PageIdentity) -> None:
        expected_path = resolve_page_path(identity, self.logical_records)
        current = urlsplit(self.page.url)
        if current.scheme + "://" + current.netloc != TARGET_ORIGIN:
            raise ReplayExecutionError("replay browser left the configured target origin")
        if current.path != expected_path:
            raise ReplayExecutionError(
                f"expected page {expected_path}, observed {current.path}"
            )
        if current.query != identity.query:
            raise ReplayExecutionError(
                f"expected page query {identity.query!r}, observed {current.query!r}"
            )
        if identity.record_ref is not None:
            title = (
                "Review access request"
                if identity.record_ref == "review"
                else "Complete access request"
            )
            heading = self.page.get_by_role(
                "heading",
                name=f"{title} #{self.ticket_id(identity.record_ref)}",
                exact=True,
            )
            if await heading.count() != 1 or not await heading.is_visible():
                raise ReplayExecutionError(
                    f"page did not establish {identity.record_ref} ticket identity"
                )

    async def _scope_locator(self, locator: RoleLocator | LabelLocator) -> Page | Locator:
        if locator.scope is None:
            return self.page
        scope_name = locator.scope.name
        if scope_name is not None:
            scope = self.page.get_by_role(locator.scope.role, name=scope_name, exact=True)
        elif locator.scope.element_id is not None:
            if not SAFE_FINGERPRINT_VALUE.fullmatch(locator.scope.element_id):
                raise ReplayExecutionError("unsafe recorded scope fingerprint")
            scope = self.page.locator(f"#{locator.scope.element_id}")
        else:
            raise ReplayExecutionError("recorded scope has no usable identity")
        if await scope.count() != 1:
            raise ReplayExecutionError("recorded semantic scope is missing or ambiguous")
        return scope

    async def _fallback_locator(
        self,
        root: Page | Locator,
        locator: RoleLocator | LabelLocator,
    ) -> Locator:
        fallback = locator.fallback
        if fallback is None:
            raise ReplayExecutionError("semantic locator found no element and has no fallback")
        for value in (fallback.tag, fallback.element_id, fallback.field_name):
            if value is not None and not SAFE_FINGERPRINT_VALUE.fullmatch(value):
                raise ReplayExecutionError("unsafe recorded fallback fingerprint")
        selector = fallback.tag
        if fallback.element_id:
            selector += f"#{fallback.element_id}"
        elif fallback.field_name:
            selector += f'[name="{fallback.field_name}"]'
        candidate = root.locator(selector)
        if fallback.input_type:
            candidate = candidate.and_(root.locator(f'[type="{fallback.input_type}"]'))
        candidate = candidate.filter(visible=True)
        if await candidate.count() != 1:
            raise ReplayExecutionError("recorded fallback is missing or ambiguous")
        return candidate

    async def locate(self, locator: RoleLocator | LabelLocator) -> tuple[Locator, str]:
        root = await self._scope_locator(locator)
        if isinstance(locator, RoleLocator):
            primary = root.get_by_role(
                locator.role,
                name=accessible_name_pattern(locator.name),
            )
            description = f"role={locator.role}, name={locator.name!r}"
        else:
            primary = root.get_by_label(accessible_name_pattern(locator.label))
            description = f"label={locator.label!r}"
        count = await primary.count()
        if count == 1:
            return primary, description
        if count > 1:
            raise ReplayExecutionError(
                f"semantic locator is ambiguous ({count} matches): {description}"
            )
        return await self._fallback_locator(root, locator), f"fallback for {description}"

    async def execute(self, action: Action) -> str:
        blocked_before = len(self.blocked_requests)
        if isinstance(action, NavigateAction):
            url = resolve_page_url(action.destination, self.logical_records)
            await self.page.goto(TARGET_ORIGIN + url, wait_until="domcontentloaded")
            await self.assert_page_identity(action.destination)
            locator_used = f"page={url}"
        elif isinstance(action, ReloadAction):
            await self.assert_page_identity(action.page)
            await self.page.reload(wait_until="domcontentloaded")
            await self.assert_page_identity(action.page)
            locator_used = "page reload"
        else:
            await self.assert_page_identity(action.page)
            target, locator_used = await self.locate(action.locator)
            if isinstance(action, ClickAction):
                try:
                    await target.click(timeout=10_000)
                except Exception as error:
                    possible = (
                        isinstance(action.locator, RoleLocator)
                        and action.locator.role == "button"
                        and action.locator.name == "Update This Ticket"
                    )
                    raise ReplayExecutionError(
                        f"click failed for {locator_used}: {error}",
                        possible_side_effect=possible,
                    ) from error
            elif isinstance(action, FillAction):
                await target.fill(action.value, timeout=10_000)
            elif isinstance(action, SelectAction):
                option = select_option_label(
                    action,
                    self.snapshot.verification_profile,
                )
                await target.select_option(label=option, timeout=10_000)
            elif isinstance(action, SetCheckedAction):
                if action.checked:
                    await target.check(timeout=10_000)
                else:
                    await target.uncheck(timeout=10_000)
            else:
                raise ReplayExecutionError(f"unsupported action: {action.kind}")
        if len(self.blocked_requests) != blocked_before:
            raise ReplayExecutionError(
                f"action attempted a disallowed request: {self.blocked_requests[-1]}",
                possible_side_effect=False,
            )
        return locator_used

    async def observe_ticket(
        self,
        record_ref: Literal["review", "completion"],
    ) -> TicketObservation:
        ticket_id = self.ticket_id(record_ref)
        title = (
            "Review access request" if record_ref == "review" else "Complete access request"
        )
        page = await self.context.new_page()
        try:
            await page.goto(
                f"{TARGET_ORIGIN}/tickets/{ticket_id}/",
                wait_until="domcontentloaded",
                timeout=10_000,
            )
            heading = page.get_by_role(
                "heading",
                name=f"{title} #{ticket_id}",
                exact=True,
            )
            if await heading.count() != 1:
                raise ReplayExecutionError(f"observation found the wrong {record_ref} ticket")
            body = await page.locator("body").inner_text()
            await page.get_by_role("tab", name="Respond", exact=True).click()
            owner = await page.get_by_label("Owner", exact=True).locator("option:checked").inner_text()
            open_control = page.get_by_role("radio", name="Open", exact=True)
            resolved_control = page.get_by_role("radio", name="Resolved", exact=True)
            open_checked = (
                await open_control.is_checked(timeout=2_000)
                if await open_control.count() == 1
                else False
            )
            resolved_checked = (
                await resolved_control.is_checked(timeout=2_000)
                if await resolved_control.count() == 1
                else False
            )
            profile = self.snapshot.verification_profile
            if profile is None:
                raise ReplayExecutionError("ticket observation requires a verification profile")
            return TicketObservation(
                record_ref=record_ref,
                ticket_id=ticket_id,
                title=title,
                open_checked=open_checked,
                resolved_checked=resolved_checked,
                owner_label=owner.strip(),
                review_note_visible=profile.review_note in body,
                completion_note_visible=(
                    profile.completion_note in body
                ),
                dependency_visible=(
                    f"#{self.ticket_id('review')} Review access request" in body
                    if record_ref == "completion"
                    else None
                ),
            )
        finally:
            await page.close()


def target_request_allowed(method: str, url: str) -> bool:
    parsed = urlsplit(url)
    allowed = parsed.scheme == "http" and parsed.netloc == f"{TARGET_HOST}:{TARGET_PORT}"
    if allowed:
        if parsed.path.startswith("/static/") or parsed.path.startswith("/media/"):
            allowed = method == "GET"
        elif parsed.path in {"/_workflow/session/", "/_workflow/session/consume/"}:
            allowed = method in {"GET", "POST"} and not parsed.query
        elif parsed.path == "/tickets/":
            try:
                if len(parsed.query) > 2_000:
                    raise ValueError("ticket-list query is too long")
                query_pairs = parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=30,
                )
                csrf_values = [
                    value
                    for key, value in query_pairs
                    if key == "csrfmiddlewaretoken"
                ]
                if len(csrf_values) > 1 or any(
                    len(value) > 200 for value in csrf_values
                ):
                    raise ValueError("ticket-list CSRF field is invalid")
                recordable_query = urlencode(
                    [
                        (key, value)
                        for key, value in query_pairs
                        if key != "csrfmiddlewaretoken"
                    ]
                )
                PageIdentity(
                    target_alias="helpdesk-demo",
                    path=parsed.path,
                    query=recordable_query,
                )
            except ValueError:
                allowed = False
            else:
                allowed = method == "GET"
        elif parsed.path == "/tickets/submit/":
            allowed = method in {"GET", "POST"} and not parsed.query
        elif re.fullmatch(r"/tickets/\d+/", parsed.path):
            allowed = method in {"GET", "POST"} and not parsed.query
        elif re.fullmatch(r"/tickets/\d+/update/", parsed.path):
            allowed = method == "POST" and not parsed.query
        elif (
            parsed.path.startswith("/datatables_ticket_list/")
            and len(parsed.path) <= 2_000
            and re.fullmatch(r"/[A-Za-z0-9_~%+\-/=]+", parsed.path)
        ):
            allowed = method == "GET"
        else:
            allowed = False
    return allowed


async def _restricted_route(route: Route, blocked_requests: list[str]) -> None:
    request = route.request
    allowed = target_request_allowed(request.method, request.url)
    if allowed:
        await route.continue_()
    else:
        parsed = urlsplit(request.url)
        blocked_requests.append(
            f"{request.method} {parsed.scheme}://{parsed.netloc}{parsed.path}"
        )
        await route.abort("blockedbyclient")


class ReplayExecutor:
    def __init__(
        self,
        client: CompanionClient,
        project_root: Path,
        *,
        headless: bool = False,
        step_delay_seconds: float = 0.9,
        final_hold_seconds: float = 2.0,
    ) -> None:
        if not 0 <= step_delay_seconds <= 3:
            raise ValueError("step delay must be between zero and three seconds")
        if not 0 <= final_hold_seconds <= 5:
            raise ValueError("final hold must be between zero and five seconds")
        self.client = client
        self.project_root = project_root.resolve()
        self.headless = headless
        self.step_delay_seconds = step_delay_seconds
        self.final_hold_seconds = final_hold_seconds
        self.target = TargetRuntime(self.project_root)

    async def _pace(self, seconds: float) -> None:
        if not self.headless and seconds > 0:
            await asyncio.sleep(seconds)

    async def _record_check(self, run_id: str, check: CheckResult) -> None:
        await self.client.record_event(
            run_id,
            CheckRunEvent(
                kind="check",
                check_id=check.check_id,
                status=check.status,
                expected=check.expected,
                actual=check.actual,
                critical=check.critical,
                observed_at=_now(),
            ),
        )

    async def _baseline_checks(self, browser: HelpdeskBrowser) -> list[CheckResult]:
        review = await browser.observe_ticket("review")
        completion = await browser.observe_ticket("completion")
        return [
            CheckResult(
                "baseline_review_open",
                "passed" if review.open_checked and not review.resolved_checked else "failed",
                "Review is Open",
                f"open={review.open_checked}, resolved={review.resolved_checked}",
                True,
            ),
            CheckResult(
                "baseline_completion_open",
                "passed" if completion.open_checked and not completion.resolved_checked else "failed",
                "Completion is Open",
                f"open={completion.open_checked}, resolved={completion.resolved_checked}",
                True,
            ),
            CheckResult(
                "baseline_unassigned",
                "passed"
                if review.owner_label == "Unassigned" and completion.owner_label == "Unassigned"
                else "failed",
                "Both tickets are unassigned",
                f"review={review.owner_label}, completion={completion.owner_label}",
                True,
            ),
            CheckResult(
                "baseline_dependency",
                "passed" if completion.dependency_visible else "failed",
                "Completion depends on Review",
                f"dependency_visible={completion.dependency_visible}",
                True,
            ),
            CheckResult(
                "baseline_notes_absent",
                "passed"
                if not review.review_note_visible and not completion.completion_note_visible
                else "failed",
                "Expected demo notes are absent",
                (
                    f"review_note={review.review_note_visible}, "
                    f"completion_note={completion.completion_note_visible}"
                ),
                True,
            ),
        ]

    async def _review_check(
        self,
        browser: HelpdeskBrowser,
        check_id: str,
    ) -> CheckResult:
        try:
            review = await browser.observe_ticket("review")
        except Exception as error:
            return CheckResult(
                check_id,
                "unknown",
                "Review is Resolved with the prescribed persisted note",
                f"observation failed: {type(error).__name__}: {str(error)[:700]}",
                True,
                milestone=True,
            )
        passed = review.resolved_checked and review.review_note_visible
        return CheckResult(
            check_id,
            "passed" if passed else "failed",
            "Review is Resolved with the prescribed persisted note",
            (
                f"resolved={review.resolved_checked}, "
                f"note_visible={review.review_note_visible}"
            ),
            True,
            milestone=True,
        )

    async def _final_checks(self, browser: HelpdeskBrowser) -> list[CheckResult]:
        try:
            review = await browser.observe_ticket("review")
            completion = await browser.observe_ticket("completion")
        except Exception as error:
            detail = f"observation failed: {type(error).__name__}: {str(error)[:700]}"
            return [
                CheckResult(
                    check_id,
                    "unknown",
                    expected,
                    detail,
                    critical,
                    milestone=milestone,
                )
                for check_id, expected, critical, milestone in (
                    ("final_review", "Review resolved with expected note", False, True),
                    ("final_completion_status", "Completion is Resolved", False, True),
                    ("final_completion_note", "Completion expected note persisted", False, True),
                    ("final_completion_owner", "Completion assigned to workflow_actor", False, True),
                    ("final_dependency", "Completion dependency on Review remains", True, False),
                )
            ]
        profile = browser.snapshot.verification_profile
        return [
            CheckResult(
                "final_review",
                "passed" if review.resolved_checked and review.review_note_visible else "failed",
                "Review resolved with expected note",
                f"resolved={review.resolved_checked}, note={review.review_note_visible}",
                False,
                milestone=True,
            ),
            CheckResult(
                "final_completion_status",
                "passed" if completion.resolved_checked else "failed",
                "Completion is Resolved",
                f"resolved={completion.resolved_checked}",
                False,
                milestone=True,
            ),
            CheckResult(
                "final_completion_note",
                "passed" if completion.completion_note_visible else "failed",
                "Completion expected note persisted",
                f"note_visible={completion.completion_note_visible}",
                False,
                milestone=True,
            ),
            CheckResult(
                "final_completion_owner",
                "passed" if completion.owner_label == profile.actor_username else "failed",
                f"Completion assigned to {profile.actor_username}",
                f"owner={completion.owner_label}",
                False,
                milestone=True,
            ),
            CheckResult(
                "final_dependency",
                "passed" if completion.dependency_visible else "failed",
                "Completion dependency on Review remains",
                f"dependency_visible={completion.dependency_visible}",
                True,
            ),
        ]

    async def _screenshot(self, page: Page, run_id: str, name: str) -> Path:
        directory = self.project_root / ".local" / "artifacts" / "runs" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.png"
        await page.screenshot(path=path, full_page=True)
        kind = {
            "baseline": "baseline",
            "review-checkpoint": "review_checkpoint",
            "final": "final",
            "failure": "failure",
        }[name]
        await self.client.record_artifact(run_id, kind, path)
        return path

    async def execute(self, snapshot: RunRequestSnapshot) -> BusinessOutcome | None:
        run_id = snapshot.run_id
        checks: list[CheckResult] = []
        uncertain = False
        playwright: Playwright | None = None
        context: BrowserContext | None = None
        page: Page | None = None
        interrupted = False
        verified_preset = snapshot.workflow.workflow_mode == "verified_preset"
        try:
            await self.client.record_event(
                run_id,
                PhaseRunEvent(kind="phase", phase="preparing", observed_at=_now()),
            )
            await self.target.prepare()
            logical_records = self.target.logical_records()
            blocked_requests: list[str] = []
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=self.headless)
            context = await browser.new_context(viewport={"width": 1440, "height": 950})
            await context.route("**/*", lambda route: _restricted_route(route, blocked_requests))
            page = await context.new_page()
            session_ticket = issue_session_ticket(
                self.target.paths.control_database,
                "replay",
            )
            await page.goto(
                f"{TARGET_ORIGIN}/_workflow/session/#ticket={session_ticket}",
                wait_until="domcontentloaded",
            )
            await page.wait_for_url(
                f"**/tickets/{logical_records['review']}/",
                timeout=10_000,
            )
            browser_driver = HelpdeskBrowser(
                context,
                page,
                snapshot,
                logical_records,
                blocked_requests,
            )
            if verified_preset:
                checks.extend(await self._baseline_checks(browser_driver))
                for check in checks:
                    await self._record_check(run_id, check)
                if any(check.status != "passed" for check in checks):
                    raise ReplayExecutionError("target baseline verification failed")
            await self._screenshot(page, run_id, "baseline")
            await self._pace(self.step_delay_seconds)

            await self.client.record_event(
                run_id,
                PhaseRunEvent(kind="phase", phase="running", observed_at=_now()),
            )
            for action in snapshot.workflow.actions:
                if (
                    verified_preset
                    and
                    isinstance(action, SetCheckedAction)
                    and action.page.record_ref == "completion"
                    and isinstance(action.locator, RoleLocator)
                    and action.locator.role == "radio"
                    and action.locator.name == "Resolved"
                    and action.checked
                ):
                    prerequisite = await self._review_check(
                        browser_driver,
                        "review_prerequisite_before_completion",
                    )
                    checks.append(prerequisite)
                    await self._record_check(run_id, prerequisite)
                    if prerequisite.status != "passed":
                        raise ReplayExecutionError(
                            "Completion resolution blocked by unresolved Review"
                        )

                await self.client.record_event(
                    run_id,
                    ActionRunEvent(
                        kind="action",
                        action_id=action.action_id,
                        action_sequence=action.sequence,
                        stage="intent",
                        status="pending",
                        observed_at=_now(),
                    ),
                )
                try:
                    locator_used = await browser_driver.execute(action)
                except ReplayExecutionError as error:
                    uncertain = error.possible_side_effect
                    await self.client.record_event(
                        run_id,
                        ActionRunEvent(
                            kind="action",
                            action_id=action.action_id,
                            action_sequence=action.sequence,
                            stage="result",
                            status="uncertain" if error.possible_side_effect else "failed",
                            observed_at=_now(),
                            error_code="action_failed",
                            error_message=str(error)[:500],
                            possible_side_effect=error.possible_side_effect,
                        ),
                    )
                    raise
                await self.client.record_event(
                    run_id,
                    ActionRunEvent(
                        kind="action",
                        action_id=action.action_id,
                        action_sequence=action.sequence,
                        stage="result",
                        status="completed",
                        observed_at=_now(),
                        locator_used=locator_used,
                    ),
                )
                await self._pace(self.step_delay_seconds)
                if (
                    verified_preset
                    and isinstance(action, ClickAction)
                    and action.page.record_ref == "review"
                    and isinstance(action.locator, RoleLocator)
                    and action.locator.role == "button"
                    and action.locator.name == "Update This Ticket"
                ):
                    checkpoint = await self._review_check(
                        browser_driver,
                        "review_persisted_after_save",
                    )
                    checks.append(checkpoint)
                    await self._record_check(run_id, checkpoint)
                    await self._screenshot(page, run_id, "review-checkpoint")
                    if checkpoint.status != "passed":
                        raise ReplayExecutionError("Review save did not persist")

            if verified_preset:
                await self.client.record_event(
                    run_id,
                    PhaseRunEvent(kind="phase", phase="verifying", observed_at=_now()),
                )
                final_checks = await self._final_checks(browser_driver)
                checks.extend(final_checks)
                for check in final_checks:
                    await self._record_check(run_id, check)
            await self._screenshot(page, run_id, "final")
            await self._pace(self.final_hold_seconds)
        except Exception:
            interrupted = True
            if page is not None:
                try:
                    await self._screenshot(page, run_id, "failure")
                    await self._pace(self.final_hold_seconds)
                except Exception:
                    pass
        finally:
            if context is not None:
                await context.close()
            if playwright is not None:
                await playwright.stop()
            await self.target.stop()

        outcome = (
            classify_outcome(
                checks,
                outcome_determining_uncertainty=uncertain,
            )
            if verified_preset
            else None
        )
        first_failed_check = next(
            (check for check in checks if check.status == "failed"),
            None,
        )
        first_unknown_check = next(
            (check for check in checks if check.status == "unknown"),
            None,
        )
        summary = {
            "succeeded": "All required Helpdesk conditions were verified.",
            "partially_succeeded": "At least one intended Helpdesk milestone persisted, but the full outcome did not.",
            "failed": (
                f"{first_failed_check.expected}; observed {first_failed_check.actual}."
                if first_failed_check
                else "The required Helpdesk outcome was not established."
            ),
            "uncertain": (
                f"{first_unknown_check.expected}; observation was {first_unknown_check.actual}."
                if first_unknown_check
                else "A possible target effect could not be resolved from the available evidence."
            ),
        }[outcome] if outcome is not None else None
        if outcome is not None and summary is not None:
            await self.client.record_event(
                run_id,
                OutcomeRunEvent(
                    kind="outcome",
                    outcome=outcome,
                    summary=summary,
                    observed_at=_now(),
                ),
            )
        await self.client.record_event(
            run_id,
            PhaseRunEvent(
                kind="phase",
                phase="interrupted" if interrupted else "finished",
                observed_at=_now(),
            ),
        )
        return outcome