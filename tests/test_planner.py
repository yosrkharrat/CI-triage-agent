"""The planner: a plan is checked against the code before Claude sees it.

A scripted model stands in for a real one and an in-memory tree for the
repository. What is pinned: each check refuses what it should, a failing plan
is sent back with the reasons, and a plan still failing after `MAX_REPAIRS` is
dropped rather than used.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

import ci_triage.planner as planner_mod
from ci_triage.agent import MAX_REPAIRS
from ci_triage.guard import PIN_RULE
from ci_triage.models import Evidence, FailureCategory, FixPlan, PlanStep, Verdict, WorkflowRun
from ci_triage.planner import DictSource, check_plan, make_plan, render_plan

SOURCE = DictSource({
    "src/checkout/cart.py": "def subtotal(lines):\n    return sum(l.price for l in lines[1:])\n",
    "tests/test_cart.py": "def test_subtotal_adds_every_line():\n    assert subtotal(LINES) == 30\n",
    "pyproject.toml": "[project]\nname = 'demo'\n",
})

RUN = WorkflowRun(
    id=1, head_sha="a" * 40, event="pull_request", status="completed", conclusion="failure",
    html_url="https://github.com/o/r/actions/runs/1", created_at=datetime(2026, 11, 25, tzinfo=UTC),
    repository={"full_name": "o/r"},
)
VERDICT = Verdict(
    category=FailureCategory.REGRESSION, confidence=0.9, summary="subtotal skips the first line",
    reasoning="r",
    evidence=[Evidence(job_name="test", log_path="1_test.txt", line_start=3, line_end=3,
                       quote="assert 20 == 30", why="the subtotal is short by one line")],
    suggested_fix="Sum every line.",
)


def _plan(**kw) -> FixPlan:
    base = {
        "feasible": True, "goal": "subtotal counts every line",
        "steps": [PlanStep(file="src/checkout/cart.py", action="edit",
                        change="sum over `lines`, not `lines[1:]`", why="assert 20 == 30")],
        "verify": ["tests/test_cart.py::test_subtotal_adds_every_line"],
    }
    return FixPlan(**{**base, **kw})


def _bad(plan: FixPlan) -> list[str]:
    return [f"{c.check}: {c.detail}" for c in check_plan(plan, SOURCE) if not c.ok]


# -- check_plan ----------------------------------------------------------------


def test_a_good_plan_passes_every_check():
    assert _bad(_plan()) == []


def test_editing_a_file_that_is_not_there_is_refused():
    plan = _plan(steps=[PlanStep(file="src/cart.py", action="edit", change="x", why="y")])
    assert any("does not exist" in b for b in _bad(plan))


def test_a_step_under_github_is_refused_by_the_guards_own_rule():
    plan = _plan(steps=[PlanStep(file=".github/workflows/ci.yml", action="edit", change="x", why="y")])
    assert any(".github/" in b and "guard" in b for b in _bad(plan))


def test_test_configuration_is_refused():
    plan = _plan(steps=[PlanStep(file="pytest.ini", action="create", change="x", why="y")])
    assert any("configuration" in b for b in _bad(plan))


def test_a_test_id_that_names_no_test_is_refused():
    assert any("no test named" in b for b in _bad(_plan(verify=["tests/test_cart.py::test_nope"])))
    assert any("no file" in b for b in _bad(_plan(verify=["tests/test_gone.py::test_x"])))


def test_a_command_in_verify_is_listed_unchecked_not_passed_as_checked():
    [check] = [c for c in check_plan(_plan(verify=["pytest -q"]), SOURCE) if c.check.startswith("verify")]
    assert check.ok and "not checked" in check.detail


def test_not_feasible_needs_a_blocker_and_no_steps():
    assert _bad(_plan(feasible=False, steps=[], blocker=None))
    assert _bad(_plan(feasible=False, blocker="add the secret"))  # still has a step
    assert _bad(_plan(feasible=False, steps=[], blocker="Add DEPLOY_KEY to the repo secrets.")) == []


def test_feasible_needs_a_step():
    assert any("at least one step" in b for b in _bad(_plan(steps=[])))


def test_a_path_leaving_the_repository_is_refused():
    plan = _plan(steps=[PlanStep(file="../etc/passwd", action="edit", change="x", why="y")])
    assert any("not a path inside" in b for b in _bad(plan))


def test_creating_a_file_that_exists_should_be_an_edit():
    plan = _plan(steps=[PlanStep(file="src/checkout/cart.py", action="create", change="x", why="y")])
    assert any("already exists" in b for b in _bad(plan))


def test_render_plan_says_what_a_person_must_do_when_not_feasible():
    text = render_plan(_plan(feasible=False, steps=[], blocker="Grant the token packages: write."))
    assert "No code fix" in text and "packages: write" in text
    assert "1. **edit `src/checkout/cart.py`**" in render_plan(_plan())


# -- the agent loop ------------------------------------------------------------


def _retries(messages) -> list[str]:
    return [str(p.content) for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, RetryPromptPart)]


@pytest.fixture
def scripted(monkeypatch):
    """Run `make_plan` against a model that reads one file, then answers `answers` in turn."""
    seen: list[list] = []

    def run(answers: list[FixPlan], verdict: Verdict = VERDICT, source: DictSource = SOURCE):
        def model(messages, info):
            seen.append(messages)
            if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
                return ModelResponse(parts=[ToolCallPart("read_file", {"path": "src/checkout/cart.py"})])
            k = len(_retries(messages))
            answer = answers[min(k, len(answers) - 1)]
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, answer.model_dump(mode="json"))])

        real = planner_mod.build_planner
        monkeypatch.setattr(planner_mod, "build_planner", lambda *a, **kw: real(FunctionModel(model)))
        return make_plan(RUN, verdict, source, files=["src/checkout/cart.py"]), seen

    return run


def test_a_good_plan_is_used_and_the_trail_says_what_was_read(scripted):
    result, _ = scripted([_plan()])
    assert result.usable and result.feasible
    assert result.repairs == 0
    assert result.trail == ("read src/checkout/cart.py",)


def test_a_failing_plan_is_sent_back_with_the_reason_then_fixed(scripted):
    wrong = _plan(steps=[PlanStep(file="src/cart.py", action="edit", change="x", why="y")])
    result, seen = scripted([wrong, _plan()])
    assert result.usable
    assert result.repairs == 1
    [hint] = _retries(seen[-1])
    assert "src/cart.py does not exist" in hint


def test_a_plan_still_failing_after_the_repairs_is_dropped(scripted):
    wrong = _plan(steps=[PlanStep(file=".github/workflows/ci.yml", action="edit", change="x", why="y")])
    result, _seen = scripted([wrong])
    assert not result.usable
    assert result.repairs == MAX_REPAIRS
    assert result.dropped and ".github/" in result.dropped
    assert result.checks_json()["ok"] is False


def test_a_not_feasible_plan_is_usable_and_says_so(scripted):
    result, _ = scripted([_plan(feasible=False, steps=[], verify=[],
                                blocker="Add the DEPLOY_KEY secret to the repository.")])
    assert result.usable and not result.feasible


def test_the_file_tool_numbers_lines_and_refuses_what_is_not_there():
    ctx = planner_mod.PlanContext(run=RUN, verdict=VERDICT, source=SOURCE)
    agent = planner_mod.build_planner(FunctionModel(lambda m, i: ModelResponse(parts=[])))
    read = agent._function_toolset.tools["read_file"].function  # type: ignore[attr-defined]

    class Ctx:
        deps = ctx

    assert "    2      return sum" in read(Ctx, "src/checkout/cart.py")
    assert "No file" in read(Ctx, "nope.py")
    assert ctx.trail == ["read src/checkout/cart.py", "looked for nope.py: not there"]


# -- reading the source through GitHub -----------------------------------------


def test_github_source_reads_through_the_tree_and_contents_apis():
    import httpx

    from ci_triage.github import GitHubClient
    from ci_triage.planner import GitHubSource

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if "/git/trees/" in request.url.path:
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": "src", "type": "tree"},
                {"path": "src/checkout/cart.py", "type": "blob"},
            ]})
        assert request.url.params["ref"] == "a" * 40
        return httpx.Response(200, text="def subtotal(): ...\n", headers={"content-type": "text/plain"})

    client = GitHubClient("t")
    client._client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    source = GitHubSource(client, "o", "r", "a" * 40)
    assert source.listdir("") == ["src/"]
    assert source.listdir("src/checkout") == ["cart.py"]
    assert source.read("src/checkout/cart.py") == "def subtotal(): ...\n"
    assert source.read("src/missing.py") is None  # answered from the tree, no request
    assert source.read("src/checkout/cart.py") is not None  # cached
    assert asked == ["/repos/o/r/git/trees/" + "a" * 40, "/repos/o/r/contents/src/checkout/cart.py"]


# -- a dependency fix may pin an action ---------------------------------------

WORKFLOW_SOURCE = DictSource({
    **SOURCE.files,
    ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - uses: actions/setup-python@v5\n",
})
DEPENDENCY = VERDICT.model_copy(update={"category": FailureCategory.DEPENDENCY})
PIN = PlanStep(file=".github/workflows/ci.yml", action="edit",
               change="pin actions/setup-python to v5.1.0", why="setup-python v5.2 broke the cache")


def test_a_pin_step_passes_its_checks_only_for_a_dependency_fix():
    plan = _plan(steps=[PIN], verify=["pytest -q"])
    assert all(c.ok for c in check_plan(plan, WORKFLOW_SOURCE, allow_pins=True))
    assert not all(c.ok for c in check_plan(plan, WORKFLOW_SOURCE))
    # Pins open workflow files, not test configuration.
    cfg = _plan(steps=[PlanStep(file="pytest.ini", action="edit", change="x", why="y")])
    assert not all(c.ok for c in check_plan(cfg, WORKFLOW_SOURCE, allow_pins=True))


def _instructions(seen) -> str:
    return "\n".join(m.instructions or "" for msgs in seen for m in msgs if isinstance(m, ModelRequest))


def test_the_planner_is_told_about_pins_for_a_dependency_and_keeps_the_plan(scripted):
    result, seen = scripted([_plan(steps=[PIN], verify=["pytest -q"])], DEPENDENCY, WORKFLOW_SOURCE)
    assert result.usable and result.repairs == 0
    assert PIN_RULE in _instructions(seen)


def test_the_planner_is_not_told_about_pins_for_a_regression(scripted):
    _, seen = scripted([_plan()])
    assert PIN_RULE not in _instructions(seen)
