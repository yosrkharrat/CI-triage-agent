"""Tests for the agent's tool surface.

Two properties are load-bearing and neither is visible by reading the output of
a successful run, so they are pinned here.

*Totality.* No tool raises for an absence it could describe. An exception inside
a tool call surfaces to the agent as a retry: it costs a turn, teaches the model
nothing, and on a fixture with no diff it would do so on every single run.

*Grouping.* Collapsing a matrix is only safe if it never merges two genuinely
different failures. Over-merging is silent — the second failure is simply never
shown — so the negative case matters more than the positive one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ci_triage.tools import NO_DIFF, TriageContext

REAL = Path("fixtures")


def _fixture(root: Path, *, jobs: dict[str, str], diff: str | None = None) -> Path:
    """Build a fixture on disk. `jobs` maps job name to raw log text."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "run.json").write_text(
        json.dumps(
            {
                "id": 1,
                "name": "CI",
                "head_branch": "main",
                "head_sha": "a" * 40,
                "event": "push",
                "status": "completed",
                "conclusion": "failure",
                "run_attempt": 1,
                "workflow_id": 9,
                "html_url": "https://example.invalid/run/1",
                "created_at": "2026-09-19T12:00:00Z",
                "repository": {"full_name": "acme/widget"},
            }
        )
    )
    (root / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": i,
                        "name": name,
                        "status": "completed",
                        "conclusion": "failure",
                        "steps": [
                            {"name": "Run tests", "number": 1, "status": "completed",
                             "conclusion": "failure"}
                        ],
                    }
                    for i, name in enumerate(jobs)
                ]
            }
        )
    )
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    for i, (name, text) in enumerate(jobs.items()):
        (logs / f"{i}_{name.replace('/', '_')}.txt").write_text(text)
    if diff is not None:
        (root / "diff.patch").write_text(diff)
    return root


# --------------------------------------------------------------------------
# get_diff — the tool most likely to be asked about something that is not there
# --------------------------------------------------------------------------


def test_missing_diff_is_described_rather_than_raised(tmp_path: Path):
    """A push event, a force-pushed commit or an older capture all land here."""
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"build": "##[error]boom\n"}))
    out = ctx.get_diff()
    assert out.startswith(NO_DIFF)
    assert "not captured" in out


def test_a_failed_diff_fetch_reports_why(tmp_path: Path):
    path = _fixture(tmp_path / "f", jobs={"build": "##[error]boom\n"})
    (path / "diff_unavailable.txt").write_text("not found: /repos/acme/widget/commits/abc")
    out = TriageContext(path).get_diff()
    assert out.startswith(NO_DIFF)
    assert "not found" in out


def test_an_empty_diff_is_not_reported_as_a_missing_one(tmp_path: Path):
    """An empty commit is a different fact from an uncaptured diff."""
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"build": "x"}, diff="   \n"))
    assert ctx.get_diff().startswith(NO_DIFF)
    assert "empty" in ctx.get_diff()


def test_a_present_diff_is_returned_verbatim(tmp_path: Path):
    patch = "diff --git a/x.py b/x.py\n+print(1)\n"
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"build": "x"}, diff=patch))
    assert ctx.get_diff() == patch


def test_an_oversized_diff_is_truncated_and_says_so(tmp_path: Path):
    big = "".join(f"+line {i}\n" for i in range(5000))
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"build": "x"}, diff=big))
    out = ctx.get_diff(max_bytes=500)
    assert "diff truncated" in out
    assert len(out) < len(big)


def test_a_truncated_diff_names_the_files_it_hid(tmp_path: Path):
    """The head of a diff is whichever files sort first, not the ones that matter."""
    big = "".join(
        f"diff --git a/pkg/mod{i}.py b/pkg/mod{i}.py\n" + "".join(f"+line {j}\n" for j in range(50))
        for i in range(20)
    )
    out = TriageContext(_fixture(tmp_path / "f", jobs={"build": "x"}, diff=big)).get_diff(
        max_bytes=1_000
    )
    assert "pkg/mod19.py" in out
    assert "pkg/mod0.py" not in out.split("files changed in the part not shown")[1]


# --------------------------------------------------------------------------
# The other tools, on absences
# --------------------------------------------------------------------------


def test_history_absence_blocks_a_flaky_claim_explicitly(tmp_path: Path):
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"build": "x"}))
    assert "flaky" in ctx.test_history().lower()


def test_asking_for_an_unknown_job_lists_the_real_ones(tmp_path: Path):
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"build (linux)": "##[error]boom\n"}))
    out = ctx.get_logs("no-such-job")
    assert "build (linux)" in out


def test_a_job_name_resolves_case_insensitively_and_by_substring(tmp_path: Path):
    ctx = TriageContext(_fixture(tmp_path / "f", jobs={"Build (linux)": "##[error]boom\n"}))
    assert "boom" in ctx.get_logs("build (linux)")
    assert "boom" in ctx.get_logs("linux")


def test_an_ambiguous_substring_is_refused_rather_than_guessed(tmp_path: Path):
    """Picking a matrix leg at random would misattribute every citation in it."""
    ctx = TriageContext(
        _fixture(tmp_path / "f", jobs={"test (3.11)": "##[error]a\n", "test (3.12)": "##[error]b\n"})
    )
    assert "No failed job matching" in ctx.get_logs("test")


def test_a_failed_job_whose_log_expired_is_reported_not_skipped(tmp_path: Path):
    path = _fixture(tmp_path / "f", jobs={"build": "##[error]boom\n"})
    next((path / "logs").glob("*.txt")).unlink()
    ctx = TriageContext(path)
    assert ctx.jobs_without_logs
    assert "no log evidence" in ctx.get_logs()
    assert "have no log in this capture" in ctx.overview()


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def _matrix_log(os_name: str, py: str) -> str:
    """Two legs of one matrix: same failure, different surrounding detail."""
    return (
        f"Setting up {os_name} runner, python {py}\n"
        f"Downloading deps to /home/{os_name}/cache/{py}\n"
        + "".join(f"compiling module_{i}\n" for i in range(20))
        + "##[error]TypeError: unsupported operand type(s) for +: 'int' and 'str'\n"
        "##[error]Process completed with exit code 1.\n"
    )


def test_matrix_legs_with_one_failure_collapse_to_one_group(tmp_path: Path):
    jobs = {f"test ({o} / {p})": _matrix_log(o, p) for o in ("linux", "macos") for p in ("3.11", "3.12")}
    ctx = TriageContext(_fixture(tmp_path / "f", jobs=jobs))
    assert len(ctx.failed_jobs) == 4
    assert len(ctx.groups) == 1
    assert ctx.groups[0].size == 4


def test_two_different_failures_are_never_merged(tmp_path: Path):
    """The regression that made this matter: fingerprinting on anchor lines
    alone reduced every job to `##[error]Process completed with exit code N`,
    which every failed job emits, so unrelated failures merged and the second
    was never shown to the model at all."""
    jobs = {
        "test (linux)": _matrix_log("linux", "3.11"),
        "lint": "checking style\n##[error]E501 line too long\n##[error]Process completed with exit code 1.\n",
    }
    ctx = TriageContext(_fixture(tmp_path / "f", jobs=jobs))
    assert len(ctx.groups) == 2
    assert "E501" in ctx.get_logs()
    assert "TypeError" in ctx.get_logs()


def test_the_fanout_count_is_reported_because_it_is_evidence(tmp_path: Path):
    """"All 12 legs fail identically" is what separates one deterministic break
    fanned out over a matrix from twelve independent problems."""
    jobs = {f"test ({i})": _matrix_log("linux", f"3.{i}") for i in range(12)}
    ctx = TriageContext(_fixture(tmp_path / "f", jobs=jobs))
    assert "[12 jobs]" in ctx.overview()
    assert "11 other job(s) failed identically" in ctx.get_logs()


# --------------------------------------------------------------------------
# Against the real golden set
# --------------------------------------------------------------------------


@pytest.mark.skipif(not (REAL / "pydantic__35411255497" / "run.json").exists(), reason="fixture not captured")
def test_pydantic_matrix_collapses_but_keeps_the_gate_job_separate():
    """37 failed jobs: 36 identical pytest legs, plus the `check` gate whose log
    says something different and would otherwise vanish into them."""
    ctx = TriageContext(REAL / "pydantic__35411255497")
    assert len(ctx.failed_jobs) == 37
    assert len(ctx.groups) == 2
    assert ctx.groups[0].size == 36
    assert ctx.groups[1].representative.name == "check"


@pytest.mark.skipif(not (REAL / "pydantic__35411255497" / "run.json").exists(), reason="fixture not captured")
def test_the_pydantic_excerpt_names_the_failing_tests():
    """Reduction is only worth anything if what survives is the evidence. These
    two test names and the exception are the whole case for the label."""
    logs = TriageContext(REAL / "pydantic__35411255497").get_logs()
    assert "test_v1_hypothesis_plugin" in logs
    assert "PydanticDeprecatedSince20" in logs


@pytest.mark.skipif(not (REAL / "sweep__30005725094").exists(), reason="fixture not captured")
def test_the_infra_fixture_reports_all_four_legs_failing_identically():
    ctx = TriageContext(REAL / "sweep__30005725094")
    assert len(ctx.groups) == 1
    assert ctx.groups[0].size == 4
    assert "resource not accessible by integration" in ctx.get_logs().lower()


@pytest.mark.skipif(not (REAL / "poetry__35343948952" / "run.json").exists(), reason="fixture not captured")
def test_the_line_budget_does_not_decide_how_many_failures_there_are():
    """`max_lines` had two jobs and only one of them was its own.

    Grouping read the same excerpt that gets rendered, so a tighter budget
    dropped the lowest-priority spans — often the very lines two matrix legs
    agree on — and one failure split into several, each then rendering its own
    representative log. The knob you reach for to fit a smaller budget made the
    prompt *bigger*: on this fixture, 300 -> 150 took 8 distinct failures to 16
    and rendered 29% more log.
    """
    counts = {ml: len(TriageContext(REAL / "poetry__35343948952", max_lines=ml).groups)
              for ml in (300, 150, 100)}
    assert len(set(counts.values())) == 1, f"grouping moved with the budget: {counts}"


@pytest.mark.skipif(not (REAL / "poetry__35343948952" / "run.json").exists(), reason="fixture not captured")
def test_a_tighter_line_budget_never_renders_more():
    """The property the fix exists to restore, stated as the user sees it."""
    sizes = [len(TriageContext(REAL / "poetry__35343948952", max_lines=ml).get_logs())
             for ml in (300, 150, 100)]
    assert sizes == sorted(sizes, reverse=True), sizes


@pytest.mark.skipif(not (REAL / "pydantic__35411255497" / "run.json").exists(), reason="fixture not captured")
def test_grouping_at_the_default_budget_is_unchanged_by_the_fix():
    """Pinned because the fix had to be free: these counts are what every
    cached verdict was answered against, and moving them would silently
    invalidate the only measurements that exist."""
    ctx = TriageContext(REAL / "pydantic__35411255497", max_lines=300)
    assert (len(ctx.groups), ctx.groups[0].size) == (2, 36)


#: What Groq's free tier accepts in one request, and what the instructions,
#: tool definitions and overview take as Groq counts, before any tool returns.
_REQUEST_CAP = 8_000
_FIXED = 3_000


def _tokens(text: str) -> int:
    # Logs run at ~3.6 characters per o200k token across this corpus; 3.0 errs
    # on the side of calling a fixture too big.
    return int(len(text) / 3.0)


@pytest.mark.skipif(not (REAL / "pandas__35489338041" / "run.json").exists(), reason="fixture not captured")
def test_asking_for_a_matrix_leg_by_name_fits_one_request():
    """Four of these legs rendered at 15k tokens, against the representative's
    3k, because of one 250k-character line of pytest-xdist dots. The prompt
    invites exactly this call to check a fan-out, so it made a 413 certain."""
    ctx = TriageContext(REAL / "pandas__35489338041")
    for job in ctx.failed_jobs:
        assert _tokens(ctx.get_logs(job.name)) < _REQUEST_CAP - _FIXED, job.name


@pytest.mark.skipif(not (REAL / "poetry__35343948952" / "run.json").exists(), reason="fixture not captured")
def test_many_distinct_failures_share_the_budget_and_the_rest_are_named():
    ctx = TriageContext(REAL / "poetry__35343948952")
    out = ctx.get_logs()
    assert len(out) <= ctx.max_chars * 1.2
    # Every distinct failure is either shown or named, never silently dropped.
    for group in ctx.groups:
        assert group.representative.name in out


def test_every_fixture_leaves_room_for_the_rest_of_the_run():
    """Logs, diff and history together, on top of the fixed prompt, under one
    request's cap. Before the character budgets, 37 of 43 fixtures 413'd."""
    over = []
    for path in sorted(p for p in REAL.iterdir() if (p / "run.json").exists()):
        ctx = TriageContext(path)
        total = _FIXED + sum(map(_tokens, (ctx.get_logs(), ctx.get_diff(), ctx.test_history())))
        if total >= _REQUEST_CAP:
            over.append((path.name, total))
    assert not over, over


# --------------------------------------------------------------------------
# The run's budget, across calls
# --------------------------------------------------------------------------


def _metered_ctx(tmp_path: Path, run_chars: int) -> TriageContext:
    return TriageContext(_fixture(tmp_path / "f", jobs={"build": "x"}), run_chars=run_chars)


def test_results_within_the_run_budget_pass_through_untouched(tmp_path: Path):
    ctx = _metered_ctx(tmp_path, 10_000)
    assert ctx.metered("a" * 4_000) == "a" * 4_000
    assert ctx.spent_chars == 4_000


def test_a_result_past_the_budget_is_cut_at_a_line_and_says_so(tmp_path: Path):
    ctx = _metered_ctx(tmp_path, 5_000)
    ctx.metered("a" * 2_000)
    out = ctx.metered("\n".join(f"{i:>4} | line" for i in range(2_000)))
    assert "cut here" in out
    kept = out.split("\n... cut here")[0]
    assert kept.endswith("| line"), "never a half line with a bad line number"
    assert ctx.spent_chars <= 5_000


def test_a_spent_budget_refuses_and_tells_the_model_to_answer(tmp_path: Path):
    ctx = _metered_ctx(tmp_path, 5_000)
    ctx.metered("a" * 4_500)
    out = ctx.metered("b" * 3_000)
    assert out.startswith("Not shown")
    assert "b" not in out.split(":")[0]


def test_the_unmetered_tools_do_not_spend_the_budget(tmp_path: Path):
    """The cache key reads every tool before a run; that must not count."""
    ctx = _metered_ctx(tmp_path, 5_000)
    ctx.get_logs(), ctx.get_diff(), ctx.test_history()
    assert ctx.spent_chars == 0
