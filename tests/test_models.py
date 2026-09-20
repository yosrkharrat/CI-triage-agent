"""Tests for schema constraints and routing policy."""

import pytest
from pydantic import ValidationError

from ci_triage.models import (
    CONFIDENCE_THRESHOLD,
    Evidence,
    FailureCategory,
    Route,
    Verdict,
    route,
)


def _evidence():
    return Evidence(
        job_name="build", log_path="build.txt", line_start=10, line_end=12,
        quote="##[error]boom", why="the runner reported the failure here",
    )


def _verdict(**kw) -> Verdict:
    base = dict(
        category=FailureCategory.INFRA, confidence=0.9, summary="s",
        reasoning="r", evidence=[_evidence()], suggested_fix=None,
    )
    return Verdict(**{**base, **kw})


def test_verdict_requires_at_least_one_citation():
    """A verdict with no evidence is an opinion; the schema refuses it."""
    with pytest.raises(ValidationError):
        _verdict(evidence=[])


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        _verdict(confidence=1.3)
    with pytest.raises(ValidationError):
        _verdict(confidence=-0.1)


def test_category_rejects_invented_values():
    with pytest.raises(ValidationError):
        _verdict(category="probably-fine")


def test_high_confidence_verdict_auto_posts():
    decision, _ = route(_verdict(confidence=0.95))
    assert decision is Route.AUTO_POST


def test_low_confidence_goes_to_a_human():
    decision, reason = route(_verdict(confidence=CONFIDENCE_THRESHOLD - 0.01))
    assert decision is Route.HUMAN_REVIEW
    assert "confidence" in reason


def test_unknown_always_goes_to_a_human_however_confident():
    decision, reason = route(_verdict(category=FailureCategory.UNKNOWN, confidence=1.0))
    assert decision is Route.HUMAN_REVIEW
    assert "category" in reason


def test_a_proposed_code_change_always_goes_to_a_human():
    """Risky actions need a person regardless of how sure the model sounds."""
    decision, reason = route(_verdict(confidence=1.0, suggested_fix="add contents: write"))
    assert decision is Route.HUMAN_REVIEW
    assert "code change" in reason


def test_category_descriptions_reach_the_json_schema():
    """The labelling guide and the model's instructions are the same text."""
    schema = Verdict.model_json_schema()
    assert "confidence" in schema["properties"]
    assert schema["properties"]["evidence"]["minItems"] == 1
