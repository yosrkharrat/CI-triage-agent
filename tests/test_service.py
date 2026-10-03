"""Tests for the webhook service.

No GitHub and no model: the pipeline is a fake that hands back a prepared
verdict, and the "captured" run is the one fixture tracked in git. What is pinned
here is the part a live deployment cannot be allowed to get wrong: that an
unsigned request is refused, that a redelivered webhook does nothing, and above
all that a verdict resting on a citation that does not verify is never posted,
however confident it is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ci_triage.agent import EvidenceCheck, TriageResult
from ci_triage.models import Evidence, FailureCategory, Route, Verdict, WorkflowRun
from ci_triage.service import (
    ReviewConflict,
    Settings,
    _fence,
    approve,
    comment_marker,
    create_app,
    decide,
    render_comment,
    verify_signature,
)
from ci_triage.store import RunRecord, Status, Store

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")
SECRET = "s3cret"
TOKEN = "r3view"


def _result(*, ok: bool = True, confidence: float = 0.9, fix: str | None = None) -> TriageResult:
    ev = Evidence(
        job_name="build (ubuntu-22.04)",
        log_path="2_build (ubuntu-22.04).txt",
        line_start=10,
        line_end=12,
        quote="Error: Resource not accessible by integration",
        why="the release step was refused write access",
    )
    verdict = Verdict(
        category=FailureCategory.INFRA,
        confidence=confidence,
        summary="The workflow lacks contents: write.",
        reasoning="r",
        evidence=[ev],
        suggested_fix=fix,
    )
    from ci_triage.models import route

    dest, reason = route(verdict)
    check = EvidenceCheck(ev, ok, "ok" if ok else "quote does not appear within the cited lines")
    return TriageResult(verdict, dest, reason, (check,))


class FakePipeline:
    def __init__(self, result: TriageResult | Exception, pulls: list[int] | None = None):
        self.result = result
        self._pulls = [7] if pulls is None else pulls
        self.posted: list[tuple[int, str, str]] = []

    def capture(self, record: RunRecord) -> Path:
        return FIXTURE

    def triage(self, fixture: Path) -> TriageResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def pulls(self, record: RunRecord, run: WorkflowRun) -> list[int]:
        return self._pulls

    def post(self, record: RunRecord, number: int, body: str, marker: str) -> str:
        self.posted.append((number, body, marker))
        return f"https://github.com/o/r/pull/{number}#issuecomment-1"


def _payload(**run_overrides) -> dict:
    run = json.loads((FIXTURE / "run.json").read_text())
    run.update(run_overrides)
    return {"action": "completed", "workflow_run": run, "installation": {"id": 42}}


def _post(client: TestClient, payload: dict, *, event: str = "workflow_run", secret: str = SECRET,
          delivery: str = "d-1"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "X-GitHub-Delivery": delivery},
    )


@pytest.fixture
def make(tmp_path: Path):
    def _make(pipeline: FakePipeline, *, post_comments: bool = True, review_token: str | None = TOKEN):
        settings = Settings(
            webhook_secret=SECRET,
            post_comments=post_comments,
            db_path=tmp_path / "db.sqlite",
            review_token=review_token,
        )
        app = create_app(settings, pipeline=pipeline, start_worker=False)
        return TestClient(app), app.state.store, app.state.worker

    return _make


# -- the door --------------------------------------------------------------


def test_signature_must_match_the_raw_body():
    body = b'{"a": 1}'
    good = "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert verify_signature("k", body, good)
    assert not verify_signature("k", b'{"a":1}', good)  # same JSON, different bytes
    assert not verify_signature("other", body, good)
    assert not verify_signature("k", body, None)
    assert not verify_signature("k", body, good.removeprefix("sha256="))


def test_unsigned_request_is_refused_and_queues_nothing(make):
    client, store, _ = make(FakePipeline(_result()))
    assert _post(client, _payload(), secret="wrong").status_code == 401
    assert store.recent() == []


def test_settings_refuse_to_start_without_a_webhook_secret(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        Settings.from_env()


def test_ping_is_answered(make):
    client, _, _ = make(FakePipeline(_result()))
    assert _post(client, {"zen": "hi"}, event="ping").json()["pong"] is True


@pytest.mark.parametrize(
    ("event", "payload", "why"),
    [
        ("push", {}, "event"),
        ("workflow_run", {"action": "requested"}, "action"),
        ("workflow_run", {"conclusion": "success"}, "conclusion"),
        ("workflow_run", {"conclusion": "cancelled"}, "conclusion"),
    ],
)
def test_irrelevant_deliveries_are_ignored(make, event, payload, why):
    client, store, _ = make(FakePipeline(_result()))
    body = _payload(**{k: v for k, v in payload.items() if k != "action"})
    body["action"] = payload.get("action", "completed")
    r = _post(client, body, event=event)
    assert r.status_code == 200
    assert why in r.json()["ignored"]
    assert store.recent() == []


def test_a_redelivered_webhook_is_a_no_op(make):
    client, store, _ = make(FakePipeline(_result()))
    first = _post(client, _payload(), delivery="d-1").json()
    again = _post(client, _payload(), delivery="d-2").json()
    assert "queued" in first
    assert again["duplicate"] is True
    assert len(store.recent()) == 1


def test_a_rerun_attempt_is_a_new_triage(make):
    client, store, _ = make(FakePipeline(_result()))
    _post(client, _payload(run_attempt=1))
    _post(client, _payload(run_attempt=2))
    assert len(store.recent()) == 2


# -- the policy ------------------------------------------------------------


def test_unverified_citation_is_never_posted_even_when_confident():
    result = _result(ok=False, confidence=0.99)
    assert result.route is Route.AUTO_POST  # the eval's routing would post it
    status, reason = decide(result, pulls=[7], post_comments=True)
    assert status is Status.AWAITING_REVIEW
    assert "failed verification" in reason


@pytest.mark.parametrize(
    ("result", "pulls", "post", "want"),
    [
        (_result(confidence=0.4), [7], True, Status.AWAITING_REVIEW),
        (_result(fix="add permissions: contents: write"), [7], True, Status.AWAITING_REVIEW),
        (_result(), [], True, Status.NO_PR),
        (_result(), [7], False, Status.DRY_RUN),
        (_result(), [7], True, Status.POSTED),
    ],
)
def test_decide(result, pulls, post, want):
    assert decide(result, pulls=pulls, post_comments=post)[0] is want


# -- end to end through the worker ----------------------------------------


def test_posts_a_sound_confident_verdict(make):
    pipeline = FakePipeline(_result())
    client, store, worker = make(pipeline)
    rid = _post(client, _payload()).json()["queued"]
    assert worker.drain() == 1

    record = store.get(rid)
    assert record.status is Status.POSTED
    assert record.comment_url.endswith("issuecomment-1")
    assert record.verdict["category"] == "infra"
    [(number, body, marker)] = pipeline.posted
    assert number == 7
    assert marker == comment_marker(30005725094) and body.startswith(marker)

    # And the review API shows it.
    assert client.get(f"/runs/{rid}").json()["status"] == "posted"
    assert [r["id"] for r in client.get("/runs?status=posted").json()] == [rid]


def test_dry_run_records_the_comment_it_would_have_posted(make):
    pipeline = FakePipeline(_result())
    client, store, worker = make(pipeline, post_comments=False)
    rid = _post(client, _payload()).json()["queued"]
    worker.drain()
    record = store.get(rid)
    assert record.status is Status.DRY_RUN
    assert pipeline.posted == []
    assert "Resource not accessible" in record.comment


def test_unsound_verdict_waits_for_review_and_posts_nothing(make):
    pipeline = FakePipeline(_result(ok=False, confidence=0.99))
    client, store, worker = make(pipeline)
    rid = _post(client, _payload()).json()["queued"]
    worker.drain()
    assert store.get(rid).status is Status.AWAITING_REVIEW
    assert store.get(rid).evidence_ok is False
    assert pipeline.posted == []


def test_a_failed_triage_is_recorded_not_raised(make):
    client, store, worker = make(FakePipeline(RuntimeError("boom")))
    rid = _post(client, _payload()).json()["queued"]
    worker.drain()
    record = store.get(rid)
    assert record.status is Status.FAILED
    assert "boom" in record.error


def test_an_exhausted_quota_defers_rather_than_fails(make):
    from pydantic_ai.exceptions import ModelHTTPError

    quota = ModelHTTPError(429, "groq:openai/gpt-oss-120b", {"error": {"message": "TPD"}})
    client, store, worker = make(FakePipeline(quota))
    rid = _post(client, _payload()).json()["queued"]
    worker.drain()
    record = store.get(rid)
    assert record.status is Status.QUEUED
    assert record.not_before is not None
    assert store.claim_next() is None  # not due yet


def test_a_run_interrupted_mid_triage_is_requeued(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    store.enqueue(repo="o/r", run_id=1, run_attempt=1, html_url="u")
    assert store.claim_next() is not None
    assert store.claim_next() is None
    assert Store(tmp_path / "db.sqlite").requeue_interrupted() == 1
    assert store.claim_next() is not None


def test_a_deferred_run_comes_back_when_due(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    rid = store.enqueue(repo="o/r", run_id=1, run_attempt=1, html_url="u")
    store.claim_next()
    store.defer(rid, timedelta(seconds=-1), "quota")
    again = store.claim_next()
    assert again is not None and again.attempts == 2


# -- the comment -----------------------------------------------------------


def test_comment_quotes_each_citation_with_its_coordinates():
    run = WorkflowRun.model_validate_json((FIXTURE / "run.json").read_text())
    body = render_comment(run, _result().verdict)
    assert "**infra**" in body and "0.90" in body
    assert "`2_build (ubuntu-22.04).txt` lines 10-12" in body
    assert "Resource not accessible by integration" in body
    assert run.html_url in body


def test_a_quote_containing_backticks_cannot_break_out_of_its_fence():
    assert _fence("plain") == "```"
    assert _fence("a ``` b") == "````"
    verdict = _result().verdict
    ev = verdict.evidence[0].model_copy(update={"quote": "```\n## injected heading"})
    body = render_comment(
        WorkflowRun.model_validate_json((FIXTURE / "run.json").read_text()),
        verdict.model_copy(update={"evidence": [ev]}),
    )
    assert "````text\n```\n## injected heading\n````" in body


# -- the review gate -------------------------------------------------------


def _awaiting(make, result: TriageResult, **kw):
    """A run that went through the worker and was routed to a human."""
    pipeline = FakePipeline(result)
    client, store, worker = make(pipeline, **kw)
    rid = _post(client, _payload()).json()["queued"]
    worker.drain()
    assert store.get(rid).status is Status.AWAITING_REVIEW
    return client, store, pipeline, rid


def _review(client: TestClient, rid: int, action: str, *, token: str = TOKEN, **body):
    return client.post(
        f"/runs/{rid}/{action}",
        json={"reviewer": "octocat", **body},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_approving_a_proposed_fix_posts_it_and_names_the_reviewer(make):
    client, store, pipeline, rid = _awaiting(make, _result(fix="add `permissions: contents: write`"))
    r = _review(client, rid, "approve", note="checked the workflow file")
    assert r.status_code == 200
    assert r.json()["status"] == "posted"

    [(number, body, marker)] = pipeline.posted
    assert number == 7 and body.startswith(marker)
    assert "**Suggested fix**" in body and "contents: write" in body
    assert "after review by @octocat" in body

    record = store.get(rid)
    assert record.reviewed_by == "octocat"
    assert record.review_note == "checked the workflow file"
    assert record.verdict["category"] == "infra"  # the verdict survives the review
    assert record.comment == body


def test_a_reviewer_cannot_vouch_for_a_quote_that_is_not_in_the_log(make):
    client, store, pipeline, rid = _awaiting(make, _result(ok=False, confidence=0.99))
    r = _review(client, rid, "approve")
    assert r.status_code == 409
    assert "rejected, not posted" in r.json()["detail"]
    assert pipeline.posted == []
    assert store.get(rid).status is Status.AWAITING_REVIEW

    assert _review(client, rid, "reject", note="invented quote").json()["status"] == "rejected"


def test_a_run_is_reviewed_once(make):
    client, _, pipeline, rid = _awaiting(make, _result(confidence=0.4))
    assert _review(client, rid, "approve").status_code == 200
    assert _review(client, rid, "approve").status_code == 409
    assert _review(client, rid, "reject").status_code == 409
    assert len(pipeline.posted) == 1


# -- concurrency -------------------------------------------------------------
#
# Every racer below gets its own `Store`, and so its own lock: nothing in Python
# serialises them, and whatever holds the line is SQLite's. A test that shared
# one `Store` across threads would pass on `Store._lock` alone and prove nothing
# about a second process, or a second host.


def _race(calls: list[Callable[[], object]]) -> list[object]:
    """Start every call at the same moment; return what each returned or raised."""
    start = threading.Barrier(len(calls))
    out: list[object] = [None] * len(calls)

    def run(i: int) -> None:
        start.wait()
        try:
            out[i] = calls[i]()
        except Exception as exc:
            out[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return out


def test_two_reviewers_approving_at_the_same_moment_post_once(make):
    client, store, pipeline, rid = _awaiting(make, _result(confidence=0.4))
    settings = client.app.state.settings
    # Both start from the same read of the run, as two people with the same page
    # open would — so both pass `approve`'s own status check, and only the
    # compare-and-set in `claim_review` stands between them and a second post.
    snapshot = store.get(rid)
    reviewers = ["alice", "bob"]
    outcomes = _race([
        lambda who=who: approve(snapshot, who, None, Store(store.path), pipeline, settings)
        for who in reviewers
    ])

    won = [who for who, o in zip(reviewers, outcomes, strict=True) if isinstance(o, RunRecord)]
    lost = [o for o in outcomes if isinstance(o, ReviewConflict)]
    assert len(won) == 1 and len(lost) == 1, outcomes
    assert "someone else first" in str(lost[0])
    assert len(pipeline.posted) == 1
    assert "after review by @" + won[0] in pipeline.posted[0][1]
    record = store.get(rid)
    assert record.status is Status.POSTED and record.reviewed_by == won[0]


def test_of_many_simultaneous_reviews_exactly_one_lands(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    stores = [Store(path) for _ in range(8)]
    for n in range(25):
        rid = stores[0].enqueue(repo="o/r", run_id=n, run_attempt=1, html_url="u")
        stores[0].finish(rid, Status.AWAITING_REVIEW)
        # Half approve and half reject, so the loser is not always the same verb.
        results = _race([
            lambda s=s, i=i, rid=rid: s.claim_review(
                rid, Status.APPROVED if i % 2 else Status.REJECTED, reviewer=f"r{i}"
            )
            for i, s in enumerate(stores)
        ])
        winners = [r for r in results if isinstance(r, RunRecord)]
        assert len(winners) == 1, results
        assert all(r is None for r in results if r is not winners[0])
        assert stores[0].get(rid).reviewed_by == winners[0].reviewed_by


def test_concurrent_workers_never_claim_the_same_run(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    stores = [Store(path) for _ in range(8)]
    queued = {
        stores[0].enqueue(repo="o/r", run_id=n, run_attempt=1, html_url="u") for n in range(50)
    }

    def drain(s: Store) -> list[int]:
        got = []
        while (record := s.claim_next()) is not None:
            got.append(record.id)
        return got

    claimed = [rid for got in _race([lambda s=s: drain(s) for s in stores]) for rid in got]
    assert len(claimed) == len(set(claimed)), "a run was claimed twice"
    assert set(claimed) == queued
    assert all(stores[0].get(rid).attempts == 1 for rid in queued)


def test_approval_respects_the_dry_run_switch(make):
    client, store, pipeline, rid = _awaiting(make, _result(confidence=0.4), post_comments=False)
    assert _review(client, rid, "approve").json()["status"] == "dry_run"
    assert pipeline.posted == []
    assert "after review by @octocat" in store.get(rid).comment


def test_a_run_that_was_posted_cannot_be_approved_again(make):
    pipeline = FakePipeline(_result())
    client, _, worker = make(pipeline)
    rid = _post(client, _payload()).json()["queued"]
    worker.drain()
    assert _review(client, rid, "approve").status_code == 409


@pytest.mark.parametrize(
    ("token", "review_token", "code"),
    [("wrong", TOKEN, 401), (TOKEN, None, 503)],
)
def test_review_needs_the_token(make, token, review_token, code):
    client, store, pipeline, rid = _awaiting(make, _result(confidence=0.4), review_token=review_token)
    assert _review(client, rid, "approve", token=token).status_code == code
    assert pipeline.posted == []
    assert store.get(rid).status is Status.AWAITING_REVIEW


def test_reviewer_must_look_like_a_github_login(make):
    client, _, _, rid = _awaiting(make, _result(confidence=0.4))
    r = client.post(
        f"/runs/{rid}/approve",
        json={"reviewer": "x](http://evil) ping @everyone"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 422


def test_a_database_from_before_review_gains_its_columns(tmp_path: Path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run_id INTEGER NOT NULL,"
        " run_attempt INTEGER NOT NULL, installation INTEGER, delivery TEXT, html_url TEXT NOT NULL,"
        " status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, not_before TEXT,"
        " received_at TEXT NOT NULL, updated_at TEXT NOT NULL, fixture TEXT, verdict TEXT,"
        " route TEXT, reason TEXT, evidence_ok INTEGER, comment TEXT, comment_url TEXT, error TEXT,"
        " UNIQUE (repo, run_id, run_attempt))"
    )
    db.commit()
    db.close()
    store = Store(path)
    rid = store.enqueue(repo="o/r", run_id=1, run_attempt=1, html_url="u")
    assert store.get(rid).reviewed_by is None
