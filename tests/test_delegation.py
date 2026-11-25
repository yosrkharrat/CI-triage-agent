"""Delegation through the service: proposed, sent, followed until fixed or not.

GitHub and the model are a scripted fake. Each test plays out a chain of
webhooks the way GitHub would deliver them (the failed run, Claude's push, the
CI run on the fix branch) and checks where the delegation ends up, what was
sent to Claude, and what was said on the pull request.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_service import SECRET, TOKEN, _post, _race

from ci_triage.agent import EvidenceCheck, TriageResult
from ci_triage.delegate import PullInfo
from ci_triage.github import RateLimited
from ci_triage.guard import ChangedFile
from ci_triage.models import Evidence, FailureCategory, Job, Verdict, WorkflowRun, route
from ci_triage.sandbox import PatchTrial, Reproduction
from ci_triage.service import Settings, create_app, housekeep, process
from ci_triage.store import (
    DelegationStatus,
    IllegalTransition,
    Kind,
    RunRecord,
    Status,
    Store,
)

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")

RUN = json.loads((FIXTURE / "run.json").read_text()) if FIXTURE.exists() else {}
REPO = RUN.get("repository", {}).get("full_name", "o/r")
SHA = RUN.get("head_sha", "0" * 40)
WORKFLOW = RUN.get("workflow_id", 1)
FAILED = sorted(
    j["name"] for j in json.loads((FIXTURE / "jobs.json").read_text())["jobs"]
    if j["conclusion"] == "failure"
) if FIXTURE.exists() else []
PR = 7


def _verdict(category=FailureCategory.REGRESSION, confidence=0.9, fix="Do the right thing.",
             summary="The build is broken.") -> Verdict:
    return Verdict(
        category=category, confidence=confidence, summary=summary, reasoning="r",
        evidence=[Evidence(job_name="build (ubuntu-22.04)", log_path="2_build (ubuntu-22.04).txt",
                           line_start=10, line_end=12, quote="error[E0425]: cannot find value",
                           why="a compile error")],
        suggested_fix=fix,
    )


def _result(ok=True, **kw) -> TriageResult:
    v = _verdict(**kw)
    dest, reason = route(v)
    return TriageResult(v, dest, reason, (EvidenceCheck(v.evidence[0], ok, "ok"),))


def _jobs(**conclusions: str) -> list[Job]:
    """Every originally failing job passing, unless named otherwise."""
    out = [Job(id=i, name=n, status="completed", conclusion="success") for i, n in enumerate(FAILED)]
    for name, c in conclusions.items():
        out = [j for j in out if j.name != name]
        out.append(Job(id=len(out) + 100, name=name, status="completed", conclusion=c))
    return out


HONEST = [ChangedFile("src-tauri/src/lib.rs", "modified", "@@ -1 +1 @@\n-let x = y;\n+let x = z;")]
CHEAT = [ChangedFile("tests/test_build.py", "modified", "@@ -1 +1 @@\n+@pytest.mark.skip")]


class FakeGitHub:
    """GitHub and the model, as far as the service can tell."""

    def __init__(self, result: TriageResult):
        self.result = result
        self.follow_result = _result(summary="Still broken after the fix.")
        self.posted: list[tuple[int, str, str]] = []
        self.dispatched: list[dict] = []
        self.opened: list[dict] = []
        self.triaged = 0
        self.pulls_ = {PR: PullInfo(PR, "open", "feature", SHA, REPO, "main")}
        self.branches: dict[str, str] = {}
        self.runs: dict[int, WorkflowRun] = {}
        self.jobs_: dict[int, list[Job]] = {}
        self.diffs: dict[str, list[ChangedFile]] = {}
        self.trial: PatchTrial | None = None
        self.fail_dispatch: Exception | None = None

    # the triage path
    def capture(self, record: RunRecord) -> Path:
        return FIXTURE

    def triage(self, fixture: Path) -> TriageResult:
        self.triaged += 1
        return self.result if self.triaged == 1 else self.follow_result

    def pulls(self, record, run) -> list[int]:
        return [PR]

    def post(self, record, number, body, marker) -> str:
        self.posted = [p for p in self.posted if p[2] != marker] + [(number, body, marker)]
        return f"https://github.com/{REPO}/pull/{number}#c"

    # delegation
    def pull(self, on, number) -> PullInfo:
        return self.pulls_[number]

    def dispatch(self, on, payload) -> None:
        if self.fail_dispatch:
            raise self.fail_dispatch
        self.dispatched.append(payload)

    def branch_head(self, on, branch) -> str | None:
        return self.branches.get(branch)

    def open_fix_pr(self, on, *, head, base, title, body) -> int:
        self.opened.append({"head": head, "base": base, "title": title, "body": body})
        return 100 + len(self.opened)

    def get_run(self, on, run_id) -> WorkflowRun:
        return self.runs[run_id]

    def jobs(self, on, run_id) -> list[Job]:
        return self.jobs_[run_id]

    def compare(self, on, base, head):
        return self.diffs.get(head, HONEST), False

    def try_fix(self, on, sha, command, base, head):
        return self.trial

    def notes(self) -> list[str]:
        return [body for _, body, marker in self.posted if "delegation:" in marker]


@pytest.fixture
def make(tmp_path: Path):
    def _make(gh: FakeGitHub, *, mode: str = "review", post_comments: bool = True, **kw):
        settings = Settings(
            webhook_secret=SECRET, post_comments=post_comments, db_path=tmp_path / "db.sqlite",
            review_token=TOKEN, delegate=mode, **kw,
        )
        app = create_app(settings, pipeline=gh, start_worker=False)
        return TestClient(app), app.state.store, app.state.worker

    return _make


def _fail(client, worker, **overrides) -> int:
    """GitHub reports the PR's CI failed; the worker triages it."""
    payload = {"action": "completed", "workflow_run": {**RUN, **overrides},
               "installation": {"id": 42}}
    rid = _post(client, payload).json()["queued"]
    worker.drain()
    return rid


def _act(client, kind, ident, action, token=TOKEN):
    return client.post(f"/{kind}/{ident}/{action}", json={"reviewer": "octocat"},
                       headers={"Authorization": f"Bearer {token}"})


def _push(gh: FakeGitHub, store: Store, settings_client, sha: str) -> None:
    """Claude pushes to the fix branch; housekeeping opens the draft PR."""
    d = store.delegations()[0]
    gh.branches[d.fix_branch] = sha
    housekeep(store, gh, settings_client.app.state.settings)


_ids = iter(range(5000, 6000))


def _ci(gh: FakeGitHub, client, worker, sha: str, jobs: list[Job], *, conclusion=None,
        workflow_id=WORKFLOW, branch: str | None = None, delivery=None) -> dict:
    """GitHub reports a CI run on the fix branch."""
    store = client.app.state.store
    branch = branch or store.delegations()[0].fix_branch
    run_id = next(_ids)
    concl = conclusion or ("failure" if any(j.failed for j in jobs) else "success")
    raw = {**RUN, "id": run_id, "head_branch": branch, "head_sha": sha, "conclusion": concl,
           "workflow_id": workflow_id, "event": "pull_request",
           "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}
    gh.runs[run_id] = WorkflowRun.model_validate(raw)
    gh.jobs_[run_id] = jobs
    r = _post(client, {"action": "completed", "workflow_run": raw, "installation": {"id": 42}},
              delivery=delivery or f"d-{run_id}").json()
    worker.drain()
    return r


def _dispatched(make, **kw):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh, mode="auto", **kw)
    rid = _fail(client, worker)
    [d] = store.delegations()
    assert d.status is DelegationStatus.DISPATCHED
    return gh, client, store, worker, rid, d


# -- proposing ---------------------------------------------------------------


def test_off_mode_creates_no_delegation(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh, mode="off")
    _fail(client, worker)
    assert store.delegations() == []
    assert gh.dispatched == []


def test_review_mode_proposes_and_sends_nothing(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh)
    rid = _fail(client, worker)
    [d] = store.delegations()
    assert d.status is DelegationStatus.PROPOSED
    assert d.origin_run == rid
    assert d.fix_branch == f"ci-triage/fix-{rid}"
    assert d.failed_jobs == FAILED
    assert d.workflow_id == WORKFLOW
    assert "error[E0425]" in d.brief
    assert gh.dispatched == []
    # The diagnosis waits in review for its proposed fix, as before.
    assert store.get(rid).status is Status.AWAITING_REVIEW


@pytest.mark.parametrize(
    "result",
    [_result(category=FailureCategory.INFRA), _result(ok=False), _result(confidence=0.5)],
)
def test_an_ineligible_verdict_is_not_proposed(make, result):
    client, store, worker = make(FakeGitHub(result))
    _fail(client, worker)
    assert store.delegations() == []


def test_a_fork_pr_is_never_delegated(make):
    gh = FakeGitHub(_result())
    gh.pulls_[PR] = PullInfo(PR, "open", "feature", SHA, "someone/fork", "main")
    client, store, worker = make(gh)
    _fail(client, worker)
    assert store.delegations() == []


def test_one_open_delegation_per_pull_request(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh)
    _fail(client, worker)
    gh.triaged = 0
    _fail(client, worker, run_attempt=2)
    assert len(store.delegations()) == 1


# -- approving and rejecting -----------------------------------------------


def test_approving_sends_it_and_approves_the_diagnosis_with_it(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh)
    rid = _fail(client, worker)
    d = store.delegations()[0]

    r = _act(client, "delegations", d.id, "approve")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dispatched"
    assert r.json()["attempt"] == 1

    [payload] = gh.dispatched
    assert payload["fix_branch"] == d.fix_branch
    assert payload["head_sha"] == SHA
    assert payload["brief"] == d.brief
    assert len(payload) <= 10  # GitHub's cap on client_payload keys

    # One approval, not two: the diagnosis went out with it.
    assert store.get(rid).status is Status.POSTED
    assert store.get(rid).reviewed_by == "octocat"
    assert any("handed to Claude, attempt 1 of 2" in n for n in gh.notes())


def test_rejecting_sends_nothing_and_leaves_the_diagnosis_in_review(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh)
    rid = _fail(client, worker)
    d = store.delegations()[0]
    assert _act(client, "delegations", d.id, "reject").json()["status"] == "rejected"
    assert gh.dispatched == []
    assert store.get(rid).status is Status.AWAITING_REVIEW
    assert _act(client, "delegations", d.id, "approve").status_code == 409


def test_rejecting_the_diagnosis_rejects_its_delegation(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh)
    rid = _fail(client, worker)
    assert _act(client, "runs", rid, "reject").status_code == 200
    assert store.delegations()[0].status is DelegationStatus.REJECTED


def test_delegation_review_needs_the_token(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh)
    _fail(client, worker)
    d = store.delegations()[0]
    assert _act(client, "delegations", d.id, "approve", token="wrong").status_code == 401
    assert gh.dispatched == []


def test_a_refused_dispatch_goes_back_and_is_sent_on_the_next_poll(make):
    gh = FakeGitHub(_result())
    gh.fail_dispatch = RuntimeError("403: Resource not accessible by integration")
    client, store, worker = make(gh, mode="auto")
    _fail(client, worker)
    d = store.delegations()[0]
    assert d.status is DelegationStatus.APPROVED
    assert d.attempt == 0
    assert "not accessible" in d.error

    gh.fail_dispatch = None
    housekeep(store, gh, client.app.state.settings)
    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.DISPATCHED and d.attempt == 1
    assert len(gh.dispatched) == 1


def test_the_run_and_delegation_endpoints_show_the_chain(make):
    _gh, client, _store, _worker, rid, d = _dispatched(make)
    assert client.get(f"/runs/{rid}").json()["delegations"][0]["id"] == d.id
    assert client.get("/delegations?status=dispatched").json()[0]["id"] == d.id
    assert client.get(f"/delegations/{d.id}").json()["follow_ups"] == []


# -- following through -------------------------------------------------------


def test_claudes_push_gets_a_draft_pr_into_the_authors_branch(make):
    gh, client, store, _worker, _rid, d = _dispatched(make)
    housekeep(store, gh, client.app.state.settings)
    assert gh.opened == []  # nothing pushed yet

    _push(gh, store, client, "c1")
    [pr] = gh.opened
    assert pr["head"] == d.fix_branch and pr["base"] == "feature"
    assert store.get_delegation(d.id).fix_pr == 101
    housekeep(store, gh, client.app.state.settings)
    assert len(gh.opened) == 1


def test_fixed_on_the_first_try(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    r = _ci(gh, client, worker, "c1", _jobs())
    assert r["follow_up"] == d.id

    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.FIXED
    assert d.guard_result["ok"] is True
    assert "now pass" in d.closed_reason
    assert any("fix ready" in n and "draft PR #101" in n for n in gh.notes())
    # A follow-up is checked, never triaged as a run of its own.
    [f] = store.follow_ups(d.id)
    assert f.kind is Kind.FOLLOW_UP and f.status is Status.CHECKED
    assert gh.triaged == 1


def test_fixed_on_the_second_try(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    _ci(gh, client, worker, "c1", _jobs(**{FAILED[0]: "failure"}))

    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.DISPATCHED and d.attempt == 2
    assert len(gh.dispatched) == 2
    retry = gh.dispatched[1]
    assert retry["attempt"] == 2
    assert "Attempt 1 did not fix it" in retry["brief"]
    assert "Still broken after the fix." in retry["brief"]  # the new diagnosis rides along
    assert gh.triaged == 2

    _push(gh, store, client, "c2")
    _ci(gh, client, worker, "c2", _jobs())
    assert store.get_delegation(d.id).status is DelegationStatus.FIXED


def test_gives_up_after_the_last_attempt(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    for sha in ("c1", "c2"):
        _push(gh, store, client, sha)
        _ci(gh, client, worker, sha, _jobs(**{FAILED[0]: "failure"}))
    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.GAVE_UP
    assert len(d.history) == 2
    assert len(gh.dispatched) == 2
    assert any("gave up, over to you" in n for n in gh.notes())


def test_a_guard_violation_is_a_failed_attempt_even_when_ci_is_green(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    gh.diffs["c1"] = CHEAT
    _ci(gh, client, worker, "c1", _jobs())

    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.DISPATCHED and d.attempt == 2
    assert d.guard_result["ok"] is False
    assert "pytest skip" in gh.dispatched[1]["brief"]
    assert gh.triaged == 1  # CI was green: nothing new to diagnose


def test_a_fix_that_turns_another_job_red_is_not_fixed(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    _ci(gh, client, worker, "c1", _jobs(lint="failure"))
    d = store.get_delegation(d.id)
    assert d.status is not DelegationStatus.FIXED
    assert "lint: newly failing" in d.history[0]["outcome"]


def test_a_run_on_an_older_fix_commit_is_ignored(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c2")
    _ci(gh, client, worker, "c1", _jobs())
    assert store.get_delegation(d.id).status is DelegationStatus.DISPATCHED
    [f] = store.follow_ups(d.id)
    assert "not the fix branch's latest" in f.reason


def test_a_commit_already_judged_is_not_judged_again(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    _ci(gh, client, worker, "c1", _jobs(**{FAILED[0]: "failure"}))
    # Attempt 2 is out, but a re-run of attempt 1's CI arrives before Claude pushes.
    _ci(gh, client, worker, "c1", _jobs())
    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.DISPATCHED and d.attempt == 2


def test_another_workflow_on_the_fix_branch_is_ignored(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    r = _ci(gh, client, worker, "c1", _jobs(), workflow_id=WORKFLOW + 1)
    assert "not the workflow" in r["ignored"]
    assert store.follow_ups(d.id) == []


def test_a_redelivered_follow_up_is_a_no_op(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c2")
    _ci(gh, client, worker, "c1", _jobs())  # judged (an older commit), delegation still open
    run = next(iter(gh.runs.values()))
    payload = {"action": "completed", "workflow_run": run.model_dump(mode="json"),
               "installation": {"id": 42}}
    assert _post(client, payload, delivery="again").json()["duplicate"] is True
    assert len(store.follow_ups(d.id)) == 1

    # Once the delegation is closed, a late delivery is not even queued.
    _ci(gh, client, worker, "c2", _jobs())
    assert store.get_delegation(d.id).status is DelegationStatus.FIXED
    late = gh.runs[max(gh.runs)].model_dump(mode="json")
    r = _post(client, {"action": "completed", "workflow_run": late}, delivery="late").json()
    assert "no open delegation" in r["ignored"]


def test_a_fix_branch_with_no_delegation_is_never_triaged(make):
    gh = FakeGitHub(_result())
    client, store, _worker = make(gh)
    r = _post(client, {"action": "completed", "workflow_run": {
        **RUN, "head_branch": "ci-triage/fix-999", "id": 1}, "installation": {"id": 42}}).json()
    assert "no open delegation" in r["ignored"]
    assert store.recent() == []


def test_the_fix_workflow_failing_is_not_triaged(make):
    gh = FakeGitHub(_result())
    client, _store, _worker = make(gh)
    r = _post(client, {"action": "completed", "workflow_run": {
        **RUN, "event": "repository_dispatch", "name": "ci-triage fix"}}).json()
    assert "fix workflow" in r["ignored"]


def test_stale_when_nothing_comes_back_in_time(make):
    gh, client, store, _worker, _rid, d = _dispatched(make, delegate_deadline=timedelta(seconds=-1))
    housekeep(store, gh, client.app.state.settings)
    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.STALE
    assert any("stale" in n for n in gh.notes())


def test_a_proposal_nobody_approves_goes_stale(make):
    gh = FakeGitHub(_result())
    client, store, worker = make(gh, proposal_deadline=timedelta(seconds=-1))
    _fail(client, worker)
    housekeep(store, gh, client.app.state.settings)
    assert store.delegations()[0].status is DelegationStatus.STALE


def test_superseded_when_the_author_pushes(make):
    gh, client, store, _worker, _rid, d = _dispatched(make)
    gh.pulls_[PR] = PullInfo(PR, "open", "feature", "f" * 40, REPO, "main")
    housekeep(store, gh, client.app.state.settings)
    assert store.get_delegation(d.id).status is DelegationStatus.SUPERSEDED


def test_not_superseded_when_the_author_takes_the_fix(make):
    gh, client, store, _worker, _rid, d = _dispatched(make)
    _push(gh, store, client, "c1")
    gh.pulls_[PR] = PullInfo(PR, "open", "feature", "c1", REPO, "main")  # fast-forwarded
    housekeep(store, gh, client.app.state.settings)
    assert store.get_delegation(d.id).status is DelegationStatus.DISPATCHED


def test_closed_when_the_pull_request_closes(make):
    gh, client, store, _worker, _rid, d = _dispatched(make)
    gh.pulls_[PR] = PullInfo(PR, "closed", "feature", SHA, REPO, "main")
    housekeep(store, gh, client.app.state.settings)
    assert store.get_delegation(d.id).status is DelegationStatus.CLOSED


def test_the_sandbox_can_overrule_a_green_run(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    # The tracked fixture's log names no failing command; give it one.
    with store._conn() as db:
        db.execute("UPDATE delegations SET command = 'cargo test' WHERE id = ?", (d.id,))

    def rep(patched: bool, code: int) -> Reproduction:
        return Reproduction("cargo test", SHA, patched, code, False, 1.0, "")

    gh.trial = PatchTrial(rep(False, 1), rep(True, 1))
    _push(gh, store, client, "c1")
    _ci(gh, client, worker, "c1", _jobs())
    d = store.get_delegation(d.id)
    assert d.status is not DelegationStatus.FIXED
    assert any("sandbox" in v for v in d.history[0]["guard"])


def test_comments_stay_off_when_posting_is_off(make):
    gh, client, store, worker, _rid, d = _dispatched(make, post_comments=False)
    _push(gh, store, client, "c1")
    _ci(gh, client, worker, "c1", _jobs())
    assert store.get_delegation(d.id).status is DelegationStatus.FIXED
    assert gh.notes() == []


# -- the store ---------------------------------------------------------------


def _open(store: Store, pr: int = 1, status=DelegationStatus.PROPOSED):
    rid = store.enqueue(repo="o/r", run_id=pr, run_attempt=1, html_url="u")
    return store.open_delegation(
        repo="o/r", pr_number=pr, installation=None, origin_run=rid, workflow_id=1,
        failed_jobs=["test"], head_branch="f", head_sha="a" * 40, base_branch="main",
        fix_branch=f"ci-triage/fix-{rid}", brief="b", status=status,
    )


def test_illegal_transitions_are_refused(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    d = _open(store)
    with pytest.raises(IllegalTransition):
        store.move(d.id, DelegationStatus.PROPOSED, DelegationStatus.FIXED)
    with pytest.raises(IllegalTransition):
        store.move(d.id, DelegationStatus.FIXED, DelegationStatus.DISPATCHED)
    with pytest.raises(IllegalTransition):
        _open(store, pr=2, status=DelegationStatus.DISPATCHED)


def test_a_move_from_the_wrong_state_changes_nothing(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    d = _open(store)
    assert store.move(d.id, DelegationStatus.APPROVED, DelegationStatus.DISPATCHED) is None
    assert store.get_delegation(d.id).status is DelegationStatus.PROPOSED


def test_only_one_open_delegation_per_pr_but_a_closed_one_allows_another(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    d = _open(store, pr=1)
    rid = store.enqueue(repo="o/r", run_id=50, run_attempt=1, html_url="u")
    again = {"repo": "o/r", "pr_number": 1, "installation": None, "origin_run": rid, "workflow_id": 1,
                 "failed_jobs": [], "head_branch": "f", "head_sha": "b" * 40, "base_branch": "main",
                 "fix_branch": "ci-triage/fix-50", "brief": "b", "status": DelegationStatus.PROPOSED}
    assert store.open_delegation(**again) is None
    store.move(d.id, DelegationStatus.PROPOSED, DelegationStatus.REJECTED)
    assert store.open_delegation(**again) is not None


def test_of_many_simultaneous_decisions_exactly_one_lands(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    stores = [Store(path) for _ in range(8)]
    for n in range(20):
        d = _open(stores[0], pr=n + 1)
        results = _race([
            lambda s=s, i=i, d=d: s.move(
                d.id, DelegationStatus.PROPOSED,
                DelegationStatus.APPROVED if i % 2 else DelegationStatus.REJECTED,
                reviewed_by=f"r{i}",
            )
            for i, s in enumerate(stores)
        ])
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, results
        assert stores[0].get_delegation(d.id).reviewed_by == winners[0].reviewed_by


def test_an_old_database_gains_the_kind_column(tmp_path: Path):
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
    db.execute(
        "INSERT INTO runs (repo, run_id, run_attempt, html_url, status, received_at, updated_at)"
        " VALUES ('o/r', 1, 1, 'u', 'posted', 'now', 'now')"
    )
    db.commit()
    db.close()
    store = Store(path)
    [old] = store.recent()
    assert old.kind is Kind.TRIAGE and old.delegation is None
    assert store.delegations() == []


# -- GitHub rate limits, and approvals that never finished ---------------------


class _Limited(FakeGitHub):
    def __init__(self, result, *, fail: str):
        super().__init__(result)
        self.fail = fail
        self.reset = datetime.now(UTC) + timedelta(minutes=10)

    def capture(self, record):
        if self.fail == "capture":
            raise RateLimited("rate limited", self.reset)
        return super().capture(record)

    def pulls(self, record, run):
        if self.fail == "pulls":
            raise RateLimited("rate limited", self.reset)
        return super().pulls(record, run)


def test_a_github_rate_limit_defers_the_run_to_its_reset(make):
    gh = _Limited(_result(category=FailureCategory.INFRA, fix=None), fail="capture")
    client, store, worker = make(gh, mode="off")
    rid = _fail(client, worker)
    r = store.get(rid)
    assert r.status is Status.QUEUED
    assert "rate limit" in r.error
    wait = datetime.fromisoformat(r.not_before) - datetime.now(UTC)
    assert timedelta(minutes=9) < wait < timedelta(minutes=11)


def test_a_rate_limited_approval_stays_approved_and_is_posted_when_due(make):
    gh = _Limited(_result(category=FailureCategory.INFRA, confidence=0.5, fix=None), fail="none")
    client, store, worker = make(gh, mode="off")
    rid = _fail(client, worker)
    assert store.get(rid).status is Status.AWAITING_REVIEW

    gh.fail = "pulls"
    assert _act(client, "runs", rid, "approve").json()["status"] == "approved"
    assert store.get(rid).not_before is not None
    worker.drain()
    assert store.get(rid).status is Status.APPROVED  # not due yet

    gh.fail = "none"
    with store._conn() as db:
        db.execute("UPDATE runs SET not_before = ? WHERE id = ?",
                   ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), rid))
    worker.drain()
    assert store.get(rid).status is Status.POSTED
    assert len([p for p in gh.posted if "run:" in p[2]]) == 1


def test_an_approval_interrupted_by_a_restart_is_posted_on_startup(make):
    gh = FakeGitHub(_result(category=FailureCategory.INFRA, confidence=0.5, fix=None))
    client, store, worker = make(gh, mode="off")
    rid = _fail(client, worker)
    # The process died after the claim and before the post.
    store.claim_review(rid, Status.APPROVED, reviewer="octocat")
    assert worker.repost_interrupted() == 1
    r = store.get(rid)
    assert r.status is Status.POSTED
    assert "after review by @octocat" in gh.posted[-1][1]


def test_process_of_a_follow_up_without_a_delegation_just_records_it(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite")
    rid = store.enqueue(repo="o/r", run_id=1, run_attempt=1, html_url="u", kind=Kind.FOLLOW_UP)
    record = store.claim_next()
    assert record is not None and record.id == rid
    settings = Settings(webhook_secret=SECRET, db_path=tmp_path / "db.sqlite", delegate="auto")
    assert process(record, store, FakeGitHub(_result()), settings) is Status.CHECKED


# -- the fix workflow's own runs ---------------------------------------------


def _fix_run(gh: FakeGitHub, client, worker, d, conclusion: str) -> dict:
    """The fix workflow finishes, its run named after the fix branch."""
    run_id = next(_ids)
    raw = {**RUN, "id": run_id, "name": "ci-triage fix", "event": "repository_dispatch",
           "head_branch": "main", "head_sha": "e" * 40, "conclusion": conclusion,
           "workflow_id": WORKFLOW + 50,
           "display_title": f"ci-triage fix {d.fix_branch} attempt {d.attempt}",
           "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}"}
    gh.runs[run_id] = WorkflowRun.model_validate(raw)
    r = _post(client, {"action": "completed", "workflow_run": raw}, delivery=f"d-{run_id}").json()
    worker.drain()
    return r


def test_a_failed_fix_workflow_is_a_failed_attempt_at_once(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    r = _fix_run(gh, client, worker, d, "failure")
    assert r["fix_workflow"] == d.id
    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.DISPATCHED and d.attempt == 2
    assert "fix workflow ended failure" in d.history[0]["outcome"]
    assert "fix workflow ended failure" in gh.dispatched[1]["brief"]
    assert gh.triaged == 1  # nothing to diagnose: no CI ran

    _fix_run(gh, client, worker, d, "failure")
    assert store.get_delegation(d.id).status is DelegationStatus.GAVE_UP


def test_claude_finishing_without_a_push_is_a_failed_attempt(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    _fix_run(gh, client, worker, d, "success")
    d = store.get_delegation(d.id)
    assert d.attempt == 2
    assert "without pushing" in d.history[0]["outcome"]


def test_claude_finishing_with_a_push_waits_for_ci(make):
    gh, client, store, worker, _rid, d = _dispatched(make)
    gh.branches[d.fix_branch] = "c1"
    _fix_run(gh, client, worker, d, "success")
    d = store.get_delegation(d.id)
    assert d.status is DelegationStatus.DISPATCHED and d.attempt == 1
    _ci(gh, client, worker, "c1", _jobs())
    assert store.get_delegation(d.id).status is DelegationStatus.FIXED


def test_a_fix_workflow_run_for_no_open_delegation_is_ignored(make):
    gh = FakeGitHub(_result())
    client, store, _worker = make(gh)
    r = _post(client, {"action": "completed", "workflow_run": {
        **RUN, "name": "ci-triage fix", "event": "repository_dispatch",
        "display_title": "ci-triage fix ci-triage/fix-77 attempt 1"}}).json()
    assert "fix workflow" in r["ignored"]
    assert store.recent() == []
