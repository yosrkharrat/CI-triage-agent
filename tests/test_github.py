"""Tests for fixture layout and log resolution.

A job whose log fails to resolve is invisible: `inspect` prints "no log", the
fixture looks fine on disk, and the agent is quietly handed nothing. These pin
the naming rules so that failure mode cannot come back unnoticed.
"""

from pathlib import Path

import pytest

from ci_triage.github import log_path_for_job, parse_run_ref
from ci_triage.models import Job

REAL = Path("fixtures/pydantic__35411255497")


def _job(name: str) -> Job:
    return Job(id=1, name=name, status="completed", conclusion="failure")


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir()
    return tmp_path


def _write(root: Path, filename: str) -> Path:
    path = root / "logs" / filename
    path.write_text("x")
    return path


def test_matches_a_plain_job_name(logs: Path):
    want = _write(logs, "0_build (ubuntu-22.04).txt")
    assert log_path_for_job(logs, _job("build (ubuntu-22.04)")) == want


def test_matches_a_matrix_job_whose_name_contains_a_slash(logs: Path):
    """The archiver writes ` / ` as ` _ `; comparing raw names finds nothing."""
    want = _write(logs, "24_Test macos-latest _ 3.13.txt")
    assert log_path_for_job(logs, _job("Test macos-latest / 3.13")) == want


def test_matches_a_name_with_several_unsafe_characters(logs: Path):
    """A slash becomes `_`; a colon is deleted rather than substituted.

    This test previously asserted `build: ` -> `build_ `, which was a guess.
    Across every captured fixture, 233 job names contain a colon: deleting it
    matches 224 of them and substituting `_` matches none. Repos that version a
    matrix with colons (`python:3.13`) had every job resolve to no log.
    """
    want = _write(logs, "3_core _ build x86_64 _ ok.txt")
    assert log_path_for_job(logs, _job('core / build: x86_64 / ok')) == want


def test_a_colon_in_a_matrix_dimension_is_deleted_not_substituted(logs: Path):
    """The real case: `prefect`, `uv` and `airflow` all name jobs this way."""
    want = _write(logs, "7_Server Tests - python3.13, postgres14.txt")
    assert log_path_for_job(logs, _job("Server Tests - python:3.13, postgres:14")) == want


def test_index_prefix_is_stripped_not_split_on_any_underscore(logs: Path):
    """Only a leading `<digits>_` is the archive index; later ones are name."""
    want = _write(logs, "7_build_release _ linux.txt")
    assert log_path_for_job(logs, _job("build_release / linux")) == want


def test_falls_back_to_prefix_match_for_a_truncated_filename(logs: Path):
    want = _write(logs, "5_Test typing-extensions (`main` branch) on Py.txt")
    job = _job("Test typing-extensions (`main` branch) on Python 3.10")
    assert log_path_for_job(logs, job) == want


def test_does_not_match_an_unrelated_job(logs: Path):
    _write(logs, "0_build (ubuntu-22.04).txt")
    assert log_path_for_job(logs, _job("Test macos-latest / 3.13")) is None


def test_missing_logs_directory_is_not_an_error(tmp_path: Path):
    assert log_path_for_job(tmp_path, _job("anything")) is None


@pytest.mark.parametrize(
    "ref",
    [
        "https://github.com/pydantic/pydantic/actions/runs/35411255497",
        "pydantic/pydantic#35411255497",
        "pydantic/pydantic 35411255497",
    ],
)
def test_run_refs_parse_from_every_accepted_form(ref: str):
    assert parse_run_ref(ref) == ("pydantic", "pydantic", 35411255497)


@pytest.mark.skipif(not (REAL / "run.json").exists(), reason="fixture not captured")
def test_every_failed_job_in_a_real_matrix_fixture_resolves_to_a_log():
    """Regression: 20+ jobs of this fixture silently resolved to nothing."""
    import json

    jobs = [Job.model_validate(j) for j in json.loads((REAL / "jobs.json").read_text())["jobs"]]
    failed = [j for j in jobs if j.failed]
    assert failed, "fixture should contain failed jobs"
    unresolved = [j.name for j in failed if log_path_for_job(REAL, j) is None]
    assert not unresolved, f"{len(unresolved)} failed job(s) have no log: {unresolved[:3]}"
