"""Deterministic log reduction.

A single failed GitHub Actions run in `fixtures/` is ~5,000 lines / 550 KB.
Feeding that to a model is slow, expensive, and *worse* — the signal is three
lines buried in setup noise. So before the agent sees anything, plain code does
the reduction:

    raw log  ->  normalize()  ->  find anchors  ->  windows  ->  merge  ->  render

Two properties matter and are worth stating plainly:

* **Deterministic.** No model runs here. The same log always reduces the same
  way, so a change in agent behaviour is never confounded by a change in what
  it was shown.
* **Addressable.** `normalize()` is a pure function, so line N of the
  normalized log is stable. The agent cites `(log_path, line_start, line_end)`
  and `verify_evidence()` can mechanically confirm the quote is really there.
  That check is what makes "is the cited evidence sound?" an answerable
  question rather than a vibe.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from ci_triage.models import Evidence

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def normalize(raw: str) -> list[str]:
    """Strip per-line timestamps, ANSI colour, and CR; return 1-based lines.

    Timestamps are ~29 bytes on every line — roughly a third of the file and
    pure noise to the model. Index i of the result is line i+1.
    """
    out = []
    for line in raw.splitlines():
        line = _TIMESTAMP.sub("", line)
        line = _ANSI.sub("", line)
        out.append(line.replace("\r", "").rstrip())
    return out


class AnchorPattern(BaseModel):
    """A signal that says 'the failure is around here'.

    `before`/`after` differ per pattern because causes sit on different sides of
    their marker: a Python traceback's real exception is *below* the header,
    while `Process completed with exit code 1` has everything useful *above* it.
    """

    name: str
    pattern: re.Pattern
    priority: int  # higher wins when the line budget is tight
    # NB: ANCHORS is kept in descending priority order, because `excerpt()`
    # breaks on the first pattern that matches a line. Tuple position is match
    # precedence; `priority` is budget precedence. They must not disagree.
    before: int
    after: int

    model_config = {"arbitrary_types_allowed": True, "frozen": True}


ANCHORS: tuple[AnchorPattern, ...] = (
    # GitHub's own error annotation — the runner already decided this is the failure.
    AnchorPattern(name="gha_error", pattern=re.compile(r"##\[error\]"), priority=100, before=25, after=8),
    # pytest's own verdict. Without this the only thing marking a failed test
    # run is GitHub's `##[error]` at the very end, so the excerpt catches the
    # failing test names only when they happen to sit within `before` lines of
    # it. On `pydantic__35411255497` they did, by luck; a suite with slow
    # teardown after the summary would have shown the model nothing but exit
    # codes and git cleanup.
    AnchorPattern(
        name="test_summary",
        pattern=re.compile(
            r"Summary of Failures|short test summary info|^=+ FAILURES =+|^\s*\d+ failed\b"
        ),
        priority=95,
        before=30,
        after=12,
    ),
    AnchorPattern(
        name="exit_code",
        pattern=re.compile(r"Process completed with exit code [1-9]"),
        priority=90,
        before=40,
        after=2,
    ),
    AnchorPattern(
        name="traceback",
        pattern=re.compile(r"Traceback \(most recent call last\)|^\s*panic:|Segmentation fault"),
        priority=80,
        before=5,
        after=30,
    ),
    # pytest marks the raised exception with a leading `E` and indentation.
    # This is the line that names *what* went wrong, as opposed to which test.
    AnchorPattern(
        name="assertion_detail",
        pattern=re.compile(r"^E\s{2,}\S"),
        priority=75,
        before=12,
        after=4,
    ),
    AnchorPattern(
        name="test_failure",
        pattern=re.compile(r"\bFAILED\b|\bAssertionError\b|^\s*[✕✗×]\s|\bFAIL\b"),
        priority=70,
        before=10,
        after=15,
    ),
    AnchorPattern(
        name="compiler_error",
        pattern=re.compile(r"^\s*error(\[[A-Z]?\d+\])?:|^\s*ERROR\b"),
        priority=60,
        before=8,
        after=20,
    ),
)


class Span(BaseModel):
    """A contiguous window of normalized log lines, 1-based and inclusive."""

    line_start: int
    line_end: int
    lines: list[str]
    anchors: list[str] = Field(default_factory=list)

    @property
    def length(self) -> int:
        return self.line_end - self.line_start + 1


class LogExcerpt(BaseModel):
    """The reduced view of one log file that the agent is allowed to see."""

    job_name: str
    log_path: str
    total_lines: int
    spans: list[Span]
    truncated: bool = False

    @property
    def kept_lines(self) -> int:
        return sum(s.length for s in self.spans)

    @property
    def reduction(self) -> float:
        """Fraction of lines discarded, 0.0-1.0."""
        return 1.0 - (self.kept_lines / self.total_lines) if self.total_lines else 0.0

    def render(self) -> str:
        """Format for the model: line-numbered spans with explicit gap markers.

        Gaps are labelled rather than silently elided so the model knows it is
        looking at a excerpt and can ask for more rather than assuming the log
        simply ended.
        """
        parts = [f"=== {self.log_path} ({self.total_lines} lines total) ==="]
        cursor = 1
        for span in self.spans:
            if span.line_start > cursor:
                parts.append(f"  ... {span.line_start - cursor} lines omitted ...")
            width = len(str(span.line_end))
            parts.extend(
                f"{n:>{width}} | {text}"
                for n, text in enumerate(span.lines, start=span.line_start)
            )
            cursor = span.line_end + 1
        if cursor <= self.total_lines:
            parts.append(f"  ... {self.total_lines - cursor + 1} lines omitted ...")
        return "\n".join(parts)


def _merge(spans: list[Span], lines: list[str]) -> list[Span]:
    """Collapse overlapping or adjacent windows into single spans."""
    if not spans:
        return []
    spans = sorted(spans, key=lambda s: s.line_start)
    merged = [spans[0]]
    for span in spans[1:]:
        last = merged[-1]
        # Join when they touch or leave a gap too small for an omission marker
        # to be worth it.
        if span.line_start <= last.line_end + 3:
            end = max(last.line_end, span.line_end)
            merged[-1] = Span(
                line_start=last.line_start,
                line_end=end,
                lines=lines[last.line_start - 1 : end],
                anchors=sorted(set(last.anchors) | set(span.anchors)),
            )
        else:
            merged.append(span)
    return merged


def excerpt(
    raw: str,
    *,
    job_name: str,
    log_path: str,
    max_lines: int = 300,
    tail_fallback: int = 60,
) -> LogExcerpt:
    """Reduce one raw log to the windows around its failure signals.

    `max_lines` is a hard budget. When anchors produce more than that, the
    lowest-priority spans are dropped first — better to show the whole of the
    strongest signal than fragments of everything.
    """
    lines = normalize(raw)
    total = len(lines)

    scored: list[tuple[int, Span]] = []
    for i, text in enumerate(lines, start=1):
        for anchor in ANCHORS:
            if anchor.pattern.search(text):
                start = max(1, i - anchor.before)
                end = min(total, i + anchor.after)
                scored.append(
                    (anchor.priority, Span(
                        line_start=start,
                        line_end=end,
                        lines=lines[start - 1 : end],
                        anchors=[anchor.name],
                    ))
                )
                break  # one anchor per line; patterns are ordered by strength

    if not scored:
        # No recognised signal. The failing output is almost always at the end,
        # so show the tail rather than nothing.
        start = max(1, total - tail_fallback + 1)
        return LogExcerpt(
            job_name=job_name,
            log_path=log_path,
            total_lines=total,
            spans=[Span(line_start=start, line_end=total, lines=lines[start - 1 :], anchors=["tail"])]
            if total
            else [],
            truncated=total > tail_fallback,
        )

    # Take spans in priority order until the budget is spent, then re-sort into
    # reading order and merge.
    scored.sort(key=lambda p: (-p[0], p[1].line_start))
    kept: list[Span] = []
    budget = max_lines
    truncated = False
    for _, span in scored:
        if span.length <= budget:
            kept.append(span)
            budget -= span.length
        else:
            truncated = True

    merged = _merge(kept, lines)
    return LogExcerpt(
        job_name=job_name,
        log_path=log_path,
        total_lines=total,
        spans=merged,
        truncated=truncated,
    )


def excerpt_file(path: Path, *, job_name: str, log_path: str | None = None, **kwargs) -> LogExcerpt:
    raw = path.read_text(encoding="utf-8", errors="replace")
    return excerpt(raw, job_name=job_name, log_path=log_path or path.name, **kwargs)


def verify_evidence(ev: Evidence, raw: str) -> tuple[bool, str]:
    """Check a citation against the source log.

    Returns `(ok, reason)`. This is the anti-hallucination gate: a model can
    write a fluent, wrong verdict, but it cannot fake a quote that is not at the
    coordinates it gave. Whitespace is collapsed before comparing so that
    re-indented quotes still pass.
    """
    lines = normalize(raw)
    if ev.line_end < ev.line_start:
        return False, "line_end precedes line_start"
    if ev.line_start > len(lines):
        return False, f"line_start {ev.line_start} beyond end of log ({len(lines)} lines)"
    if ev.line_end > len(lines):
        return False, f"line_end {ev.line_end} beyond end of log ({len(lines)} lines)"

    cited = " ".join(" ".join(lines[ev.line_start - 1 : ev.line_end]).split())
    quote = " ".join(ev.quote.split())
    if not quote:
        return False, "empty quote"
    if quote not in cited:
        return False, "quote does not appear within the cited lines"
    return True, "ok"
