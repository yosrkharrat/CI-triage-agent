"""The triage agent: a model, three read-only tools, and a checked verdict.

The agent's whole job is to turn one captured run into a `Verdict`. Everything
it is allowed to look at comes from `TriageContext`, so a run is reproducible
offline and an eval score that moves means the agent moved.

Three things are deliberately kept out of the model's hands:

* **Routing.** `route()` decides whether a verdict may be auto-posted. The model
  reports; policy is code, so the threshold can be swept in the eval harness
  without touching the prompt.
* **The category definitions.** They come from `CATEGORY_GUIDE`, the same text
  the JSON schema carries and a human follows when labelling a fixture. Writing
  them out again here would let the prompt and the labelling guide drift, and
  eval scores would quietly stop meaning anything.
* **Whether the evidence is real.** Every citation is checked against the log
  after the run by `verify_evidence`. A model can write a fluent, wrong verdict;
  it cannot fake a quote that is not at the coordinates it gave.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent, RunContext, capture_run_messages
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, ThinkingPart
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from ci_triage.logs import verify_evidence
from ci_triage.models import Evidence, Route, Verdict, category_guide, route
from ci_triage.tools import NO_DIFF, TriageContext

if TYPE_CHECKING:
    from ci_triage.sandbox import Reproduction, Sandbox

if TYPE_CHECKING:
    from pydantic_ai.models.anthropic import AnthropicModelSettings
    from pydantic_ai.models.groq import GroqModelSettings

#: Triage is a judgement call on ambiguous evidence. The default is an
#: open-weights reasoning model on Groq's free tier: 120B, 131k context, native
#: tool calls. Overridable so the eval harness can sweep models — the credential
#: each provider needs is left to pydantic-ai to demand rather than checked here.
#:
#: Alternatives, with the env var each wants:
#:
#:   groq:openai/gpt-oss-20b          GROQ_API_KEY      smaller, same family
#:   groq:qwen/qwen3.8-27b            GROQ_API_KEY      16k output cap
#:   anthropic:claude-opus-5          ANTHROPIC_API_KEY frontier, paid
#:   google-gla:gemini-2.0-flash      GOOGLE_API_KEY    free tier, not open
#:   ollama:qwen2.5:14b               (none)            local, offline, free
#:
#: Names are worth checking against the account rather than against memory.
#: Groq has retired `llama-3.3-70b-versatile` and `qwen/qwen3-32b`, both of
#: which this comment recommended until the model list was actually queried;
#: `GET https://api.groq.com/openai/v1/models` says what a key can reach today.
#:
#: Running below the frontier is a deliberate choice, not a concession. The gap
#: shows up as *citations* rather than as prose: a weaker model writes a fluent
#: verdict and invents the line numbers under it. `check_evidence` catches
#: exactly that, so the share of citations that fail verification is a real,
#: mechanical quality measure to put next to category accuracy — and it only
#: measures anything on a model that actually makes the mistake.
MODEL = "groq:openai/gpt-oss-120b"

INSTRUCTIONS = f"""\
You are a CI triage engineer. You are given one failed GitHub Actions run and
must decide why it is red, citing the log evidence that proves it.

## Your tools

- `get_logs()` — reduced excerpts of the failing jobs. Jobs that failed
  identically are collapsed into one representative, and the number of jobs
  sharing that failure is stated. Pass a job name to see one specific job.
- `get_diff()` — the diff of the commit under test.
- `test_history()` — how this workflow behaved on this commit and on recent
  other commits.

Call the tools you need. `get_logs()` first is almost always right; the other
two answer questions the logs alone cannot settle. A run can receive only so
much tool output in total, so ask for another job's log only when it would
change your answer.

## Evidence

Return your answer by calling `final_result`. That is the only tool that ends
the run; there is no `json` tool.

Each entry in `evidence` has exactly these six fields, all required:

    job_name    "build (ubuntu-22.04)"           the failing job
    log_path    "2_build (ubuntu-22.04).txt"     the filename from the excerpt's
                                                 `=== ... ===` header — NOT the
                                                 job name, which is different
    line_start  1994                             the number printed left of the line
    line_end    1995                             may equal line_start
    quote       "..."                            copied verbatim from those lines
    why         "..."                            one sentence on what it proves

There is no `file`, `line`, `text` or `content` field. A citation missing
`line_start`/`line_end`, or naming the job where the filename belongs, is
rejected.

Every citation must be a verbatim quote from a log you were actually shown, at
the line numbers printed beside it in the excerpt. Those numbers are checked
mechanically against the log after you answer, so a quote that is not at the
coordinates you gave is a failed verdict regardless of how good the reasoning
is. Never reconstruct a line from memory of what such logs usually say. Excerpts
are reduced, with omitted ranges marked — if the evidence you want is in a gap,
ask for that job's log by name rather than guessing at its contents. A very long
line is shown with its middle cut out and marked `[... N chars cut ...]`; quote
from one side of the mark only, since the text it replaced is not what you saw.

## Categories

{category_guide()}

## How these are decided

Four mistakes account for most wrong verdicts. Check yourself against each:

1. **Which package raised the error does not settle the category.** An exception
   thrown from inside a third-party library is still a `regression` when the
   repo's own new code is what reached that path. `dependency` requires that the
   repo's code is unchanged and previously passed.
2. **Breadth is not `infra`.** One deterministic break fans out across every leg
   of a matrix and still fails as a `regression`. When `get_logs()` reports that
   N jobs failed identically, that is evidence of one cause, not of a broken
   environment. `infra` means the fault lies outside the repo's own source.
3. **`flaky` requires history.** It is defined as one commit producing both
   outcomes. A single red run, however intermittent it looks, is not evidence of
   non-determinism — call `test_history()` and cite what it says, or do not use
   this category.
4. **A diff that looks innocent settles nothing.** The failure need not
   originate in the diff: running CI for the first time can expose a break that
   was already there. If `get_diff()` reports "{NO_DIFF}", treat that as a gap in
   what you know, not as evidence that nothing changed.

## Your answer

Set `confidence` to your honest probability that `category` is correct. Below
0.7 the verdict goes to a human instead of being posted, which is the right
outcome when the evidence is thin — prefer `unknown` to a confident guess. Give
`suggested_fix` only when the evidence points to a specific remedy; it always
routes to a human for review. Write `summary` for a reviewer who has not opened
the logs, and `reasoning` for one who wants to check your work.
"""


def enable_tracing() -> bool:
    """Turn on Logfire tracing, if it is configured.

    Every tool call, its arguments and the model's reasoning become a span,
    which is how you answer "why did it say that?" without re-running anything.
    Returns False rather than raising when no token is set: tracing is how you
    watch a triage run, not a condition for one happening.
    """
    import logfire

    token = os.environ.get("LOGFIRE_TOKEN")
    logfire.configure(send_to_logfire=bool(token), token=token, service_name="ci-triage")
    logfire.instrument_pydantic_ai()
    return bool(token)


#: Retries before a run is abandoned. Generous on purpose, and load-bearing now
#: that the default is not a frontier model: weaker models get the reasoning
#: right and the *schema* wrong. `gpt-oss-120b` invented a tool named `json`,
#: then invented an `Evidence` shape (`log_line`/`text`), before settling on the
#: real one. Every one of those misses is recoverable and costs a turn, so a
#: budget tuned to a frontier model silently reads as "this model cannot do the
#: task" — the run fails on bookkeeping and the verdict is never reached.
RETRIES = 5


#: Model requests one run may make before it is abandoned. Three tools, the
#: answer, and room for the retries `RETRIES` allows and for Groq refusing a
#: turn the model tried to end in plain text ("Tool choice is required, but
#: model did not call a tool"), which it did twice in one fastapi run. Without
#: a cap nothing bounds a run that keeps circling, and one sweep sat on a
#: single fixture for eight hours.
REQUEST_LIMIT = 12


#: HTTP-level retries for a single model call. Distinct from `RETRIES` below,
#: which re-runs the *agent loop* when the model returns a malformed verdict:
#: this one re-sends an HTTP request the API refused to serve.
#:
#: Eight because on Groq's free tier a 429 is the ordinary case rather than the
#: exceptional one. The budget is 8,000 tokens per minute, and a triage run
#: makes several requests that each re-send the whole conversation, so one run
#: can exceed a minute's allowance on its own. The SDK default of 2 turns that
#: routine throttling into a crashed run.
HTTP_RETRIES = 8

#: Reasoning at high effort with a 16k output budget can sit quiet for a long
#: time before the first byte, and the SDK's 60s read timeout cuts it off.
_READ_TIMEOUT = 180.0


def _model_for(model: str | Model) -> Model | str:
    """Resolve a model string, attaching rate-limit retries where we can.

    An already-constructed `Model` passes through untouched. Callers that build
    their own — the eval harness pinning a client, a test substituting a stub —
    have already decided how it should talk to the network, and second-guessing
    that here would silently replace their client with ours.

    Returns the string unchanged for providers this has nothing to add to;
    pydantic-ai resolves it as usual.

    For Groq this hands the provider a client configured to retry, which is
    what makes the free tier usable rather than merely available. The SDK's
    backoff already draws the distinction that matters, so it is used rather
    than replaced: it sleeps for `retry-after` when that is 60s or less — which
    is what a per-*minute* budget returns, and it genuinely does clear on its
    own — and otherwise falls back to a short exponential backoff. A per-*day*
    lockout reports tens of minutes, is therefore never slept through, and
    surfaces as an error the CLI explains instead of a process that appears to
    hang until tomorrow.

    Falls back to the plain string when no credential is set, so constructing an
    agent stays free of credentials and the prompt and schemas remain testable
    without a key.
    """
    if not isinstance(model, str):
        return model
    provider, _, name = model.partition(":")
    if provider != "groq" or not name:
        return model
    if not os.environ.get("GROQ_API_KEY"):
        return model
    try:
        import httpx
        from groq import AsyncGroq
        from pydantic_ai.models.groq import GroqModel
        from pydantic_ai.providers.groq import GroqProvider
    except ImportError:  # pragma: no cover - the groq extra is not installed
        return model

    client = AsyncGroq(
        api_key=os.environ["GROQ_API_KEY"],
        max_retries=HTTP_RETRIES,
        timeout=httpx.Timeout(connect=5.0, read=_READ_TIMEOUT, write=60.0, pool=60.0),
    )
    return GroqModel(name, provider=GroqProvider(groq_client=client))


def _model_settings(model: str | Model) -> ModelSettings:
    """Reasoning settings for whichever provider `model` names.

    Provider-specific settings are namespaced in pydantic-ai (`anthropic_*`,
    `groq_*`), and a provider ignores the keys that are not its own rather than
    rejecting them. That is why this has to switch on the prefix: the Anthropic
    thinking settings left hardcoded here did no harm when the default moved to
    Groq, and did no work either — every run looked configured and none of them
    were. Silence is the failure mode, so the mapping is explicit.

    Extending this to a new provider is the one-line change the eval harness
    needs to sweep it.
    """
    # Enough for a reasoning trace plus a verdict carrying several verbatim
    # log quotes. gpt-oss allows 65k, so this budget, not the model, binds.
    max_tokens = 16000
    # A constructed `Model` carries its own settings; `system` is the provider
    # name for the instance forms, and unknown ones simply get no reasoning key.
    provider = model.split(":", 1)[0] if isinstance(model, str) else getattr(model, "system", "")
    if provider == "anthropic":
        anthropic: AnthropicModelSettings = {
            "max_tokens": max_tokens,
            "anthropic_thinking": {"type": "adaptive"},
            "anthropic_effort": "high",
        }
        return anthropic
    if provider == "groq":
        groq: GroqModelSettings = {
            "max_tokens": max_tokens,
            "groq_reasoning_effort": "high",
            # "parsed" keeps the reasoning on its own field instead of inlining
            # it into content, where it would be parsed as part of the answer.
            # Groq rejects "raw" for gpt-oss, and "hidden" would throw the
            # trace away.
            "groq_reasoning_format": "parsed",
        }
        return groq
    return {"max_tokens": max_tokens}


def drop_reasoning(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The conversation without the model's earlier reasoning traces.

    pydantic-ai's Groq adapter sends every earlier `ThinkingPart` back inside
    `<think>` tags, and Groq bills them against the per-request cap. Measured on
    the wire for `airflow__32529760720`, the assistant turns were 945 tokens by
    the third request — almost all reasoning around two short tool calls —
    and the request was refused at 8,056 against 8,000. Without them it is
    ~6.5k.

    Nothing is lost that the verdict can use. Every citation has to come from a
    tool result, which stays; a trace only restates what the model already
    concluded from one. gpt-oss's own chat format drops earlier reasoning
    between turns for the same reason.
    """
    return [
        replace(m, parts=[p for p in m.parts if not isinstance(p, ThinkingPart)])
        if isinstance(m, ModelResponse) and any(isinstance(p, ThinkingPart) for p in m.parts)
        else m
        for m in messages
    ]


def build_agent(
    model: str | Model = MODEL, *, retries: int = RETRIES, sandbox: bool = False
) -> Agent[TriageContext, Verdict]:
    """Construct the agent. Tools are bound to the fixture passed as `deps`.

    With `sandbox`, a fourth tool can run a command at the run's commit. It is
    left off the agent otherwise rather than registered and refused, so the
    offline agent — the one the eval scores — is shown exactly the tools it
    always was.

    `defer_model_check` keeps construction free of credentials, so the prompt,
    the tool schemas and the output type can all be tested without an API key
    and without spending a request.
    """
    agent = Agent(
        _model_for(model),
        deps_type=TriageContext,
        output_type=Verdict,
        instructions=INSTRUCTIONS,
        retries=retries,
        defer_model_check=True,
        model_settings=_model_settings(model),
        capabilities=[ProcessHistory(drop_reasoning)],
    )

    @agent.tool
    def get_logs(ctx: RunContext[TriageContext], job_name: str | None = None) -> str:
        """Reduced log excerpts for the jobs that failed.

        Args:
            job_name: A specific failed job. Omit for one representative per
                distinct failure, which is the right default.
        """
        return ctx.deps.metered(ctx.deps.get_logs(job_name))

    @agent.tool
    def get_diff(ctx: RunContext[TriageContext]) -> str:
        """The unified diff of the commit under test, when one was captured."""
        return ctx.deps.metered(ctx.deps.get_diff())

    @agent.tool
    def test_history(ctx: RunContext[TriageContext]) -> str:
        """Outcomes of this workflow on this commit and on recent other commits.

        The only evidence that can support or rule out `flaky`.
        """
        return ctx.deps.metered(ctx.deps.test_history())

    if sandbox:

        @agent.tool
        def reproduce(
            ctx: RunContext[TriageContext], command: str | None = None, patch: str | None = None
        ) -> str:
            """Run a command at the failing commit in an isolated sandbox.

            Use it to check a hypothesis the logs leave open — above all, whether
            a failure recurs (a regression) or not (flaky) — or to check that a
            fix you intend to suggest turns the command green. It costs minutes,
            so run it at most a couple of times. Its output cannot be cited as
            evidence; citations still come from `get_logs`.

            Args:
                command: Shell to run from the repository root. Omit to rerun
                    the script of the step that failed. The sandbox has only
                    the repository, not the job's setup steps, so prefer the
                    narrowest command that shows the failure.
                patch: A unified diff to apply first, relative to the root.
            """
            cmd = command or ctx.deps.failing_command()
            if not cmd:
                return "The failing step ran an action, not a command; pass `command`."
            return ctx.deps.metered(ctx.deps.reproduce(cmd, patch))

    return agent


# --------------------------------------------------------------------------
# Checking the answer
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceCheck:
    """One citation, and whether it survives being looked up."""

    evidence: Evidence
    ok: bool
    reason: str


@dataclass(frozen=True)
class TriageResult:
    """A verdict, where it routes, whether its citations hold up, what it cost.

    `usage` is carried here rather than logged and dropped because the cost of a
    triage is a property of the run, not a side effect of it. A free tier makes
    that concrete: a fixture holding 1.3k tokens of logs costs tens of thousands
    of tokens to triage, because every turn of the agent loop re-sends the whole
    conversation. Nothing in the reduced excerpt hints at that multiplier, so the
    number has to be measured and kept next to the verdict it paid for.
    """

    verdict: Verdict
    route: Route
    route_reason: str
    checks: tuple[EvidenceCheck, ...]
    usage: RunUsage | None = None
    cached: bool = False
    #: What the agent ran in the sandbox on the way to this verdict. Empty when
    #: there was no sandbox, or the verdict came from the cache.
    reproductions: tuple[Reproduction, ...] = ()

    @property
    def evidence_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> str:
        lines = [
            f"category:   {self.verdict.category.value}  (confidence {self.verdict.confidence:.2f})",
            f"route:      {self.route.value} — {self.route_reason}",
            f"summary:    {self.verdict.summary}",
            "",
            "evidence:",
        ]
        for check in self.checks:
            mark = "ok  " if check.ok else "BAD "
            ev = check.evidence
            lines.append(f"  [{mark}] {ev.log_path}:{ev.line_start}-{ev.line_end} — {ev.why}")
            if not check.ok:
                lines.append(f"          {check.reason}")
        if self.verdict.suggested_fix:
            lines += ["", f"suggested fix: {self.verdict.suggested_fix}"]
        for r in self.reproductions:
            what = "patched" if r.patched else "as committed"
            lines += ["", f"reproduced: {r.command!r} {what} — {r.outcome()}"]
        if self.usage is not None:
            u = self.usage
            total = (u.input_tokens or 0) + (u.output_tokens or 0)
            spent = " (cached — nothing spent)" if self.cached else ""
            lines += [
                "",
                f"cost:       {total:,} tokens "
                f"({u.input_tokens:,} in / {u.output_tokens:,} out) "
                f"over {u.requests} request(s){spent}",
            ]
        elif self.cached:
            lines += ["", "cost:       cached — nothing spent"]
        return "\n".join(lines)


def check_evidence(ctx: TriageContext, verdict: Verdict) -> tuple[EvidenceCheck, ...]:
    """Look every citation up in the log it claims to come from."""
    checks = []
    for ev in verdict.evidence:
        raw = ctx.raw_log(ev.log_path)
        if raw is None:
            checks.append(EvidenceCheck(ev, False, f"no log named {ev.log_path!r} in this fixture"))
            continue
        ok, reason = verify_evidence(ev, raw)
        checks.append(EvidenceCheck(ev, ok, reason))
    return tuple(checks)


#: Where answered verdicts are kept. Gitignored: it is a cache, not a result.
CACHE_DIR = Path(".triage-cache")


def _cache_key(ctx: TriageContext, model: str) -> str:
    """Identify a verdict by everything that could have changed it.

    Everything means everything the model can see, so the key hashes the actual
    output of every tool, not just `INSTRUCTIONS` and the fixture's name. The
    earlier key named the fixture, the model, `max_lines` and the prompt, which
    covers a prompt edit and misses the whole layer underneath it: the anchors
    in `logs.py`, the excerpt budget arithmetic, the failure fingerprint in
    `tools.py`. Change any of those and the model is shown different text under
    an unchanged key — so the cache serves answers to a question no longer being
    asked, and the first symptom is an eval score that refuses to move no matter
    what you improve. That is precisely the failure this docstring already
    warned about for the prompt, with a door left open beside it.

    Hashing the rendered text closes it without anyone having to remember to
    bump a version: the reduction *is* part of the prompt, so it belongs in the
    key the same way the prompt does. `max_lines` drops out as a separate
    component because `get_logs()` already reflects it.

    The cost is reading the fixture's logs on a cache lookup, which is local
    work on a few hundred KB — nothing next to the request it avoids.
    """
    h = hashlib.sha256()
    parts: tuple[str, ...] = (
        ctx.fixture.name,
        model,
        # What the tools show after the first call depends on the run budget,
        # and nothing rendered below reflects it.
        f"run_chars={ctx.run_chars}",
        INSTRUCTIONS,
        ctx.overview(),
        ctx.get_logs(),
        ctx.get_diff(),
        ctx.test_history(),
    )
    # A sandbox gives the agent a tool, and so a different question. Appended
    # only when there is one, so every offline verdict keeps the key it was
    # paid for under.
    if ctx.sandbox is not None:
        parts += (f"sandbox={ctx.sandbox.describe()}",)
    for part in parts:
        h.update(part.encode())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _cached_verdict(key: str) -> tuple[Verdict, RunUsage | None] | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text())
        usage = RunUsage(**blob["usage"]) if blob.get("usage") else None
        return Verdict.model_validate(blob["verdict"]), usage
    except (ValueError, KeyError, TypeError):
        # A cache that cannot be read is a cache miss, never an error. The most
        # likely cause is a `Verdict` field added since it was written.
        return None


def is_answered(fixture: Path | str, *, model: str = MODEL, max_lines: int = 300) -> bool:
    """Whether `triage` could answer for this fixture without calling the model.

    The eval harness asks this before a sweep so it can report over the verdicts
    already paid for without spending anything — which is the difference between
    a report you can regenerate while rate-limited and one you cannot.
    """
    ctx = TriageContext(Path(fixture), max_lines=max_lines)
    return _cached_verdict(_cache_key(ctx, model)) is not None


def _store_verdict(key: str, verdict: Verdict, usage: RunUsage | None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    blob: dict = {"verdict": verdict.model_dump(mode="json")}
    if usage is not None:
        blob["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "requests": usage.requests,
        }
    (CACHE_DIR / f"{key}.json").write_text(json.dumps(blob, indent=2))


#: Attribute the partial usage of a failed run is attached to, on the exception
#: that ended it.
#:
#: A run that dies has still been paid for: every model request before the one
#: that failed was served and billed, and on a free tier those are the tokens
#: that will not be there tomorrow. `run_sync` raises instead of returning, so
#: its `RunUsage` goes with it, and a sweep of 43 fixtures where 37 fail reports
#: the cost of the 6 — today that read 7,673 tokens against a real spend near
#: 200,000, which is the kind of wrong number that makes someone budget two days
#: for something that ends in an hour.
#:
#: Attached to the exception rather than raised as a new one so that every
#: existing `except ModelHTTPError` in the CLI keeps working, and so the status
#: code a caller needs is still on the object it was always on.
SPENT_ATTR = "triage_spent_usage"


def _usage_of(messages: list[ModelMessage]) -> RunUsage | None:
    """Total usage across the model responses a run did get back."""
    spent = RunUsage()
    served = 0
    for message in messages:
        if isinstance(message, ModelResponse) and message.usage is not None:
            spent.incr(message.usage)
            served += 1
    if not served:
        return None
    # `incr` carries the token counts of a `RequestUsage` and not a request
    # count, which only a `RunUsage` has. Counting the responses is the same
    # number and is the one that makes "N tokens over 0 requests" impossible.
    spent.requests = served
    return spent


#: Times a run is attempted from scratch when it ends in a failure a second
#: attempt can plausibly avoid. Each attempt is a fresh conversation, since the
#: one that failed is the thing being escaped.
#:
#: Two failures qualify, and both are the model's dice rather than the input's:
#:
#: * **413.** A request only outgrows the cap when the loop runs long — the
#:   schema missed, the model asked for more logs — so the same fixture fits
#:   on one attempt and not the next. `core__35505915015` answered in 4.1k
#:   tokens once and was refused at 9.0k another time.
#: * **400 `tool_use_failed`.** Groq refuses a generation it cannot parse as a
#:   tool call. pydantic-ai turns most of these into a retry within the run,
#:   and raises the rest, which then ended the fixture on a coin toss.
#:
#: A 429 is not retried here: the SDK already waits out a per-minute limit, and
#: a per-day one is not going to clear in the next few seconds.
RUN_ATTEMPTS = 2


def _worth_another_attempt(exc: Exception) -> bool:
    if not isinstance(exc, ModelHTTPError):
        return False
    if exc.status_code == 413:
        return True
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    return exc.status_code == 400 and isinstance(error, dict) and error.get("code") == "tool_use_failed"


def _add(a: RunUsage | None, b: RunUsage | None) -> RunUsage | None:
    if a is None or b is None:
        return a or b
    return a + b


def spent_on(exc: BaseException) -> RunUsage | None:
    """What a failed triage cost before it failed, if it got that far."""
    return getattr(exc, SPENT_ATTR, None)


def triage(
    fixture: Path | str,
    *,
    model: str = MODEL,
    max_lines: int = 300,
    trace: bool = False,
    cache: bool = True,
    sandbox: Sandbox | None = None,
) -> TriageResult:
    """Triage one captured run end to end.

    Only the model's answer is cached. Routing and evidence verification are
    recomputed on every call from the verdict, so raising the confidence
    threshold or tightening `verify_evidence` re-scores every run already
    answered, for free. That split matters on a free tier: the expensive half is
    the half that does not change when you improve the cheap half.
    """
    ctx = TriageContext(Path(fixture), max_lines=max_lines, sandbox=sandbox)
    if trace:
        enable_tracing()

    key = _cache_key(ctx, model)
    hit = _cached_verdict(key) if cache else None
    if hit is not None:
        verdict, usage = hit
        cached = True
    else:
        agent = build_agent(model, sandbox=sandbox is not None)
        # What earlier attempts spent. The verdict's cost is every attempt it
        # took, not only the one that answered.
        spent: RunUsage | None = None
        for attempt in range(1, RUN_ATTEMPTS + 1):
            ctx.reset_meter()
            with capture_run_messages() as messages:
                try:
                    run = agent.run_sync(
                        ctx.overview(),
                        deps=ctx,
                        usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
                    )
                except Exception as exc:
                    spent = _add(spent, _usage_of(messages))
                    if attempt < RUN_ATTEMPTS and _worth_another_attempt(exc):
                        continue
                    setattr(exc, SPENT_ATTR, spent)
                    raise
            break
        verdict, usage = run.output, _add(spent, run.usage)
        _store_verdict(key, verdict, usage)
        cached = False

    destination, reason = route(verdict)
    return TriageResult(
        verdict,
        destination,
        reason,
        check_evidence(ctx, verdict),
        usage=usage,
        cached=cached,
        reproductions=tuple(ctx.reproductions),
    )
