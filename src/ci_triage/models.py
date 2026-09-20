"""Pydantic models for every boundary of the triage agent.

Three groups live here:

1. GitHub API shapes (`WorkflowRun`, `Job`, `Step`) — the subset of the REST
   response we actually depend on.
2. The agent's output (`Verdict`, `Evidence`) — what the model is constrained
   to produce.
3. The golden dataset (`Label`, `FixtureMeta`) — human ground truth, recorded
   at fetch time so the eval set builds up as a side effect of normal use.

Routing policy (auto-post vs. human review) is deliberately *not* a field the
model fills in. The model reports; `route()` decides.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# GitHub keeps adding fields to these payloads. Ignoring unknown keys means a
# new field upstream can never break parsing; the tradeoff is that a *renamed*
# field shows up as a validation error on the one we named, which is what we want.
_GH = ConfigDict(extra="ignore", frozen=True)

#: Step/job conclusions that mean "this is why the run is red".
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "startup_failure"})


# --------------------------------------------------------------------------
# GitHub API shapes
# --------------------------------------------------------------------------


class Step(BaseModel):
    model_config = _GH

    name: str
    number: int
    status: str
    conclusion: str | None = None

    @property
    def failed(self) -> bool:
        return self.conclusion in FAILED_CONCLUSIONS


class Job(BaseModel):
    model_config = _GH

    id: int
    name: str
    status: str
    conclusion: str | None = None
    html_url: str | None = None
    steps: list[Step] = Field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.conclusion in FAILED_CONCLUSIONS

    @property
    def failed_steps(self) -> list[Step]:
        return [s for s in self.steps if s.failed]


class Repository(BaseModel):
    model_config = _GH

    full_name: str


class PullRequestRef(BaseModel):
    model_config = _GH

    number: int


class WorkflowRun(BaseModel):
    model_config = _GH

    id: int
    name: str | None = None
    head_branch: str | None = None
    head_sha: str
    event: str
    status: str
    conclusion: str | None = None
    run_attempt: int = 1
    workflow_id: int | None = None
    html_url: str
    created_at: datetime
    repository: Repository
    pull_requests: list[PullRequestRef] = Field(default_factory=list)

    @property
    def slug(self) -> str:
        """Filesystem-safe id for this run, e.g. `sweep__30005725094`."""
        return f"{self.repository.full_name.split('/')[-1]}__{self.id}"


# --------------------------------------------------------------------------
# Run history — the evidence that makes `flaky` decidable
# --------------------------------------------------------------------------


class HistoricalRun(BaseModel):
    """One neighbouring run, reduced to the fields that carry signal."""

    model_config = _GH

    id: int
    name: str | None = None
    run_attempt: int = 1
    status: str
    conclusion: str | None = None
    head_sha: str
    created_at: datetime
    html_url: str

    @property
    def failed(self) -> bool:
        return self.conclusion in FAILED_CONCLUSIONS


class RunHistory(BaseModel):
    """What happened around this run, captured so the agent can see it.

    `FailureCategory.FLAKY` is defined as "the same commit passes and fails".
    A single run's logs cannot show that, so without this the flaky label would
    rest on evidence the agent never receives — every flaky miss would be an
    information gap wearing a reasoning gap's clothes, and no amount of prompt
    work would close it.

    Two neighbourhoods, answering two different questions:

    * `same_commit` — did this exact tree ever go green? A pass and a fail on
      one SHA is the definition of non-determinism.
    * `same_workflow` — was this job already red before this commit landed? A
      workflow failing across unrelated commits points at infra or a dependency
      rather than at the diff under test.
    """

    model_config = ConfigDict(frozen=True)

    head_sha: str
    workflow_name: str | None = None
    same_commit: list[HistoricalRun] = Field(default_factory=list)
    same_workflow: list[HistoricalRun] = Field(default_factory=list)
    truncated: bool = Field(
        default=False, description="True when the neighbourhood was larger than the fetch budget."
    )

    @property
    def same_commit_conclusions(self) -> set[str]:
        return {r.conclusion for r in self.same_commit if r.conclusion}

    @property
    def passed_and_failed_on_same_commit(self) -> bool:
        """The definitional flake signal: one tree, both outcomes."""
        seen = self.same_commit_conclusions
        return "success" in seen and bool(seen & FAILED_CONCLUSIONS)

    @property
    def workflow_failure_rate(self) -> float | None:
        """Fraction of recent same-workflow runs on *other* commits that failed."""
        others = [r for r in self.same_workflow if r.conclusion]
        if not others:
            return None
        return sum(r.failed for r in others) / len(others)

    def summary(self) -> str:
        """Compact rendering for the agent prompt.

        Deliberately states the observations and not the conclusion. Writing
        "this looks flaky" here would move the judgement out of the model and
        into a heuristic, and then the eval would be scoring this function.
        """
        lines = [f"=== run history for {self.head_sha[:8]} ==="]

        if self.same_commit:
            lines.append(f"{len(self.same_commit)} run(s) on this exact commit:")
            for r in sorted(self.same_commit, key=lambda r: r.created_at):
                lines.append(
                    f"  attempt {r.run_attempt}: {r.conclusion or r.status}"
                    f"  ({r.name or 'workflow'}, {r.created_at:%Y-%m-%d %H:%M})"
                )
            if self.passed_and_failed_on_same_commit:
                lines.append("  -> this commit has BOTH passed and failed")
            else:
                lines.append("  -> no passing run recorded for this commit")
        else:
            lines.append("no other runs recorded on this commit")

        rate = self.workflow_failure_rate
        if rate is not None:
            n = len([r for r in self.same_workflow if r.conclusion])
            lines.append(
                f"same workflow, {n} recent run(s) on other commits: "
                f"{rate:.0%} failed"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# The agent's output
# --------------------------------------------------------------------------


class FailureCategory(str, Enum):
    """Why the run is red. The definitions live in `CATEGORY_GUIDE` below."""

    FLAKY = "flaky"
    REGRESSION = "regression"
    INFRA = "infra"
    DEPENDENCY = "dependency"
    UNKNOWN = "unknown"


#: What each category means. These are not decoration: they are handed to the
#: model as part of the `category` field description, *and* they are the
#: labelling guide a human follows when adding a fixture to the golden set. One
#: definition, both uses — if the two ever drift, eval scores stop meaning
#: anything.
#:
#: They live in a dict rather than in per-member docstrings because a string
#: literal written under an enum member is discarded at runtime: it is an
#: expression statement, not an assignment, so `FailureCategory.FLAKY.__doc__`
#: returns the *class* docstring. Written that way the definitions reached no
#: schema and no prompt, and the agent was left to work out for itself what
#: separates `infra` from `dependency` — with the eval then scoring that guess.
CATEGORY_GUIDE: dict[FailureCategory, str] = {
    FailureCategory.FLAKY: (
        "Non-deterministic. The same commit passes and fails across attempts: "
        "timing/race conditions, test-order dependence, network blips, resource "
        "exhaustion on the runner. Requires history showing both outcomes on one "
        "commit — a single red run is not evidence of flakiness."
    ),
    FailureCategory.REGRESSION: (
        "The repo's own source is genuinely broken: compile errors, failing "
        "assertions, type errors. A human fixes it by editing application or test "
        "code.\n\n"
        "Deliberately NOT conditioned on the failure originating in this run's "
        "diff. A Rust compile error in code the diff never touched is still a "
        "regression when running CI for the first time merely exposed a latent "
        "break. Whether the diff introduced the fault is a genuinely useful "
        "signal, but it belongs in the evidence, not in the category."
    ),
    FailureCategory.INFRA: (
        "The CI environment or workflow configuration is at fault, not the code: "
        "missing permissions or secrets, bad runner image, misconfigured workflow "
        "YAML, quota and registry outages.\n\n"
        "Breadth alone does not make a failure infra. One deterministic break can "
        "fan out across every leg of a matrix and still be a regression; what "
        "distinguishes infra is that the fault lies outside the repo's own source."
    ),
    FailureCategory.DEPENDENCY: (
        "An external package, action, or service changed underneath an unpinned "
        "reference. The repo's own code is unchanged and previously passed.\n\n"
        "Which package raised the error does not settle this. An exception thrown "
        "from inside a third-party library is still a regression when the repo's "
        "own new code is what reached that path."
    ),
    FailureCategory.UNKNOWN: (
        "The available evidence does not support any of the above. Preferred over "
        "a confident guess — an honest `unknown` routes to a human, a wrong "
        "confident label wastes their time."
    ),
}


def category_guide() -> str:
    """Render the category definitions for a prompt or a field description."""
    return "\n\n".join(f"{c.value}: {text}" for c, text in CATEGORY_GUIDE.items())


class Evidence(BaseModel):
    """A citation into a specific log location.

    `line_start`/`line_end` index the *normalized* log (see `logs.py`), 1-based
    and inclusive. Because the quote and its coordinates are both recorded, a
    citation can be mechanically checked against the source — which is what lets
    the eval harness catch a model that invented a plausible-sounding log line.
    """

    model_config = ConfigDict(frozen=True)

    job_name: str = Field(description="Name of the job this log belongs to.")
    log_path: str = Field(description="Log file path, relative to the run's logs directory.")
    line_start: int = Field(ge=1, description="First line of the cited span (1-based, inclusive).")
    line_end: int = Field(ge=1, description="Last line of the cited span (1-based, inclusive).")
    quote: str = Field(description="The cited text, copied verbatim from the log.")
    why: str = Field(description="One sentence: what this line proves about the failure.")


class Verdict(BaseModel):
    """The agent's structured answer. This is the model's constrained output."""

    model_config = ConfigDict(frozen=True)

    category: FailureCategory = Field(
        description="The single best-fitting failure category.\n\n" + category_guide()
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Calibrated probability that `category` is correct. Below 0.7 the "
            "verdict is routed to a human instead of posted."
        ),
    )
    summary: str = Field(
        max_length=400,
        description="One or two sentences a reviewer can read without opening the logs.",
    )
    reasoning: str = Field(
        description="How the cited evidence leads to the category. Not shown in the PR comment."
    )
    evidence: list[Evidence] = Field(
        min_length=1,
        description="Log citations supporting the verdict. At least one is required.",
    )
    suggested_fix: str | None = Field(
        default=None,
        description="Concrete remedy, if one is well-supported by the evidence. Null otherwise.",
    )


# --------------------------------------------------------------------------
# Routing policy — code's decision, not the model's
# --------------------------------------------------------------------------


class Route(str, Enum):
    AUTO_POST = "auto_post"
    HUMAN_REVIEW = "human_review"


#: Below this confidence, nothing gets posted automatically.
CONFIDENCE_THRESHOLD = 0.7


def route(verdict: Verdict, threshold: float = CONFIDENCE_THRESHOLD) -> tuple[Route, str]:
    """Decide whether a verdict may be posted automatically.

    Returns the route and a human-readable reason. Kept as a pure function so
    the threshold can be swept in the eval harness without touching the agent.
    """
    if verdict.category is FailureCategory.UNKNOWN:
        return Route.HUMAN_REVIEW, "agent could not determine a category"
    if verdict.confidence < threshold:
        return Route.HUMAN_REVIEW, f"confidence {verdict.confidence:.2f} below {threshold:.2f}"
    if verdict.suggested_fix is not None:
        return Route.HUMAN_REVIEW, "verdict proposes a code change"
    return Route.AUTO_POST, f"confidence {verdict.confidence:.2f} at or above {threshold:.2f}"


# --------------------------------------------------------------------------
# Golden dataset
# --------------------------------------------------------------------------


class Label(BaseModel):
    """Human ground truth for one fixture."""

    category: FailureCategory
    root_cause: str = Field(description="One sentence, in your own words, on what actually broke.")
    notes: str | None = None
    labeled_at: datetime | None = None


class FixtureMeta(BaseModel):
    """`meta.json` alongside a saved run — how it was captured and what it is."""

    repo: str
    run_id: int
    run_attempt: int
    fetched_at: datetime
    html_url: str
    label: Label | None = None
