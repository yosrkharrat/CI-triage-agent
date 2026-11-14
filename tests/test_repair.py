"""The citation check sends a failing verdict back before accepting it.

A scripted model stands in for a real one: it answers with a citation that is
one line off, or that copies the clip marker, and the test reads what the agent
told it. What is pinned: the check itself is never loosened (a citation passes
only on the same verbatim test), the model is told where the text really is
and never what to write instead, repairs stop at `MAX_REPAIRS`, and the first
try is kept so the eval can still report how well the model quotes unaided.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import ci_triage.agent as agent_mod
from ci_triage.agent import MAX_REPAIRS, _cache_key, is_answered, repair_hint, triage
from ci_triage.models import Evidence, FailureCategory, Verdict
from ci_triage.tools import TriageContext

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")


def _citation(ctx: TriageContext, *, shift: int = 0, quote: str | None = None) -> Evidence:
    span = ctx.groups[0].excerpt.spans[0]
    offset = next(i for i, line in enumerate(span.lines) if line.strip())
    n = span.line_start + offset + shift
    return Evidence(
        job_name=ctx.groups[0].representative.name,
        log_path=ctx.groups[0].excerpt.log_path,
        line_start=n,
        line_end=n,
        quote=quote if quote is not None else span.lines[offset].strip(),
        why="because",
    )


def _verdict(ev: Evidence) -> dict:
    return Verdict(
        category=FailureCategory.INFRA, confidence=0.9, summary="s", reasoning="r", evidence=[ev]
    ).model_dump(mode="json")


def _retry_texts(messages) -> list[str]:
    return [
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, RetryPromptPart)
    ]


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    """Run `triage` against a model that returns `answers` in turn."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    seen: list[list] = []

    def run(answers: list[dict]):
        def model(messages, info):
            seen.append(messages)
            answer = answers[min(len(seen) - 1, len(answers) - 1)]
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])

        real = agent_mod.build_agent
        monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: real(FunctionModel(model)))
        return triage(FIXTURE, cache=True), seen

    return run


def test_a_citation_one_line_off_is_sent_back_and_fixed(scripted):
    ctx = TriageContext(FIXTURE)
    good, off = _citation(ctx), _citation(ctx, shift=1)
    result, seen = scripted([_verdict(off), _verdict(good)])

    assert result.evidence_ok
    assert result.repairs == 1
    assert result.first_try == (0, 1)
    [hint] = _retry_texts(seen[-1])
    assert f"at line(s) {good.line_start}" in hint
    assert good.quote not in hint.split("at line(s)")[0]  # says where, not what to write


def test_a_verdict_right_first_time_is_not_sent_back(scripted):
    ctx = TriageContext(FIXTURE)
    result, seen = scripted([_verdict(_citation(ctx))])
    assert result.repairs == 0
    assert result.first_try == (1, 1)
    assert len(seen) == 1


def test_repairs_stop_and_the_verdict_is_held_as_before(scripted):
    ctx = TriageContext(FIXTURE)
    invented = _citation(ctx, quote="this line was never in any log")
    result, seen = scripted([_verdict(invented)])

    assert result.repairs == MAX_REPAIRS
    assert not result.evidence_ok  # still failing, so `decide` holds it
    assert len(seen) == MAX_REPAIRS + 1
    assert result.first_try == (0, 1)


def test_the_first_try_survives_the_cache(scripted):
    ctx = TriageContext(FIXTURE)
    scripted([_verdict(_citation(ctx, shift=1)), _verdict(_citation(ctx))])
    again = triage(FIXTURE)  # served from the cache this time
    assert again.cached
    assert (again.repairs, again.first_try) == (1, (0, 1))


def test_an_old_failing_answer_is_rerun_with_repair_and_counted_as_a_miss(scripted, monkeypatch):
    """Cached before repair existed, it never had the chance the current agent
    gives. It must not be scored as the current agent's answer."""
    ctx = TriageContext(FIXTURE)
    key = _cache_key(ctx, agent_mod.MODEL)
    legacy = Verdict.model_validate(_verdict(_citation(ctx, shift=1)))
    agent_mod.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (agent_mod.CACHE_DIR / f"{key}.json").write_text(
        '{"verdict": ' + legacy.model_dump_json() + "}"
    )
    assert not is_answered(FIXTURE)

    result, _ = scripted([_verdict(_citation(ctx))])
    assert not result.cached
    assert result.evidence_ok
    assert result.first_try == (0, 1)  # the old answer was the model's first try


def test_an_old_passing_answer_is_still_served(tmp_path, monkeypatch):
    """The check would have accepted it as it stands, so it is still a valid
    answer of the current agent, and costs nothing to keep."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    ctx = TriageContext(FIXTURE)
    good = Verdict.model_validate(_verdict(_citation(ctx)))
    (tmp_path / f"{_cache_key(ctx, agent_mod.MODEL)}.json").write_text(
        '{"verdict": ' + good.model_dump_json() + "}"
    )
    assert is_answered(FIXTURE)


@pytest.mark.parametrize(
    ("quote", "expect"),
    [
        ("head [... 40 chars cut ...] tail", "clip marker"),
        ('Command \\"npm\\" failed', "backslash escapes"),
        ("first line\nsecond line", "not consecutive"),
        ("nothing like this is in the log", "not in that log"),
    ],
)
def test_each_kind_of_failure_gets_its_own_hint(quote: str, expect: str):
    ctx = TriageContext(FIXTURE)
    ev = _citation(ctx, quote=quote)
    assert expect in repair_hint(ctx, ev, "quote does not appear within the cited lines")


def test_a_cached_only_report_says_what_it_left_out(tmp_path, monkeypatch):
    """Leaving an old failing verdict out of the rates would make every score
    look better than the model is, so the report has to name it."""
    from ci_triage.eval import AWAITS_REPAIR, run_eval

    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    ctx = TriageContext(FIXTURE)
    legacy = Verdict.model_validate(_verdict(_citation(ctx, shift=1)))
    (tmp_path / f"{_cache_key(ctx, agent_mod.MODEL)}.json").write_text(
        '{"verdict": ' + legacy.model_dump_json() + "}"
    )
    report = run_eval([FIXTURE], cached_only=True)
    assert report.scores[0].error == AWAITS_REPAIR
    assert "NOT COUNTED" in report.render()


def test_a_repair_turn_that_fails_keeps_the_answer_already_given(tmp_path, monkeypatch):
    """On a capped free tier the repair turn, carrying the answer being repaired,
    can be the request that no longer fits. Measured on gpt-oss-20b: 7 runs that
    used to answer hit 413 on it. The repair must never cost the verdict."""
    from pydantic_ai.exceptions import ModelHTTPError

    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    ctx = TriageContext(FIXTURE)
    first = _verdict(_citation(ctx, shift=1))
    calls: list[int] = []

    def model(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, first)])
        raise ModelHTTPError(413, "groq:openai/gpt-oss-20b", {"error": {"message": "Request too large"}})

    real = agent_mod.build_agent
    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: real(FunctionModel(model)))
    result = triage(FIXTURE)

    assert result.verdict == Verdict.model_validate(first)
    assert not result.evidence_ok  # held for a person, exactly as before repair existed
    assert (result.repairs, result.first_try) == (1, (0, 1))
    assert len(calls) == 2  # not restarted from scratch either
