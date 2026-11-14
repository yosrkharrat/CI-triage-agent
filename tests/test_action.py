"""Tests for the GitHub Action entry point.

No GitHub and no model: the backend is a fake handing back a prepared verdict,
and the "captured" run is the one fixture tracked in git. What is pinned here is
what the action owes the repo it is installed in: it reads the run from the
event, writes the verdict to the job summary whatever happens to it, never posts
a citation that failed verification, and does not turn a model outage into a
second red run unless asked to.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_triage.action import ActionError, Inputs, missing_key, run_action, write_outputs
from ci_triage.agent import EvidenceCheck, TriageResult
from ci_triage.models import Evidence, FailureCategory, Verdict, WorkflowRun, route

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")


def _result(*, ok: bool = True, confidence: float = 0.9) -> TriageResult:
    ev = Evidence(
        job_name="build (ubuntu-22.04)",
        log_path="2_build (ubuntu-22.04).txt",
        line_start=10,
        line_end=12,
        quote="Error: Resource not accessible by integration",
        why="the release step was refused write access",
    )
    verdict = Verdict(
        category=FailureCategory.INFRA,
        confidence=confidence,
        summary="The workflow lacks contents: write.",
        reasoning="r",
        evidence=[ev],
    )
    dest, reason = route(verdict)
    check = EvidenceCheck(ev, ok, "ok" if ok else "quote does not appear within the cited lines")
    return TriageResult(verdict, dest, reason, (check,))


class FakeBackend:
    def __init__(self, result: TriageResult | Exception, pulls: list[int] | None = None):
        self.result = result
        self._pulls = [7] if pulls is None else pulls
        self.posted: list[tuple[int, str, str]] = []
        self.captured: list[int] = []

    def capture(self, inputs: Inputs, root: Path) -> Path:
        self.captured.append(inputs.run_id)
        return FIXTURE

    def triage(self, fixture: Path, model: str) -> TriageResult:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def pulls(self, inputs: Inputs, run: WorkflowRun) -> list[int]:
        return self._pulls

    def post(self, inputs: Inputs, number: int, body: str, marker: str) -> str:
        self.posted.append((number, body, marker))
        return f"https://github.com/o/r/pull/{number}#issuecomment-1"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    run = json.loads((FIXTURE / "run.json").read_text())
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "completed", "workflow_run": run}))
    return {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_EVENT_NAME": "workflow_run",
        "GITHUB_REPOSITORY": "someone/else",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "RUNNER_TEMP": str(tmp_path / "tmp"),
        "CI_TRIAGE_GITHUB_TOKEN": "ghs_x",
        "GROQ_API_KEY": "gsk_x",
    }


def _outputs(env: dict[str, str]) -> dict[str, str]:
    """Parse the `key<<DELIM ... DELIM` blocks GitHub reads from $GITHUB_OUTPUT."""
    out: dict[str, str] = {}
    lines = Path(env["GITHUB_OUTPUT"]).read_text().splitlines()
    i = 0
    while i < len(lines):
        key, delim = lines[i].split("<<", 1)
        end = lines.index(delim, i + 1)
        out[key] = "\n".join(lines[i + 1 : end])
        i = end + 1
    return out


def _summary(env: dict[str, str]) -> str:
    return Path(env["GITHUB_STEP_SUMMARY"]).read_text()


# -- reading the event -------------------------------------------------------


def test_the_run_comes_from_the_workflow_run_event(env):
    inputs = Inputs.from_env(env)
    assert inputs.run_id == 30005725094
    assert inputs.repo != "someone/else"  # the event's repo, not the workflow's
    assert inputs.conclusion == "failure"
    assert inputs.post_comment is True
    assert inputs.fail_on_error is False


def test_an_explicit_run_id_wins_and_skips_the_conclusion_filter(env):
    env["CI_TRIAGE_RUN_ID"] = "123"
    inputs = Inputs.from_env(env)
    assert inputs.run_id == 123
    assert inputs.conclusion is None


def test_any_other_trigger_without_a_run_id_is_a_setup_error(env, tmp_path):
    event = tmp_path / "push.json"
    event.write_text("{}")
    env.update(GITHUB_EVENT_PATH=str(event), GITHUB_EVENT_NAME="push")
    with pytest.raises(ActionError, match="workflow_run"):
        Inputs.from_env(env)
    assert run_action(env, FakeBackend(_result())) == 1
    assert "did not run" in _summary(env)


def test_a_missing_model_key_names_the_input_to_set(env):
    del env["GROQ_API_KEY"]
    assert missing_key("groq:openai/gpt-oss-120b", env) == "groq-api-key"
    assert missing_key("someprovider:x", env) is None
    backend = FakeBackend(_result())
    assert run_action(env, backend) == 1
    assert "groq-api-key" in _summary(env)
    assert backend.captured == []  # nothing fetched before the setup is right


def test_a_green_run_is_skipped_without_spending_anything(env):
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_text())
    event["workflow_run"]["conclusion"] = "success"
    Path(env["GITHUB_EVENT_PATH"]).write_text(json.dumps(event))
    backend = FakeBackend(_result())
    assert run_action(env, backend) == 0
    assert backend.captured == []
    assert _outputs(env)["status"] == "skipped"


# -- what gets said, and where ------------------------------------------------


def test_a_confident_verified_verdict_is_posted_and_summarised(env):
    backend = FakeBackend(_result())
    assert run_action(env, backend) == 0
    assert [n for n, _, _ in backend.posted] == [7]
    out = _outputs(env)
    assert out["status"] == "posted"
    assert out["category"] == "infra"
    assert out["verified"] == "true"
    assert out["comment-url"].endswith("#issuecomment-1")
    summary = _summary(env)
    assert "## CI triage: `infra`" in summary
    assert "✅ verified" in summary


def test_an_unverified_citation_is_never_posted_but_is_shown_in_the_summary(env):
    backend = FakeBackend(_result(ok=False, confidence=0.99))
    assert run_action(env, backend) == 0
    assert backend.posted == []
    out = _outputs(env)
    assert out["status"] == "awaiting_review"
    assert out["verified"] == "false"
    summary = _summary(env)
    assert "needs a human" in summary
    assert "❌ **not found**" in summary


def test_a_low_confidence_verdict_is_not_posted(env):
    backend = FakeBackend(_result(confidence=0.4))
    run_action(env, backend)
    assert backend.posted == []
    assert _outputs(env)["status"] == "awaiting_review"


def test_post_comment_off_is_a_dry_run(env):
    env["CI_TRIAGE_POST_COMMENT"] = "false"
    backend = FakeBackend(_result())
    run_action(env, backend)
    assert backend.posted == []
    assert _outputs(env)["status"] == "dry_run"
    assert "`post-comment` is off" in _summary(env)


def test_no_open_pull_request_means_nothing_to_post_on(env):
    backend = FakeBackend(_result(), pulls=[])
    run_action(env, backend)
    assert _outputs(env)["status"] == "no_pr"


# -- when the triage itself breaks -------------------------------------------


def test_a_model_failure_warns_and_exits_zero_by_default(env, capsys):
    backend = FakeBackend(RuntimeError("429 quota exhausted"))
    assert run_action(env, backend) == 0
    assert "::warning title=ci-triage::" in capsys.readouterr().out
    assert _outputs(env)["status"] == "failed"
    assert "could not finish" in _summary(env)


def test_fail_on_error_turns_it_red(env, capsys):
    env["CI_TRIAGE_FAIL_ON_ERROR"] = "true"
    assert run_action(env, FakeBackend(RuntimeError("boom"))) == 1
    assert "::error title=ci-triage::" in capsys.readouterr().out


def test_the_capture_directory_is_removed_afterwards(env):
    run_action(env, FakeBackend(_result()))
    assert not (Path(env["RUNNER_TEMP"]) / "ci-triage").exists()


def test_multi_line_outputs_survive_the_round_trip(tmp_path):
    path = tmp_path / "out"
    write_outputs(str(path), {"summary": "line one\nline two", "x": "1"})
    lines = path.read_text().splitlines()
    assert lines[0].startswith("summary<<")
    assert lines[1:3] == ["line one", "line two"]
