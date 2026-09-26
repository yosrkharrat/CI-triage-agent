"""Tests for the agent's wiring and its anti-hallucination gate.

None of these call a model. What is worth pinning here is not what the model
says — that is what the eval harness is for — but the machinery around it: that
the prompt still carries the category definitions, that routing stays code's
decision, that reasoning is actually switched on for whichever provider is in
use, and above all that a citation which is not really in the log is caught. A
model can write a fluent, wrong verdict; the whole point of recording
coordinates alongside the quote is that it cannot fake one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ci_triage.agent as agent_mod
from ci_triage.agent import (
    INSTRUCTIONS,
    MODEL,
    _cache_key,
    _model_settings,
    build_agent,
    check_evidence,
    triage,
)
from ci_triage.models import (
    CATEGORY_GUIDE,
    Evidence,
    FailureCategory,
    Route,
    Verdict,
    route,
)
from ci_triage.tools import TriageContext

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")


def _verdict(evidence: list[Evidence], **kw) -> Verdict:
    return Verdict(
        **{
            "category": FailureCategory.INFRA,
            "confidence": 0.9,
            "summary": "s",
            "reasoning": "r",
            "evidence": evidence,
            **kw,
        }
    )


@pytest.fixture
def ctx() -> TriageContext:
    return TriageContext(FIXTURE)


def _real_citation(ctx: TriageContext) -> Evidence:
    """A citation built from what the agent is actually shown."""
    group = ctx.groups[0]
    span = group.excerpt.spans[0]
    offset = next(i for i, line in enumerate(span.lines) if line.strip())
    n = span.line_start + offset
    return Evidence(
        job_name=group.representative.name,
        log_path=group.excerpt.log_path,
        line_start=n,
        line_end=n,
        quote=span.lines[offset].strip(),
        why="because",
    )


# --------------------------------------------------------------------------
# The evidence gate
# --------------------------------------------------------------------------


def test_a_real_quote_at_real_coordinates_verifies(ctx: TriageContext):
    checks = check_evidence(ctx, _verdict([_real_citation(ctx)]))
    assert [c.ok for c in checks] == [True], checks[0].reason


def test_an_invented_quote_is_caught(ctx: TriageContext):
    """The failure mode this exists for: plausible text that is not in the log."""
    ev = _real_citation(ctx).model_copy(
        update={"quote": "FATAL: the widget subsystem returned an unexpected nil"}
    )
    (check,) = check_evidence(ctx, _verdict([ev]))
    assert not check.ok
    assert "does not appear" in check.reason


def test_a_real_quote_at_the_wrong_line_is_caught(ctx: TriageContext):
    """Right text, wrong coordinates — the citation still does not hold up."""
    real = _real_citation(ctx)
    ev = real.model_copy(update={"line_start": real.line_start + 40, "line_end": real.line_end + 40})
    (check,) = check_evidence(ctx, _verdict([ev]))
    assert not check.ok


def test_a_citation_to_a_log_that_does_not_exist_is_caught(ctx: TriageContext):
    ev = _real_citation(ctx).model_copy(update={"log_path": "99_imaginary job.txt"})
    (check,) = check_evidence(ctx, _verdict([ev]))
    assert not check.ok
    assert "no log named" in check.reason


def test_one_bad_citation_among_good_ones_is_reported(ctx: TriageContext):
    good = _real_citation(ctx)
    bad = good.model_copy(update={"quote": "this line was never written"})
    verdict = _verdict([good, bad])
    checks = check_evidence(ctx, verdict)
    assert [c.ok for c in checks] == [True, False]


def test_a_verdict_must_cite_something(ctx: TriageContext):
    """`min_length=1` on evidence: an uncited verdict is not reviewable."""
    with pytest.raises(ValueError):
        _verdict([])


# --------------------------------------------------------------------------
# Routing stays code's decision
# --------------------------------------------------------------------------


def test_low_confidence_routes_to_a_human(ctx: TriageContext):
    v = _verdict([_real_citation(ctx)], confidence=0.4)
    assert route(v)[0] is Route.HUMAN_REVIEW


def test_a_proposed_code_change_always_routes_to_a_human(ctx: TriageContext):
    v = _verdict([_real_citation(ctx)], confidence=0.99, suggested_fix="add permissions: contents: write")
    assert route(v)[0] is Route.HUMAN_REVIEW


def test_unknown_routes_to_a_human_however_confident(ctx: TriageContext):
    v = _verdict([_real_citation(ctx)], category=FailureCategory.UNKNOWN, confidence=1.0)
    assert route(v)[0] is Route.HUMAN_REVIEW


def test_a_confident_uncontroversial_verdict_auto_posts(ctx: TriageContext):
    assert route(_verdict([_real_citation(ctx)]))[0] is Route.AUTO_POST


# --------------------------------------------------------------------------
# Prompt wiring
# --------------------------------------------------------------------------


def test_the_prompt_carries_every_category_definition():
    """The guard against the drift `models.py` warns about: the prompt and the
    labelling guide must be one text, not two that agree today."""
    for category, text in CATEGORY_GUIDE.items():
        assert category.value in INSTRUCTIONS
        assert text.split(".")[0] in INSTRUCTIONS, f"{category.value} definition missing"


def test_the_agent_exposes_exactly_the_three_read_only_tools():
    agent = build_agent()
    names = set(agent._function_toolset.tools)
    assert names == {"get_logs", "get_diff", "test_history"}


def test_the_agent_can_be_built_without_credentials():
    """Construction must not need an API key, or none of the above could run."""
    assert build_agent() is not None


def test_triage_requires_a_fixture_that_exists(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        triage(tmp_path / "nope")


# --------------------------------------------------------------------------
# Provider settings
# --------------------------------------------------------------------------


def test_reasoning_is_enabled_for_the_default_model():
    """The regression this exists for: the settings were hardcoded to Anthropic
    and the default moved to Groq. Nothing raised — pydantic-ai passes a
    provider its own namespaced keys and drops the rest — so every run looked
    configured while running with reasoning off. Assert against MODEL rather
    than a literal, so swapping the default cannot silently reintroduce it."""
    settings = _model_settings(MODEL)
    provider = MODEL.split(":", 1)[0]
    assert any(k.startswith(f"{provider}_") for k in settings), (
        f"no {provider}_* reasoning settings for the default model {MODEL!r}"
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("groq:openai/gpt-oss-120b", {"groq_reasoning_effort", "groq_reasoning_format"}),
        ("anthropic:claude-opus-5", {"anthropic_thinking", "anthropic_effort"}),
    ],
)
def test_each_provider_gets_its_own_reasoning_keys(model: str, expected: set[str]):
    settings = _model_settings(model)
    assert expected <= set(settings)
    # And none belonging to the other provider, which would be dead config.
    others = {"groq", "anthropic"} - {model.split(":", 1)[0]}
    for other in others:
        assert not [k for k in settings if k.startswith(f"{other}_")]


def test_an_unknown_provider_still_gets_a_token_budget():
    """A model the mapping has never seen must still run — `ollama:` and
    `google-gla:` are listed as alternatives and neither takes a reasoning key."""
    settings = _model_settings("ollama:qwen2.5:14b")
    assert settings["max_tokens"] > 0
    assert not [k for k in settings if k.startswith(("groq_", "anthropic_"))]


def test_a_groq_model_name_containing_a_slash_is_parsed_as_one_name():
    """`groq:openai/gpt-oss-120b` has a provider prefix and a vendor-namespaced
    model. Splitting on the wrong separator reads the provider as `openai`."""
    assert _model_settings("groq:openai/gpt-oss-120b")["groq_reasoning_effort"] == "high"


# --------------------------------------------------------------------------
# The verdict cache
# --------------------------------------------------------------------------


def _a_verdict() -> Verdict:
    return Verdict(
        category=FailureCategory.INFRA,
        confidence=0.9,
        summary="missing permission",
        reasoning="the release step was refused",
        evidence=[
            Evidence(
                job_name="build (ubuntu-22.04)",
                log_path="2_build (ubuntu-22.04).txt",
                line_start=1,
                line_end=1,
                quote="x",
                why="y",
            )
        ],
    )


def test_a_cached_verdict_is_served_without_a_model(tmp_path, monkeypatch):
    """The property the free tier depends on. If this passes with no API key
    set, nothing reached the network — a cache hit cannot be costing tokens."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    key = _cache_key(TriageContext(FIXTURE), MODEL)
    agent_mod._store_verdict(key, _a_verdict(), None)

    result = triage(FIXTURE)
    assert result.cached is True
    assert result.verdict.category is FailureCategory.INFRA


def test_routing_is_recomputed_rather_than_cached(tmp_path, monkeypatch):
    """Only the model's answer is cached. Sweeping the confidence threshold, or
    tightening the evidence check, must re-score answered runs for free."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    agent_mod._store_verdict(_cache_key(TriageContext(FIXTURE), MODEL), _a_verdict(), None)

    assert triage(FIXTURE).route is Route.AUTO_POST
    # The same cached answer, re-routed under a stricter policy.
    monkeypatch.setattr(agent_mod, "CONFIDENCE_THRESHOLD", 0.95, raising=False)
    monkeypatch.setattr(agent_mod, "route", lambda v: route(v, threshold=0.95))
    assert triage(FIXTURE).route is Route.HUMAN_REVIEW


def test_editing_the_prompt_invalidates_every_cached_verdict():
    """Otherwise a prompt change would be scored against answers from the old
    prompt, and the eval would report that nothing you did had any effect."""
    before = _cache_key(TriageContext(FIXTURE), MODEL)
    original = agent_mod.INSTRUCTIONS
    try:
        agent_mod.INSTRUCTIONS = original + "\none more rule.\n"
        assert _cache_key(TriageContext(FIXTURE), MODEL) != before
    finally:
        agent_mod.INSTRUCTIONS = original


def test_the_cache_key_separates_models():
    assert _cache_key(TriageContext(FIXTURE), "groq:openai/gpt-oss-120b") != _cache_key(
        TriageContext(FIXTURE), "groq:openai/gpt-oss-20b"
    )


@pytest.mark.skipif(not Path("fixtures/pydantic__35411255497").exists(), reason="not captured")
def test_a_line_budget_that_changes_the_excerpt_changes_the_key():
    big = TriageContext(Path("fixtures/pydantic__35411255497"), max_lines=300)
    small = TriageContext(Path("fixtures/pydantic__35411255497"), max_lines=40)
    assert big.get_logs() != small.get_logs(), "fixture no longer exercises the budget"
    assert _cache_key(big, MODEL) != _cache_key(small, MODEL)


def test_a_line_budget_that_changes_nothing_reuses_the_answer():
    """Keying on the rendered text rather than on `max_lines` means a budget
    the logs already fit under is not a different question, so the verdict it
    was already given still answers it. The old key said otherwise and paid for
    the same answer twice."""
    loose = TriageContext(FIXTURE, max_lines=300)
    tight = TriageContext(FIXTURE, max_lines=150)
    assert loose.get_logs() == tight.get_logs(), "fixture no longer exercises this"
    assert _cache_key(loose, MODEL) == _cache_key(tight, MODEL)


def test_changing_what_the_model_is_shown_invalidates_the_cache(monkeypatch):
    """The hole the old key left open. `INSTRUCTIONS` was hashed and the layer
    that builds the rest of the prompt was not, so an edit to the anchors in
    `logs.py` or the fingerprint in `tools.py` left every key unchanged while
    the model saw different text — and the symptom is an eval score that will
    not move however much you improve the reduction."""
    ctx = TriageContext(FIXTURE)
    before = _cache_key(ctx, MODEL)

    shown = ctx.get_logs()
    monkeypatch.setattr(ctx, "get_logs", lambda job_name=None: shown + "\n... one more line ...")
    assert _cache_key(ctx, MODEL) != before


def test_the_cache_key_covers_every_tool_the_agent_can_call(monkeypatch):
    """Not just the logs: a re-fetched diff or a backfilled history changes the
    evidence a verdict rests on just as much."""
    ctx = TriageContext(FIXTURE)
    before = _cache_key(ctx, MODEL)
    for tool, replacement in (
        ("get_diff", lambda: "a different diff"),
        ("test_history", lambda: "a different history"),
        ("overview", lambda: "a different overview"),
    ):
        with monkeypatch.context() as m:
            m.setattr(ctx, tool, replacement)
            assert _cache_key(ctx, MODEL) != before, f"{tool} is not in the cache key"


def test_an_unreadable_cache_entry_is_a_miss_not_a_crash(tmp_path, monkeypatch):
    """A `Verdict` field added later must not make the cache raise on every
    read; the fixture simply gets re-answered."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    key = _cache_key(TriageContext(FIXTURE), MODEL)
    (tmp_path / f"{key}.json").write_text("{not json at all")
    assert agent_mod._cached_verdict(key) is None


# --------------------------------------------------------------------------
# The agent loop, driven by a stub model
# --------------------------------------------------------------------------
#
# These run the real loop — tools, output schema, caching, usage accounting —
# against a model that never leaves the process. Everything above this point
# tests pieces in isolation, and a release of that discipline is what let
# `run.usage()` ship: `usage` is a property, the mistake is invisible until the
# loop actually runs, and no unit test touched the line. A stub model costs
# nothing and exercises it.


def _stub_agent(monkeypatch, **kwargs):
    """Rebuild the agent against `TestModel`, leaving all other wiring intact."""
    from pydantic_ai.models.test import TestModel

    real = agent_mod.build_agent

    def build(model=agent_mod.MODEL, **kw):
        return real(TestModel(**kwargs), **kw)

    monkeypatch.setattr(agent_mod, "build_agent", build)


def test_the_loop_produces_a_verdict_and_records_what_it_cost(tmp_path, monkeypatch):
    """The regression test for `run.usage()`: reaching this assertion at all
    means the loop ran to completion and usage was read the way the installed
    pydantic-ai actually exposes it."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    _stub_agent(monkeypatch)

    result = triage(FIXTURE)
    assert isinstance(result.verdict, Verdict)
    assert result.cached is False
    assert result.usage is not None
    assert result.usage.requests >= 1
    # It must also render without raising — `render()` reaches into usage.
    assert "cost:" in result.render()


def test_a_run_is_answered_once_and_then_served_from_cache(tmp_path, monkeypatch):
    """The whole point of the cache on a metered free tier."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)

    calls = {"n": 0}
    real = agent_mod.build_agent

    def counting_build(model=agent_mod.MODEL, **kw):
        from pydantic_ai.models.test import TestModel

        calls["n"] += 1
        return real(TestModel(), **kw)

    monkeypatch.setattr(agent_mod, "build_agent", counting_build)

    first = triage(FIXTURE)
    second = triage(FIXTURE)
    assert calls["n"] == 1, "the model was asked twice for the same run"
    assert first.cached is False and second.cached is True
    assert first.verdict == second.verdict


def test_refresh_asks_again_even_with_a_cache_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    _stub_agent(monkeypatch)

    triage(FIXTURE)
    assert triage(FIXTURE, cache=False).cached is False


def test_the_tools_are_reachable_from_inside_the_loop(tmp_path, monkeypatch):
    """`TestModel` calls every tool it is offered before answering, so a tool
    that raises on this fixture fails here. `tools.py` promises none of them
    do — an exception becomes a wasted retry, not a message the model can use."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    _stub_agent(monkeypatch, call_tools="all")
    assert triage(FIXTURE).verdict is not None


# --------------------------------------------------------------------------
# What a failed run cost
# --------------------------------------------------------------------------


def _response(inp: int, out: int):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage

    return ModelResponse(parts=[TextPart("x")], usage=RequestUsage(input_tokens=inp, output_tokens=out))


def test_the_usage_of_a_failed_run_is_summed_across_its_responses():
    """`run_sync` raises instead of returning, so its `RunUsage` goes with it —
    but every request the model did answer was served and billed. A sweep that
    loses 37 of 43 fixtures and reports the cost of the 6 understates a day's
    budget as an hour's."""
    spent = agent_mod._usage_of([_response(1200, 300), _response(2400, 500)])
    assert spent is not None
    assert (spent.input_tokens, spent.output_tokens) == (3600, 800)
    # `incr` carries a RequestUsage's tokens and not a request count, so
    # without counting responses the report reads "3,600 tokens over 0 requests".
    assert spent.requests == 2


def test_a_run_that_failed_before_any_response_reports_nothing_spent():
    """Nothing was served, so there is nothing to account for — and `None`
    rather than a confident zero, which would be a measurement."""
    assert agent_mod._usage_of([]) is None


def test_a_failing_triage_always_carries_its_spend_on_the_exception(tmp_path, monkeypatch):
    """The contract the harness reads. Attached to the original exception so
    that `except ModelHTTPError` in the CLI keeps seeing its status code."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)

    class Exploding:
        def run_sync(self, *a, **kw):
            raise RuntimeError("refused")

    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: Exploding())
    with pytest.raises(RuntimeError) as caught:
        triage(FIXTURE, cache=False)
    assert hasattr(caught.value, agent_mod.SPENT_ATTR)
    assert agent_mod.spent_on(caught.value) is None


def _http(status: int, code: str | None = None):
    from pydantic_ai.exceptions import ModelHTTPError

    body = {"error": {"message": "x", "code": code}} if code else {"error": {"message": "x"}}
    return ModelHTTPError(status, "m", body)


class _Flaky:
    """An agent whose first `failures` runs raise `exc`, then fail loudly."""

    def __init__(self, exc, failures: int):
        self.exc, self.failures, self.calls = exc, failures, 0

    def run_sync(self, *a, **kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        raise RuntimeError("answered")  # stands in for success: the retry happened


@pytest.mark.parametrize(
    "exc",
    [_http(413), _http(400, "tool_use_failed")],
    ids=["too-large", "tool-use-failed"],
)
def test_a_failure_another_attempt_can_avoid_is_tried_once_more(tmp_path, monkeypatch, exc):
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    flaky = _Flaky(exc, failures=1)
    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: flaky)
    with pytest.raises(RuntimeError, match="answered"):
        triage(FIXTURE, cache=False)
    assert flaky.calls == 2


@pytest.mark.parametrize(
    "exc",
    [_http(429), _http(400, "invalid_request_error"), _http(500)],
    ids=["rate-limit", "other-400", "server"],
)
def test_other_failures_are_not_retried(tmp_path, monkeypatch, exc):
    """A 429 already had the SDK's backoff, and a per-day lockout will not clear
    in seconds; retrying anything else is spending quota on a guess."""
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    flaky = _Flaky(exc, failures=5)
    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: flaky)
    with pytest.raises(type(exc)):
        triage(FIXTURE, cache=False)
    assert flaky.calls == 1


def test_attempts_are_bounded_and_each_one_is_billed(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    flaky = _Flaky(_http(413), failures=99)
    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: flaky)
    from pydantic_ai.usage import RunUsage

    monkeypatch.setattr(agent_mod, "_usage_of", lambda messages: RunUsage(input_tokens=100, requests=1))
    with pytest.raises(type(_http(413))) as caught:
        triage(FIXTURE, cache=False)
    assert flaky.calls == agent_mod.RUN_ATTEMPTS
    spent = agent_mod.spent_on(caught.value)
    assert (spent.input_tokens, spent.requests) == (100 * agent_mod.RUN_ATTEMPTS, agent_mod.RUN_ATTEMPTS)


def test_earlier_reasoning_is_not_sent_back_but_everything_citable_is():
    """Groq bills re-sent `<think>` traces against the per-request cap; on
    airflow they were most of the assistant turns and the margin of a 413."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ThinkingPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    history = [
        ModelRequest(parts=[UserPromptPart("overview")]),
        ModelResponse(parts=[ThinkingPart("long deliberation"), ToolCallPart("get_logs", {})]),
        ModelRequest(parts=[ToolReturnPart("get_logs", "12 | ##[error]boom", tool_call_id="c")]),
    ]
    kept = agent_mod.drop_reasoning(history)
    parts = [p for m in kept for p in m.parts]
    assert not any(isinstance(p, ThinkingPart) for p in parts)
    assert any(isinstance(p, ToolCallPart) for p in parts)
    assert any(isinstance(p, ToolReturnPart) and "boom" in p.content for p in parts)
    assert kept[0] is history[0], "messages with nothing to drop are left alone"


def test_the_agent_drops_reasoning_before_every_request(ctx):
    """Through the real agent loop, not just the function: what the model is
    sent on its second turn carries the first turn's tool call and result, and
    none of its reasoning."""
    from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    seen: list[list] = []
    answer = _verdict([_real_citation(ctx)]).model_dump(mode="json")

    def model(messages, info):
        seen.append(messages)
        if len(seen) == 1:
            return ModelResponse(parts=[ThinkingPart("x" * 5_000), ToolCallPart("get_logs", {})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer)])

    build_agent(FunctionModel(model)).run_sync(ctx.overview(), deps=ctx)
    second = [p for m in seen[1] for p in m.parts]
    assert not any(isinstance(p, ThinkingPart) for p in second)
    assert any(isinstance(p, ToolCallPart) and p.tool_name == "get_logs" for p in second)


def test_a_run_that_never_answers_is_stopped_by_the_request_limit(tmp_path, monkeypatch):
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    calls = []

    def circling(messages, info):
        calls.append(1)
        return ModelResponse(parts=[ToolCallPart("test_history", {})])

    real = agent_mod.build_agent
    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: real(FunctionModel(circling)))
    with pytest.raises(UsageLimitExceeded):
        triage(FIXTURE, cache=False)
    assert len(calls) == agent_mod.REQUEST_LIMIT
