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
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ci_triage.api import dashboard_router, require_token
from ci_triage.github import AppAuth, GitHubClient, GitHubError, load_fixture, save_fixture
from ci_triage.models import Route, Verdict, WorkflowRun
from ci_triage.store import RunRecord, Status, Store

if TYPE_CHECKING:
    from ci_triage.agent import TriageResult

log = logging.getLogger("ci_triage.service")

#: Run conclusions worth a triage. `cancelled` is left out: almost every
#: cancelled run was superseded by a newer push, and says nothing about the code.
TRIAGE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

#: How long a run waits after the model's quota ran out, and how many times.
#: Groq's daily cap is a rolling window, so an hour usually buys something back.
QUOTA_BACKOFF = timedelta(minutes=30)
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

    @classmethod
    def from_env(cls) -> Settings:
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            # An unsigned endpoint lets anyone on the internet queue a triage,
            # which spends model quota and posts comments. Refuse to start.
            raise RuntimeError("GITHUB_WEBHOOK_SECRET is not set")
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


def render_comment(run: WorkflowRun, v: Verdict, *, approved_by: str | None = None) -> str:
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
    if v.suggested_fix:
        lines += ["**Suggested fix**", "", v.suggested_fix, ""]
    why = (
        f"after review by @{approved_by}"
        if approved_by
        else "because its confidence cleared the threshold"
    )
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


class Pipeline(Protocol):
    def capture(self, record: RunRecord) -> Path: ...
    def triage(self, fixture: Path) -> TriageResult: ...
    def pulls(self, record: RunRecord, run: WorkflowRun) -> list[int]: ...
    def post(self, record: RunRecord, number: int, body: str, marker: str) -> str: ...


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

    def _client(self, record: RunRecord) -> GitHubClient:
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

    def post(self, record: RunRecord, number: int, body: str, marker: str) -> str:
        owner, repo = record.owner_repo
        with self._client(record) as client:
            return client.upsert_comment(owner, repo, number, body, marker)


# --------------------------------------------------------------------------
# The worker
# --------------------------------------------------------------------------


def _quota_exhausted(exc: BaseException) -> bool:
    from pydantic_ai.exceptions import ModelHTTPError

    return isinstance(exc, ModelHTTPError) and exc.status_code == 429


def process(record: RunRecord, store: Store, pipeline: Pipeline, settings: Settings) -> Status:
    """Take one claimed run from capture to its final status."""
    try:
        fixture = pipeline.capture(record)
        result = pipeline.triage(fixture)
    except Exception as exc:
        if _quota_exhausted(exc) and record.attempts < MAX_ATTEMPTS:
            log.warning("run %s: model quota exhausted, retrying in %s", record.id, QUOTA_BACKOFF)
            store.defer(record.id, QUOTA_BACKOFF, f"model quota exhausted: {exc}")
            return Status.QUEUED
        log.exception("run %s failed", record.id)
        store.finish(record.id, Status.FAILED, error=f"{type(exc).__name__}: {exc}")
        return Status.FAILED

    run, _, _ = load_fixture(fixture)
    body = render_comment(run, result.verdict)

    def done(status: Status, **kw: str | None) -> Status:
        store.finish(
            record.id,
            status,
            fixture=str(fixture),
            verdict=result.verdict.model_dump(mode="json"),
            route=result.route.value,
            evidence_ok=result.evidence_ok,
            comment=body,
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
            return done(Status.FAILED, error=f"finding the PR: {exc}")

    status, reason = decide(result, pulls=pulls, post_comments=settings.post_comments)
    url = None
    if status is Status.POSTED:
        try:
            urls = [pipeline.post(record, n, body, comment_marker(run.id)) for n in pulls]
        except Exception as exc:
            log.exception("run %s: posting failed", record.id)
            return done(Status.FAILED, reason=reason, error=f"posting: {exc}")
        url = " ".join(urls)
    log.info("run %s (%s#%s): %s — %s", record.id, record.repo, record.run_id, status.value, reason)
    return done(status, reason=reason, comment_url=url)


class ReviewConflict(Exception):
    """A review action that the run's current state does not allow."""


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

    run, _, _ = load_fixture(Path(record.fixture))
    body = render_comment(run, Verdict.model_validate(record.verdict), approved_by=reviewer)
    reason = f"approved by {reviewer}"
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
    except Exception as exc:
        log.exception("run %s: posting an approved verdict failed", record.id)
        store.settle(record.id, Status.FAILED, reason=reason, comment=body, error=f"posting: {exc}")
    else:
        store.settle(record.id, status, reason=reason, comment=body, comment_url=url)
    log.info("run %s approved by %s", record.id, reviewer)
    updated = store.get(record.id)
    assert updated is not None
    return updated


def reject(record: RunRecord, reviewer: str, note: str | None, store: Store) -> RunRecord:
    """Close a verdict without posting it."""
    rejected = store.claim_review(record.id, Status.REJECTED, reviewer=reviewer, note=note)
    if rejected is None:
        raise ReviewConflict(f"run {record.id} is {record.status.value}, not awaiting review")
    log.info("run %s rejected by %s", record.id, reviewer)
    return rejected


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
        self._thread.start()

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
        return n

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain()
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
        return {"ok": True, "post_comments": settings.post_comments}

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
        if x_github_event != "workflow_run":
            return {"ok": True, "ignored": f"event {x_github_event!r}"}

        try:
            event = WorkflowRunEvent.model_validate(json.loads(body))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"unexpected payload: {exc}") from None
        run = event.workflow_run
        if event.action != "completed":
            return {"ok": True, "ignored": f"action {event.action!r}"}
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
        return record.to_json()

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
