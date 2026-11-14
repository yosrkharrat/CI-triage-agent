"""Run as a GitHub Action: one failed run in, one checked verdict out.

    on: workflow_run (completed) ──▶ capture the run ──▶ triage
                                                          │
              job summary ◀── always: the verdict, every citation marked
              PR comment  ◀── only when `decide` says it may be posted

This is the webhook service with the server taken out. GitHub already runs the
workflow on the event the service would have been sent, with a token scoped to
the repo, so there is no signature to check, no queue and no App to install —
the same capture, the same agent and the same posting rule, in one process.

The posting rule is the service's own (`service.decide`), unchanged: **a verdict
whose citations do not all verify is never posted on a pull request**. Without a
review queue to hold it, such a verdict still goes to the job summary, marked for
what it is, so a person who opens the run can read it and judge it.

A triage that cannot run — the model's quota is spent, the provider is down —
is reported as a warning and exits 0 unless `fail-on-error` is set. The run it
was asked about is already red; a second red run saying "the triage failed" is
noise on top of it. A missing credential or an unreadable event is a setup
mistake, and fails loudly either way.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ci_triage.github import GitHubClient, load_fixture, save_fixture
from ci_triage.models import Route, WorkflowRun, route
from ci_triage.service import (
    TRIAGE_CONCLUSIONS,
    _fence,
    comment_marker,
    decide,
    render_comment,
)
from ci_triage.store import Status

if TYPE_CHECKING:
    from ci_triage.agent import TriageResult

FIX_NOTE = "Not in the PR comment: a proposed code change is only posted after a person reviews it."


def without_fix(result: TriageResult) -> TriageResult:
    """The verdict as it may be posted: the diagnosis, without the proposed fix.

    `route` sends any verdict carrying a fix to review, because a code change on
    a pull request needs a person to vouch for it. The service has a review
    queue for that; the action does not. Holding back the whole verdict would
    mean almost no regression ever got a comment, since that is where a fix is
    usually proposed. So the fix is dropped from what is posted, and the rest is
    routed on its own merits. The fix stays in the job summary, and a citation
    that fails verification still blocks the post, as before.
    """
    if result.verdict.suggested_fix is None:
        return result
    verdict = result.verdict.model_copy(update={"suggested_fix": None})
    destination, reason = route(verdict)
    return replace(result, verdict=verdict, route=destination, route_reason=reason)


class ActionError(RuntimeError):
    """A setup mistake: wrong event, missing input, missing credential."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Inputs:
    repo: str
    run_id: int
    token: str
    model: str
    post_comment: bool
    fail_on_error: bool
    #: The conclusion the event reported, when there was an event. A run named
    #: explicitly through `run-id` is triaged whatever its conclusion.
    conclusion: str | None = None

    @property
    def owner_repo(self) -> tuple[str, str]:
        owner, _, name = self.repo.partition("/")
        return owner, name

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Inputs:
        from ci_triage.agent import MODEL

        token = env.get("CI_TRIAGE_GITHUB_TOKEN") or env.get("GITHUB_TOKEN") or ""
        if not token:
            raise ActionError("no GitHub token: pass `github-token` (the default is github.token)")

        event: dict = {}
        if path := env.get("GITHUB_EVENT_PATH"):
            try:
                event = json.loads(Path(path).read_text())
            except (OSError, ValueError) as exc:
                raise ActionError(f"could not read the event payload at {path}: {exc}") from None
        wr = event.get("workflow_run") if isinstance(event.get("workflow_run"), dict) else None

        repo = (wr or {}).get("repository", {}).get("full_name") or env.get("GITHUB_REPOSITORY", "")
        explicit = (env.get("CI_TRIAGE_RUN_ID") or "").strip()
        if explicit:
            try:
                run_id = int(explicit)
            except ValueError:
                raise ActionError(f"`run-id` must be a number, got {explicit!r}") from None
            conclusion = None
        elif wr is not None:
            run_id, conclusion = int(wr["id"]), wr.get("conclusion")
        else:
            raise ActionError(
                f"nothing to triage: this workflow was triggered by "
                f"{env.get('GITHUB_EVENT_NAME') or 'an unknown event'!r}. Trigger it on "
                "`workflow_run: types: [completed]`, or pass `run-id`."
            )
        if "/" not in repo:
            raise ActionError("could not tell which repository the run belongs to")

        return cls(
            repo=repo,
            run_id=run_id,
            token=token,
            model=(env.get("CI_TRIAGE_MODEL") or "").strip() or MODEL,
            post_comment=_truthy(env.get("CI_TRIAGE_POST_COMMENT"), True),
            fail_on_error=_truthy(env.get("CI_TRIAGE_FAIL_ON_ERROR"), False),
            conclusion=conclusion,
        )


#: Which environment variable each provider prefix reads its key from. Only
#: used to fail early with a sentence instead of a pydantic-ai traceback; a
#: provider missing from here is left for pydantic-ai to judge.
PROVIDER_KEYS = {
    "groq": "GROQ_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "google-gla": "GOOGLE_API_KEY",
}


def missing_key(model: str, env: Mapping[str, str]) -> str | None:
    """The input a model needs and was not given, if any."""
    var = PROVIDER_KEYS.get(model.partition(":")[0])
    if var and not env.get(var):
        return var.lower().replace("_", "-")
    return None


# --------------------------------------------------------------------------
# What the job summary says
# --------------------------------------------------------------------------

_STATUS_LINE = {
    Status.POSTED: "Posted on the pull request",
    Status.DRY_RUN: "Not posted",
    Status.NO_PR: "Not posted",
    Status.AWAITING_REVIEW: "**Not posted — needs a human**",
}


def render_summary(
    run: WorkflowRun,
    result: TriageResult,
    status: Status,
    reason: str,
    *,
    model: str,
    comment_urls: list[str] | None = None,
) -> str:
    """The job summary: the whole verdict, posted or not.

    Unlike the PR comment, this shows a verdict that failed its checks, because
    it is only read by someone who opened the run on purpose — and it marks each
    citation, so a quote the log does not contain is visible as exactly that.
    """
    v = result.verdict
    lines = [
        f"## CI triage: `{v.category.value}` (confidence {v.confidence:.2f})",
        "",
        f"[{run.name or 'workflow'} · run {run.id}, attempt {run.run_attempt}]({run.html_url})",
        "",
        v.summary,
        "",
        f"{_STATUS_LINE.get(status, status.value)}: {reason}.",
        "",
    ]
    if comment_urls:
        lines += ["Comment: " + " · ".join(comment_urls), ""]

    ok = sum(c.ok for c in result.checks)
    lines += [f"### Evidence — {ok} of {len(result.checks)} citation(s) verified against the log", ""]
    for check in result.checks:
        ev = check.evidence
        span = f"{ev.line_start}" if ev.line_start == ev.line_end else f"{ev.line_start}-{ev.line_end}"
        mark = "✅ verified" if check.ok else f"❌ **not found** — {check.reason}"
        fence = _fence(ev.quote)
        lines += [
            f"**{ev.job_name}** · `{ev.log_path}:{span}` · {mark}",
            f"> {ev.why}",
            "",
            f"{fence}text",
            ev.quote,
            fence,
            "",
        ]
    if v.suggested_fix:
        lines += ["### Suggested fix", "", v.suggested_fix, "", f"<sub>{FIX_NOTE}</sub>", ""]
    lines += ["<details><summary>Reasoning</summary>", "", v.reasoning, "", "</details>", ""]

    cost = ""
    if (u := result.usage) is not None:
        total = (u.input_tokens or 0) + (u.output_tokens or 0)
        cost = f" · {total:,} tokens over {u.requests} request(s)"
    lines.append(f"<sub>{model}{cost} · [ci-triage](https://github.com/yosrkharrat/CI-triage-agent)</sub>")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# GitHub's runner files
# --------------------------------------------------------------------------


def write_outputs(path: str | None, values: Mapping[str, str]) -> None:
    """Append step outputs to `$GITHUB_OUTPUT`, multi-line safe."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            delim = f"ci_triage_{uuid.uuid4().hex}"
            f.write(f"{key}<<{delim}\n{value}\n{delim}\n")


def append_summary(path: str | None, markdown: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown)


def annotate(level: str, message: str) -> None:
    """A workflow command, shown on the run page. Newlines must be escaped."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title=ci-triage::{escaped}", flush=True)


# --------------------------------------------------------------------------
# The side effects, behind one seam
# --------------------------------------------------------------------------


class Backend(Protocol):
    def capture(self, inputs: Inputs, root: Path) -> Path: ...
    def triage(self, fixture: Path, model: str) -> TriageResult: ...
    def pulls(self, inputs: Inputs, run: WorkflowRun) -> list[int]: ...
    def post(self, inputs: Inputs, number: int, body: str, marker: str) -> str: ...


class GitHubBackend:
    def capture(self, inputs: Inputs, root: Path) -> Path:
        owner, repo = inputs.owner_repo
        with GitHubClient(inputs.token) as client:
            return save_fixture(client, owner, repo, inputs.run_id, root=root, overwrite=True)

    def triage(self, fixture: Path, model: str) -> TriageResult:
        from ci_triage.agent import triage

        # No cache: a runner is thrown away after the job, and the working
        # directory belongs to the user's repo, not to us.
        return triage(fixture, model=model, cache=False)

    def pulls(self, inputs: Inputs, run: WorkflowRun) -> list[int]:
        if run.pull_requests:
            return [p.number for p in run.pull_requests]
        owner, repo = inputs.owner_repo
        with GitHubClient(inputs.token) as client:
            return client.open_pulls_for_commit(owner, repo, run.head_sha)

    def post(self, inputs: Inputs, number: int, body: str, marker: str) -> str:
        owner, repo = inputs.owner_repo
        with GitHubClient(inputs.token) as client:
            return client.upsert_comment(owner, repo, number, body, marker)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run_action(
    env: Mapping[str, str] | None = None,
    backend: Backend | None = None,
) -> int:
    """Triage one run. Returns the process exit code."""
    env = os.environ if env is None else env
    backend = backend or GitHubBackend()
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    output_path = env.get("GITHUB_OUTPUT")

    try:
        inputs = Inputs.from_env(env)
        if key := missing_key(inputs.model, env):
            raise ActionError(f"{inputs.model} needs the `{key}` input (store it as a repo secret)")
    except ActionError as exc:
        annotate("error", str(exc))
        append_summary(summary_path, f"## CI triage did not run\n\n{exc}\n")
        return 1

    if inputs.conclusion is not None and inputs.conclusion not in TRIAGE_CONCLUSIONS:
        print(f"run {inputs.run_id} concluded {inputs.conclusion!r}; nothing to triage")
        write_outputs(output_path, {"status": "skipped"})
        return 0

    root = Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "ci-triage"
    try:
        print(f"capturing {inputs.repo} run {inputs.run_id}...", flush=True)
        fixture = backend.capture(inputs, root)
        print(f"triaging with {inputs.model}...", flush=True)
        result = backend.triage(fixture, inputs.model)
        run, _, _ = load_fixture(fixture)

        postable = without_fix(result)
        pulls: list[int] = []
        if postable.evidence_ok and postable.route is Route.AUTO_POST:
            pulls = backend.pulls(inputs, run)
        status, reason = decide(postable, pulls=pulls, post_comments=inputs.post_comment)
        if status is Status.DRY_RUN:
            reason = "`post-comment` is off"

        urls: list[str] = []
        if status is Status.POSTED:
            body = render_comment(run, postable.verdict)
            urls = [backend.post(inputs, n, body, comment_marker(run.id)) for n in pulls]
    except Exception as exc:
        message = f"triage of run {inputs.run_id} failed: {type(exc).__name__}: {exc}"
        annotate("error" if inputs.fail_on_error else "warning", message)
        append_summary(summary_path, f"## CI triage could not finish\n\n{message}\n")
        write_outputs(output_path, {"status": "failed"})
        return 1 if inputs.fail_on_error else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)

    v = result.verdict
    print(result.render(), flush=True)
    append_summary(
        summary_path,
        render_summary(run, result, status, reason, model=inputs.model, comment_urls=urls),
    )
    write_outputs(
        output_path,
        {
            "category": v.category.value,
            "confidence": f"{v.confidence:.2f}",
            "summary": v.summary,
            "verified": "true" if result.evidence_ok else "false",
            "status": status.value,
            "comment-url": " ".join(urls),
        },
    )
    annotate(
        "notice",
        f"{v.category.value} ({v.confidence:.2f}): {v.summary}"
        + ("" if result.evidence_ok else " [a citation failed verification; not posted]"),
    )
    return 0
