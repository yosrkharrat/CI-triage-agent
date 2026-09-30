"""What the dashboard reads, and the live triage it watches.

The dashboard is a Next.js app in `dashboard/`, and this is its only door into
the agent. Everything here sits behind the same bearer token as the review
endpoints: the Next.js server holds it and the browser never sees it, because
one of these routes spends model quota and the rest serve logs.

    GET  /api/fixtures                   the corpus, with each model's last scored verdict
    GET  /api/fixtures/{name}            one run: overview, failed jobs, label, verdicts
    GET  /api/fixtures/{name}/logs       what the agent's own tools return —
    GET  /api/fixtures/{name}/diff         the second agent in the dashboard asks
    GET  /api/fixtures/{name}/history      its questions through these
    POST /api/fixtures/{name}/triage     a triage run, streamed as it happens
    GET  /api/runs/{id}/verdict          a live run's verdict, each citation re-checked

The stream speaks the Vercel AI SDK's UI message protocol, through pydantic-ai's
own adapter, so the browser renders each tool call as the agent makes it. It is
the same agent `ci-triage triage` runs and the eval scores, shown the same
prompt, and its answer lands in the same verdict cache: a triage watched live
is one the eval does not pay for again. What the stream adds at the end is what
the model cannot say about itself — whether each citation verified, and where
routing sends the verdict — as a `data-verdict` part.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response

from ci_triage.github import load_fixture

if TYPE_CHECKING:
    from pydantic_ai.agent import AgentRunResult

    from ci_triage.models import Verdict
    from ci_triage.service import Settings
    from ci_triage.store import Store

#: Where the corpus and the eval reports live, relative to the service's cwd.
FIXTURES = Path("fixtures")
REPORTS = Path("reports")

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def require_token(settings: Settings, authorization: str | None) -> None:
    """The bearer check shared by review and dashboard routes."""
    if not settings.review_token:
        raise HTTPException(503, "switched off: set CI_TRIAGE_REVIEW_TOKEN")
    expected = f"Bearer {settings.review_token}".encode()
    if not hmac.compare_digest((authorization or "").encode(), expected):
        raise HTTPException(401, "bad token")


def resolve_fixture(name: str, roots: list[Path]) -> Path:
    """A fixture by name, from the corpus or from the service's own captures.

    Only a plain directory name that is a direct child of a known root: the name
    comes from a URL, and a URL must not be able to point at `../..`.
    """
    if not _NAME.match(name):
        raise HTTPException(400, "not a fixture name")
    for root in roots:
        path = root / name
        if (path / "run.json").is_file():
            return path
    raise HTTPException(404, f"no fixture {name!r}")


def _reports(root: Path) -> dict[str, dict[str, dict]]:
    """Each eval report's per-fixture score, keyed by model then fixture."""
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(root.glob("eval-*.json")):
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        out[report.get("model", path.stem)] = {s["fixture"]: s for s in report.get("scores", [])}
    return out


def _verdict_payload(ctx: Any, verdict: Verdict, usage: Any = None) -> dict:
    from ci_triage.agent import check_evidence
    from ci_triage.models import route

    dest, reason = route(verdict)
    checks = check_evidence(ctx, verdict)
    return {
        "verdict": verdict.model_dump(mode="json"),
        "route": dest.value,
        "route_reason": reason,
        "evidence_ok": all(c.ok for c in checks),
        "checks": [{"ok": c.ok, "reason": c.reason} for c in checks],
        "tokens": None
        if usage is None
        else (usage.input_tokens or 0) + (usage.output_tokens or 0),
    }


def dashboard_router(
    settings: Settings,
    store: Store | None = None,
    *,
    fixtures: Path | None = None,
    reports: Path | None = None,
) -> APIRouter:
    fixtures = fixtures or FIXTURES
    reports = reports or REPORTS
    roots = [fixtures, settings.capture_root]

    def auth(authorization: str | None = Header(None)) -> None:
        require_token(settings, authorization)

    router = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    def context(name: str) -> Any:
        from ci_triage.tools import TriageContext

        return TriageContext(resolve_fixture(name, roots))

    @router.get("/fixtures")
    def list_fixtures() -> list[dict]:
        scores = _reports(reports)
        out = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir()):
                if not (path / "run.json").is_file():
                    continue
                run, jobs, meta = load_fixture(path)
                out.append(
                    {
                        "name": path.name,
                        "source": "corpus" if root == fixtures else "live",
                        "repo": run.repository.full_name,
                        "branch": run.head_branch,
                        "workflow": run.name,
                        "conclusion": run.conclusion,
                        "created_at": run.created_at.isoformat(),
                        "failed_jobs": sum(1 for j in jobs if j.failed),
                        "label": meta.label.category.value if meta and meta.label else None,
                        "verdicts": {
                            model: {
                                "category": s.get("predicted"),
                                "confidence": s.get("confidence"),
                                "cited": s.get("cited"),
                                "verified": s.get("verified"),
                                "route": s.get("route"),
                            }
                            for model, by_fixture in scores.items()
                            if (s := by_fixture.get(path.name)) and s.get("predicted")
                        },
                    }
                )
        return out

    @router.get("/fixtures/{name}")
    def fixture_detail(name: str) -> dict:
        ctx = context(name)
        label = ctx.meta.label if ctx.meta else None
        return {
            "name": name,
            "repo": ctx.run.repository.full_name,
            "branch": ctx.run.head_branch,
            "workflow": ctx.run.name,
            "html_url": ctx.run.html_url,
            "head_sha": ctx.run.head_sha,
            "overview": ctx.overview(),
            "failed_jobs": [j.name for j in ctx.failed_jobs],
            "label": label.model_dump(mode="json") if label else None,
            "scores": {
                model: by_fixture[name]
                for model, by_fixture in _reports(reports).items()
                if name in by_fixture
            },
        }

    @router.get("/fixtures/{name}/logs")
    def fixture_logs(name: str, job: str | None = None) -> dict:
        return {"text": context(name).get_logs(job)}

    @router.get("/fixtures/{name}/diff")
    def fixture_diff(name: str) -> dict:
        return {"text": context(name).get_diff()}

    @router.get("/fixtures/{name}/history")
    def fixture_history(name: str) -> dict:
        return {"text": context(name).test_history()}

    @router.post("/fixtures/{name}/triage")
    async def stream_triage(name: str, request: Request, model: str | None = None) -> Response:
        from pydantic_ai.ui.vercel_ai import VercelAIAdapter
        from pydantic_ai.ui.vercel_ai.response_types import DataChunk
        from pydantic_ai.usage import UsageLimits

        from ci_triage.agent import MODEL, REQUEST_LIMIT, _cache_key, _store_verdict, build_agent

        ctx = context(name)
        model_name = model or settings.model or MODEL
        agent = build_agent(model_name)
        adapter = await VercelAIAdapter.from_request(request, agent=agent, sdk_version=7)

        async def on_complete(result: AgentRunResult[Any]) -> AsyncIterator[DataChunk]:
            verdict = result.output
            usage = result.usage
            # Same key as `triage()`: the prompt is the fixture's overview, not
            # whatever the browser sent, so the question is the eval's question.
            _store_verdict(_cache_key(type(ctx)(ctx.fixture), model_name), verdict, usage)
            yield DataChunk(type="data-verdict", data={"model": model_name, **_verdict_payload(ctx, verdict, usage)})

        async def events() -> AsyncIterator[Any]:
            async with agent.run_stream_events(
                ctx.overview(),
                deps=ctx,
                usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
            ) as stream:
                async for event in stream:
                    yield event

        return adapter.streaming_response(adapter.transform_stream(events(), on_complete=on_complete))

    @router.get("/runs/{run}/verdict")
    def run_verdict(run: int) -> dict:
        """A queued verdict with each citation checked again against its capture.

        The run's row keeps only whether every citation held. A reviewer
        deciding on it needs to see which one did not, and the stored comment
        cannot show that: it is written as the comment that would be posted.
        """
        from ci_triage.models import Verdict
        from ci_triage.tools import TriageContext

        record = store.get(run) if store is not None else None
        if record is None or record.verdict is None or record.fixture is None:
            raise HTTPException(404, "no verdict for that run")
        ctx = TriageContext(Path(record.fixture))
        return {"model": settings.model or "", **_verdict_payload(ctx, Verdict.model_validate(record.verdict))}

    return router
