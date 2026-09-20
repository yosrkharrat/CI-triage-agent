"""Tests for the eval harness.

None of these call a model either. What is pinned here is the arithmetic and the
survival behaviour, because both are easy to get quietly wrong in ways that
produce a plausible number:

* The citation rate must be computed over *every* answered fixture, labelled or
  not. If it silently restricted itself to the labelled subset the way accuracy
  must, the headline metric would report over three fixtures while claiming
  forty-three, and it would look right.
* Accuracy must be computed over labelled fixtures only, and an unlabelled
  fixture must not land in the denominator as an implicit wrong answer.
* A sweep must not die on one fixture, and must not grind through forty more
  after the daily quota is gone.

`classify_fault` is driven through the real verifier rather than against string
literals, so rewording a message in `logs.py` fails a test here instead of
quietly landing every fault in `OTHER`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

import ci_triage.eval as eval_mod
from ci_triage.agent import EvidenceCheck, TriageResult, check_evidence
from ci_triage.eval import (
    CitationFault,
    EvalReport,
    FixtureScore,
    classify_fault,
    find_fixtures,
    run_eval,
    write_report,
)
from ci_triage.models import Evidence, FailureCategory, Label, Route, Verdict, route
from ci_triage.tools import TriageContext

FIXTURE = Path("fixtures/sweep__30005725094")


# --------------------------------------------------------------------------
# Builders — a verdict and its checks, without a model
# --------------------------------------------------------------------------


def _evidence(**kw) -> Evidence:
    return Evidence(
        **{
            "job_name": "build",
            "log_path": "1_build.txt",
            "line_start": 1,
            "line_end": 1,
            "quote": "boom",
            "why": "because",
            **kw,
        }
    )


def _verdict(category=FailureCategory.REGRESSION, confidence=0.9, **kw) -> Verdict:
    return Verdict(
        **{
            "category": category,
            "confidence": confidence,
            "summary": "s",
            "reasoning": "r",
            "evidence": [_evidence()],
            **kw,
        }
    )


def _result(*, ok: tuple[bool, ...] = (True,), verdict: Verdict | None = None) -> TriageResult:
    """A `TriageResult` with `ok` describing each citation's verification."""
    verdict = verdict or _verdict()
    checks = tuple(
        EvidenceCheck(_evidence(), good, "ok" if good else "quote does not appear within the cited lines")
        for good in ok
    )
    destination, reason = route(verdict)
    return TriageResult(verdict, destination, reason, checks)


def _score(name="f", *, label=None, ok=(True,), verdict=None, error=None) -> FixtureScore:
    lbl = Label(category=label, root_cause="rc") if label else None
    if error:
        return FixtureScore(name, lbl, error=error)
    return FixtureScore(name, lbl, _result(ok=ok, verdict=verdict))


def _report(*scores: FixtureScore, stopped: str | None = None) -> EvalReport:
    from datetime import datetime, timezone

    return EvalReport(
        model="test:model",
        max_lines=300,
        scores=tuple(scores),
        started_at=datetime.now(timezone.utc),
        seconds=1.0,
        stopped_early=stopped,
    )


# --------------------------------------------------------------------------
# Citation verification — the half that needs no ground truth
# --------------------------------------------------------------------------


def test_citation_rate_is_computed_without_any_labels():
    """The claim the harness rests on: this number exists on day one.

    Three unlabelled fixtures, five citations, one bad. Accuracy is undefined
    and the citation rate is not.
    """
    report = _report(
        _score("a", ok=(True, True)),
        _score("b", ok=(True, False)),
        _score("c", ok=(True,)),
    )
    assert report.accuracy is None
    assert (report.verified, report.cited) == (4, 5)
    assert report.citation_rate == pytest.approx(0.8)


def test_a_verdict_is_only_as_sound_as_its_weakest_citation():
    """`sound_verdict_rate` is stricter than `citation_rate`, deliberately.

    Nine of ten citations verifying reads well until you notice all ten sit on
    one verdict, and a reviewer who finds the invented one stops believing the
    other nine.
    """
    report = _report(_score("a", ok=(True,) * 9 + (False,)))
    assert report.citation_rate == pytest.approx(0.9)
    assert report.sound_verdict_rate == 0.0


def test_citations_are_counted_across_every_answered_fixture():
    report = _report(
        _score("a", label=FailureCategory.INFRA, ok=(True,)),
        _score("b", ok=(False,)),
    )
    assert report.cited == 2, "unlabelled fixtures must count toward the citation metric"
    assert len(report.labelled) == 1, "but not toward accuracy"


# --------------------------------------------------------------------------
# Fault taxonomy, driven through the real verifier
# --------------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")
@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        ({"log_path": "99_imaginary.txt"}, CitationFault.MISSING_LOG),
        ({"line_start": 10**7, "line_end": 10**7}, CitationFault.OUT_OF_RANGE),
        ({"line_start": 9, "line_end": 2}, CitationFault.BAD_RANGE),
        ({"quote": "   "}, CitationFault.EMPTY_QUOTE),
        ({"quote": "FATAL: the widget subsystem returned nil"}, CitationFault.NOT_FOUND),
    ],
)
def test_every_real_verification_failure_classifies(break_it: dict, expected: CitationFault):
    """Guards the taxonomy against a reworded message in `logs.py`.

    Each case is a citation broken one way and passed through the real
    `check_evidence`, so the fault buckets track what the verifier actually
    says rather than what this module remembers it saying.
    """
    ctx = TriageContext(FIXTURE)
    group = ctx.groups[0]
    span = group.excerpt.spans[0]
    offset = next(i for i, line in enumerate(span.lines) if line.strip())
    n = span.line_start + offset
    real = Evidence(
        job_name=group.representative.name,
        log_path=group.excerpt.log_path,
        line_start=n,
        line_end=n,
        quote=span.lines[offset].strip(),
        why="because",
    )
    assert check_evidence(ctx, _verdict(evidence=[real]))[0].ok, "the control citation must verify"

    (check,) = check_evidence(ctx, _verdict(evidence=[real.model_copy(update=break_it)]))
    assert not check.ok
    assert classify_fault(check.reason) is expected, check.reason


def test_an_unrecognised_reason_falls_through_rather_than_raising():
    assert classify_fault("something nobody wrote yet") is CitationFault.OTHER


# --------------------------------------------------------------------------
# Accuracy — the half that needs labels
# --------------------------------------------------------------------------


def test_accuracy_counts_only_labelled_fixtures():
    report = _report(
        _score("a", label=FailureCategory.REGRESSION, verdict=_verdict(FailureCategory.REGRESSION)),
        _score("b", label=FailureCategory.INFRA, verdict=_verdict(FailureCategory.REGRESSION)),
        _score("c", verdict=_verdict(FailureCategory.FLAKY)),
    )
    assert len(report.labelled) == 2
    assert report.accuracy == pytest.approx(0.5)
    assert report.confusion[("infra", "regression")] == 1


def test_an_unlabelled_fixture_is_not_a_wrong_answer():
    report = _report(_score("a"), _score("b"))
    assert report.accuracy is None
    assert report.correct == 0


def test_accuracy_is_broken_down_by_ground_truth_class():
    """A single accuracy over an unbalanced corpus hides which class earned it,
    and this corpus cannot be balanced: `flaky` needs a commit that both passed
    and failed, which two of the 43 captured runs have."""
    report = _report(
        _score("a", label=FailureCategory.REGRESSION, verdict=_verdict(FailureCategory.REGRESSION)),
        _score("b", label=FailureCategory.REGRESSION, verdict=_verdict(FailureCategory.REGRESSION)),
        _score("c", label=FailureCategory.FLAKY, verdict=_verdict(FailureCategory.INFRA)),
    )
    assert report.accuracy == pytest.approx(2 / 3)
    assert report.per_class == {"flaky": (1, 0), "regression": (2, 2)}
    assert "flaky 0/1" in report.render()


def test_per_class_counts_ignore_unlabelled_and_failed_runs():
    report = _report(
        _score("a", label=FailureCategory.INFRA, verdict=_verdict(FailureCategory.INFRA)),
        _score("b"),
        _score("c", error="429"),
    )
    assert report.per_class == {"infra": (1, 1)}


def test_abstentions_are_counted_separately_from_errors():
    """`unknown` is an answer the routing policy respects, not a failure."""
    report = _report(_score("a", verdict=_verdict(FailureCategory.UNKNOWN)))
    assert report.abstentions == 1
    assert report.errored == []


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_an_auto_post_carrying_a_bad_citation_is_flagged():
    """The liability metric, and it needs no label: a comment would have gone
    onto a pull request quoting a line that is not in the log."""
    report = _report(_score("a", ok=(False,), verdict=_verdict(confidence=0.95)))
    assert [s.fixture for s in report.auto_posts] == ["a"]
    assert [s.fixture for s in report.unsound_auto_posts] == ["a"]


def test_a_low_confidence_verdict_is_not_an_auto_post():
    report = _report(_score("a", ok=(False,), verdict=_verdict(confidence=0.4)))
    assert report.auto_posts == []
    assert report.unsound_auto_posts == []


def test_raising_the_threshold_never_posts_more():
    report = _report(
        _score("a", verdict=_verdict(confidence=0.55)),
        _score("b", verdict=_verdict(confidence=0.75)),
        _score("c", label=FailureCategory.REGRESSION, verdict=_verdict(confidence=0.99)),
    )
    posted = [row[1] for row in report.threshold_sweep()]
    assert posted == sorted(posted, reverse=True)
    assert posted[0] == 3 and posted[-1] == 1


def test_the_threshold_sweep_reads_stored_verdicts_rather_than_re_running(monkeypatch):
    """It is free, and it has to stay free — the sweep is the reason routing is
    code rather than a field the model fills in."""
    monkeypatch.setattr(
        eval_mod, "triage", lambda *a, **k: pytest.fail("threshold sweep called the model")
    )
    _report(_score("a", label=FailureCategory.INFRA)).threshold_sweep()


# --------------------------------------------------------------------------
# Surviving a sweep
# --------------------------------------------------------------------------


def _usage(inp: int, out: int):
    from pydantic_ai.usage import RunUsage

    return RunUsage(input_tokens=inp, output_tokens=out, requests=3)


def _fixture_paths(tmp_path: Path, n: int) -> list[Path]:
    paths = []
    for i in range(n):
        p = tmp_path / f"repo__{i}"
        p.mkdir()
        (p / "run.json").write_text("{}")
        paths.append(p)
    return paths


def test_one_unparseable_answer_does_not_end_the_sweep(tmp_path, monkeypatch):
    paths = _fixture_paths(tmp_path, 3)
    calls: list[str] = []

    def fake(path, **kw):
        calls.append(Path(path).name)
        if Path(path).name == "repo__1":
            raise UnexpectedModelBehavior("exceeded max retries")
        return _result()

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)

    assert calls == ["repo__0", "repo__1", "repo__2"]
    assert len(report.answered) == 2
    assert [s.fixture for s in report.errored] == ["repo__1"]
    assert report.citation_rate == 1.0, "the failed run must not dilute the citation rate"


def test_a_rate_limit_stops_the_sweep_and_says_so(tmp_path, monkeypatch):
    """Every later call would hit the same wall. The report still comes back."""
    paths = _fixture_paths(tmp_path, 5)
    calls: list[str] = []

    def fake(path, **kw):
        calls.append(Path(path).name)
        if len(calls) == 3:
            raise ModelHTTPError(
                status_code=429,
                model_name="groq:openai/gpt-oss-120b",
                body={"error": {"message": "daily limit reached, resets in 4h"}},
            )
        return _result()

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)

    assert len(calls) == 3, "the sweep kept going after a quota error"
    assert report.stopped_early and "daily limit" in report.stopped_early
    assert len(report.answered) == 2, "the fixtures already answered are still reported"


def _too_large(model="groq:openai/gpt-oss-20b") -> ModelHTTPError:
    return ModelHTTPError(
        status_code=413,
        model_name=model,
        body={"error": {"message": "Request too large: Limit 8000, Requested 9362"}},
    )


def test_an_oversized_prompt_is_recorded_apart_from_a_wrong_answer(tmp_path, monkeypatch):
    """The request never reached the model, so it says nothing about the model.

    `airflow__32529760720` reduces to ~9.4k tokens against an 8k per-request
    cap. Counting that as a failed verdict would charge the agent for an
    account limit.
    """
    paths = _fixture_paths(tmp_path, 2)

    def fake(path, **kw):
        if Path(path).name == "repo__0":
            raise _too_large()
        return _result()

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)

    assert [s.fixture for s in report.oversized] == ["repo__0"]
    assert report.citation_rate == 1.0, "an unsent request must not dilute the citation rate"
    assert "never reached the model" in report.render()


def test_oversized_fixtures_do_not_trip_the_give_up_streak(tmp_path, monkeypatch):
    """A 413 is deterministic per fixture and the next one may be smaller.

    Three large fixtures in a row are not a broken provider, and abandoning the
    other forty over them would cost a day's quota to learn nothing.
    """
    paths = _fixture_paths(tmp_path, 6)

    def fake(path, **kw):
        if int(Path(path).name[-1]) < 4:
            raise _too_large()
        return _result()

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)

    assert len(report.scores) == 6, "the sweep gave up on oversized fixtures"
    assert report.stopped_early is None
    assert len(report.oversized) == 4
    assert len(report.answered) == 2


def test_a_persistently_failing_provider_stops_the_sweep(tmp_path, monkeypatch):
    paths = _fixture_paths(tmp_path, 10)
    monkeypatch.setattr(
        eval_mod, "triage", lambda *a, **k: (_ for _ in ()).throw(UnexpectedModelBehavior("nope"))
    )
    report = run_eval(paths)
    assert len(report.scores) == eval_mod._MAX_CONSECUTIVE_ERRORS
    assert report.stopped_early and "in a row" in report.stopped_early


def test_a_recovered_failure_does_not_count_toward_the_streak(tmp_path, monkeypatch):
    """Two failures either side of a success is not a broken provider."""
    paths = _fixture_paths(tmp_path, 5)
    seen: list[str] = []

    def fake(path, **kw):
        seen.append(Path(path).name)
        if len(seen) in (1, 2, 4):
            raise UnexpectedModelBehavior("transient")
        return _result()

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)
    assert len(report.scores) == 5


def test_cached_only_never_calls_the_model(tmp_path, monkeypatch):
    """What you run on the day the quota is gone and you still need the number."""
    paths = _fixture_paths(tmp_path, 2)
    monkeypatch.setattr(eval_mod, "is_answered", lambda *a, **k: False)
    monkeypatch.setattr(
        eval_mod, "triage", lambda *a, **k: pytest.fail("--cached-only called the model")
    )
    report = run_eval(paths, cached_only=True)
    assert len(report.errored) == 2
    assert all("cache" in (s.error or "") for s in report.scores)


def test_a_failed_run_still_counts_toward_what_the_sweep_cost(tmp_path, monkeypatch):
    """The report used to bill only the fixtures that answered. Today's sweep
    answered 5 of 43 and called it 7,673 tokens; the 37 that failed had each
    been served several requests first, and the real spend was near a day's
    budget."""
    paths = _fixture_paths(tmp_path, 2)

    def fake(path, **kw):
        if Path(path).name == "repo__0":
            exc = UnexpectedModelBehavior("exceeded max retries")
            setattr(exc, "triage_spent_usage", _usage(9000, 1000))
            raise exc
        return _result()

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)

    assert report.burned == 10_000
    assert report.tokens_spent == 10_000, "the failed run is what this sweep actually paid for"
    assert "bought no verdict" in report.render()


def test_a_run_that_never_reached_the_model_burned_nothing(tmp_path, monkeypatch):
    paths = _fixture_paths(tmp_path, 1)
    monkeypatch.setattr(
        eval_mod, "triage", lambda *a, **k: (_ for _ in ()).throw(_too_large())
    )
    report = run_eval(paths)
    assert report.burned == 0
    assert "bought no verdict" not in report.render()


def test_cost_separates_what_this_sweep_paid_from_what_is_cached(tmp_path, monkeypatch):
    from pydantic_ai.usage import RunUsage

    paths = _fixture_paths(tmp_path, 2)
    usage = RunUsage(input_tokens=1000, output_tokens=200, requests=2)

    def fake(path, **kw):
        cached = Path(path).name == "repo__0"
        base = _result()
        return TriageResult(
            base.verdict, base.route, base.route_reason, base.checks, usage=usage, cached=cached
        )

    monkeypatch.setattr(eval_mod, "triage", fake)
    report = run_eval(paths)
    assert report.tokens == 2400
    assert report.tokens_spent == 1200


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def test_the_report_renders_with_no_labels_at_all():
    """Day one: forty-three captured runs, nothing labelled yet. The report must
    still be worth reading, and must say what is missing rather than print a
    misleading accuracy of zero."""
    text = _report(_score("a", ok=(True, False))).render()
    assert "no labelled fixtures" in text
    assert "1 of 2 citations" in text
    assert "0%" not in text.split("-- categories")[1].split("-- routing")[0]


def test_the_report_renders_with_labels():
    text = _report(
        _score("a", label=FailureCategory.INFRA, verdict=_verdict(FailureCategory.INFRA)),
        _score("b", label=FailureCategory.FLAKY, verdict=_verdict(FailureCategory.INFRA)),
    ).render()
    assert "accuracy" in text
    assert "flaky -> infra: 1" in text


def test_an_account_id_never_reaches_a_written_report():
    """Reports are committed, so a quota message quoted out of an API response
    gets published along with whatever account it names."""
    exc = ModelHTTPError(
        status_code=429,
        model_name="groq:openai/gpt-oss-20b",
        body={"error": {"message": "Rate limit for org `org_0123456789abcdefghij` (TPD): 200000"}},
    )
    detail = eval_mod._http_detail(exc)
    assert "org_0123456789abcdefghij" not in detail
    assert "org_<redacted>" in detail
    assert "200000" in detail, "the quota itself is the part worth keeping"


def test_the_report_round_trips_through_json(tmp_path):
    report = _report(
        _score("a", label=FailureCategory.INFRA, ok=(True, False)),
        _score("b", error="429: out of quota"),
    )
    path = write_report(report, tmp_path / "nested" / "report.json")
    blob = json.loads(path.read_text())
    assert blob["citations"]["verified"] == 1
    assert blob["categories"]["labelled"] == 1
    assert blob["scores"][1]["error"] == "429: out of quota"


# --------------------------------------------------------------------------
# Fixture selection
# --------------------------------------------------------------------------


@pytest.mark.skipif(not FIXTURE.parent.exists(), reason="no fixtures captured")
def test_find_fixtures_is_ordered_and_can_filter_to_labelled():
    every = find_fixtures()
    assert every == sorted(every), "two sweeps must be comparable row for row"
    labelled = find_fixtures(labelled_only=True)
    assert set(labelled) <= set(every)
    assert all((p / "meta.json").exists() for p in labelled)


def test_find_fixtures_accepts_explicit_names(tmp_path):
    paths = _fixture_paths(tmp_path, 3)
    chosen = find_fixtures(tmp_path, names=["repo__2", "repo__0"])
    assert [p.name for p in chosen] == ["repo__2", "repo__0"]
    assert set(chosen) <= set(paths)
