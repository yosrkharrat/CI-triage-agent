"""Reproduce a failure, and try a patch against it, somewhere it cannot hurt.

A verdict says why a run is red; a reproduction checks it. Given a repository,
a commit and a command, the sandbox fetches exactly that commit, optionally
applies a patch, runs the command and reports how it ended. Run once without a
patch and once with, it answers the question a reviewer actually has about a
suggested fix: does it turn this failure green?

The steps live in one shell script, `REPRO_SCRIPT`, and a backend only decides
where it runs:

* `DockerSandbox` runs it in a throwaway container with every capability
  dropped, no privilege escalation, a read-only root, and caps on memory, CPU
  and process count. The code under test is a stranger's, and the command may
  have been chosen by a model; the container is the only thing between either
  of them and the host. It does have network, because fetching the commit and
  installing its dependencies need it.
* `LocalSandbox` runs the same script in a temporary directory with **no
  isolation at all**. It exists so the script itself can be tested on a machine
  without Docker, against a repository built for the test. Never point it at
  code you did not write.

The sandbox is opt-in everywhere. The eval harness never uses it: a
reproduction reaches the network and a live repository, and the harness's whole
point is that a triage run is offline and asks the same question next month as
today.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ci_triage.logs import normalize

#: How much of a command's output is kept. The end is what matters: that is
#: where a test runner prints its summary and a build prints its error.
OUTPUT_LINES = 80
OUTPUT_CHARS = 6_000

#: Wall-clock limit for one reproduction, setup included.
TIMEOUT = 15 * 60

SETUP_OK = "::ci-triage::setup-ok"
SETUP_FAILED = "::ci-triage::setup-failed "

#: Everything a reproduction does, as one script. Inputs arrive as environment
#: variables and the patch on stdin, never spliced into the script text, so a
#: repository URL or a command cannot rewrite the steps around it. The command
#: is shell by design — it is what the job ran — and runs with the same
#: `-e -o pipefail` GitHub's bash steps use.
REPRO_SCRIPT = r"""
set -u
fail() { echo "::ci-triage::setup-failed $1"; exit 97; }
patch_file="$WORK.patch"
if [ "${HAS_PATCH:-0}" = 1 ]; then cat > "$patch_file" || fail read-patch; fi
mkdir -p "$WORK" && cd "$WORK" || fail workdir
git init -q . || fail git-init
git fetch -q --depth 1 "$REPO_URL" "$SHA" || fail fetch
git -c advice.detachedHead=false checkout -q FETCH_HEAD || fail checkout
if [ "${HAS_PATCH:-0}" = 1 ]; then
  git apply --whitespace=nowarn "$patch_file" || fail apply-patch
fi
echo "::ci-triage::setup-ok"
exec bash -e -o pipefail -c "$CI_COMMAND"
"""


@dataclass(frozen=True)
class Reproduction:
    """How one command ended at one commit, with or without a patch."""

    command: str
    sha: str
    patched: bool
    exit_code: int | None
    timed_out: bool
    seconds: float
    output: str
    #: The step that failed before the command could run — `fetch`,
    #: `apply-patch` — or None when the command ran.
    setup_failed: str | None = None

    @property
    def ran(self) -> bool:
        return self.setup_failed is None and not self.timed_out

    @property
    def passed(self) -> bool:
        return self.ran and self.exit_code == 0

    def outcome(self) -> str:
        if self.setup_failed:
            return f"setup failed at {self.setup_failed}"
        if self.timed_out:
            return f"timed out after {self.seconds:.0f}s"
        return "passed" if self.exit_code == 0 else f"failed with exit code {self.exit_code}"

    def render(self) -> str:
        what = "with the patch" if self.patched else "as committed"
        return "\n".join(
            [
                f"$ {self.command}",
                f"at {self.sha[:12]} {what}: {self.outcome()} ({self.seconds:.0f}s)",
                "",
                self.output or "(no output)",
            ]
        )


def _tail(raw: str) -> str:
    lines = [ln for ln in normalize(raw) if not ln.startswith("::ci-triage::")]
    kept = lines[-OUTPUT_LINES:]
    text = "\n".join(kept)
    if len(text) > OUTPUT_CHARS:
        text = text[-OUTPUT_CHARS:].split("\n", 1)[-1]
    dropped = len(lines) - len(text.splitlines())
    return (f"[… {dropped} earlier line(s) not shown]\n" if dropped > 0 else "") + text


def _result(
    command: str, sha: str, patched: bool, code: int | None, timed_out: bool, started: float, raw: str
) -> Reproduction:
    setup_failed = None
    if SETUP_OK not in raw and not timed_out:
        m = re.search(re.escape(SETUP_FAILED) + r"(\S+)", raw)
        setup_failed = m.group(1) if m else "unknown"
    return Reproduction(
        command=command,
        sha=sha,
        patched=patched,
        exit_code=None if timed_out else code,
        timed_out=timed_out,
        seconds=time.monotonic() - started,
        output=_tail(raw),
        setup_failed=setup_failed,
    )


class Sandbox(Protocol):
    def run(self, repo_url: str, sha: str, command: str, patch: str | None = None) -> Reproduction: ...

    def describe(self) -> str: ...


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class DockerSandbox:
    """Run a reproduction in a locked-down, throwaway container."""

    #: Needs bash and git. The full Python image has both, and covers most of
    #: the corpus; pass another for a Rust or Node project.
    image: str = "python:3.12"
    memory: str = "4g"
    cpus: str = "2"
    pids: int = 512
    #: Not root, and not anyone the image knows: nothing it runs owns anything.
    user: str = "1000:1000"
    timeout: float = TIMEOUT
    #: Extra `docker run` arguments, for a test that mounts a local repository.
    extra_args: Sequence[str] = ()
    runner: Runner = field(default=subprocess.run, repr=False)

    def describe(self) -> str:
        return f"docker:{self.image}"

    def argv(self, name: str, repo_url: str, sha: str, command: str, has_patch: bool) -> list[str]:
        return [
            "docker", "run", "--rm", "-i", "--name", name,
            # Nothing the container does may reach back into the host.
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", self.user,
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,size=4g",
            # A fork bomb or a leak is the test's problem, not the machine's.
            "--pids-limit", str(self.pids),
            "--memory", self.memory,
            "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "-e", "HOME=/tmp",
            "-e", "WORK=/tmp/work",
            "-e", f"REPO_URL={repo_url}",
            "-e", f"SHA={sha}",
            "-e", f"CI_COMMAND={command}",
            "-e", f"HAS_PATCH={int(has_patch)}",
            *self.extra_args,
            self.image,
            "bash", "-c", REPRO_SCRIPT,
        ]  # fmt: skip

    def run(self, repo_url: str, sha: str, command: str, patch: str | None = None) -> Reproduction:
        if shutil.which("docker") is None and self.runner is subprocess.run:
            raise RuntimeError("docker is not installed or not on PATH")
        name = f"ci-triage-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        try:
            proc = self.runner(
                self.argv(name, repo_url, sha, command, patch is not None),
                input=patch or "",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # Killing the docker client leaves the container running.
            self.runner(["docker", "kill", name], capture_output=True, text=True)
            raw = exc.output if isinstance(exc.output, str) else (exc.output or b"").decode(errors="replace")
            return _result(command, sha, patch is not None, None, True, started, raw)
        return _result(command, sha, patch is not None, proc.returncode, False, started, proc.stdout)


@dataclass
class LocalSandbox:
    """The same script on this machine, unisolated. For tests and trusted code only."""

    timeout: float = TIMEOUT

    def describe(self) -> str:
        return "local (unisolated)"

    def run(self, repo_url: str, sha: str, command: str, patch: str | None = None) -> Reproduction:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="ci-triage-") as tmp:
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
                "HOME": tmp,
                "WORK": str(Path(tmp) / "work"),
                "REPO_URL": repo_url,
                "SHA": sha,
                "CI_COMMAND": command,
                "HAS_PATCH": str(int(patch is not None)),
                # Commits made by a test have no identity configured.
                "GIT_CONFIG_NOSYSTEM": "1",
            }
            try:
                proc = subprocess.run(
                    ["bash", "-c", REPRO_SCRIPT],
                    input=patch or "",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    env=env,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raw = exc.output if isinstance(exc.output, str) else (exc.output or b"").decode(errors="replace")
                return _result(command, sha, patch is not None, None, True, started, raw)
        return _result(command, sha, patch is not None, proc.returncode, False, started, proc.stdout)


def make_sandbox(backend: str, *, image: str | None = None) -> Sandbox:
    if backend == "docker":
        return DockerSandbox(image=image) if image else DockerSandbox()
    if backend == "local":
        return LocalSandbox()
    raise ValueError(f"unknown sandbox backend {backend!r}: use 'docker' or 'local'")


# --------------------------------------------------------------------------
# Trying a patch
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchTrial:
    """The command as committed, then with the patch applied."""

    before: Reproduction
    after: Reproduction | None

    def verdict(self) -> str:
        b, a = self.before, self.after
        if not b.ran:
            return f"could not reproduce: {b.outcome()}"
        if b.passed:
            return "did not reproduce: the command passes as committed"
        if a is None:
            return "reproduced: the command fails as committed"
        if not a.ran:
            return f"reproduced, but the patch could not be tried: {a.outcome()}"
        if a.passed:
            return "reproduced, and the patch fixes it"
        return "reproduced, and the patch does not fix it"

    @property
    def fixed(self) -> bool:
        return self.before.ran and not self.before.passed and self.after is not None and self.after.passed

    def render(self) -> str:
        parts = [self.verdict(), "", self.before.render()]
        if self.after is not None:
            parts += ["", self.after.render()]
        return "\n".join(parts)


def try_patch(
    sandbox: Sandbox, repo_url: str, sha: str, command: str, patch: str | None = None
) -> PatchTrial:
    """Reproduce first; only a failure that reproduces is worth patching."""
    before = sandbox.run(repo_url, sha, command)
    if patch is None or not before.ran or before.passed:
        return PatchTrial(before, None)
    return PatchTrial(before, sandbox.run(repo_url, sha, command, patch))


# --------------------------------------------------------------------------
# What the job ran
# --------------------------------------------------------------------------

_RUN_GROUP = re.compile(r"^##\[group\]Run (.*)$")
_SCRIPT_LINE = re.compile(r"^\x1b\[36;1m(.*?)\x1b\[0m$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ?")


@dataclass(frozen=True)
class Step:
    """The step a job was on when it failed."""

    title: str
    #: The shell it ran, or None for a `uses:` action, which has no command to
    #: replay — its behaviour lives in the action's own code.
    script: str | None


def failing_step(raw_log: str) -> Step | None:
    """The step that raised the job's first `##[error]`, read from its raw log.

    GitHub echoes a `run:` step's script inside the step's `Run` group, one
    line per script line in cyan. A `uses:` step shows its `with:` inputs there
    instead, so a group without cyan lines is an action, not a command.
    """
    lines = [_TIMESTAMP.sub("", ln).rstrip("\r") for ln in raw_log.splitlines()]
    error = next((i for i, ln in enumerate(lines) if ln.startswith("##[error]")), None)
    if error is None:
        return None
    start = next((i for i in range(error, -1, -1) if _RUN_GROUP.match(lines[i])), None)
    if start is None:
        return None
    title = _RUN_GROUP.match(lines[start]).group(1)  # type: ignore[union-attr]
    script = []
    for ln in lines[start + 1 : error]:
        if ln.startswith("##[endgroup]"):
            break
        if m := _SCRIPT_LINE.match(ln):
            script.append(m.group(1))
    return Step(title, "\n".join(script) if script else None)
