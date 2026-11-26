"""The service's record of every run it was asked to triage.

A webhook is a notification, not a job: GitHub gives up on a delivery after ten
seconds, redelivers when it thinks one was lost, and a triage takes minutes. So
the handler only writes a row here and returns, and a worker drains the rows.
That split is what makes three things hold:

* **Idempotency.** A run is keyed on `(repo, run_id, run_attempt)`. A redelivered
  webhook hits the unique constraint and changes nothing, so it cannot post a
  second comment or spend a second triage.
* **Surviving a restart.** A row claimed by a worker that then died is still
  `running` on the next start, and is put back in the queue. Re-running it is
  cheap: the verdict cache means a model that already answered is not asked again.
* **A review queue for free.** A verdict that may not be posted stays here as
  `awaiting_review`, with everything a human needs to decide on it. A reviewer
  moves it on exactly once: `claim_review` is a compare-and-set, so two people
  approving the same run at the same moment cannot both post it.

SQLite rather than Postgres because the service runs as one process with one
worker. The schema is plain enough to move when that stops being true.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    #: Comment posted, or an existing one updated.
    POSTED = "posted"
    #: Would have been posted, but the service is not allowed to post.
    DRY_RUN = "dry_run"
    #: Routed to a human: low confidence, unknown, a proposed fix, or a citation
    #: that did not verify.
    AWAITING_REVIEW = "awaiting_review"
    #: Postable, but no open pull request to post it on.
    NO_PR = "no_pr"
    #: A human approved it and it is being posted. A row left here means the
    #: process died mid-post, or GitHub rate-limited the post (then
    #: `not_before` says when to try again). The worker re-posts both: the
    #: comment is keyed on a marker, so posting again edits rather than
    #: duplicates it.
    APPROVED = "approved"
    #: A human decided it should not be posted.
    REJECTED = "rejected"
    FAILED = "failed"
    #: A run on a fix branch, checked against its delegation rather than
    #: triaged on its own. `reason` says what the check concluded.
    CHECKED = "checked"


class Kind(str, Enum):
    #: A failed run, triaged and routed.
    TRIAGE = "triage"
    #: A completed run on a delegation's fix branch, of any conclusion. It is
    #: never triaged as a run of its own: the normal path would look up PRs
    #: for Claude's commit and could comment on the wrong one.
    FOLLOW_UP = "follow_up"


class DelegationStatus(str, Enum):
    #: Eligible, waiting for a person (`CI_TRIAGE_DELEGATE=review`).
    PROPOSED = "proposed"
    #: Cleared to be sent, by a person or by `auto` mode. Not sent yet.
    APPROVED = "approved"
    #: Sent to Claude; waiting for its push and the CI run after it.
    DISPATCHED = "dispatched"
    #: An attempt failed and another is allowed. Waiting to be sent again.
    RETRY = "retry"
    FIXED = "fixed"
    GAVE_UP = "gave_up"
    REJECTED = "rejected"
    #: Nothing came back before the deadline.
    STALE = "stale"
    #: The author pushed to the PR, so the fix was for a commit that is gone.
    SUPERSEDED = "superseded"
    #: The pull request was closed or merged.
    CLOSED = "closed"


#: A delegation in one of these is still in flight; at most one per PR.
OPEN_DELEGATION = (
    DelegationStatus.PROPOSED,
    DelegationStatus.APPROVED,
    DelegationStatus.DISPATCHED,
    DelegationStatus.RETRY,
)

#: Every move a delegation may make. Anything else is refused, so a late or
#: redelivered event cannot reopen a closed one.
TRANSITIONS: dict[DelegationStatus, frozenset[DelegationStatus]] = {
    # Either can go straight to `gave_up` when the planner finds no code fix
    # within the rules: nothing is sent, and no Claude run is spent.
    DelegationStatus.PROPOSED: frozenset({
        DelegationStatus.APPROVED, DelegationStatus.REJECTED,
        DelegationStatus.SUPERSEDED, DelegationStatus.CLOSED, DelegationStatus.STALE,
        DelegationStatus.GAVE_UP,
    }),
    DelegationStatus.APPROVED: frozenset({
        DelegationStatus.DISPATCHED, DelegationStatus.SUPERSEDED, DelegationStatus.CLOSED,
        DelegationStatus.GAVE_UP,
    }),
    DelegationStatus.DISPATCHED: frozenset({
        DelegationStatus.FIXED, DelegationStatus.RETRY, DelegationStatus.GAVE_UP,
        DelegationStatus.STALE, DelegationStatus.SUPERSEDED, DelegationStatus.CLOSED,
        # A dispatch that GitHub refused goes back to wait for the next try.
        DelegationStatus.APPROVED,
    }),
    DelegationStatus.RETRY: frozenset({
        DelegationStatus.DISPATCHED, DelegationStatus.GAVE_UP,
        DelegationStatus.SUPERSEDED, DelegationStatus.CLOSED,
    }),
}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    repo          TEXT    NOT NULL,
    run_id        INTEGER NOT NULL,
    run_attempt   INTEGER NOT NULL,
    installation  INTEGER,
    delivery      TEXT,
    html_url      TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    not_before    TEXT,
    received_at   TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    fixture       TEXT,
    verdict       TEXT,
    route         TEXT,
    reason        TEXT,
    evidence_ok   INTEGER,
    comment       TEXT,
    comment_url   TEXT,
    error         TEXT,
    reviewed_by   TEXT,
    reviewed_at   TEXT,
    review_note   TEXT,
    kind          TEXT    NOT NULL DEFAULT 'triage',
    delegation    INTEGER,
    trail         TEXT,
    UNIQUE (repo, run_id, run_attempt)
);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, received_at);

CREATE TABLE IF NOT EXISTS delegations (
    id            INTEGER PRIMARY KEY,
    repo          TEXT    NOT NULL,
    pr_number     INTEGER NOT NULL,
    installation  INTEGER,
    origin_run    INTEGER NOT NULL REFERENCES runs (id),
    workflow_id   INTEGER NOT NULL,
    failed_jobs   TEXT    NOT NULL,
    head_branch   TEXT    NOT NULL,
    head_sha      TEXT    NOT NULL,
    base_branch   TEXT,
    fix_branch    TEXT    NOT NULL,
    fix_pr        INTEGER,
    command       TEXT,
    cited         TEXT,
    status        TEXT    NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 2,
    brief         TEXT    NOT NULL,
    history       TEXT    NOT NULL DEFAULT '[]',
    last_run_id   INTEGER,
    guard_result  TEXT,
    closed_reason TEXT,
    error         TEXT,
    reviewed_by   TEXT,
    comment_url   TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    deadline      TEXT,
    plan          TEXT,
    plan_checks   TEXT,
    trail         TEXT,
    category      TEXT    NOT NULL DEFAULT 'regression'
);
-- SQLite will not take `UNIQUE ... WHERE` as a table constraint; a partial
-- unique index says the same thing: one open delegation per pull request.
CREATE UNIQUE INDEX IF NOT EXISTS delegations_one_open ON delegations (repo, pr_number)
    WHERE status IN ('proposed', 'approved', 'dispatched', 'retry');
CREATE INDEX IF NOT EXISTS delegations_branch ON delegations (repo, fix_branch);
"""

#: Columns added after the first schema, and so missing from a database the
#: service created before them. `CREATE TABLE IF NOT EXISTS` will not add them.
_ADDED_COLUMNS = {
    "reviewed_by": "TEXT",
    "reviewed_at": "TEXT",
    "review_note": "TEXT",
    "kind": "TEXT NOT NULL DEFAULT 'triage'",
    "delegation": "INTEGER",
    "trail": "TEXT",
}
#: The same, for `delegations`.
_ADDED_DELEGATION_COLUMNS = {
    "plan": "TEXT",
    "plan_checks": "TEXT",
    "trail": "TEXT",
    # Every delegation before this column was a regression; none other was allowed.
    "category": "TEXT NOT NULL DEFAULT 'regression'",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RunRecord:
    id: int
    repo: str
    run_id: int
    run_attempt: int
    installation: int | None
    delivery: str | None
    html_url: str
    status: Status
    attempts: int
    not_before: str | None
    received_at: str
    updated_at: str
    fixture: str | None
    verdict: dict | None
    route: str | None
    reason: str | None
    evidence_ok: bool | None
    comment: str | None
    comment_url: str | None
    error: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_note: str | None
    kind: Kind = Kind.TRIAGE
    delegation: int | None = None
    #: How the verdict was reached: one line per tool call.
    trail: list[str] = field(default_factory=list)
    #: The diagnosis's category. A `dependency` fix may pin actions under
    #: `.github/workflows/`; nothing else may touch `.github/`.
    category: str = "regression"

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, repo = self.repo.split("/", 1)
        return owner, repo

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> RunRecord:
        d = dict(row)
        d["status"] = Status(d["status"])
        d["kind"] = Kind(d.get("kind") or Kind.TRIAGE.value)
        d["verdict"] = json.loads(d["verdict"]) if d["verdict"] else None
        d["evidence_ok"] = None if d["evidence_ok"] is None else bool(d["evidence_ok"])
        d["trail"] = json.loads(d["trail"]) if d.get("trail") else []
        return cls(**d)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        d["kind"] = self.kind.value
        return d


@dataclass(frozen=True)
class Delegation:
    id: int
    repo: str
    pr_number: int
    installation: int | None
    origin_run: int
    workflow_id: int
    failed_jobs: list[str]
    head_branch: str
    head_sha: str
    base_branch: str | None
    fix_branch: str
    fix_pr: int | None
    command: str | None
    cited: str | None
    status: DelegationStatus
    attempt: int
    max_attempts: int
    brief: str
    #: One entry per attempt that did not fix it: {attempt, outcome, run_id,
    #: verdict?, guard?}. Carried into the next brief and shown on the dashboard.
    history: list[dict]
    last_run_id: int | None
    guard_result: dict | None
    closed_reason: str | None
    error: str | None
    reviewed_by: str | None
    comment_url: str | None
    created_at: str
    updated_at: str
    deadline: str | None
    #: The planner's `FixPlan`, when one was made and passed its checks.
    plan: dict | None = None
    #: What the planner's checks found, and why a plan was dropped if it was.
    plan_checks: dict | None = None
    #: How the diagnosis was reached, from the triage's tool calls.
    trail: list[str] = field(default_factory=list)
    #: The diagnosis's category. A `dependency` fix may pin actions under
    #: `.github/workflows/`; nothing else may touch `.github/`.
    category: str = "regression"

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, repo = self.repo.split("/", 1)
        return owner, repo

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_DELEGATION

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> Delegation:
        d = dict(row)
        d["status"] = DelegationStatus(d["status"])
        d["failed_jobs"] = json.loads(d["failed_jobs"])
        d["history"] = json.loads(d["history"] or "[]")
        d["guard_result"] = json.loads(d["guard_result"]) if d["guard_result"] else None
        for k in ("plan", "plan_checks"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        d["trail"] = json.loads(d["trail"]) if d.get("trail") else []
        return cls(**d)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        return d


class IllegalTransition(Exception):
    """A delegation move that `TRANSITIONS` does not allow."""


class Store:
    """A thread-safe handle on the runs table.

    One connection per operation, serialised by a lock: the webhook handler and
    the worker run on different threads, and SQLite connections do not cross
    threads. Throughput here is a handful of writes per triage, so the lock
    costs nothing that matters.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as db:
            db.executescript(_SCHEMA)
            for table, added in (("runs", _ADDED_COLUMNS), ("delegations", _ADDED_DELEGATION_COLUMNS)):
                have = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
                for name, kind in added.items():
                    if name not in have:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def enqueue(
        self,
        *,
        repo: str,
        run_id: int,
        run_attempt: int,
        html_url: str,
        installation: int | None = None,
        delivery: str | None = None,
        kind: Kind = Kind.TRIAGE,
        delegation: int | None = None,
    ) -> int | None:
        """Queue a run. Returns its id, or None when it is already known."""
        now = _now()
        with self._conn() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO runs (repo, run_id, run_attempt, installation, delivery,"
                " html_url, status, received_at, updated_at, kind, delegation)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (repo, run_id, run_attempt, installation, delivery, html_url,
                 Status.QUEUED.value, now, now, kind.value, delegation),
            )
            return cur.lastrowid if cur.rowcount else None

    def claim_next(self) -> RunRecord | None:
        """Take the oldest queued run that is due, marking it running.

        The status is checked again outside the subquery. SQLite does not need
        it — a write statement holds the database's write lock from its first
        read — but a database that re-checks only the outer WHERE of a row it
        waited on, as Postgres does under READ COMMITTED, would otherwise let
        two workers claim the same row.
        """
        now = _now()
        with self._conn() as db:
            row = db.execute(
                "UPDATE runs SET status = ?, attempts = attempts + 1, updated_at = ?"
                " WHERE id = (SELECT id FROM runs WHERE status = ?"
                "   AND (not_before IS NULL OR not_before <= ?)"
                "   ORDER BY received_at LIMIT 1)"
                " AND status = ?"
                " RETURNING *",
                (Status.RUNNING.value, now, Status.QUEUED.value, now, Status.QUEUED.value),
            ).fetchone()
        return RunRecord._from_row(row) if row else None

    def requeue_interrupted(self) -> int:
        """Put back runs a previous process claimed and never finished."""
        with self._conn() as db:
            return db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE status = ?",
                (Status.QUEUED.value, _now(), Status.RUNNING.value),
            ).rowcount

    def interrupted_approvals(self) -> list[RunRecord]:
        """Approved runs a previous process never finished posting.

        Only for startup, before the HTTP server takes requests: at runtime an
        approved row with no `not_before` is a post in flight, not a stuck one.
        """
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE status = ? AND not_before IS NULL",
                (Status.APPROVED.value,),
            ).fetchall()
        return [RunRecord._from_row(r) for r in rows]

    def defer_approval(self, run: int, until: datetime, error: str) -> None:
        """Keep an approved run approved, to be posted again after `until`."""
        with self._conn() as db:
            db.execute(
                "UPDATE runs SET not_before = ?, error = ?, updated_at = ?"
                " WHERE id = ? AND status = ?",
                (until.isoformat(), error, _now(), run, Status.APPROVED.value),
            )

    def claim_due_approval(self) -> RunRecord | None:
        """Take one approved run whose deferred post is due.

        Clearing `not_before` is the claim: a second worker's compare-and-set
        then matches nothing, so the same comment is not posted twice at once.
        """
        now = _now()
        with self._conn() as db:
            row = db.execute(
                "UPDATE runs SET not_before = NULL, updated_at = ?"
                " WHERE id = (SELECT id FROM runs WHERE status = ? AND not_before IS NOT NULL"
                "   AND not_before <= ? ORDER BY not_before LIMIT 1)"
                " AND status = ? AND not_before IS NOT NULL RETURNING *",
                (now, Status.APPROVED.value, now, Status.APPROVED.value),
            ).fetchone()
        return RunRecord._from_row(row) if row else None

    def defer(self, run: int, delay: timedelta, error: str) -> None:
        """Queue a run again after `delay` — a quota that resets, not a failure."""
        with self._conn() as db:
            db.execute(
                "UPDATE runs SET status = ?, not_before = ?, error = ?, updated_at = ? WHERE id = ?",
                (Status.QUEUED.value, (datetime.now(UTC) + delay).isoformat(), error, _now(), run),
            )

    def finish(
        self,
        run: int,
        status: Status,
        *,
        fixture: str | None = None,
        verdict: dict | None = None,
        route: str | None = None,
        reason: str | None = None,
        evidence_ok: bool | None = None,
        comment: str | None = None,
        comment_url: str | None = None,
        error: str | None = None,
        trail: list[str] | None = None,
    ) -> None:
        with self._conn() as db:
            db.execute(
                "UPDATE runs SET status = ?, fixture = ?, verdict = ?, route = ?, reason = ?,"
                " evidence_ok = ?, comment = ?, comment_url = ?, error = ?,"
                " trail = COALESCE(?, trail), not_before = NULL,"
                " updated_at = ? WHERE id = ?",
                (
                    status.value,
                    fixture,
                    json.dumps(verdict) if verdict is not None else None,
                    route,
                    reason,
                    None if evidence_ok is None else int(evidence_ok),
                    comment,
                    comment_url,
                    error,
                    json.dumps(trail) if trail is not None else None,
                    _now(),
                    run,
                ),
            )

    def claim_review(
        self, run: int, to: Status, *, reviewer: str, note: str | None = None
    ) -> RunRecord | None:
        """Move a run out of `awaiting_review`, recording who did it.

        Returns the updated row, or None when the run was not awaiting review —
        including when another reviewer got there first.
        """
        now = _now()
        with self._conn() as db:
            row = db.execute(
                "UPDATE runs SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?,"
                " updated_at = ? WHERE id = ? AND status = ? RETURNING *",
                (to.value, reviewer, now, note, now, run, Status.AWAITING_REVIEW.value),
            ).fetchone()
        return RunRecord._from_row(row) if row else None

    def settle(
        self,
        run: int,
        status: Status,
        *,
        reason: str,
        comment: str | None = None,
        comment_url: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record where an approved run ended up, keeping its verdict intact."""
        with self._conn() as db:
            db.execute(
                "UPDATE runs SET status = ?, reason = ?, comment = COALESCE(?, comment),"
                " comment_url = ?, error = ?, updated_at = ? WHERE id = ?",
                (status.value, reason, comment, comment_url, error, _now(), run),
            )

    def get(self, run: int) -> RunRecord | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run,)).fetchone()
        return RunRecord._from_row(row) if row else None

    def recent(self, status: Status | None = None, limit: int = 50) -> list[RunRecord]:
        with self._conn() as db:
            if status is None:
                rows = db.execute(
                    "SELECT * FROM runs ORDER BY received_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM runs WHERE status = ? ORDER BY received_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        return [RunRecord._from_row(r) for r in rows]

    # -- delegations --------------------------------------------------------

    def open_delegation(
        self,
        *,
        repo: str,
        pr_number: int,
        installation: int | None,
        origin_run: int,
        workflow_id: int,
        failed_jobs: list[str],
        head_branch: str,
        head_sha: str,
        base_branch: str | None,
        fix_branch: str,
        brief: str,
        status: DelegationStatus,
        command: str | None = None,
        cited: str | None = None,
        max_attempts: int = 2,
        reviewed_by: str | None = None,
        plan: dict | None = None,
        plan_checks: dict | None = None,
        trail: list[str] | None = None,
        category: str = "regression",
    ) -> Delegation | None:
        """Start a delegation. None when one is already open on that PR."""
        if status not in (DelegationStatus.PROPOSED, DelegationStatus.APPROVED):
            raise IllegalTransition(f"a delegation starts proposed or approved, not {status.value}")
        now = _now()
        try:
            with self._conn() as db:
                row = db.execute(
                    "INSERT INTO delegations (repo, pr_number, installation, origin_run,"
                    " workflow_id, failed_jobs, head_branch, head_sha, base_branch, fix_branch,"
                    " command, cited, status, max_attempts, brief, reviewed_by, created_at,"
                    " updated_at, plan, plan_checks, trail, category)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING *",
                    (repo, pr_number, installation, origin_run, workflow_id,
                     json.dumps(failed_jobs), head_branch, head_sha, base_branch, fix_branch,
                     command, cited, status.value, max_attempts, brief, reviewed_by, now, now,
                     json.dumps(plan) if plan is not None else None,
                     json.dumps(plan_checks) if plan_checks is not None else None,
                     json.dumps(trail or []), category),
                ).fetchone()
        except sqlite3.IntegrityError:
            return None
        return Delegation._from_row(row)

    def move(
        self,
        delegation: int,
        frm: DelegationStatus | tuple[DelegationStatus, ...],
        to: DelegationStatus,
        **fields: object,
    ) -> Delegation | None:
        """Compare-and-set a delegation from `frm` to `to`, writing `fields` with it.

        Returns the updated row, or None when it was not in `frm` any more —
        another thread, or a redelivered webhook, moved it first. Raises on a
        move `TRANSITIONS` forbids, which is a bug rather than a race.
        """
        froms = (frm,) if isinstance(frm, DelegationStatus) else frm
        for f in froms:
            if to not in TRANSITIONS.get(f, frozenset()):
                raise IllegalTransition(f"{f.value} -> {to.value}")
        allowed = {
            "fix_pr", "attempt", "brief", "history", "last_run_id", "guard_result",
            "closed_reason", "error", "reviewed_by", "comment_url", "deadline",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot set {sorted(bad)} on a move")
        sets = ["status = ?", "updated_at = ?"]
        args: list[object] = [to.value, _now()]
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            args.append(json.dumps(v) if k in ("history", "guard_result") and v is not None else v)
        marks = ",".join("?" * len(froms))
        with self._conn() as db:
            row = db.execute(
                f"UPDATE delegations SET {', '.join(sets)} WHERE id = ? AND status IN ({marks})"
                " RETURNING *",
                (*args, delegation, *(f.value for f in froms)),
            ).fetchone()
        return Delegation._from_row(row) if row else None

    def note_delegation(self, delegation: int, **fields: object) -> Delegation | None:
        """Write bookkeeping fields without moving the status."""
        allowed = {"fix_pr", "error", "comment_url", "last_run_id", "deadline"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot note {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as db:
            row = db.execute(
                f"UPDATE delegations SET {sets}, updated_at = ? WHERE id = ? RETURNING *",
                (*fields.values(), _now(), delegation),
            ).fetchone()
        return Delegation._from_row(row) if row else None

    def get_delegation(self, delegation: int) -> Delegation | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM delegations WHERE id = ?", (delegation,)).fetchone()
        return Delegation._from_row(row) if row else None

    def delegation_for_branch(self, repo: str, branch: str) -> Delegation | None:
        """The open delegation working on `branch`, if any."""
        marks = ",".join("?" * len(OPEN_DELEGATION))
        with self._conn() as db:
            row = db.execute(
                f"SELECT * FROM delegations WHERE repo = ? AND fix_branch = ? AND status IN ({marks})"
                " ORDER BY id DESC LIMIT 1",
                (repo, branch, *(s.value for s in OPEN_DELEGATION)),
            ).fetchone()
        return Delegation._from_row(row) if row else None

    def open_delegation_on(self, repo: str, pr_number: int) -> Delegation | None:
        marks = ",".join("?" * len(OPEN_DELEGATION))
        with self._conn() as db:
            row = db.execute(
                f"SELECT * FROM delegations WHERE repo = ? AND pr_number = ? AND status IN ({marks})",
                (repo, pr_number, *(s.value for s in OPEN_DELEGATION)),
            ).fetchone()
        return Delegation._from_row(row) if row else None

    def delegations(
        self,
        status: DelegationStatus | tuple[DelegationStatus, ...] | None = None,
        *,
        origin_run: int | None = None,
        limit: int = 50,
    ) -> list[Delegation]:
        where: list[str] = []
        args: list[object] = []
        if status is not None:
            states = (status,) if isinstance(status, DelegationStatus) else status
            where.append(f"status IN ({','.join('?' * len(states))})")
            args += [s.value for s in states]
        if origin_run is not None:
            where.append("origin_run = ?")
            args.append(origin_run)
        sql = "SELECT * FROM delegations"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._conn() as db:
            rows = db.execute(sql, (*args, limit)).fetchall()
        return [Delegation._from_row(r) for r in rows]

    def follow_ups(self, delegation: int) -> list[RunRecord]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE delegation = ? AND kind = ? ORDER BY received_at",
                (delegation, Kind.FOLLOW_UP.value),
            ).fetchall()
        return [RunRecord._from_row(r) for r in rows]
