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
from dataclasses import dataclass
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
    #: process died mid-post; the comment is keyed on a marker, so approving
    #: the run again edits rather than duplicates it.
    APPROVED = "approved"
    #: A human decided it should not be posted.
    REJECTED = "rejected"
    FAILED = "failed"


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
    UNIQUE (repo, run_id, run_attempt)
);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, received_at);
"""

#: Columns added after the first schema, and so missing from a database the
#: service created before them. `CREATE TABLE IF NOT EXISTS` will not add them.
_ADDED_COLUMNS = {"reviewed_by": "TEXT", "reviewed_at": "TEXT", "review_note": "TEXT"}


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

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, repo = self.repo.split("/", 1)
        return owner, repo

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> RunRecord:
        d = dict(row)
        d["status"] = Status(d["status"])
        d["verdict"] = json.loads(d["verdict"]) if d["verdict"] else None
        d["evidence_ok"] = None if d["evidence_ok"] is None else bool(d["evidence_ok"])
        return cls(**d)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d["status"] = self.status.value
        return d


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
            have = {r["name"] for r in db.execute("PRAGMA table_info(runs)")}
            for name, kind in _ADDED_COLUMNS.items():
                if name not in have:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")

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
    ) -> int | None:
        """Queue a run. Returns its id, or None when it is already known."""
        now = _now()
        with self._conn() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO runs (repo, run_id, run_attempt, installation, delivery,"
                " html_url, status, received_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (repo, run_id, run_attempt, installation, delivery, html_url,
                 Status.QUEUED.value, now, now),
            )
            return cur.lastrowid if cur.rowcount else None

    def claim_next(self) -> RunRecord | None:
        """Take the oldest queued run that is due, marking it running."""
        now = _now()
        with self._conn() as db:
            row = db.execute(
                "UPDATE runs SET status = ?, attempts = attempts + 1, updated_at = ?"
                " WHERE id = (SELECT id FROM runs WHERE status = ?"
                "   AND (not_before IS NULL OR not_before <= ?)"
                "   ORDER BY received_at LIMIT 1)"
                " RETURNING *",
                (Status.RUNNING.value, now, Status.QUEUED.value, now),
            ).fetchone()
        return RunRecord._from_row(row) if row else None

    def requeue_interrupted(self) -> int:
        """Put back runs a previous process claimed and never finished."""
        with self._conn() as db:
            return db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE status = ?",
                (Status.QUEUED.value, _now(), Status.RUNNING.value),
            ).rowcount

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
    ) -> None:
        with self._conn() as db:
            db.execute(
                "UPDATE runs SET status = ?, fixture = ?, verdict = ?, route = ?, reason = ?,"
                " evidence_ok = ?, comment = ?, comment_url = ?, error = ?,"
                " not_before = NULL,"
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
