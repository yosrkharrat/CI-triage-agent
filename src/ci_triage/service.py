"""The webhook service: live runs in, checked verdicts out.

    GitHub ──workflow_run──▶ POST /webhook ──▶ runs table ──▶ worker
                                                              │
                             capture ◀────────────────────────┘
                                │
                             triage  (the same agent the eval harness scores)
                                │
                     decide ── posted | dry_run | awaiting_review | no_pr

The handler does nothing slow. It checks the signature, keeps only failed
`workflow_run.completed` events, writes a row and returns inside GitHub's ten
second delivery timeout. A single worker thread does the rest, one run at a
time, which also keeps a free-tier model quota from being hit by a burst.

A live run goes through the same path as the corpus: it is captured to disk
first and triaged from the capture. So a verdict posted on a PR can be replayed
offline with `ci-triage triage` from exactly the fixture it was made from, and a
capture worth keeping can be moved into `fixtures/` and labelled.

What gets posted is decided by `decide`, not by the model, and it is stricter
than the eval's routing on one point: **a verdict whose citations do not all
verify is never posted**, whatever its confidence. The eval reports those as
`unsound_auto_posts` — comments quoting a line that is not in the log. Here that
number is held at zero by construction, and the verdict waits for review instead.

A verdict waiting for review is moved on by a person, through
`POST /runs/{id}/approve` or `/reject`. Approval does not lift the rule above: a
reviewer can vouch for a category or a proposed fix, but not for a quote the log
does not contain, so a verdict with a failed citation can only be rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ci_triage.api import dashboard_router, require_token
from ci_triage.delegate import (
    FIX_BRANCH,
    MODES,
    Attempt,
    PullInfo,
    changed_files,
    check_fixed,
    eligible,
    failed_job_names,
    fix_branch,
    render_brief,
    worth_a_look,
)
from ci_triage.github import (
    AppAuth,
    GitHubClient,
    GitHubError,
    RateLimited,
    load_fixture,
    save_fixture,
)
from ci_triage.guard import ChangedFile, GuardResult, guard
from ci_triage.models import FixPlan, Job, Route, Verdict, WorkflowRun
from ci_triage.store import (
    Delegation,
    DelegationStatus,
    Kind,
    RunRecord,
    Status,
    Store,
)

if TYPE_CHECKING:
    from ci_triage.agent import TriageResult
    from ci_triage.planner import PlanResult
    from ci_triage.sandbox import PatchTrial

log = logging.getLogger("ci_triage.service")

#: Run conclusions worth a triage. `cancelled` is left out: almost every
#: cancelled run was superseded by a newer push, and says nothing about the code.
TRIAGE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

#: How long a run waits after the model's quota ran out, and how many times.
#: Groq's daily cap is a rolling window, so an hour usually buys something back.
QUOTA_BACKOFF = timedelta(minutes=30)
#: How long a run waits after the provider said it is overloaded or down. A
#: 503 "high demand" passes in minutes; failing the run for it loses the run.
OUTAGE_BACKOFF = timedelta(minutes=5)
#: Provider answers that mean "not now", not "not this request".
_OUTAGE_STATUS = frozenset({500, 502, 503, 504, 529})
MAX_ATTEMPTS = 6


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    #: Off unless asked for. Posting on a pull request is visible to everyone
    #: watching it; a misconfigured service should fail quiet, not loud.
    post_comments: bool = False
    model: str | None = None
    db_path: Path = Path(".ci-triage/service.db")
    capture_root: Path = Path(".ci-triage/runs")
    github_token: str | None = None
    app_id: str | None = None
    app_private_key: str | None = field(default=None, repr=False)
    trace: bool = False
    #: Bearer token for the review endpoints. Unset, they are switched off:
    #: approving posts on a pull request, so it must not be open to anyone who
    #: can reach /runs.
    review_token: str | None = field(default=None, repr=False)
    #: `docker` lets the agent reproduce a failure before answering. Off by
    #: default: it runs a stranger's code, if contained, on this machine.
    sandbox: str | None = None
    sandbox_image: str | None = None
    #: Hand an eligible regression to Claude: `off`, `review` (a person
    #: approves each one) or `auto`. Off by default: it gives an agent write
    #: access to a branch of the repository.
    delegate: str = "off"
    delegate_attempts: int = 2
    #: How long a dispatched attempt may go without a CI run to judge it.
    delegate_deadline: timedelta = timedelta(hours=2)
    #: How long a proposal waits for a person before it is dropped as stale.
    proposal_deadline: timedelta = timedelta(hours=24)

    @classmethod
    def from_env(cls) -> Settings:
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            # An unsigned endpoint lets anyone on the internet queue a triage,
            # which spends model quota and posts comments. Refuse to start.
            raise RuntimeError("GITHUB_WEBHOOK_SECRET is not set")
        delegate = (os.environ.get("CI_TRIAGE_DELEGATE") or "off").lower()
        if delegate not in MODES:
            raise RuntimeError(f"CI_TRIAGE_DELEGATE must be one of {', '.join(MODES)}, not {delegate!r}")
        key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
        if key is None and (key_file := os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE")):
            key = Path(key_file).read_text()
        return cls(
            webhook_secret=secret,
            post_comments=os.environ.get("CI_TRIAGE_POST_COMMENTS", "").lower() in {"1", "true", "yes"},
            model=os.environ.get("CI_TRIAGE_MODEL") or None,
            db_path=Path(os.environ.get("CI_TRIAGE_DB", ".ci-triage/service.db")),
            capture_root=Path(os.environ.get("CI_TRIAGE_CAPTURES", ".ci-triage/runs")),
            github_token=os.environ.get("GITHUB_TOKEN") or None,
            app_id=os.environ.get("GITHUB_APP_ID") or None,
            app_private_key=key,
            trace=bool(os.environ.get("LOGFIRE_TOKEN")),
            review_token=os.environ.get("CI_TRIAGE_REVIEW_TOKEN") or None,
            sandbox=os.environ.get("CI_TRIAGE_SANDBOX") or None,
            sandbox_image=os.environ.get("CI_TRIAGE_SANDBOX_IMAGE") or None,
            delegate=delegate,
            delegate_attempts=int(os.environ.get("CI_TRIAGE_DELEGATE_ATTEMPTS") or 2),
            delegate_deadline=timedelta(
                minutes=int(os.environ.get("CI_TRIAGE_DELEGATE_DEADLINE_MINUTES") or 120)
            ),
        )


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Check GitHub's `X-Hub-Signature-256` against the raw request body.

    Must be the raw bytes: re-serialising parsed JSON changes whitespace and key
    order, and the digest with it. Compared in constant time so the endpoint
    does not leak how many leading characters of a forged signature were right.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# --------------------------------------------------------------------------
# What to say, and whether to say it
# --------------------------------------------------------------------------


def comment_marker(run_id: int) -> str:
    """Hidden tag identifying the comment about one run, across its attempts."""
    return f"<!-- ci-triage run:{run_id} -->"


def _fence(text: str) -> str:
    """A code fence longer than any backtick run inside `text`."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


#: Who "approved" a diagnosis posted alongside an automatic delegation. Not a
#: valid GitHub login, so no person can be mistaken for it.
AUTO_REVIEWER = "ci-triage[auto]"


def render_comment(
    run: WorkflowRun,
    v: Verdict,
    *,
    approved_by: str | None = None,
    trail: list[str] | tuple[str, ...] = (),
) -> str:
    """The PR comment for one verdict.

    Every quote is shown with its coordinates, and the footer says why the
    comment exists at all — because each of those quotes was found at the place
    it claims to be. A reader who doubts the verdict can check it in one click.

    Only called for a verdict whose citations all verified, so the evidence
    shown is the verdict's own. A suggested fix only reaches a comment through
    a reviewer, and the footer names them.
    """
    lines = [
        comment_marker(run.id),
        f"### CI triage: **{v.category.value}** (confidence {v.confidence:.2f})",
        "",
        v.summary,
        "",
        f"<details><summary>Evidence — {len(v.evidence)} citation(s), "
        "each verified against the log</summary>",
        "",
    ]
    for ev in v.evidence:
        span = f"line {ev.line_start}" if ev.line_start == ev.line_end else (
            f"lines {ev.line_start}-{ev.line_end}"
        )
        fence = _fence(ev.quote)
        lines += [
            f"**{ev.job_name}** · `{ev.log_path}` {span}",
            f"> {ev.why}",
            "",
            f"{fence}text",
            ev.quote,
            fence,
            "",
        ]
    lines += ["</details>", ""]
    if trail:
        lines += ["<details><summary>How it was diagnosed</summary>", ""]
        lines += [f"{i}. {t}" for i, t in enumerate(trail, 1)]
        lines += ["", "</details>", ""]
    if v.reasoning.strip():
        lines += ["<details><summary>Reasoning</summary>", "", v.reasoning, "", "</details>", ""]
    if v.suggested_fix:
        lines += ["**Suggested fix**", "", v.suggested_fix, ""]
    if approved_by == AUTO_REVIEWER:
        why = "with its fix handed to Claude automatically (`CI_TRIAGE_DELEGATE=auto`)"
    elif approved_by:
        why = f"after review by @{approved_by}"
    else:
        why = "because its confidence cleared the threshold"
    lines.append(
        f"<sub>[run {run.id}, attempt {run.run_attempt}]({run.html_url}) · posted by ci-triage "
        f"{why}, and every quoted line was found where it says it is.</sub>"
    )
    return "\n".join(lines)


def decide(result: TriageResult, *, pulls: list[int], post_comments: bool) -> tuple[Status, str]:
    """Where a verdict goes. Pure, so the policy is testable without GitHub."""
    if not result.evidence_ok:
        bad = [c for c in result.checks if not c.ok]
        return Status.AWAITING_REVIEW, (
            f"{len(bad)} of {len(result.checks)} citation(s) failed verification: {bad[0].reason}"
        )
    if result.route is not Route.AUTO_POST:
        return Status.AWAITING_REVIEW, result.route_reason
    if not pulls:
        return Status.NO_PR, "no open pull request for this commit"
    if not post_comments:
        return Status.DRY_RUN, "CI_TRIAGE_POST_COMMENTS is off"
    return Status.POSTED, result.route_reason


# --------------------------------------------------------------------------
# The side effects, behind one seam
# --------------------------------------------------------------------------


class Target(Protocol):
    """Anything that names a repository and the installation that can reach it:
    a run record or a delegation."""

    @property
    def repo(self) -> str: ...
    @property
    def installation(self) -> int | None: ...
    @property
    def owner_repo(self) -> tuple[str, str]: ...


class Pipeline(Protocol):
    def capture(self, record: RunRecord) -> Path: ...
    def triage(self, fixture: Path) -> TriageResult: ...
    def pulls(self, record: RunRecord, run: WorkflowRun) -> list[int]: ...
    def post(self, record: Target, number: int, body: str, marker: str) -> str: ...
    # Delegation.
    def pull(self, on: Target, number: int) -> PullInfo: ...
    def dispatch(self, on: Target, payload: dict) -> None: ...
    def branch_head(self, on: Target, branch: str) -> str | None: ...
    def open_fix_pr(self, on: Target, *, head: str, base: str, title: str, body: str) -> int: ...
    def get_run(self, on: Target, run_id: int) -> WorkflowRun: ...
    def jobs(self, on: Target, run_id: int) -> list[Job]: ...
    def compare(self, on: Target, base: str, head: str) -> tuple[list[ChangedFile], bool]: ...
    def try_fix(self, on: Target, sha: str, command: str, base: str, head: str) -> PatchTrial | None: ...
    def claude_said(self, on: Target, run_id: int) -> str | None: ...
    def plan(self, on: Target, run: WorkflowRun, verdict: Verdict, fixture: Path) -> PlanResult: ...


class GitHubPipeline:
    """The real thing: GitHub for capture and comments, the agent for the verdict."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.app = (
            AppAuth(settings.app_id, settings.app_private_key)
            if settings.app_id and settings.app_private_key
            else None
        )
        if self.app is None and not settings.github_token:
            raise RuntimeError(
                "no GitHub credentials: set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY(_FILE), "
                "or GITHUB_TOKEN"
            )

    def _client(self, record: Target) -> GitHubClient:
        if self.app is not None and record.installation is not None:
            return GitHubClient(self.app.token(record.installation))
        if self.settings.github_token:
            return GitHubClient(self.settings.github_token)
        raise GitHubError(f"{record.repo}: delivery carried no installation id and no GITHUB_TOKEN is set")

    def capture(self, record: RunRecord) -> Path:
        owner, repo = record.owner_repo
        with self._client(record) as client:
            return save_fixture(
                client, owner, repo, record.run_id, root=self.settings.capture_root, overwrite=True
            )

    def triage(self, fixture: Path) -> TriageResult:
        from ci_triage.agent import MODEL, triage
        from ci_triage.sandbox import make_sandbox

        s = self.settings
        sandbox = make_sandbox(s.sandbox, image=s.sandbox_image) if s.sandbox else None
        return triage(fixture, model=s.model or MODEL, trace=s.trace, sandbox=sandbox)

    def pulls(self, record: RunRecord, run: WorkflowRun) -> list[int]:
        if run.pull_requests:
            return [p.number for p in run.pull_requests]
        owner, repo = record.owner_repo
        with self._client(record) as client:
            return client.open_pulls_for_commit(owner, repo, run.head_sha)

    def post(self, record: Target, number: int, body: str, marker: str) -> str:
        owner, repo = record.owner_repo
        with self._client(record) as client:
            return client.upsert_comment(owner, repo, number, body, marker)

    def pull(self, on: Target, number: int) -> PullInfo:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            return PullInfo.from_api(client.get_pull(owner, repo, number))

    def dispatch(self, on: Target, payload: dict) -> None:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            client.repository_dispatch(owner, repo, DISPATCH_EVENT, payload)

    def branch_head(self, on: Target, branch: str) -> str | None:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            return client.branch_head(owner, repo, branch)

    def open_fix_pr(self, on: Target, *, head: str, base: str, title: str, body: str) -> int:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            # A PR left from a process that died after opening it is reused.
            if existing := client.pulls_for_branch(owner, repo, head, base):
                return existing[0]
            return client.create_pull(owner, repo, head=head, base=base, title=title, body=body)

    def get_run(self, on: Target, run_id: int) -> WorkflowRun:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            return client.get_run(owner, repo, run_id)[0]

    def jobs(self, on: Target, run_id: int) -> list[Job]:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            return client.get_jobs(owner, repo, run_id)[0]

    def compare(self, on: Target, base: str, head: str) -> tuple[list[ChangedFile], bool]:
        owner, repo = on.owner_repo
        with self._client(on) as client:
            payload = client.compare(owner, repo, base, head)
        files = [ChangedFile.from_api(f) for f in payload.get("files") or []]
        # The API reports no "truncated" flag; it just stops at 300 files.
        return files, len(files) >= 300

    def claude_said(self, on: Target, run_id: int) -> str | None:
        """Claude's final message on a fix run, from the annotation the workflow leaves.

        The action hides Claude's messages from the log, and a run's summary page
        has no API, so the fix workflow writes the last message as a notice
        annotation titled `CLAUDE_SAID`. None when there is none.
        """
        owner, repo = on.owner_repo
        with self._client(on) as client:
            for job in client.get_jobs(owner, repo, run_id)[0]:
                for note in client.annotations(owner, repo, job.id):
                    if note.get("title") == CLAUDE_SAID:
                        return str(note.get("message") or "")[:2000] or None
        return None

    def plan(self, on: Target, run: WorkflowRun, verdict: Verdict, fixture: Path) -> PlanResult:
        """Plan the fix, reading the source at the run's commit from GitHub."""
        from ci_triage.agent import MODEL
        from ci_triage.planner import GitHubSource, make_plan
        from ci_triage.tools import TriageContext

        command, files = _brief_inputs(fixture)
        diff = TriageContext(fixture).get_diff()
        owner, repo = on.owner_repo
        with self._client(on) as client:
            source = GitHubSource(client, owner, repo, run.head_sha)
            return make_plan(run, verdict, source, diff=diff, failing_command=command,
                             files=files, model=self.settings.model or MODEL)

    def try_fix(self, on: Target, sha: str, command: str, base: str, head: str) -> PatchTrial | None:
        """Reproduce at the original commit, then with the fix applied (Phase 8).

        None when no sandbox is configured.
        """
        if not self.settings.sandbox:
            return None
        from ci_triage.sandbox import make_sandbox, try_patch

        owner, repo = on.owner_repo
        with self._client(on) as client:
            patch = client.compare_diff(owner, repo, base, head)
        sandbox = make_sandbox(self.settings.sandbox, image=self.settings.sandbox_image)
        return try_patch(sandbox, f"https://github.com/{owner}/{repo}", sha, command, patch)


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------


def _quota_exhausted(exc: BaseException) -> bool:
    from pydantic_ai.exceptions import ModelHTTPError

    return isinstance(exc, ModelHTTPError) and exc.status_code == 429


def _provider_outage(exc: BaseException) -> bool:
    from pydantic_ai.exceptions import ModelHTTPError

    return isinstance(exc, ModelHTTPError) and exc.status_code in _OUTAGE_STATUS


def _until(reset: datetime) -> timedelta:
    """How long to wait for a GitHub rate limit to reset, with a little slack."""
    return max(reset - datetime.now(UTC), timedelta(0)) + timedelta(seconds=5)


def _wait_out(record: RunRecord, exc: BaseException, store: Store) -> Status | None:
    """Defer a run that ran into a limit which resets. None when `exc` is not one."""
    if record.attempts >= MAX_ATTEMPTS:
        return None
    if isinstance(exc, RateLimited):
        log.warning("run %s: GitHub rate limit, retrying at %s", record.id, exc.reset)
        store.defer(record.id, _until(exc.reset), f"GitHub rate limit: {exc}")
        return Status.QUEUED
    if _quota_exhausted(exc):
        log.warning("run %s: model quota exhausted, retrying in %s", record.id, QUOTA_BACKOFF)
        store.defer(record.id, QUOTA_BACKOFF, f"model quota exhausted: {exc}")
        return Status.QUEUED
    if _provider_outage(exc):
        log.warning("run %s: model provider unavailable, retrying in %s", record.id, OUTAGE_BACKOFF)
        store.defer(record.id, OUTAGE_BACKOFF, f"model provider unavailable: {exc}")
        return Status.QUEUED
    return None


def process(record: RunRecord, store: Store, pipeline: Pipeline, settings: Settings) -> Status:
    """Take one claimed run from capture to its final status."""
    if record.kind is Kind.FOLLOW_UP:
        return follow_up(record, store, pipeline, settings)
    try:
        fixture = pipeline.capture(record)
        result = pipeline.triage(fixture)
    except Exception as exc:
        if (waiting := _wait_out(record, exc, store)) is not None:
            return waiting
        log.exception("run %s failed", record.id)
        store.finish(record.id, Status.FAILED, error=f"{type(exc).__name__}: {exc}")
        return Status.FAILED

    run, _, _ = load_fixture(fixture)
    body = render_comment(run, result.verdict, trail=result.trail)

    def done(status: Status, **kw: str | None) -> Status:
        store.finish(
            record.id,
            status,
            fixture=str(fixture),
            verdict=result.verdict.model_dump(mode="json"),
            route=result.route.value,
            evidence_ok=result.evidence_ok,
            comment=body,
            trail=list(result.trail),
            **kw,
        )
        return status

    # Only look for a PR when there is something that may be posted on it; a
    # verdict bound for review should not cost an API call to say so.
    pulls: list[int] = []
    if result.evidence_ok and result.route is Route.AUTO_POST:
        try:
            pulls = pipeline.pulls(record, run)
        except Exception as exc:
            if (waiting := _wait_out(record, exc, store)) is not None:
                return waiting
            return done(Status.FAILED, error=f"finding the PR: {exc}")

    status, reason = decide(result, pulls=pulls, post_comments=settings.post_comments)
    url = None
    if status is Status.POSTED:
        try:
            urls = [pipeline.post(record, n, body, comment_marker(run.id)) for n in pulls]
        except Exception as exc:
            if (waiting := _wait_out(record, exc, store)) is not None:
                return waiting
            log.exception("run %s: posting failed", record.id)
            return done(Status.FAILED, reason=reason, error=f"posting: {exc}")
        url = " ".join(urls)
    log.info("run %s (%s#%s): %s — %s", record.id, record.repo, record.run_id, status.value, reason)
    final = done(status, reason=reason, comment_url=url)

    if settings.delegate != "off":
        try:
            propose(record, run, result, fixture, pulls, store, pipeline, settings)
        except Exception:
            # The diagnosis stands on its own; a delegation that could not be
            # set up is logged, not allowed to fail the run.
            log.exception("run %s: setting up a delegation failed", record.id)
    return final


class ReviewConflict(Exception):
    """A review action that the run's current state does not allow."""


def _post_approved(
    claimed: RunRecord, reviewer: str, store: Store, pipeline: Pipeline, settings: Settings
) -> None:
    """Post a run that is `approved`, and record where it ended up.

    Shared by a reviewer's approval and by the worker picking up an approval
    that was interrupted or rate-limited, so a row left in `approved` has a way
    out. Posting is keyed on a marker, so doing it again edits the comment.
    """
    assert claimed.fixture is not None and claimed.verdict is not None
    run, _, _ = load_fixture(Path(claimed.fixture))
    body = render_comment(run, Verdict.model_validate(claimed.verdict), approved_by=reviewer,
                          trail=claimed.trail)
    reason = (
        "posted with an automatic delegation" if reviewer == AUTO_REVIEWER
        else f"approved by {reviewer}"
    )
    try:
        pulls = pipeline.pulls(claimed, run)
        if not pulls:
            status, reason = Status.NO_PR, f"{reason}; no open pull request for this commit"
            url = None
        elif not settings.post_comments:
            status, reason = Status.DRY_RUN, f"{reason}; CI_TRIAGE_POST_COMMENTS is off"
            url = None
        else:
            status = Status.POSTED
            url = " ".join(pipeline.post(claimed, n, body, comment_marker(run.id)) for n in pulls)
    except RateLimited as exc:
        log.warning("run %s: rate limited posting an approval; again at %s", claimed.id, exc.reset)
        store.defer_approval(claimed.id, exc.reset + timedelta(seconds=5), f"posting: {exc}")
    except Exception as exc:
        log.exception("run %s: posting an approved verdict failed", claimed.id)
        store.settle(claimed.id, Status.FAILED, reason=reason, comment=body, error=f"posting: {exc}")
    else:
        store.settle(claimed.id, status, reason=reason, comment=body, comment_url=url)


def approve(
    record: RunRecord,
    reviewer: str,
    note: str | None,
    store: Store,
    pipeline: Pipeline,
    settings: Settings,
) -> RunRecord:
    """Post a verdict a human has approved, or say why it cannot be.

    The same outcomes as the worker's, with the reviewer standing in for the
    routing policy — and only for it. The evidence rule still holds, and so do
    the dry-run switch and the need for an open pull request.
    """
    if record.status is not Status.AWAITING_REVIEW:
        raise ReviewConflict(f"run {record.id} is {record.status.value}, not awaiting review")
    if not record.evidence_ok or record.verdict is None or record.fixture is None:
        raise ReviewConflict(
            f"run {record.id} cites a line that failed verification; it can be rejected, not posted"
        )
    claimed = store.claim_review(record.id, Status.APPROVED, reviewer=reviewer, note=note)
    if claimed is None:
        raise ReviewConflict(f"run {record.id} was reviewed by someone else first")
    _post_approved(claimed, reviewer, store, pipeline, settings)
    log.info("run %s approved by %s", record.id, reviewer)
    updated = store.get(record.id)
    assert updated is not None
    return updated


def reject(record: RunRecord, reviewer: str, note: str | None, store: Store) -> RunRecord:
    """Close a verdict without posting it.

    A delegation still waiting on this diagnosis is rejected with it: a fix
    built on a diagnosis a person turned down has nothing to stand on.
    """
    rejected = store.claim_review(record.id, Status.REJECTED, reviewer=reviewer, note=note)
    if rejected is None:
        raise ReviewConflict(f"run {record.id} is {record.status.value}, not awaiting review")
    for d in store.delegations(DelegationStatus.PROPOSED, origin_run=record.id):
        store.move(
            d.id, DelegationStatus.PROPOSED, DelegationStatus.REJECTED,
            reviewed_by=reviewer, closed_reason="the diagnosis was rejected",
        )
    log.info("run %s rejected by %s", record.id, reviewer)
    return rejected


# --------------------------------------------------------------------------
# Delegation: hand a regression to Claude, follow it until it is fixed
# --------------------------------------------------------------------------

#: The annotation title the fix workflow gives Claude's final message.
CLAUDE_SAID = "What Claude concluded"

#: The `repository_dispatch` event type the fix workflow listens for.
DISPATCH_EVENT = "ci-triage-fix"

#: The fix workflow's own runs. Never triaged as a CI failure: a finished one
#: is judged as an attempt of the delegation its run name points to.
FIX_WORKFLOW = "ci-triage fix"


def is_fix_run(run: WorkflowRun) -> bool:
    """Whether `run` is the fix workflow's own run.

    By prefix, not equality: the workflow sets a `run-name:` carrying the fix
    branch, and GitHub then reports that run name in `name` as well as in
    `display_title`, so the bare workflow name matches neither.
    """
    return run.event == "repository_dispatch" and any(
        (t or "").startswith(FIX_WORKFLOW) for t in (run.name, run.display_title)
    )


def delegation_marker(delegation: int) -> str:
    return f"<!-- ci-triage delegation:{delegation} -->"


def render_delegation_note(d: Delegation, headline: str, detail: str = "") -> str:
    """The one comment a delegation keeps on the PR, edited as it moves."""
    lines = [delegation_marker(d.id), f"### ci-triage fix: {headline}", ""]
    if detail:
        lines += [detail, ""]
    plan = _plan_of(d)
    if plan is not None and plan.feasible:
        from ci_triage.planner import render_plan

        lines += [
            "<details><summary>The plan Claude was given, checked against the code</summary>",
            "", render_plan(plan), "", "</details>", "",
        ]
    branch = f"`{d.fix_branch}`"
    where = f"draft PR #{d.fix_pr}" if d.fix_pr else f"branch {branch}"
    lines.append(
        f"<sub>Attempt {max(d.attempt, 1)} of {d.max_attempts} · {where} · ci-triage reports a "
        "fix only when the failing jobs pass on the fix branch's latest commit, nothing else "
        "fails, and the diff deletes, skips and reconfigures no test.</sub>"
    )
    return "\n".join(lines)


def _plan_of(d: Delegation) -> FixPlan | None:
    """The delegation's plan, or None when there is none (or it no longer parses)."""
    if not d.plan:
        return None
    try:
        return FixPlan.model_validate(d.plan)
    except ValidationError:
        return None


def _note(d: Delegation, headline: str, detail: str, store: Store, pipeline: Pipeline,
          settings: Settings) -> None:
    """Edit the delegation's PR comment, when posting is on. Never raises."""
    if not settings.post_comments:
        return
    try:
        url = pipeline.post(d, d.pr_number, render_delegation_note(d, headline, detail),
                            delegation_marker(d.id))
        store.note_delegation(d.id, comment_url=url)
    except Exception:
        log.exception("delegation %s: could not update its comment", d.id)


def _origin(d: Delegation, store: Store) -> tuple[WorkflowRun, Verdict, Path] | None:
    rec = store.get(d.origin_run)
    if rec is None or rec.fixture is None or rec.verdict is None:
        return None
    fixture = Path(rec.fixture)
    run, _, _ = load_fixture(fixture)
    return run, Verdict.model_validate(rec.verdict), fixture


def _brief_inputs(fixture: Path) -> tuple[str | None, list[str]]:
    """The failing command and the changed files, both read from the capture."""
    command: str | None = None
    try:
        from ci_triage.tools import TriageContext

        command = TriageContext(fixture).failing_command()
    except Exception:
        log.debug("no failing command for %s", fixture, exc_info=True)
    diff = fixture / "diff.patch"
    files = changed_files(diff.read_text(errors="replace")) if diff.exists() else []
    return command, files


def _attempts(history: list[dict]) -> list[Attempt]:
    return [
        Attempt(
            number=h["attempt"],
            outcome=h["outcome"],
            verdict=Verdict.model_validate(h["verdict"]) if h.get("verdict") else None,
            guard=h.get("guard"),
        )
        for h in history
    ]


def propose(
    record: RunRecord,
    run: WorkflowRun,
    result: TriageResult,
    fixture: Path,
    pulls: list[int],
    store: Store,
    pipeline: Pipeline,
    settings: Settings,
) -> Delegation | None:
    """Open a delegation for a triaged run, when it is eligible.

    `proposed` in review mode, waiting for a person; `approved` and sent at
    once in auto mode. Returns None, with the reason logged, when it is not.
    """
    ok, why = worth_a_look(result)
    if not ok:
        log.info("run %s: not delegated: %s", record.id, why)
        return None
    if not pulls:
        pulls = pipeline.pulls(record, run)
    pull: PullInfo | None = None
    for number in pulls:
        candidate = pipeline.pull(record, number)
        if candidate.state == "open" and candidate.head_sha == run.head_sha:
            pull = candidate
            break
    already = pull is not None and store.open_delegation_on(record.repo, pull.number) is not None
    ok, why = eligible(result, run, pull, mode=settings.delegate, already_open=already)
    if not ok or pull is None or run.workflow_id is None:
        log.info("run %s: not delegated: %s", record.id, why)
        return None

    _, jobs, _ = load_fixture(fixture)
    command, files = _brief_inputs(fixture)
    branch = fix_branch(record.id)
    planned = _plan_for(record, run, result.verdict, fixture, pipeline)
    plan = planned.plan if planned is not None and planned.usable else None
    brief = render_brief(
        run, result.verdict, branch=branch, pr_number=pull.number, failing_command=command,
        files=files, attempt=1, max_attempts=settings.delegate_attempts,
        plan=plan, trail=result.trail,
    )
    auto = settings.delegate == "auto"
    d = store.open_delegation(
        repo=record.repo,
        pr_number=pull.number,
        installation=record.installation,
        origin_run=record.id,
        workflow_id=run.workflow_id,
        failed_jobs=failed_job_names(jobs),
        head_branch=pull.head_ref,
        head_sha=run.head_sha,
        base_branch=pull.base_ref,
        fix_branch=branch,
        brief=brief,
        status=DelegationStatus.APPROVED if auto else DelegationStatus.PROPOSED,
        command=command,
        cited="\n".join(ev.quote for ev in result.verdict.evidence),
        max_attempts=settings.delegate_attempts,
        reviewed_by="auto" if auto else None,
        plan=plan.model_dump(mode="json") if plan is not None else None,
        plan_checks=_plan_checks(planned),
        trail=list(result.trail),
    )
    if d is None:
        log.info("run %s: a delegation is already open on #%s", record.id, pull.number)
        return None
    store.note_delegation(
        d.id, deadline=(datetime.now(UTC) + settings.proposal_deadline).isoformat()
    )
    log.info("run %s: delegation %s %s on #%s", record.id, d.id, d.status.value, pull.number)

    if plan is not None and not plan.feasible:
        # The planner read the code and found no fix within the rules. Say
        # what a person must do, and spend no Claude run finding that out.
        gave = store.move(d.id, d.status, DelegationStatus.GAVE_UP,
                          closed_reason=f"planner: {plan.blocker}")
        log.info("run %s: delegation %s gave up at the plan: %s", record.id, d.id, plan.blocker)
        if gave is None:
            return store.get_delegation(d.id)
        if auto:
            _post_diagnosis(gave, store, pipeline, settings)
        _note(gave, "no code fix, over to you",
              f"ci-triage read the code at `{gave.head_sha[:8]}` and found no fix it may make "
              f"within its rules, so nothing was handed to Claude.\n\n**What a person must do:** "
              f"{plan.blocker}", store, pipeline, settings)
        return store.get_delegation(d.id)

    if auto:
        _post_diagnosis(d, store, pipeline, settings)
        return dispatch(d, store, pipeline, settings)
    return store.get_delegation(d.id)


def _plan_for(
    record: RunRecord, run: WorkflowRun, verdict: Verdict, fixture: Path, pipeline: Pipeline
) -> PlanResult | None:
    """Plan the fix. None when the planner itself failed: the delegation then
    goes ahead with the plain brief, since a missing plan must never block a fix."""
    try:
        planned = pipeline.plan(record, run, verdict, fixture)
    except Exception as exc:
        log.warning("run %s: the planner failed, delegating without a plan: %s", record.id, exc)
        from ci_triage.planner import PlanResult

        return PlanResult(None, dropped=f"the planner failed: {type(exc).__name__}: {exc}"[:500])
    if planned.dropped:
        log.info("run %s: plan dropped: %s", record.id, planned.dropped)
    return planned


def _plan_checks(planned: PlanResult | None) -> dict | None:
    return planned.checks_json() if planned is not None else None


def _post_diagnosis(d: Delegation, store: Store, pipeline: Pipeline, settings: Settings) -> None:
    """Post the diagnosis a delegation in auto mode rests on.

    A regression with a proposed fix waits in review for its comment; in auto
    mode nobody reviews it, so without this the PR would get a note that Claude
    is fixing something it was never told about.
    """
    origin = store.get(d.origin_run)
    if origin is None or origin.status is not Status.AWAITING_REVIEW or not origin.evidence_ok:
        return
    claimed = store.claim_review(origin.id, Status.APPROVED, reviewer=AUTO_REVIEWER,
                                 note="posted with an automatic delegation")
    if claimed is not None:
        _post_approved(claimed, AUTO_REVIEWER, store, pipeline, settings)


def dispatch(d: Delegation, store: Store, pipeline: Pipeline, settings: Settings) -> Delegation:
    """Send an approved (or retrying) delegation to Claude.

    The move to `dispatched` is the claim, made before the send: two callers
    (a reviewer's approval and the worker's housekeeping) cannot both send the
    same attempt. A send GitHub refuses moves it back, to be tried again.
    """
    was = d.status
    claimed = store.move(
        d.id, (DelegationStatus.APPROVED, DelegationStatus.RETRY), DelegationStatus.DISPATCHED,
        attempt=d.attempt + 1,
        deadline=(datetime.now(UTC) + settings.delegate_deadline).isoformat(),
        error=None,
    )
    if claimed is None:
        current = store.get_delegation(d.id)
        assert current is not None
        return current
    payload = {
        "fix_branch": claimed.fix_branch,
        "head_sha": claimed.head_sha,
        "brief": claimed.brief,
        "attempt": claimed.attempt,
        "delegation": claimed.id,
        "pr_number": claimed.pr_number,
    }
    try:
        pipeline.dispatch(claimed, payload)
    except Exception as exc:
        log.exception("delegation %s: dispatch failed", d.id)
        back = store.move(
            d.id, DelegationStatus.DISPATCHED, was, attempt=d.attempt, error=f"dispatch: {exc}"
        )
        return back or claimed
    log.info("delegation %s: attempt %s dispatched", d.id, claimed.attempt)
    _note(
        claimed, f"handed to Claude, attempt {claimed.attempt} of {claimed.max_attempts}",
        f"The diagnosis of this PR's failing run was handed to Claude, which will push a fix to "
        f"`{claimed.fix_branch}`. ci-triage will open it as a draft PR into "
        f"`{claimed.head_branch}` and follow its CI.",
        store, pipeline, settings,
    )
    return claimed


def approve_delegation(
    d: Delegation, reviewer: str, note: str | None, store: Store, pipeline: Pipeline,
    settings: Settings,
) -> Delegation:
    """Approve a proposed delegation, and the diagnosis it rests on with it.

    One approval, not two: the diagnosis of an eligible regression usually
    waits in review for its own comment, and approving the fix vouches for it.
    """
    moved = store.move(d.id, DelegationStatus.PROPOSED, DelegationStatus.APPROVED,
                       reviewed_by=reviewer)
    if moved is None:
        raise ReviewConflict(f"delegation {d.id} is {d.status.value}, not proposed")
    origin = store.get(d.origin_run)
    if origin is not None and origin.status is Status.AWAITING_REVIEW:
        try:
            approve(origin, reviewer, note, store, pipeline, settings)
        except ReviewConflict:
            log.info("delegation %s: its diagnosis was already reviewed", d.id)
    log.info("delegation %s approved by %s", d.id, reviewer)
    return dispatch(moved, store, pipeline, settings)


def reject_delegation(d: Delegation, reviewer: str, note: str | None, store: Store) -> Delegation:
    """Decline a proposed delegation. The diagnosis stays in review."""
    moved = store.move(
        d.id, DelegationStatus.PROPOSED, DelegationStatus.REJECTED,
        reviewed_by=reviewer, closed_reason=note or f"rejected by {reviewer}",
    )
    if moved is None:
        raise ReviewConflict(f"delegation {d.id} is {d.status.value}, not proposed")
    log.info("delegation %s rejected by %s", d.id, reviewer)
    return moved


def _close(d: Delegation, to: DelegationStatus, reason: str, store: Store, pipeline: Pipeline,
           settings: Settings, headline: str | None = None) -> Delegation | None:
    moved = store.move(d.id, d.status, to, closed_reason=reason)
    if moved is not None and headline:
        _note(moved, headline, reason, store, pipeline, settings)
    return moved


def _attempt_failed(
    d: Delegation,
    entry: dict,
    run_id: int,
    run_url: str,
    guard_json: dict | None,
    store: Store,
    pipeline: Pipeline,
    settings: Settings,
) -> str:
    """Record an attempt that did not fix it, then send the next one or give up."""
    outcome = entry["outcome"]
    history = [*d.history, entry]
    if d.attempt < d.max_attempts:
        origin = _origin(d, store)
        brief = d.brief
        if origin is not None:
            o_run, o_verdict, o_fixture = origin
            command, touched = _brief_inputs(o_fixture)
            brief = render_brief(
                o_run, o_verdict, branch=d.fix_branch, pr_number=d.pr_number,
                failing_command=command, files=touched, attempt=d.attempt + 1,
                max_attempts=d.max_attempts, previous=_attempts(history),
                plan=_plan_of(d), trail=d.trail,
            )
        moved = store.move(
            d.id, DelegationStatus.DISPATCHED, DelegationStatus.RETRY,
            history=history, brief=brief, guard_result=guard_json, last_run_id=run_id,
        )
        if moved is not None:
            dispatch(moved, store, pipeline, settings)
        return f"attempt {d.attempt} not fixed ({outcome}); retrying"
    moved = store.move(
        d.id, DelegationStatus.DISPATCHED, DelegationStatus.GAVE_UP,
        history=history, guard_result=guard_json, last_run_id=run_id, closed_reason=outcome,
    )
    if moved is not None:
        _note(
            moved, "gave up, over to you",
            f"{d.max_attempts} attempt(s) did not fix it. The last one: {outcome} "
            f"([run]({run_url})). The diagnosis above still stands; this one needs a person.",
            store, pipeline, settings,
        )
    return f"attempt {d.attempt} not fixed ({outcome}); gave up"


def _fix_workflow_done(
    d: Delegation, run: WorkflowRun, record: RunRecord, store: Store, pipeline: Pipeline,
    settings: Settings,
) -> str:
    """Judge a finished run of the fix workflow itself.

    A failure there (Claude could not start, ran out of turns, could not push)
    is a failed attempt now, not two hours from now. A success that left the
    fix branch where it was means Claude pushed nothing, which it is told to do
    when it cannot fix the failure within the rules: that is a failed attempt
    too, and the run's log holds its reason.
    """
    if run.conclusion == "success":
        head = pipeline.branch_head(d, d.fix_branch)
        before = next((h["sha"] for h in reversed(d.history) if h.get("sha")), d.head_sha)
        if head is not None and head != before:
            return f"attempt {d.attempt}: Claude pushed {head[:8]}; waiting for its CI"
        outcome = "Claude finished without pushing a fix"
    else:
        outcome = f"the fix workflow ended {run.conclusion} before a fix was pushed"
    try:
        said = pipeline.claude_said(d, record.run_id)
    except RateLimited:
        raise
    except Exception:
        log.warning("delegation %s: could not read what Claude concluded", d.id, exc_info=True)
        said = None
    entry = {"attempt": d.attempt, "outcome": outcome, "run_id": record.run_id,
             "html_url": run.html_url}
    if said:
        # Claude's own words: shown to the next attempt, and on the PR. Data
        # from a model, quoted, never acted on by the service.
        entry["claude_said"] = said
        entry["outcome"] = f"{outcome}. It said: {said}"
    return _attempt_failed(d, entry, record.run_id, run.html_url, None, store, pipeline, settings)


def follow_up(record: RunRecord, store: Store, pipeline: Pipeline, settings: Settings) -> Status:
    """Judge a completed run on a fix branch: fixed, another attempt, or give up."""
    d = store.get_delegation(record.delegation) if record.delegation is not None else None

    def checked(reason: str, *, fixture: str | None = None, verdict: dict | None = None) -> Status:
        store.finish(record.id, Status.CHECKED, reason=reason, fixture=fixture, verdict=verdict)
        log.info("follow-up %s: %s", record.id, reason)
        return Status.CHECKED

    if d is None:
        return checked("no delegation")
    if d.status is not DelegationStatus.DISPATCHED:
        return checked(f"delegation {d.id} is {d.status.value}; nothing to judge")

    try:
        run = pipeline.get_run(d, record.run_id)
        if is_fix_run(run):
            return checked(_fix_workflow_done(d, run, record, store, pipeline, settings))
        if run.workflow_id != d.workflow_id:
            return checked("a different workflow; only the one that failed counts")
        if any(h.get("sha") == run.head_sha for h in d.history):
            return checked(f"commit {run.head_sha[:8]} was already judged")
        head = pipeline.branch_head(d, d.fix_branch)
        if run.head_sha != head:
            return checked(f"commit {run.head_sha[:8]} is not the fix branch's latest")
        pull = pipeline.pull(d, d.pr_number)
        if pull.state != "open":
            _close(d, DelegationStatus.CLOSED, f"#{d.pr_number} was closed", store, pipeline, settings)
            return checked(f"#{d.pr_number} is closed")
        if pull.head_sha not in (d.head_sha, head):
            _close(d, DelegationStatus.SUPERSEDED,
                   f"#{d.pr_number} moved on to {pull.head_sha[:8]}; the fix was for "
                   f"{d.head_sha[:8]}", store, pipeline, settings, "superseded")
            return checked("the pull request moved on")

        jobs = pipeline.jobs(d, record.run_id)
        fixed, why = check_fixed(jobs, d.failed_jobs)
        result: GuardResult | None = None
        trial_note: str | None = None
        if fixed:
            files, truncated = pipeline.compare(d, d.head_sha, run.head_sha)
            result = guard(files, truncated=truncated, cited=d.cited or "")
            if result.ok and d.command:
                trial = pipeline.try_fix(d, d.head_sha, d.command, d.head_sha, run.head_sha)
                if trial is not None:
                    trial_note = trial.verdict()
                    if trial.before.ran and not trial.before.passed and not trial.fixed:
                        result = GuardResult(
                            (*result.violations, f"sandbox: {trial_note}"), result.flags, result.files
                        )
                    elif not trial.fixed:
                        result = GuardResult(
                            result.violations, (*result.flags, f"sandbox: {trial_note}"), result.files
                        )
            if result.ok:
                moved = store.move(
                    d.id, DelegationStatus.DISPATCHED, DelegationStatus.FIXED,
                    guard_result=result.to_json(), last_run_id=record.run_id, closed_reason=why,
                )
                if moved is not None:
                    flags = "".join(f"\n- {f}" for f in result.flags)
                    _note(
                        moved, "fix ready",
                        f"[CI passed]({run.html_url}) on `{moved.fix_branch}`: {why}. The diff "
                        f"guard passed{' (sandbox: ' + trial_note + ')' if trial_note else ''}."
                        f"{chr(10) + 'For a reviewer:' + flags if flags else ''}\n\n"
                        f"Review and merge {'draft PR #' + str(moved.fix_pr) if moved.fix_pr else 'it'}"
                        f" into `{moved.head_branch}` if you accept it.",
                        store, pipeline, settings,
                    )
                return checked(f"fixed: {why}")

        # Not fixed: by CI, or by the guard.
        outcome = why if not fixed else "CI passed, but the diff guard failed"
        new_verdict: dict | None = None
        fixture: Path | None = None
        if not fixed and run.conclusion in TRIAGE_CONCLUSIONS:
            fixture = pipeline.capture(record)
            triaged = pipeline.triage(fixture)
            if triaged.evidence_ok:
                new_verdict = triaged.verdict.model_dump(mode="json")
        entry: dict = {
            "attempt": d.attempt,
            "outcome": outcome,
            "run_id": record.run_id,
            "html_url": run.html_url,
            "sha": run.head_sha,
        }
        if new_verdict is not None:
            entry["verdict"] = new_verdict
        if result is not None and not result.ok:
            entry["guard"] = list(result.violations)
        guard_json = result.to_json() if result is not None else None
        status_reason = _attempt_failed(
            d, entry, record.run_id, run.html_url, guard_json, store, pipeline, settings
        )
        return checked(
            status_reason,
            fixture=str(fixture) if fixture else None,
            verdict=new_verdict,
        )
    except Exception as exc:
        if (waiting := _wait_out(record, exc, store)) is not None:
            return waiting
        log.exception("follow-up %s failed", record.id)
        store.finish(record.id, Status.FAILED, error=f"{type(exc).__name__}: {exc}")
        return Status.FAILED


def _deadline_passed(d: Delegation) -> bool:
    return d.deadline is not None and datetime.fromisoformat(d.deadline) <= datetime.now(UTC)


def housekeep(store: Store, pipeline: Pipeline, settings: Settings) -> None:
    """Move open delegations on where no webhook will: on the worker's poll.

    Sends the ones approved but not yet sent, opens the draft PR once Claude
    has pushed, and closes the ones whose PR moved on, closed, or that ran out
    of time. A rate limit stops the round; the next poll picks it up.
    """
    for d in store.delegations((
        DelegationStatus.PROPOSED, DelegationStatus.APPROVED,
        DelegationStatus.DISPATCHED, DelegationStatus.RETRY,
    ), limit=200):
        try:
            _housekeep_one(d, store, pipeline, settings)
        except RateLimited as exc:
            log.warning("housekeeping paused by a GitHub rate limit until %s", exc.reset)
            return
        except Exception:
            log.exception("delegation %s: housekeeping failed", d.id)


def _housekeep_one(d: Delegation, store: Store, pipeline: Pipeline, settings: Settings) -> None:
    if d.status in (DelegationStatus.APPROVED, DelegationStatus.RETRY):
        dispatch(d, store, pipeline, settings)
        return

    pull = pipeline.pull(d, d.pr_number)
    if pull.state != "open":
        _close(d, DelegationStatus.CLOSED,
               f"#{d.pr_number} was {'merged' if pull.merged else 'closed'}", store, pipeline, settings)
        return
    head = pipeline.branch_head(d, d.fix_branch) if d.status is DelegationStatus.DISPATCHED else None
    if pull.head_sha not in (d.head_sha, head):
        _close(d, DelegationStatus.SUPERSEDED,
               f"#{d.pr_number} moved on to {pull.head_sha[:8]}; the fix was for {d.head_sha[:8]}",
               store, pipeline, settings,
               "superseded" if d.status is DelegationStatus.DISPATCHED else None)
        return

    if d.status is DelegationStatus.PROPOSED:
        if _deadline_passed(d):
            _close(d, DelegationStatus.STALE, "nobody approved it in time", store, pipeline, settings)
        return

    # Dispatched. Claude has pushed once the branch exists past the PR head;
    # the draft PR is what gives the user's `pull_request` CI a run to start.
    if d.fix_pr is None and head is not None and head != d.head_sha:
        number = pipeline.open_fix_pr(
            d, head=d.fix_branch, base=d.head_branch,
            title=f"ci-triage: fix for #{d.pr_number}",
            body=(
                f"Claude's fix for the failing CI on #{d.pr_number}, from ci-triage's diagnosis.\n\n"
                f"ci-triage follows this PR's CI and reports it fixed only when the jobs that "
                f"failed ({', '.join(d.failed_jobs) or 'none recorded'}) pass, nothing else "
                "fails, and the diff guard passes. Merge it into "
                f"`{d.head_branch}` if you accept it.\n\n{delegation_marker(d.id)}"
            ),
        )
        store.note_delegation(d.id, fix_pr=number)
        log.info("delegation %s: opened draft PR #%s", d.id, number)
        return
    if _deadline_passed(d):
        _close(d, DelegationStatus.STALE,
               f"no CI run to judge attempt {d.attempt} within {settings.delegate_deadline}",
               store, pipeline, settings, "stale")


class Worker:
    """One background thread draining the queue.

    Woken by the webhook handler when a run arrives, and otherwise every
    `poll` seconds — which is how a run deferred for quota gets picked up again.
    """

    def __init__(self, store: Store, pipeline: Pipeline, settings: Settings, *, poll: float = 60.0):
        self.store = store
        self.pipeline = pipeline
        self.settings = settings
        self.poll = poll
        self.wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="ci-triage-worker", daemon=True)

    def start(self) -> None:
        if n := self.store.requeue_interrupted():
            log.info("requeued %d run(s) interrupted by the last shutdown", n)
        self.repost_interrupted()
        self._thread.start()

    def repost_interrupted(self) -> int:
        """Post the approvals the last process died in the middle of.

        Startup only: nothing else is posting yet, so an `approved` row with no
        retry time is one nobody is working on.
        """
        stuck = self.store.interrupted_approvals()
        for record in stuck:
            log.info("run %s: re-posting an approval interrupted by the last shutdown", record.id)
            _post_approved(record, record.reviewed_by or "a reviewer", self.store, self.pipeline,
                           self.settings)
        return len(stuck)

    def stop(self) -> None:
        self._stop.set()
        self.wake.set()
        self._thread.join(timeout=5)

    def drain(self) -> int:
        """Process every due run. Returns how many were taken."""
        n = 0
        while not self._stop.is_set() and (record := self.store.claim_next()) is not None:
            process(record, self.store, self.pipeline, self.settings)
            n += 1
        while not self._stop.is_set() and (approved := self.store.claim_due_approval()) is not None:
            _post_approved(approved, approved.reviewed_by or "a reviewer", self.store,
                           self.pipeline, self.settings)
            n += 1
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain()
                if self.settings.delegate != "off":
                    housekeep(self.store, self.pipeline, self.settings)
            except Exception:
                # The worker is the only thing doing work; one bad row must not
                # end it. `process` already records per-run failures, so this
                # is the store itself misbehaving.
                log.exception("worker loop error")
            self.wake.wait(self.poll)
            self.wake.clear()


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------


class _Installation(BaseModel):
    id: int


class ReviewRequest(BaseModel):
    #: Shown in the posted comment as an @-mention, so held to the shape of a
    #: GitHub login rather than trusted as free text.
    reviewer: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
    note: str | None = Field(default=None, max_length=2000)


class WorkflowRunEvent(BaseModel):
    action: str
    workflow_run: WorkflowRun
    installation: _Installation | None = None


def create_app(
    settings: Settings | None = None,
    *,
    pipeline: Pipeline | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    worker = Worker(store, pipeline or GitHubPipeline(settings), settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                worker.stop()

    app = FastAPI(title="ci-triage", lifespan=lifespan)
    app.state.store = store
    app.state.worker = worker
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "post_comments": settings.post_comments, "delegate": settings.delegate}

    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_github_event: str = Header(""),
        x_github_delivery: str = Header(""),
        x_hub_signature_256: str | None = Header(None),
    ) -> dict:
        body = await request.body()
        if not verify_signature(settings.webhook_secret, body, x_hub_signature_256):
            raise HTTPException(401, "bad signature")

        if x_github_event == "ping":
            return {"ok": True, "pong": True}
        if x_github_event in ("push", "pull_request") and settings.delegate != "off":
            # Claude pushed, or a PR moved or closed: housekeeping has
            # something to do sooner than its next poll.
            worker.wake.set()
            return {"ok": True, "woke": x_github_event}
        if x_github_event != "workflow_run":
            return {"ok": True, "ignored": f"event {x_github_event!r}"}

        try:
            event = WorkflowRunEvent.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"unexpected payload: {exc}") from None
        run = event.workflow_run
        if event.action != "completed":
            return {"ok": True, "ignored": f"action {event.action!r}"}

        # A run on a fix branch is judged against its delegation, whatever its
        # conclusion, and never triaged as a run of its own.
        if run.head_branch and FIX_BRANCH.match(run.head_branch):
            d = store.delegation_for_branch(run.repository.full_name, run.head_branch)
            if d is None:
                return {"ok": True, "ignored": "fix branch with no open delegation"}
            if run.workflow_id != d.workflow_id:
                return {"ok": True, "ignored": "not the workflow the delegation follows"}
            queued = store.enqueue(
                repo=run.repository.full_name,
                run_id=run.id,
                run_attempt=run.run_attempt,
                html_url=run.html_url,
                installation=event.installation.id if event.installation else None,
                delivery=x_github_delivery or None,
                kind=Kind.FOLLOW_UP,
                delegation=d.id,
            )
            if queued is None:
                return {"ok": True, "duplicate": True}
            worker.wake.set()
            return {"ok": True, "queued": queued, "follow_up": d.id}

        if is_fix_run(run):
            # Its run name carries the fix branch (see examples/ci-triage-fix.yml),
            # which is how a finished attempt finds its delegation.
            m = re.search(r"ci-triage/fix-\d+", run.display_title or "")
            d = store.delegation_for_branch(run.repository.full_name, m.group(0)) if m else None
            if d is None:
                return {"ok": True, "ignored": "the fix workflow itself"}
            queued = store.enqueue(
                repo=run.repository.full_name,
                run_id=run.id,
                run_attempt=run.run_attempt,
                html_url=run.html_url,
                installation=event.installation.id if event.installation else None,
                delivery=x_github_delivery or None,
                kind=Kind.FOLLOW_UP,
                delegation=d.id,
            )
            if queued is None:
                return {"ok": True, "duplicate": True}
            worker.wake.set()
            return {"ok": True, "queued": queued, "fix_workflow": d.id}
        if run.conclusion not in TRIAGE_CONCLUSIONS:
            return {"ok": True, "ignored": f"conclusion {run.conclusion!r}"}

        queued = store.enqueue(
            repo=run.repository.full_name,
            run_id=run.id,
            run_attempt=run.run_attempt,
            html_url=run.html_url,
            installation=event.installation.id if event.installation else None,
            delivery=x_github_delivery or None,
        )
        if queued is None:
            return {"ok": True, "duplicate": True}
        worker.wake.set()
        return {"ok": True, "queued": queued}

    @app.get("/runs")
    def runs(status: Status | None = None, limit: int = 50) -> list[dict]:
        return [r.to_json() for r in store.recent(status, limit=min(limit, 500))]

    @app.get("/runs/{run}")
    def run_detail(run: int) -> dict:
        record = store.get(run)
        if record is None:
            raise HTTPException(404, "no such run")
        out = record.to_json()
        out["delegations"] = [d.to_json() for d in store.delegations(origin_run=run)]
        return out

    @app.get("/delegations")
    def delegations(status: DelegationStatus | None = None, limit: int = 50) -> list[dict]:
        return [d.to_json() for d in store.delegations(status, limit=min(limit, 500))]

    @app.get("/delegations/{delegation}")
    def delegation_detail(delegation: int) -> dict:
        d = store.get_delegation(delegation)
        if d is None:
            raise HTTPException(404, "no such delegation")
        out = d.to_json()
        out["follow_ups"] = [r.to_json() for r in store.follow_ups(d.id)]
        return out

    def _delegation(delegation: int, authorization: str | None) -> Delegation:
        require_token(settings, authorization)
        d = store.get_delegation(delegation)
        if d is None:
            raise HTTPException(404, "no such delegation")
        return d

    @app.post("/delegations/{delegation}/approve")
    def approve_delegation_route(
        delegation: int, review: ReviewRequest, authorization: str | None = Header(None)
    ) -> dict:
        d = _delegation(delegation, authorization)
        try:
            return approve_delegation(
                d, review.reviewer, review.note, store, worker.pipeline, settings
            ).to_json()
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/delegations/{delegation}/reject")
    def reject_delegation_route(
        delegation: int, review: ReviewRequest, authorization: str | None = Header(None)
    ) -> dict:
        d = _delegation(delegation, authorization)
        try:
            return reject_delegation(d, review.reviewer, review.note, store).to_json()
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None

    def _reviewable(run: int, authorization: str | None) -> RunRecord:
        require_token(settings, authorization)
        record = store.get(run)
        if record is None:
            raise HTTPException(404, "no such run")
        return record

    # Plain `def`, so FastAPI runs them on a thread: approving calls GitHub.
    @app.post("/runs/{run}/approve")
    def approve_run(
        run: int, review: ReviewRequest, authorization: str | None = Header(None)
    ) -> dict:
        record = _reviewable(run, authorization)
        try:
            return approve(record, review.reviewer, review.note, store, worker.pipeline, settings).to_json()
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/runs/{run}/reject")
    def reject_run(
        run: int, review: ReviewRequest, authorization: str | None = Header(None)
    ) -> dict:
        record = _reviewable(run, authorization)
        try:
            return reject(record, review.reviewer, review.note, store).to_json()
        except ReviewConflict as exc:
            raise HTTPException(409, str(exc)) from None

    app.include_router(dashboard_router(settings, store))
    return app
