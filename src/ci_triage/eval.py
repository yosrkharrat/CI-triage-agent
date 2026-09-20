"""Scoring the agent over the captured corpus.

Two numbers come out of a sweep, and only one of them needs a human:

* **Citation verification rate** — of every quote the model cited, what share is
  really at the coordinates it gave. `verify_evidence` answers that against the
  log itself, so it needs no ground truth and runs over every captured fixture
  from the first day, labelled or not.
* **Category accuracy** — whether the verdict matches the human label. This one
  needs a label, so it covers the labelled subset and widens as labelling does.

Reporting them together is the point rather than a convenience. Accuracy alone
scores a model that reached the right category on invented evidence as a
success, which is the single most expensive thing this system could do: a
confident, correct-sounding comment on a stranger's PR citing a log line that
does not exist. Citation rate alone scores a model that cites impeccably and
concludes wrongly. The pair is what the routing policy is actually trading
between, so `unsound_auto_posts` — verdicts that would have been posted while
carrying a citation that fails verification — is reported next to both.

Cost, and why a sweep is resumable
----------------------------------

Only the model's answer is cached (`agent._store_verdict`), keyed on a hash of
the prompt. So a sweep interrupted by a rate limit resumes for the price of the
fixtures it never reached, and *re-scoring* is free forever after: tightening
`verify_evidence` or moving the confidence threshold re-reads the same stored
verdicts. On a free tier that split is what makes the harness usable at all —
the expensive half is the half that does not change when you improve the cheap
half.

`--cached-only` leans on the same property: it reports over exactly the verdicts
already paid for and calls no model, which is what you want on the day the daily
quota is gone and you still need the number.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from pydantic_ai.exceptions import AgentRunError, ModelHTTPError

from ci_triage.agent import MODEL, EvidenceCheck, TriageResult, is_answered, triage
from ci_triage.github import load_fixture
from ci_triage.models import FailureCategory, Label, Route, Verdict, route

FIXTURES = Path("fixtures")


# --------------------------------------------------------------------------
# Why a citation failed
# --------------------------------------------------------------------------


class CitationFault(str, Enum):
    """How a citation failed verification.

    Worth separating because the faults mean different things about the model.
    `NOT_FOUND` is the interesting one — text that was never in those lines, the
    failure the whole evidence schema exists to catch. `OUT_OF_RANGE` and
    `MISSING_LOG` are closer to bookkeeping: the model read something real and
    addressed it wrongly. Both are wrong and neither may be posted, but a model
    that only ever makes the second kind is a prompt fix away from being useful,
    and one making the first is not.
    """

    MISSING_LOG = "log does not exist"
    OUT_OF_RANGE = "line beyond end of log"
    BAD_RANGE = "inverted line range"
    EMPTY_QUOTE = "empty quote"
    NOT_FOUND = "quote not in the cited lines"
    OTHER = "other"


#: Matched against the reason text `check_evidence` produced. Substrings rather
#: than whole messages because the messages interpolate line numbers and paths;
#: each one here is a fragment `tests/test_eval.py` drives out of the real
#: verifier, so a reworded message fails a test instead of silently landing
#: every fault in `OTHER`.
_FAULT_MARKERS: tuple[tuple[str, CitationFault], ...] = (
    ("no log named", CitationFault.MISSING_LOG),
    ("beyond end of log", CitationFault.OUT_OF_RANGE),
    ("precedes line_start", CitationFault.BAD_RANGE),
    ("empty quote", CitationFault.EMPTY_QUOTE),
    ("does not appear", CitationFault.NOT_FOUND),
)


def classify_fault(reason: str) -> CitationFault:
    """Bucket one failed check's reason text."""
    lowered = reason.lower()
    for marker, fault in _FAULT_MARKERS:
        if marker in lowered:
            return fault
    return CitationFault.OTHER


# --------------------------------------------------------------------------
# One fixture's result
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureScore:
    """What the sweep learned about one captured run.

    `result` is None when the run never produced a verdict. That is recorded
    rather than raised: a model that cannot be made to emit a valid `Verdict` in
    five retries has told you something about itself, and on a corpus this size
    one unparseable answer must not end the sweep.
    """

    fixture: str
    label: Label | None
    result: TriageResult | None = None
    error: str | None = None
    seconds: float = 0.0

    # -- the verdict -------------------------------------------------------

    @property
    def verdict(self) -> Verdict | None:
        return self.result.verdict if self.result else None

    @property
    def predicted(self) -> FailureCategory | None:
        return self.result.verdict.category if self.result else None

    @property
    def correct(self) -> bool | None:
        """True/False when this fixture is both labelled and answered."""
        if self.result is None or self.label is None:
            return None
        return self.label.category is self.result.verdict.category

    @property
    def abstained(self) -> bool:
        return self.predicted is FailureCategory.UNKNOWN

    # -- the citations -----------------------------------------------------

    @property
    def checks(self) -> tuple[EvidenceCheck, ...]:
        return self.result.checks if self.result else ()

    @property
    def cited(self) -> int:
        return len(self.checks)

    @property
    def verified(self) -> int:
        return sum(c.ok for c in self.checks)

    @property
    def evidence_ok(self) -> bool:
        """Every citation holds up. False for a run that produced no verdict."""
        return self.result is not None and self.result.evidence_ok

    @property
    def faults(self) -> Counter[CitationFault]:
        return Counter(classify_fault(c.reason) for c in self.checks if not c.ok)

    # -- what it cost ------------------------------------------------------

    @property
    def tokens(self) -> int:
        usage = self.result.usage if self.result else None
        if usage is None:
            return 0
        return (usage.input_tokens or 0) + (usage.output_tokens or 0)

    @property
    def requests(self) -> int:
        usage = self.result.usage if self.result else None
        return usage.requests if usage else 0

    @property
    def cached(self) -> bool:
        return bool(self.result and self.result.cached)

    def to_dict(self) -> dict:
        return {
            "fixture": self.fixture,
            "label": self.label.category.value if self.label else None,
            "predicted": self.predicted.value if self.predicted else None,
            "correct": self.correct,
            "confidence": self.verdict.confidence if self.verdict else None,
            "route": self.result.route.value if self.result else None,
            "cited": self.cited,
            "verified": self.verified,
            "faults": {f.value: n for f, n in self.faults.items()},
            "tokens": self.tokens,
            "requests": self.requests,
            "cached": self.cached,
            "seconds": round(self.seconds, 1),
            "error": self.error,
            "verdict": self.verdict.model_dump(mode="json") if self.verdict else None,
        }


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

#: Confidence cuts the threshold sweep walks. `route()` is a pure function of
#: the verdict, so every one of these is re-derived from verdicts already paid
#: for — the sweep costs nothing and needs no re-run.
THRESHOLDS: tuple[float, ...] = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


@dataclass(frozen=True)
class EvalReport:
    model: str
    max_lines: int
    scores: tuple[FixtureScore, ...]
    started_at: datetime
    seconds: float
    stopped_early: str | None = None

    # -- subsets -----------------------------------------------------------

    @property
    def answered(self) -> list[FixtureScore]:
        """Fixtures that produced a verdict at all."""
        return [s for s in self.scores if s.result is not None]

    @property
    def errored(self) -> list[FixtureScore]:
        return [s for s in self.scores if s.result is None]

    @property
    def labelled(self) -> list[FixtureScore]:
        """Answered *and* labelled — the only rows accuracy can be read from."""
        return [s for s in self.answered if s.label is not None]

    @property
    def oversized(self) -> list[FixtureScore]:
        """Runs whose request exceeded the provider's per-request token cap.

        Counted apart from the quality metrics because the request never reached
        the model, so it is not an answer the model got wrong. It is not a
        property of the fixture either, which is the part that took a sweep to
        learn: on `gpt-oss-20b`'s 8k-per-minute free tier, 37 of 43 fixtures
        returned 413, and the rejected sizes clustered at 8.2k-11k while the
        logs behind them ranged from 489 to 34,662 tokens. The logs were never
        in those requests.

        What fills them is the agent loop. Every turn re-sends the whole
        conversation, reasoning traces included, and `RETRIES` allows five
        rounds of a model getting the schema wrong before the verdict is
        abandoned. `core__35505915015` answers in 2 requests and 4,089 tokens
        when the model gets the tool call right the first time, and 413s at
        9,020 when it does not — same fixture, same prompt, same budget.

        So a 413 here is stochastic and a re-run is worth making: it says the
        model spent its budget arguing with the schema, which is a fact about
        the model on a small budget rather than about the run being triaged.
        Groq distinguishes the two cases cleanly — a request that is itself too
        large is 413, a request that does not fit the *remaining* window is 429
        — so this never silently absorbs ordinary rate limiting.
        """
        return [s for s in self.errored if (s.error or "").startswith(str(_TOO_LARGE))]

    # -- citation verification (no labels needed) --------------------------

    @property
    def cited(self) -> int:
        return sum(s.cited for s in self.answered)

    @property
    def verified(self) -> int:
        return sum(s.verified for s in self.answered)

    @property
    def citation_rate(self) -> float | None:
        """Share of all citations that survive being looked up."""
        return self.verified / self.cited if self.cited else None

    @property
    def sound_verdicts(self) -> int:
        return sum(s.evidence_ok for s in self.answered)

    @property
    def sound_verdict_rate(self) -> float | None:
        """Share of verdicts in which *every* citation holds up.

        Stricter than `citation_rate` and the one a reviewer feels: a verdict is
        only as trustworthy as its weakest citation, and a reader who finds one
        invented quote stops believing the other three.
        """
        return self.sound_verdicts / len(self.answered) if self.answered else None

    @property
    def faults(self) -> Counter[CitationFault]:
        total: Counter[CitationFault] = Counter()
        for score in self.answered:
            total += score.faults
        return total

    # -- accuracy (labels needed) -----------------------------------------

    @property
    def correct(self) -> int:
        return sum(bool(s.correct) for s in self.labelled)

    @property
    def accuracy(self) -> float | None:
        return self.correct / len(self.labelled) if self.labelled else None

    @property
    def confusion(self) -> Counter[tuple[str, str]]:
        """(label, predicted) pairs where they disagree, commonest first."""
        return Counter(
            (s.label.category.value, s.predicted.value)  # type: ignore[union-attr]
            for s in self.labelled
            if s.correct is False
        )

    @property
    def per_class(self) -> dict[str, tuple[int, int]]:
        """`{category: (labelled, correct)}` over the ground-truth labels.

        Reported beside the accuracy because a single figure over an unbalanced
        corpus hides which class it was earned on. `flaky` is the case that
        forces the issue: it is decidable only where the history records one
        commit both passing and failing, which two of the 43 captured runs do,
        so that class stays tiny however long the labelling continues. An
        accuracy that quietly averages over two flaky fixtures and twenty
        regressions is not wrong, but it is not the number anyone thinks it is.
        """
        counts: dict[str, tuple[int, int]] = {}
        for score in self.labelled:
            assert score.label is not None
            key = score.label.category.value
            labelled, correct = counts.get(key, (0, 0))
            counts[key] = (labelled + 1, correct + bool(score.correct))
        return dict(sorted(counts.items()))

    @property
    def abstentions(self) -> int:
        return sum(s.abstained for s in self.answered)

    # -- routing -----------------------------------------------------------

    @property
    def auto_posts(self) -> list[FixtureScore]:
        return [s for s in self.answered if s.result and s.result.route is Route.AUTO_POST]

    @property
    def unsound_auto_posts(self) -> list[FixtureScore]:
        """Would have been posted, carrying a citation that does not verify.

        The number this harness exists to put a figure on, and it needs no
        ground truth: a comment on someone's pull request quoting a log line
        that is not in the log. Everything else here is a quality metric; this
        one is a liability.
        """
        return [s for s in self.auto_posts if not s.evidence_ok]

    @property
    def wrong_auto_posts(self) -> list[FixtureScore]:
        """Would have been posted, and disagrees with the human label."""
        return [s for s in self.auto_posts if s.correct is False]

    def threshold_sweep(
        self, thresholds: Sequence[float] = THRESHOLDS
    ) -> list[tuple[float, int, int, int]]:
        """`(threshold, auto-posted, of which correct, of which unsound)`.

        Re-derived from stored verdicts, so moving the confidence cut costs
        nothing. `correct` counts only labelled rows; `unsound` counts every
        answered one, which is why the two columns do not add up and should not.
        """
        rows = []
        for t in thresholds:
            posted = [
                s for s in self.answered
                if s.verdict and route(s.verdict, t)[0] is Route.AUTO_POST
            ]
            correct = sum(1 for s in posted if s.correct is True)
            unsound = sum(1 for s in posted if not s.evidence_ok)
            rows.append((t, len(posted), correct, unsound))
        return rows

    # -- cost --------------------------------------------------------------

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.answered)

    @property
    def tokens_spent(self) -> int:
        """Tokens this sweep actually paid for — cache hits cost nothing."""
        return sum(s.tokens for s in self.answered if not s.cached)

    @property
    def requests(self) -> int:
        return sum(s.requests for s in self.answered if not s.cached)

    # -- output ------------------------------------------------------------

    def render(self) -> str:
        total = len(self.scores)
        lines = [
            f"eval — {self.model}, {total} fixture(s), {_duration(self.seconds)}",
            "",
            _row("fixture", "label", "verdict", "conf", "route", "cites", "note"),
            _row(*("-" * min(w, _NOTE_MAX) for w in _WIDTHS)),
        ]
        for s in self.scores:
            if s.result is None:
                lines.append(_row(s.fixture, _cat(s.label), "—", "—", "—", "—", s.error or "no verdict"))
                continue
            mark = "" if s.correct is None else ("ok" if s.correct else "WRONG")
            note = " ".join(
                filter(
                    None,
                    [
                        mark,
                        " ".join(
                            f"{n}x {_SHORT_FAULT[f]}" if n > 1 else _SHORT_FAULT[f]
                            for f, n in s.faults.most_common()
                        ),
                    ],
                )
            )
            lines.append(
                _row(
                    s.fixture,
                    _cat(s.label),
                    s.predicted.value if s.predicted else "—",
                    f"{s.verdict.confidence:.2f}" if s.verdict else "—",
                    _ROUTE_ABBREV[s.result.route],
                    f"{s.verified}/{s.cited}",
                    note,
                )
            )

        lines += ["", "-- citations " + "-" * 48]
        if self.citation_rate is None:
            lines.append("no citations to verify")
        else:
            lines.append(
                f"verified        {self.verified} of {self.cited} citations "
                f"({self.citation_rate:.0%}) across {len(self.answered)} verdict(s) — "
                "no labels needed"
            )
            lines.append(
                f"sound verdicts  {self.sound_verdicts} of {len(self.answered)} "
                f"({self.sound_verdict_rate:.0%}) cite nothing that fails verification"
            )
            if faults := self.faults:
                detail = "  ".join(f"{f.value}: {c}" for f, c in faults.most_common())
                lines.append(f"faults          {detail}")

        lines += ["", "-- categories " + "-" * 47]
        if not self.labelled:
            unlabelled = len(self.answered)
            lines.append(
                f"no labelled fixtures in this sweep — {unlabelled} verdict(s) have "
                "nothing to be scored against"
            )
            lines.append("label one with: uv run ci-triage label <fixture> -c <category> -r '...'")
        else:
            lines.append(
                f"accuracy        {self.correct} of {len(self.labelled)} correct "
                f"({self.accuracy:.0%}) — labelled fixtures only, "
                f"{len(self.labelled)} of {total} in this sweep"
            )
            by_class = "  ".join(
                f"{cat} {correct}/{n}" for cat, (n, correct) in self.per_class.items()
            )
            lines.append(f"by class        {by_class}")
            if confusion := self.confusion:
                detail = "  ".join(f"{a} -> {b}: {c}" for (a, b), c in confusion.most_common())
                lines.append(f"confusion       {detail}")
        if self.answered:
            lines.append(
                f"abstained       {self.abstentions} verdict(s) answered `unknown`"
            )

        lines += ["", "-- routing " + "-" * 50]
        posted = len(self.auto_posts)
        lines.append(
            f"auto_post       {posted} of {len(self.answered)} verdict(s); "
            f"{len(self.answered) - posted} to human review"
        )
        if unsound := self.unsound_auto_posts:
            lines.append(
                f"unsound         {len(unsound)} auto-post(s) carry a citation that does "
                "not verify — these would have been posted"
            )
            for s in unsound[:5]:
                lines.append(f"                  {s.fixture}")
        if wrong := self.wrong_auto_posts:
            lines.append(f"wrong           {len(wrong)} auto-post(s) disagree with their label")

        if self.labelled:
            lines += ["", "-- confidence threshold " + "-" * 37]
            lines.append("threshold   auto-posted   correct   unsound")
            for t, n_posted, n_correct, n_unsound in self.threshold_sweep():
                lines.append(
                    f"  {t:.2f}      {n_posted:>9}   {n_correct:>7}   {n_unsound:>7}"
                )

        lines += ["", "-- cost " + "-" * 53]
        lines.append(
            f"tokens          {self.tokens:,} total; {self.tokens_spent:,} spent this sweep "
            f"over {self.requests} request(s)"
        )
        if self.errored:
            lines.append("")
            lines.append(f"{len(self.errored)} fixture(s) produced no verdict:")
            # Capped: a sweep run before anything is cached lists all 43 with
            # the same reason, which buries every number above it.
            for s in self.errored[:8]:
                lines.append(f"  {s.fixture}: {s.error}")
            if rest := self.errored[8:]:
                commonest, repeats = Counter(s.error for s in rest).most_common(1)[0]
                lines.append(
                    f"  ... and {len(rest)} more"
                    + (f", {repeats} of them: {commonest}" if repeats > 1 else "")
                )
        if oversized := self.oversized:
            lines.append("")
            lines.append(
                f"{len(oversized)} of those never reached the model: the request itself was "
                "larger than this account's per-request cap."
            )
            lines.append(
                "  usually the agent loop rather than the logs — every turn re-sends the whole "
                "conversation with its reasoning, so a model that retries its way through the "
                "schema can cross the cap on a fixture whose logs are tiny. Worth re-running; "
                "the same fixture often answers on a cleaner attempt."
            )
            for s in oversized[:5]:
                lines.append(f"  {s.fixture}")

        if self.stopped_early:
            lines.append("")
            lines.append(f"STOPPED EARLY — {self.stopped_early}")
            lines.append(
                "verdicts already answered are cached, so re-running resumes "
                "rather than starting over."
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "max_lines": self.max_lines,
            "started_at": self.started_at.isoformat(),
            "seconds": round(self.seconds, 1),
            "stopped_early": self.stopped_early,
            "fixtures": len(self.scores),
            "answered": len(self.answered),
            "oversized": [s.fixture for s in self.oversized],
            "citations": {
                "cited": self.cited,
                "verified": self.verified,
                "rate": self.citation_rate,
                "sound_verdicts": self.sound_verdicts,
                "sound_verdict_rate": self.sound_verdict_rate,
                "faults": {f.value: n for f, n in self.faults.items()},
            },
            "categories": {
                "labelled": len(self.labelled),
                "correct": self.correct,
                "accuracy": self.accuracy,
                "per_class": {
                    cat: {"labelled": n, "correct": correct}
                    for cat, (n, correct) in self.per_class.items()
                },
                "confusion": {f"{a}->{b}": n for (a, b), n in self.confusion.items()},
                "abstentions": self.abstentions,
            },
            "routing": {
                "auto_post": len(self.auto_posts),
                "human_review": len(self.answered) - len(self.auto_posts),
                "unsound_auto_posts": [s.fixture for s in self.unsound_auto_posts],
                "wrong_auto_posts": [s.fixture for s in self.wrong_auto_posts],
                "threshold_sweep": [
                    {"threshold": t, "auto_posted": p, "correct": c, "unsound": u}
                    for t, p, c, u in self.threshold_sweep()
                ],
            },
            "cost": {
                "tokens": self.tokens,
                "tokens_spent": self.tokens_spent,
                "requests": self.requests,
            },
            "scores": [s.to_dict() for s in self.scores],
        }


def _cat(label: Label | None) -> str:
    return label.category.value if label else "—"


#: Column widths for the per-fixture table. The first is the longest fixture
#: name in the corpus (`scikit-learn__35425065902`) rather than a round number,
#: because a truncated name is not a name you can pass back to `ci-triage
#: triage`. The last column is not padded or cut, so a long error still reads in
#: full on a wide terminal; the CLI prints with `soft_wrap` so that a report
#: piped to a file keeps one row per line.
_WIDTHS: tuple[int, ...] = (25, 10, 10, 4, 5, 5, 16)

#: The route values are `auto_post`/`human_review`, which cost twelve columns to
#: say what five can. The full value is what goes into the JSON report.
_ROUTE_ABBREV = {Route.AUTO_POST: "post", Route.HUMAN_REVIEW: "human"}

#: Fault names short enough for a table cell; `CitationFault.value` is the
#: readable form and is what the summary block and the JSON report use.
_SHORT_FAULT = {
    CitationFault.MISSING_LOG: "no-log",
    CitationFault.OUT_OF_RANGE: "range",
    CitationFault.BAD_RANGE: "inverted",
    CitationFault.EMPTY_QUOTE: "empty",
    CitationFault.NOT_FOUND: "invented",
    CitationFault.OTHER: "other",
}


#: A provider's 429 body runs to 300 characters — quota, usage and a reset time.
#: All of it is worth keeping, none of it belongs in a table cell.
_NOTE_MAX = 48


def _row(*cells: str) -> str:
    cells = cells[:-1] + (_clip(cells[-1], _NOTE_MAX),)
    return "  ".join(
        cell if i == len(cells) - 1 else f"{cell[:w]:<{w}}"
        for i, (cell, w) in enumerate(zip(cells, _WIDTHS))
    ).rstrip()


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


# --------------------------------------------------------------------------
# Running a sweep
# --------------------------------------------------------------------------


def find_fixtures(
    root: Path = FIXTURES,
    *,
    names: Sequence[str] = (),
    labelled_only: bool = False,
) -> list[Path]:
    """Captured runs to score, in a stable order.

    Sorted by name rather than by capture time so two sweeps are comparable
    row for row, and a diff between two reports is readable.
    """
    if names:
        paths = [root / n if not Path(n).exists() else Path(n) for n in names]
    else:
        paths = sorted(p for p in root.iterdir() if (p / "run.json").exists()) if root.is_dir() else []
    if labelled_only:
        paths = [p for p in paths if _label_of(p) is not None]
    return paths


def _label_of(path: Path) -> Label | None:
    try:
        _, _, meta = load_fixture(path)
    except (OSError, ValueError):
        return None
    return meta.label if meta else None


#: HTTP status for a request the provider refused to even read, because it was
#: larger than the per-request or per-minute token cap.
_TOO_LARGE = 413

#: Consecutive failures before a sweep gives up. One bad fixture is a data
#: point; three in a row is the provider, and grinding through forty more to
#: learn the same thing wastes the quota that would have answered them.
_MAX_CONSECUTIVE_ERRORS = 3


def run_eval(
    fixtures: Sequence[Path],
    *,
    model: str = MODEL,
    max_lines: int = 300,
    cache: bool = True,
    cached_only: bool = False,
    on_start: Callable[[int, Path], None] | None = None,
    on_score: Callable[[FixtureScore], None] | None = None,
) -> EvalReport:
    """Triage every fixture and score the results.

    Sequential on purpose. The free tier meters tokens per minute and a triage
    run re-sends the whole conversation on every turn, so concurrency here buys
    a faster path to the same 429 rather than a faster sweep.

    A sweep never dies on one fixture. A rate limit stops it — every later call
    would hit the same wall — but the report is still returned, marked with why
    it stopped, and re-running picks up where it left off from the cache.
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.monotonic()
    scores: list[FixtureScore] = []
    stopped: str | None = None
    consecutive = 0

    for i, path in enumerate(fixtures, start=1):
        if on_start:
            on_start(i, path)
        label = _label_of(path)

        if cached_only and not is_answered(path, model=model, max_lines=max_lines):
            score = FixtureScore(path.name, label, error="not in the verdict cache")
            scores.append(score)
            if on_score:
                on_score(score)
            continue

        step = time.monotonic()
        try:
            result = triage(path, model=model, max_lines=max_lines, cache=cache)
        except ModelHTTPError as exc:
            detail = _http_detail(exc)
            score = FixtureScore(path.name, label, error=detail, seconds=time.monotonic() - step)
            scores.append(score)
            if on_score:
                on_score(score)
            if exc.status_code == 429:
                stopped = f"rate limited by {exc.model_name}: {detail}"
                break
            # A 413 is a property of this fixture against this account's budget:
            # deterministic, and the next fixture may well be smaller. Letting a
            # run of oversized fixtures trip the give-up streak would abandon a
            # sweep over something no retry could have fixed.
            if exc.status_code != _TOO_LARGE:
                consecutive += 1
        except AgentRunError as exc:
            score = FixtureScore(
                path.name,
                label,
                error=f"{type(exc).__name__}: {exc}"[:200],
                seconds=time.monotonic() - step,
            )
            scores.append(score)
            if on_score:
                on_score(score)
            consecutive += 1
        else:
            score = FixtureScore(path.name, label, result, seconds=time.monotonic() - step)
            scores.append(score)
            if on_score:
                on_score(score)
            consecutive = 0

        if consecutive >= _MAX_CONSECUTIVE_ERRORS:
            stopped = f"{consecutive} fixtures failed in a row"
            break

    return EvalReport(
        model=model,
        max_lines=max_lines,
        scores=tuple(scores),
        started_at=started_at,
        seconds=time.monotonic() - t0,
        stopped_early=stopped,
    )


def _http_detail(exc: ModelHTTPError) -> str:
    """The provider's own message, which is where a quota reset time lives."""
    body = exc.body if isinstance(exc.body, dict) else {}
    if isinstance(err := body.get("error"), dict):
        if message := err.get("message"):
            return f"{exc.status_code}: {message}"[:300]
    return f"{exc.status_code}: {exc}"[:300]


def write_report(report: EvalReport, path: Path) -> Path:
    """Write the full report as JSON, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2))
    return path
