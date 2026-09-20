"""The read-only tools the agent is given, backed by a captured fixture.

Everything the model is allowed to look at comes through here, and nothing
reaches it that did not. Two consequences are worth stating plainly, because
they are the reason this layer exists at all rather than the agent calling
`github.py` directly:

* **Offline.** A tool call reads the fixture on disk, so a triage run costs no
  API calls, needs no token, and produces the same prompt in six weeks as it
  does today. An eval score that moves means the *agent* moved.
* **Total.** No tool raises for an absence it can describe. A missing diff is a
  fact about the run — the agent should reason about it, not crash on it. An
  exception here becomes a retry in the agent loop, which burns a turn and
  teaches the model nothing.

The matrix problem
------------------

`pydantic__35411255497` has 37 failed jobs whose logs say the same thing in 37
slightly different ways. Handing the model all of them costs ~40x the tokens for
one failure's worth of information, and buries the signal. `FailureGroup`
collapses them: jobs are fingerprinted on their failure lines with volatile
detail (versions, paths, timings, line numbers) masked out, and one
representative is shown per distinct failure.

The count that collapses is itself evidence, so it is reported rather than
hidden. "37 jobs, all failing identically across every OS and Python version"
is what separates one deterministic break fanned out over a matrix from 37
independent problems — and it is the distinction between `regression` and
`infra` on exactly that fixture.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from ci_triage.github import load_fixture, load_history, log_path_for_job
from ci_triage.logs import ANCHORS, LogExcerpt, excerpt_file
from ci_triage.models import FixtureMeta, Job, RunHistory, WorkflowRun

#: What `get_diff` says when there is no diff. A fixed, greppable phrase: the
#: prompt names it, and the eval harness checks the agent did not treat it as
#: evidence of anything.
NO_DIFF = "no diff available"

#: Detail that differs between matrix legs while the failure is the same.
#: Applied in order — paths before numbers, since a path contains digits.
_VOLATILE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z]:\\[^\s:]+|(?:/[\w.+-]+){2,}"), "<path>"),
    (re.compile(r"\b[0-9a-f]{7,40}\b"), "<sha>"),
    (re.compile(r"\b\d+(?:\.\d+)+\b"), "<ver>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    # Table borders, Unicode and ASCII alike. `rich` draws the same table with
    # heavy, light or plain-ASCII rules depending on what the runner's terminal
    # advertises, and pydantic's 36 identically-failing pytest jobs split three
    # ways on nothing but the character used to separate two columns.
    (re.compile(r"[\u2500-\u257f\u2580-\u259f|+=_-]{2,}|[\u2500-\u257f\u2580-\u259f|]"), " "),
    (re.compile(r"\s+"), " "),
)

#: Runner chrome that every job emits regardless of why it failed. Left in, it
#: is shared text that drags unrelated failures toward each other.
_BOILERPLATE = re.compile(
    r"^(\[command\]|post job cleanup|temporarily overriding home"
    r"|adding repository directory|removing (ssh|http|includeif)|cleaning up orphan"
    r"|git version|##\[group\]|##\[endgroup\]|\$ )",
    re.IGNORECASE,
)

#: Lines that say something about *why* a job failed.
_DIAGNOSTIC = re.compile(
    r"##\[error\]|\berror\b|\bfail(ed|ure|s)?\b|\bpanic\b|traceback|exception"
    r"|assert|\bfatal\b|^e \S",
    re.IGNORECASE,
)

#: Diagnostic in form but carrying no information: every failed job emits one,
#: so they are what made an anchor-line fingerprint collapse 37 pydantic jobs
#: — including the unrelated `check` gate — into a single group.
_UNINFORMATIVE = re.compile(
    r"^##\[error\]process completed with exit code"
    r"|^process completed with exit code"
    r"|^make(\[<n>\])?: \*\*\*"
    r"|^error: process didn't exit successfully"
    r"|^##\[error\]the (process|operation) .{0,40}failed with exit code",
    re.IGNORECASE,
)

#: Jaccard overlap at which two jobs are called the same failure.
_SIMILARITY = 0.5

#: Line budget the *fingerprint* is always computed at, whatever budget the
#: excerpt shown to the model uses.
#:
#: Grouping used to read the same excerpt that gets rendered, which quietly made
#: `max_lines` two knobs at once: it set how much of a failure the model sees,
#: and it decided which failures are the same failure. A tighter budget drops
#: the lowest-priority spans first, and those spans are often the lines two
#: matrix legs agree on — so the overlap falls under `_SIMILARITY` and one
#: failure splits into several, each then rendering its own representative log.
#:
#: The effect ran backwards from the intent. On `poetry__35343948952`, halving
#: `max_lines` from 300 to 150 took 8 distinct failures to 16 and rendered 29%
#: *more* log, so the one knob you would reach for to fit a smaller budget made
#: the prompt bigger. Pinning the fingerprint here leaves `max_lines` meaning
#: only what it says, and leaves grouping identical at the default.
_FINGERPRINT_LINES = 300


def _fingerprint(excerpt: LogExcerpt) -> frozenset[str]:
    """The set of lines that could distinguish this failure from another.

    Fingerprinting on anchor-matching lines alone looks tempting and is wrong:
    on `pydantic__35411255497` the only anchor that fires is GitHub's own
    `##[error]Process completed with exit code N`, which every failed job emits.
    That collapses the `check` gate job into the 36 pytest jobs — two genuinely
    different failures reported as one, with the second never shown to the
    model at all. Silently dropping a distinct failure is precisely the class of
    bug the fixture layer is careful about, so the whole excerpt is used and
    volatile detail is masked instead.
    """
    body: set[str] = set()
    for span in excerpt.spans:
        for line in span.lines:
            stripped = line.strip()
            if not stripped or _BOILERPLATE.match(stripped):
                continue
            # Box-drawing borders and rules carry no information but are long
            # and identical everywhere, so they inflate every comparison.
            if not any(ch.isalnum() for ch in stripped):
                continue
            masked = stripped
            for pattern, repl in _VOLATILE:
                masked = pattern.sub(repl, masked)
            masked = masked.strip().lower()
            if masked:
                body.add(masked)

    # Prefer the diagnostic lines. Comparing whole excerpts fails the other way
    # round from comparing anchor lines: on `sweep__30005725094` all four matrix
    # legs report the same missing permission, but each carries a different
    # platform's build output around it, so whole-body overlap is 0.29-0.46 and
    # one failure splits into three. The lines that name the fault agree
    # exactly; the lines around them are the noise.
    diagnostic = {
        line for line in body if _DIAGNOSTIC.search(line) and not _UNINFORMATIVE.match(line)
    }
    return frozenset(diagnostic or body)


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity, with two empty fingerprints counted as identical."""
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


@dataclass
class FailureGroup:
    """One distinct failure and every job exhibiting it."""

    jobs: list[Job]
    excerpt: LogExcerpt
    fingerprint: frozenset[str]

    @property
    def representative(self) -> Job:
        return self.jobs[0]

    @property
    def size(self) -> int:
        return len(self.jobs)

    def header(self) -> str:
        if self.size == 1:
            return f"--- job: {self.representative.name}"
        others = ", ".join(j.name for j in self.jobs[1:6])
        more = f", +{self.size - 6} more" if self.size > 6 else ""
        return (
            f"--- job: {self.representative.name}\n"
            f"    {self.size - 1} other job(s) failed identically: {others}{more}"
        )


class TriageContext:
    """Everything the agent may look at for one run.

    Constructed once per triage run and passed to the agent as its dependency,
    so the tools are bound to a fixture rather than reaching for global state.
    """

    def __init__(self, fixture: Path, *, max_lines: int = 300):
        self.fixture = Path(fixture)
        self.max_lines = max_lines
        self.run: WorkflowRun
        self.jobs: list[Job]
        self.meta: FixtureMeta | None
        self.run, self.jobs, self.meta = load_fixture(self.fixture)

    # -- derived views -----------------------------------------------------

    @property
    def failed_jobs(self) -> list[Job]:
        return [j for j in self.jobs if j.failed]

    @cached_property
    def history(self) -> RunHistory | None:
        return load_history(self.fixture)

    @cached_property
    def groups(self) -> list[FailureGroup]:
        """Failed jobs collapsed onto their distinct failures, largest first.

        What a job *is* and how much of it is shown are decided separately: the
        fingerprint always reads a `_FINGERPRINT_LINES` excerpt, so changing
        `max_lines` changes the size of each representative and never the number
        of representatives.
        """
        groups: list[FailureGroup] = []
        for job in self.failed_jobs:
            log = log_path_for_job(self.fixture, job)
            if log is None:
                continue
            excerpt = excerpt_file(
                log, job_name=job.name, log_path=log.name, max_lines=self.max_lines
            )
            fingerprint = _fingerprint(
                excerpt
                if self.max_lines == _FINGERPRINT_LINES
                else excerpt_file(
                    log, job_name=job.name, log_path=log.name, max_lines=_FINGERPRINT_LINES
                )
            )
            # Greedy, against the group's representative rather than against
            # every member: a chain of pairwise-similar jobs would otherwise
            # drift arbitrarily far from where it started.
            best = max(
                groups, key=lambda g: _overlap(g.fingerprint, fingerprint), default=None
            )
            if best is not None and _overlap(best.fingerprint, fingerprint) >= _SIMILARITY:
                best.jobs.append(job)
            else:
                groups.append(FailureGroup([job], excerpt, fingerprint))
        return sorted(groups, key=lambda g: (-g.size, g.representative.name))

    @property
    def jobs_without_logs(self) -> list[Job]:
        return [j for j in self.failed_jobs if log_path_for_job(self.fixture, j) is None]

    # -- the tools ---------------------------------------------------------

    def overview(self) -> str:
        """The opening brief: what ran, what broke, what can be asked about.

        Handed to the model up front rather than behind a tool call. It is
        needed on every single run, so making the agent spend a turn asking for
        it buys nothing.
        """
        run = self.run
        lines = [
            f"repository:  {run.repository.full_name}",
            f"workflow:    {run.name or '(unnamed)'}",
            f"event:       {run.event} on {run.head_branch or '(unknown branch)'}",
            f"commit:      {run.head_sha}",
            f"conclusion:  {run.conclusion} (attempt {run.run_attempt})",
            f"jobs:        {len(self.failed_jobs)} failed of {len(self.jobs)}",
            "",
        ]

        if not self.failed_jobs:
            lines.append("No job reports a failed conclusion.")
            return "\n".join(lines)

        lines.append("distinct failures:")
        for i, group in enumerate(self.groups, start=1):
            job = group.representative
            step = job.failed_steps[0].name if job.failed_steps else "(no failed step)"
            fanout = f"  [{group.size} jobs]" if group.size > 1 else ""
            lines.append(f"  {i}. {job.name}{fanout}")
            lines.append(f"     failed at step: {step}")

        if self.jobs_without_logs:
            names = ", ".join(j.name for j in self.jobs_without_logs[:5])
            lines.append("")
            lines.append(
                f"{len(self.jobs_without_logs)} failed job(s) have no log in this capture: {names}"
            )
        return "\n".join(lines)

    def get_logs(self, job_name: str | None = None) -> str:
        """Reduced log excerpts for the failed jobs.

        With no argument, one representative per distinct failure. With a job
        name, that job specifically — the model asks for one when it wants to
        check whether a matrix leg really is identical.
        """
        if not self.failed_jobs:
            return "No failed jobs in this run, so there are no failure logs to show."

        if job_name is not None:
            job = self._match_job(job_name)
            if job is None:
                known = ", ".join(j.name for j in self.failed_jobs[:10])
                return f"No failed job matching {job_name!r}. Failed jobs include: {known}"
            log = log_path_for_job(self.fixture, job)
            if log is None:
                return (
                    f"Job {job.name!r} failed but its log is not in this capture. "
                    "GitHub discards Actions logs after ~90 days; this run may have been "
                    "captured after its logs expired."
                )
            excerpt = excerpt_file(
                log, job_name=job.name, log_path=log.name, max_lines=self.max_lines
            )
            return f"--- job: {job.name}\n{excerpt.render()}"

        if not self.groups:
            return (
                f"{len(self.failed_jobs)} job(s) failed but none of their logs are in this "
                "capture, so there is no log evidence to cite."
            )

        parts = []
        if len(self.groups) < len(self.failed_jobs):
            parts.append(
                f"{len(self.failed_jobs)} failed job(s) show {len(self.groups)} distinct "
                "failure(s). One representative log per distinct failure follows; ask for a "
                "specific job by name to see its log in full."
            )
        for group in self.groups:
            parts.append(f"{group.header()}\n{group.excerpt.render()}")
        return "\n\n".join(parts)

    def get_diff(self, max_bytes: int = 60_000) -> str:
        """The diff of the commit under test.

        Returns a description of the absence rather than raising when there is
        no diff. A run can legitimately have none — the commit was force-pushed
        away, the fork was deleted, the capture predates diff collection — and
        "there is no diff" is a fact the model should weigh, not an error.

        It is deliberately not an argument against the diff-based reasoning the
        model does elsewhere: `sweep__30005314760` is a genuine regression whose
        diff never touches the broken file, so a present diff that looks
        innocent and an absent diff both mean "you cannot settle this from the
        diff alone".
        """
        patch = self.fixture / "diff.patch"
        if patch.exists():
            text = patch.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                return f"{NO_DIFF}: the captured diff is empty (an empty or merge commit)."
            if len(text) > max_bytes:
                shown = text[:max_bytes]
                cut = len(text.splitlines()) - len(shown.splitlines())
                return f"{shown}\n... diff truncated, {cut} more lines ...".rstrip()
            return text

        reason = self.fixture / "diff_unavailable.txt"
        if reason.exists():
            detail = reason.read_text(encoding="utf-8", errors="replace").strip()
            return f"{NO_DIFF}: fetching it failed — {detail}"
        return (
            f"{NO_DIFF}: it was not captured for this run. Reason the failure from the logs "
            "and run history; do not assume the diff was empty or innocent."
        )

    def test_history(self) -> str:
        """How this workflow behaved around this run.

        The only tool that can settle `flaky`, which is defined as one commit
        producing both outcomes — something a single run's logs can never show.
        """
        if self.history is None:
            return (
                "No run history was captured for this run, so there is no evidence either way "
                "about whether this commit has also passed. A `flaky` verdict is not "
                "supportable without it."
            )
        return self.history.summary()

    def raw_log(self, log_path: str) -> str | None:
        """The unreduced text behind a cited log, for `verify_evidence`.

        Citations are checked against the same normalization the excerpt was
        rendered from, which is what makes "is this quote really there?" a
        mechanical question rather than a judgement.
        """
        candidate = self.fixture / "logs" / log_path
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
        return None

    # -- helpers -----------------------------------------------------------

    def _match_job(self, name: str) -> Job | None:
        """Resolve a model-supplied job name, tolerantly.

        The model types a name back from the overview, where it may have been
        truncated or reformatted. Exact match wins; then case-insensitive; then
        a unique substring, which is rejected when ambiguous rather than
        guessing at which matrix leg was meant.
        """
        for job in self.failed_jobs:
            if job.name == name:
                return job
        lowered = name.strip().lower()
        for job in self.failed_jobs:
            if job.name.lower() == lowered:
                return job
        partial = [j for j in self.failed_jobs if lowered in j.name.lower()]
        return partial[0] if len(partial) == 1 else None
