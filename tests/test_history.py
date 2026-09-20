"""Tests for the run-history signal.

`FailureCategory.FLAKY` is defined as one commit producing both outcomes. That
definition is only worth anything if the data behind it is actually assembled
correctly, so the assembly is pinned here.
"""

from datetime import datetime, timedelta, timezone

from ci_triage.models import HistoricalRun, RunHistory

T0 = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _run(conclusion: str | None, *, attempt: int = 1, sha: str = "abc123", minutes: int = 0):
    return HistoricalRun(
        id=1 + minutes,
        name="CI",
        run_attempt=attempt,
        status="completed" if conclusion else "in_progress",
        conclusion=conclusion,
        head_sha=sha,
        created_at=T0 + timedelta(minutes=minutes),
        html_url="https://example.invalid/run",
    )


def _history(same_commit=(), same_workflow=()):
    return RunHistory(
        head_sha="abc123",
        workflow_name="CI",
        same_commit=list(same_commit),
        same_workflow=list(same_workflow),
    )


def test_one_commit_with_both_outcomes_is_the_flake_signal():
    h = _history([_run("failure", attempt=1), _run("success", attempt=2, minutes=5)])
    assert h.passed_and_failed_on_same_commit


def test_repeated_failures_on_one_commit_are_not_a_flake_signal():
    """Three reds in a row is a reproducible break, not non-determinism."""
    h = _history([_run("failure", attempt=n, minutes=n) for n in (1, 2, 3)])
    assert not h.passed_and_failed_on_same_commit


def test_timed_out_counts_as_a_failure_alongside_a_pass():
    h = _history([_run("timed_out"), _run("success", minutes=9)])
    assert h.passed_and_failed_on_same_commit


def test_no_neighbours_means_no_flake_claim():
    assert not _history().passed_and_failed_on_same_commit
    assert _history().workflow_failure_rate is None


def test_workflow_failure_rate_ignores_unfinished_runs():
    h = _history(same_workflow=[_run("failure", sha="d1"), _run("success", sha="d2"), _run(None, sha="d3")])
    assert h.workflow_failure_rate == 0.5


def test_a_workflow_red_across_other_commits_is_visible():
    """High background failure rate points away from the diff under test."""
    h = _history(same_workflow=[_run("failure", sha=f"d{i}", minutes=i) for i in range(8)])
    assert h.workflow_failure_rate == 1.0


def test_summary_states_observations_without_drawing_the_conclusion():
    h = _history([_run("failure"), _run("success", attempt=2, minutes=5)])
    text = h.summary()
    assert "BOTH passed and failed" in text
    assert "flaky" not in text.lower(), "the category call belongs to the model, not this function"


def test_summary_is_explicit_when_a_commit_never_went_green():
    assert "no passing run recorded" in _history([_run("failure")]).summary()


def test_history_round_trips_through_json():
    h = _history([_run("failure"), _run("success", minutes=5)], [_run("success", sha="zz")])
    back = RunHistory.model_validate_json(h.model_dump_json())
    assert back == h
    assert back.passed_and_failed_on_same_commit
