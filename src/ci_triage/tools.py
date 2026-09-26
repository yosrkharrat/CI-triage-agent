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
from ci_triage.logs import ANCHORS, LogExcerpt, excerpt
from ci_triage.models import FixtureMeta, Job, RunHistory, WorkflowRun

#: What `get_diff` says when there is no diff. A fixed, greppable phrase: the
#: prompt names it, and the eval harness checks the agent did not treat it as
#: evidence of anything.
NO_DIFF = "no diff available"

#: A file header in a unified diff.
_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)

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

#: Characters of log one `get_logs` call may return, about 2.2k tokens.
#:
#: Sized against the budget that actually binds on the default model: Groq's
#: free tier refuses any single request over 8,000 tokens, and every request
#: re-sends the conversation so far. The instructions, tool definitions and
#: output schema are ~3.0k of that as Groq counts before the run has shown
#: anything, which leaves room for one call of this, one `get_diff`, the
#: history and a retry — see `RUN_CHARS` for the arithmetic.
#: Measured before this existed, 6 of 43 fixtures rendered their default view
#: over that and two rendered it at 39k; asking for one pandas matrix leg by
#: name returned 15k.
#:
#: The fingerprint never reads under this budget, for the same reason it never
#: reads under `max_lines`: how much of a failure the model is shown must not
#: decide which failures are the same failure.
LOG_CHARS = 8_000

#: Least log each distinct failure gets in the default view before some are
#: listed by name instead. Below this a representative is too short to cite.
_MIN_SHARE = 2_500

#: Characters of tool output one triage run may receive in total, about 3.6k
#: tokens.
#:
#: The per-call budgets above bound each result and not their sum, and the sum
#: is what a request carries: every turn re-sends every result so far. With
#: each call fitting, `poetry__35343948952` still 413'd at 10.3k because the
#: model read the default view and then asked for two more legs by name. That
#: is a reasonable thing to do, and past this point it cannot fit, so the tools
#: say so and the model answers from what it has — which beats a run refused
#: outright, and beats silently pruning earlier results from the history, since
#: the model would then cite from memory text it can no longer see.
#:
#: The arithmetic, from request bodies logged on the wire. Groq counts about
#: 8% more than o200k does (refused at 8,056 for a body o200k makes 7,439).
#: Instructions are 1.3k, tool definitions with the output schema 1.25k, the
#: overview up to 0.4k: ~3.0k as Groq counts. Tool output runs at ~3.6
#: characters per token, so 13k characters is ~3.9k, and what is left — about
#: 1k — covers the model's own tool calls and a retry prompt or two. The two
#: per-call budgets together (8k + 4k) fit inside it with the history.
RUN_CHARS = 13_000

#: Below this much budget a result is refused rather than cut; a few lines of
#: log are more likely to mislead than to settle anything.
_MIN_RESULT = 1_500

#: Characters of diff `get_diff` returns, about 1.2k tokens. The largest diff
#: in the corpus is 23k tokens, three times the whole request budget alone.
DIFF_CHARS = 4_000


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
    raw: str

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

    def __init__(
        self,
        fixture: Path,
        *,
        max_lines: int = 300,
        max_chars: int = LOG_CHARS,
        run_chars: int = RUN_CHARS,
    ):
        self.fixture = Path(fixture)
        self.max_lines = max_lines
        self.max_chars = max_chars
        self.run_chars = run_chars
        self.spent_chars = 0
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
            raw = log.read_text(encoding="utf-8", errors="replace")
            shown = self._excerpt(job, log.name, raw, self.max_chars)
            fingerprint = _fingerprint(
                excerpt(raw, job_name=job.name, log_path=log.name, max_lines=_FINGERPRINT_LINES)
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
                groups.append(FailureGroup([job], shown, fingerprint, raw))
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
            raw = log.read_text(encoding="utf-8", errors="replace")
            shown = self._excerpt(job, log.name, raw, self.max_chars)
            return f"--- job: {job.name}\n{shown.render()}"

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
        shown, listed = self._share_budget()
        for group, excerpt_ in shown:
            parts.append(f"{group.header()}\n{excerpt_.render()}")
        if listed:
            names = "\n".join(f"  {g.representative.name}  [{g.size} job(s)]" for g in listed)
            parts.append(
                f"{len(listed)} more distinct failure(s), not shown to stay within the size "
                f"limit. Ask for one by job name to see its log:\n{names}"
            )
        return "\n\n".join(parts)

    def _share_budget(self) -> tuple[list[tuple[FailureGroup, LogExcerpt]], list[FailureGroup]]:
        """Split `max_chars` across the distinct failures, largest first.

        Every representative at its own full budget is what the default view
        used to be, and on `poetry__35343948952` that was 39k tokens of 8
        distinct failures. Each now gets an even share, and when there are too
        many for a share worth reading, the smallest groups are named rather
        than shown — the model can ask for any of them, and at least knows they
        exist.
        """
        groups = self.groups
        if sum(len(g.excerpt.render()) for g in groups) <= self.max_chars:
            return [(g, g.excerpt) for g in groups], []
        count = max(1, min(len(groups), self.max_chars // _MIN_SHARE))
        share = self.max_chars // count
        shown = [
            (g, self._excerpt(g.representative, g.excerpt.log_path, g.raw, share))
            for g in groups[:count]
        ]
        return shown, groups[count:]

    def _excerpt(self, job: Job, log_path: str, raw: str, max_chars: int) -> LogExcerpt:
        return excerpt(
            raw, job_name=job.name, log_path=log_path, max_lines=self.max_lines, max_chars=max_chars
        )

    def get_diff(self, max_bytes: int = DIFF_CHARS) -> str:
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
                # The head of a diff is whichever files sort first, not the ones
                # that matter, so name every file the cut hid. That is often
                # enough to tell whether the failing module was touched at all.
                seen = set(_DIFF_FILE.findall(shown))
                hidden = [f for f in dict.fromkeys(_DIFF_FILE.findall(text)) if f not in seen]
                tail = f"\n... diff truncated, {cut} more lines ..."
                if hidden:
                    more = f", +{len(hidden) - 40} more" if len(hidden) > 40 else ""
                    tail += f"\nfiles changed in the part not shown: {', '.join(hidden[:40])}{more}"
                return f"{shown}{tail}".rstrip()
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

    # -- the run's budget --------------------------------------------------

    def metered(self, text: str) -> str:
        """`text` as far as this run's remaining budget allows.

        Only the agent's tools go through this. The tool methods themselves stay
        unmetered, because the cache key and `inspect` read them too, and
        neither is a model spending a request. A result that does not fit is
        cut at a line boundary, and one that would leave too little to be worth
        reading is refused; both say so, in words the model can act on.
        """
        left = self.run_chars - self.spent_chars
        if len(text) <= left:
            self.spent_chars += len(text)
            return text
        if left < _MIN_RESULT:
            return (
                f"Not shown: this run has already received {self.spent_chars:,} characters of "
                "tool output, which is all one request can carry. Answer from what you have "
                "seen, and cite only lines that were shown to you."
            )
        cut = text[:left].rsplit("\n", 1)[0]
        self.spent_chars += len(cut)
        return (
            f"{cut}\n... cut here: {len(text) - len(cut):,} more characters would not fit in "
            "what one request can carry. Nothing further can be shown this run."
        )

    def reset_meter(self) -> None:
        self.spent_chars = 0

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
