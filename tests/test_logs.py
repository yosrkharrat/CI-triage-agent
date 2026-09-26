"""Tests for the deterministic reduction layer.

These matter more than they look: every eval score downstream assumes the agent
was shown the right lines, at stable coordinates. If reduction silently drifts,
a prompt-quality regression and a trimmer bug are indistinguishable.
"""

from pathlib import Path

import pytest

from ci_triage.logs import excerpt, excerpt_file, normalize, verify_evidence
from ci_triage.models import Evidence

FIXTURE = Path("fixtures/sweep__30005725094")


def test_normalize_strips_timestamps_ansi_and_cr():
    raw = "2026-07-23T12:13:41.8076290Z \x1b[31mboom\x1b[0m\r\n2026-07-23T12:13:42.0Z done  "
    assert normalize(raw) == ["boom", "done"]


def test_normalize_preserves_line_count_and_indices():
    raw = "\n".join(f"2026-07-23T12:00:0{i}.0Z line{i}" for i in range(5))
    lines = normalize(raw)
    assert len(lines) == 5
    assert lines[2] == "line2"  # index i == line number i+1


def test_excerpt_windows_around_gha_error():
    raw = "\n".join([*(f"setup {i}" for i in range(200)), "##[error]it broke", "cleanup"])
    ex = excerpt(raw, job_name="j", log_path="j.txt")
    assert ex.total_lines == 202
    assert ex.kept_lines < 40
    body = ex.render()
    assert "##[error]it broke" in body
    assert "lines omitted" in body  # the gap is labelled, not silently dropped


def test_excerpt_merges_overlapping_windows():
    raw = "\n".join(["pad"] * 50 + ["##[error]a", "x", "##[error]b"] + ["pad"] * 50)
    ex = excerpt(raw, job_name="j", log_path="j.txt")
    assert len(ex.spans) == 1, "adjacent anchors should collapse into one span"


def test_excerpt_respects_line_budget():
    raw = "\n".join(f"##[error]failure {i}" for i in range(500))
    ex = excerpt(raw, job_name="j", log_path="j.txt", max_lines=100)
    assert ex.kept_lines <= 100
    assert ex.truncated


def test_excerpt_falls_back_to_tail_when_no_anchor():
    raw = "\n".join(f"quiet line {i}" for i in range(300))
    ex = excerpt(raw, job_name="j", log_path="j.txt", tail_fallback=25)
    assert ex.spans[0].anchors == ["tail"]
    assert ex.spans[0].line_end == 300
    assert ex.kept_lines == 25


def test_excerpt_handles_empty_log():
    ex = excerpt("", job_name="j", log_path="j.txt")
    assert ex.spans == []
    assert ex.reduction == 0.0


def _ev(**kw):
    base = {"job_name": "j", "log_path": "j.txt", "line_start": 1, "line_end": 1, "quote": "q", "why": "w"}
    return Evidence(**{**base, **kw})


def test_verify_evidence_accepts_a_real_quote():
    raw = "2026-07-23T12:00:00.0Z alpha\n2026-07-23T12:00:01.0Z ##[error]bravo"
    ok, reason = verify_evidence(_ev(line_start=2, line_end=2, quote="##[error]bravo"), raw)
    assert ok, reason


def test_verify_evidence_tolerates_whitespace_differences():
    raw = "2026-07-23T12:00:00.0Z    spaced      out"
    ok, _ = verify_evidence(_ev(quote="spaced out"), raw)
    assert ok


def test_verify_evidence_rejects_a_fabricated_quote():
    raw = "2026-07-23T12:00:00.0Z alpha"
    ok, reason = verify_evidence(_ev(quote="ModuleNotFoundError: no such module"), raw)
    assert not ok
    assert "does not appear" in reason


def test_verify_evidence_rejects_out_of_range_and_inverted_spans():
    raw = "2026-07-23T12:00:00.0Z alpha"
    assert not verify_evidence(_ev(line_start=9, line_end=9, quote="alpha"), raw)[0]
    assert not verify_evidence(_ev(line_start=1, line_end=99, quote="alpha"), raw)[0]
    assert not verify_evidence(_ev(line_start=5, line_end=2, quote="alpha"), raw)[0]


def test_verify_evidence_rejects_a_right_quote_at_the_wrong_place():
    """Catches a model that quotes correctly but cites coordinates it never read."""
    raw = "\n".join(["2026-07-23T12:00:00.0Z filler"] * 10 + ["2026-07-23T12:00:00.0Z ##[error]x"])
    ok, _ = verify_evidence(_ev(line_start=2, line_end=3, quote="##[error]x"), raw)
    assert not ok


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")
def test_real_fixture_reduces_hard_and_keeps_the_root_cause():
    log = FIXTURE / "logs" / "2_build (ubuntu-22.04).txt"
    ex = excerpt_file(log, job_name="build (ubuntu-22.04)", log_path=log.name)
    assert ex.reduction > 0.9
    assert "Resource not accessible by integration" in ex.render()


# --------------------------------------------------------------------------
# Size, as opposed to line count
# --------------------------------------------------------------------------


def test_a_long_line_is_shown_clipped_with_both_ends_kept():
    line = "START" + "." * 250_000 + "END"
    raw = "\n".join(["pad"] * 5 + [line, "##[error]boom"])
    body = excerpt(raw, job_name="j", log_path="j.txt").render()
    assert "START" in body and "END" in body
    assert "chars cut" in body
    assert len(body) < 2_000


def test_clipping_is_display_only_so_either_end_still_verifies():
    """Clipping must not make an honest citation of a clipped line fail."""
    line = "START" + "." * 10_000 + "END"
    raw = "\n".join([line, "##[error]boom"])
    ex = excerpt(raw, job_name="j", log_path="j.txt")
    assert ex.spans[0].lines[0] == line, "spans keep the full text"
    assert verify_evidence(_ev(quote="START..."), raw)[0]
    assert verify_evidence(_ev(quote="...END"), raw)[0]
    # A quote across the mark claims text the model was never shown.
    assert not verify_evidence(_ev(quote="START [... 9803 chars cut ...] END"), raw)[0]


def test_excerpt_respects_a_character_budget():
    raw = "\n".join(f"##[error]failure {i} " + "x" * 200 for i in range(0, 5000, 50))
    ex = excerpt(raw, job_name="j", log_path="j.txt", max_chars=3_000)
    assert len(ex.render()) < 3_600
    assert ex.truncated


def test_a_window_over_budget_on_its_own_is_narrowed_onto_its_anchor():
    """The strongest span alone exceeding the budget must not leave nothing."""
    raw = "\n".join(["y" * 290] * 40 + ["##[error]the actual failure"] + ["z" * 290] * 20)
    ex = excerpt(raw, job_name="j", log_path="j.txt", max_chars=1_500)
    assert ex.spans, "an empty excerpt shows the model nothing to cite"
    assert "##[error]the actual failure" in ex.render()
    assert len(ex.render()) < 1_800


def test_the_tail_fallback_respects_a_character_budget():
    raw = "\n".join(f"quiet {i} " + "q" * 290 for i in range(300))
    ex = excerpt(raw, job_name="j", log_path="j.txt", max_chars=2_000)
    assert ex.spans[0].line_end == 300, "the bottom of the log is what matters"
    assert len(ex.render()) < 2_400
