"""Tests for the sandbox.

The reproduction script is exercised for real, through `LocalSandbox`, against
a git repository built here: a failing test, and a patch that fixes it. That
proves the steps — fetch one commit, apply a patch, run, read the outcome —
without Docker or the network.

What Docker adds is isolation, and that is pinned on the `docker run` command
line the sandbox builds: the flags are the isolation. One test runs the real
container too, and skips wherever Docker is not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ci_triage.sandbox import (
    OUTPUT_LINES,
    DockerSandbox,
    LocalSandbox,
    Reproduction,
    failing_step,
    make_sandbox,
    try_patch,
)

FIX = """\
--- a/check.sh
+++ b/check.sh
@@ -1,2 +1,2 @@
 echo checking
-exit 1
+exit 0
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, str]:
    """A repository whose one check fails. Returns its URL and head commit."""
    root = tmp_path / "origin"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "check.sh").write_text("echo checking\nexit 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "a failing check")
    return f"file://{root}", _git(root, "rev-parse", "HEAD")


# -- the script, for real ---------------------------------------------------


def test_reproduces_a_failure_at_the_commit(repo):
    url, sha = repo
    r = LocalSandbox().run(url, sha, "bash check.sh")
    assert r.ran and not r.passed
    assert r.exit_code == 1
    assert "checking" in r.output
    assert "::ci-triage::" not in r.output  # markers are the sandbox's, not the job's


def test_a_patch_that_fixes_it_is_seen_to_fix_it(repo):
    url, sha = repo
    trial = try_patch(LocalSandbox(), url, sha, "bash check.sh", FIX)
    assert trial.fixed
    assert trial.verdict() == "reproduced, and the patch fixes it"
    assert trial.after is not None and trial.after.patched


def test_a_patch_that_does_not_apply_is_reported_as_such(repo):
    url, sha = repo
    trial = try_patch(LocalSandbox(), url, sha, "bash check.sh", FIX.replace("echo checking", "nope"))
    assert not trial.fixed
    assert trial.after is not None and trial.after.setup_failed == "apply-patch"
    assert "could not be tried" in trial.verdict()


def test_an_unknown_commit_fails_setup_not_the_command(repo):
    url, _ = repo
    r = LocalSandbox().run(url, "0" * 40, "bash check.sh")
    assert r.setup_failed == "fetch"
    assert not r.ran
    assert try_patch(LocalSandbox(), url, "0" * 40, "true", FIX).after is None


def test_a_passing_command_is_not_a_reproduction(repo):
    url, sha = repo
    trial = try_patch(LocalSandbox(), url, sha, "true", FIX)
    assert trial.verdict().startswith("did not reproduce")
    assert trial.after is None  # nothing to fix, so the patch is not tried


def test_the_command_cannot_rewrite_the_script(repo):
    url, sha = repo
    # The command arrives as data. Were it spliced into the script, the quote
    # would end a string and the rest would run as the script's own steps.
    r = LocalSandbox().run(url, sha, "echo 'unterminated")
    assert r.ran and r.exit_code != 0


def test_a_command_that_runs_too_long_is_cut_off(repo):
    url, sha = repo
    r = LocalSandbox(timeout=2).run(url, sha, "sleep 30")
    assert r.timed_out and r.exit_code is None
    assert r.outcome().startswith("timed out")


def test_output_keeps_the_end():
    from ci_triage.sandbox import _tail

    text = _tail("\n".join(f"line {i}" for i in range(500)))
    assert text.endswith("line 499")
    assert "line 0\n" not in text
    assert text.startswith(f"[… {500 - OUTPUT_LINES} earlier line(s) not shown]")


# -- the container --------------------------------------------------------


class _Recorder:
    def __init__(self, *, timeout: bool = False):
        self.calls: list[tuple[list[str], dict]] = []
        self.timeout = timeout

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.timeout and argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0), output="::ci-triage::setup-ok\n")
        return subprocess.CompletedProcess(argv, 1, stdout="::ci-triage::setup-ok\nFAILED\n")


def test_the_container_is_locked_down():
    rec = _Recorder()
    DockerSandbox(runner=rec).run("https://github.com/o/r", "abc", "pytest -x", "a patch")
    [(argv, kw)] = rec.calls
    joined = " ".join(argv)
    for flag in (
        "--rm", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only",
        "--user 1000:1000", "--pids-limit 512", "--memory 4g", "--memory-swap 4g", "--cpus 2",
    ):
        assert flag in joined, flag
    assert "-v" not in argv and "--privileged" not in argv  # nothing of the host is mounted
    assert "CI_COMMAND=pytest -x" in argv  # the command travels as data
    assert "HAS_PATCH=1" in argv and kw["input"] == "a patch"


def test_a_timed_out_container_is_killed():
    rec = _Recorder(timeout=True)
    r = DockerSandbox(runner=rec, timeout=1).run("u", "abc", "sleep 1000")
    assert r.timed_out
    name = rec.calls[0][0][rec.calls[0][0].index("--name") + 1]
    assert rec.calls[1][0] == ["docker", "kill", name]


def test_make_sandbox():
    assert make_sandbox("docker", image="rust:1").describe() == "docker:rust:1"
    assert isinstance(make_sandbox("local"), LocalSandbox)
    with pytest.raises(ValueError, match="unknown sandbox"):
        make_sandbox("vm")


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_reproduces_inside_a_real_container(repo):
    url, sha = repo
    src = url.removeprefix("file://")
    sandbox = DockerSandbox(
        image="python:3.12",
        timeout=600,
        # The test repository belongs to whoever runs the tests; run as them so
        # it can be read, served read-only, and trusted by git.
        user=f"{os.getuid()}:{os.getgid()}",
        extra_args=(
            "-v", f"{src}:/origin:ro",
            "-e", "GIT_CONFIG_COUNT=1",
            "-e", "GIT_CONFIG_KEY_0=safe.directory",
            "-e", "GIT_CONFIG_VALUE_0=*",
        ),
    )
    trial = try_patch(sandbox, "file:///origin", sha, "bash check.sh", FIX)
    assert trial.fixed, trial.render()


# -- what the job ran -----------------------------------------------------


def test_a_failing_uses_step_has_no_command():
    log = Path("fixtures/sweep__30005725094/logs/3_build (macos-latest, --target aarch64-apple-darwin).txt")
    if not log.exists():
        pytest.skip("fixture not captured")
    step = failing_step(log.read_text())
    assert step is not None
    assert step.title == "tauri-apps/tauri-action@v0"
    assert step.script is None


def test_a_failing_run_step_yields_its_script():
    log = "\n".join(
        [
            "2026-01-01T00:00:00.0000000Z ##[group]Run actions/checkout@v4",
            "2026-01-01T00:00:00.0000000Z with:",
            "2026-01-01T00:00:00.0000000Z ##[endgroup]",
            "2026-01-01T00:00:01.0000000Z ##[group]Run uv sync",
            "2026-01-01T00:00:01.0000000Z \x1b[36;1muv sync\x1b[0m",
            "2026-01-01T00:00:01.0000000Z \x1b[36;1muv run pytest -x tests/\x1b[0m",
            "2026-01-01T00:00:01.0000000Z shell: /usr/bin/bash -e {0}",
            "2026-01-01T00:00:01.0000000Z ##[endgroup]",
            "2026-01-01T00:00:09.0000000Z FAILED tests/test_a.py::test_b",
            "2026-01-01T00:00:09.0000000Z ##[error]Process completed with exit code 1.",
        ]
    )
    step = failing_step(log)
    assert step is not None
    assert step.title == "uv sync"
    assert step.script == "uv sync\nuv run pytest -x tests/"


def test_a_log_without_an_error_has_no_failing_step():
    assert failing_step("##[group]Run true\n\x1b[36;1mtrue\x1b[0m\n##[endgroup]\n") is None


def test_render_names_the_commit_and_the_outcome():
    r = Reproduction("pytest", "a" * 40, True, 1, False, 3.2, "boom")
    assert "at aaaaaaaaaaaa with the patch: failed with exit code 1" in r.render()
