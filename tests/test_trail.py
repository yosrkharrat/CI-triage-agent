"""The investigation trail: one line per tool call the agent actually made.

A scripted model calls the real tools against a captured fixture, then answers.
What is pinned: the trail comes from the calls, not from the model; it survives
a cache hit; and it is not part of the cache key, so adding it re-triaged
nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

import ci_triage.agent as agent_mod
from ci_triage.agent import _cache_key, triage
from ci_triage.models import Evidence, FailureCategory, Verdict
from ci_triage.tools import TriageContext

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")


def _answer(ctx: TriageContext) -> dict:
    span = ctx.groups[0].excerpt.spans[0]
    i = next(i for i, line in enumerate(span.lines) if line.strip())
    n = span.line_start + i
    return Verdict(
        category=FailureCategory.INFRA, confidence=0.9, summary="s", reasoning="r",
        evidence=[Evidence(job_name=ctx.groups[0].representative.name,
                           log_path=ctx.groups[0].excerpt.log_path, line_start=n, line_end=n,
                           quote=span.lines[i].strip(), why="because")],
    ).model_dump(mode="json")


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path)
    calls = ["get_logs", "get_diff", "test_history"]

    def model(messages, info):
        done = sum(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if done < len(calls):
            return ModelResponse(parts=[ToolCallPart(calls[done], {})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _answer(TriageContext(FIXTURE)))])

    real = agent_mod.build_agent
    monkeypatch.setattr(agent_mod, "build_agent", lambda *a, **kw: real(FunctionModel(model)))


def test_each_tool_call_leaves_one_line(scripted):
    result = triage(FIXTURE)
    assert len(result.trail) == 3
    logs, diff, history = result.trail
    assert logs.startswith("read the logs:") and "distinct failure(s)" in logs
    assert diff.startswith(("read the diff (", "asked for the diff: none"))
    assert history.startswith("checked run history:")
    assert "how it was diagnosed:" in result.render()


def test_the_trail_survives_a_cache_hit(scripted):
    first = triage(FIXTURE)
    again = triage(FIXTURE)
    assert again.cached
    assert again.trail == first.trail


def test_the_trail_is_not_part_of_the_cache_key():
    ctx = TriageContext(FIXTURE)
    before = _cache_key(ctx, agent_mod.MODEL)
    ctx.note("read the logs")
    assert _cache_key(ctx, agent_mod.MODEL) == before


def test_a_withheld_result_says_so():
    assert "not shown" in agent_mod._trail_line("read the log of 'x'", "Not shown: this run ...")
    assert agent_mod._trail_line("read the diff (1 file(s))", "diff --git a/x b/x") == "read the diff (1 file(s))"
