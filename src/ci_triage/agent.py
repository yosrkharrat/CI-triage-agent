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
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

from ci_triage.logs import verify_evidence
from ci_triage.models import Evidence, Route, Verdict, category_guide, route
from ci_triage.tools import NO_DIFF, TriageContext

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
two answer questions the logs alone cannot settle.

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
ask for that job's log by name rather than guessing at its contents.

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
    settings: ModelSettings = {
        # Enough for a reasoning trace plus a verdict carrying several verbatim
        # log quotes. gpt-oss allows 65k, so this budget, not the model, binds.
        "max_tokens": 16000,
    }
    # A constructed `Model` carries its own settings; `system` is the provider
    # name for the instance forms, and unknown ones simply get no reasoning key.
    provider = model.split(":", 1)[0] if isinstance(model, str) else getattr(model, "system", "")
    if provider == "anthropic":
        settings["anthropic_thinking"] = {"type": "adaptive"}
        settings["anthropic_effort"] = "high"
    elif provider == "groq":
        settings["groq_reasoning_effort"] = "high"
        # "parsed" keeps the reasoning on its own field instead of inlining it
        # into content, where it would be parsed as part of the answer. Groq
        # rejects "raw" for gpt-oss, and "hidden" would throw the trace away.
        settings["groq_reasoning_format"] = "parsed"
    return settings


def build_agent(
    model: str | Model = MODEL, *, retries: int = RETRIES
) -> Agent[TriageContext, Verdict]:
    """Construct the agent. Tools are bound to the fixture passed as `deps`.

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
    )

    @agent.tool
    def get_logs(ctx: RunContext[TriageContext], job_name: str | None = None) -> str:
        """Reduced log excerpts for the jobs that failed.

        Args:
            job_name: A specific failed job. Omit for one representative per
                distinct failure, which is the right default.
        """
        return ctx.deps.get_logs(job_name)

    @agent.tool
    def get_diff(ctx: RunContext[TriageContext]) -> str:
        """The unified diff of the commit under test, when one was captured."""
        return ctx.deps.get_diff()

    @agent.tool
    def test_history(ctx: RunContext[TriageContext]) -> str:
        """Outcomes of this workflow on this commit and on recent other commits.

        The only evidence that can support or rule out `flaky`.
        """
        return ctx.deps.test_history()

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


def _cache_key(fixture: Path, model: str, max_lines: int) -> str:
    """Identify a verdict by everything that could have changed it.

    The prompt is hashed along with the model and the fixture, so editing
    `INSTRUCTIONS` invalidates every cached verdict automatically. That is the
    only behaviour that is safe: a cache which survived a prompt change would
    quietly serve answers from the old prompt, and the first thing to look wrong
    would be an eval score that refused to move.
    """
    h = hashlib.sha256()
    for part in (fixture.name, model, str(max_lines), INSTRUCTIONS):
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
    return _cached_verdict(_cache_key(Path(fixture), model, max_lines)) is not None


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


def triage(
    fixture: Path | str,
    *,
    model: str = MODEL,
    max_lines: int = 300,
    trace: bool = False,
    cache: bool = True,
) -> TriageResult:
    """Triage one captured run end to end.

    Only the model's answer is cached. Routing and evidence verification are
    recomputed on every call from the verdict, so raising the confidence
    threshold or tightening `verify_evidence` re-scores every run already
    answered, for free. That split matters on a free tier: the expensive half is
    the half that does not change when you improve the cheap half.
    """
    ctx = TriageContext(Path(fixture), max_lines=max_lines)
    if trace:
        enable_tracing()

    key = _cache_key(ctx.fixture, model, max_lines)
    hit = _cached_verdict(key) if cache else None
    if hit is not None:
        verdict, usage = hit
        cached = True
    else:
        agent = build_agent(model)
        run = agent.run_sync(ctx.overview(), deps=ctx)
        verdict, usage = run.output, run.usage
        _store_verdict(key, verdict, usage)
        cached = False

    destination, reason = route(verdict)
    return TriageResult(
        verdict, destination, reason, check_evidence(ctx, verdict), usage=usage, cached=cached
    )
