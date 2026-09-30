"""Tests for the dashboard's API.

The live triage is streamed through a stub model, so what is pinned is the
wiring: the stream speaks the AI SDK's protocol, carries each tool call, ends
with the checks the model cannot make about itself, and leaves its answer in
the cache the eval reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ci_triage.agent as agent_mod
from ci_triage.api import resolve_fixture
from ci_triage.service import Settings, create_app

FIXTURE = Path("fixtures/sweep__30005725094")
pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="fixture not captured")
TOKEN = "t0ken"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class _NoPipeline:
    def capture(self, record):  # pragma: no cover - the dashboard never queues
        raise AssertionError


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        webhook_secret="s",
        db_path=tmp_path / "db.sqlite",
        capture_root=tmp_path / "runs",
        review_token=TOKEN,
    )
    return TestClient(create_app(settings, pipeline=_NoPipeline(), start_worker=False))  # type: ignore[arg-type]


def test_every_route_needs_the_token(client: TestClient):
    for path in ("/api/fixtures", f"/api/fixtures/{FIXTURE.name}", f"/api/fixtures/{FIXTURE.name}/logs"):
        assert client.get(path).status_code == 401
    assert client.post(f"/api/fixtures/{FIXTURE.name}/triage", json={}).status_code == 401


def test_a_url_cannot_reach_outside_the_fixture_roots(tmp_path: Path):
    from fastapi import HTTPException

    for bad in ("..", "../fixtures", "a/b", ".hidden", ""):
        with pytest.raises(HTTPException) as exc:
            resolve_fixture(bad, [Path("fixtures")])
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        resolve_fixture("nope", [Path("fixtures")])
    assert exc.value.status_code == 404


@pytest.fixture
def client_with_report(tmp_path: Path, monkeypatch) -> TestClient:
    import ci_triage.api as api

    score = {"fixture": FIXTURE.name, "predicted": "infra", "confidence": 0.9, "cited": 1,
             "verified": 1, "route": "auto_post"}  # fmt: skip
    (tmp_path / "eval-x.json").write_text(json.dumps({"model": "m:x", "scores": [score]}))
    monkeypatch.setattr(api, "REPORTS", tmp_path)
    settings = Settings(webhook_secret="s", db_path=tmp_path / "db.sqlite", review_token=TOKEN)
    return TestClient(create_app(settings, pipeline=_NoPipeline(), start_worker=False))  # type: ignore[arg-type]


def test_the_corpus_lists_with_last_scored_verdicts(client_with_report: TestClient):
    client = client_with_report
    rows = {r["name"]: r for r in client.get("/api/fixtures", headers=AUTH).json()}
    row = rows[FIXTURE.name]
    assert row["source"] == "corpus"
    assert row["failed_jobs"] >= 1
    assert row["verdicts"] == {
        "m:x": {"category": "infra", "confidence": 0.9, "cited": 1, "verified": 1, "route": "auto_post"}
    }


def test_the_tools_the_second_agent_calls_are_the_agents_own(client: TestClient):
    from ci_triage.tools import TriageContext

    ctx = TriageContext(FIXTURE)
    base = f"/api/fixtures/{FIXTURE.name}"
    assert client.get(f"{base}/logs", headers=AUTH).json()["text"] == ctx.get_logs()
    assert client.get(f"{base}/diff", headers=AUTH).json()["text"] == ctx.get_diff()
    assert client.get(f"{base}/history", headers=AUTH).json()["text"] == ctx.test_history()
    detail = client.get(base, headers=AUTH).json()
    assert detail["overview"] == ctx.overview()
    assert detail["head_sha"] == ctx.run.head_sha


def _events(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: {")
    ]


def test_a_live_triage_streams_tool_calls_then_the_checks(client: TestClient, tmp_path, monkeypatch):
    from pydantic_ai.models.test import TestModel

    monkeypatch.setattr(agent_mod, "CACHE_DIR", tmp_path / "cache")
    real = agent_mod.build_agent
    monkeypatch.setattr(agent_mod, "build_agent", lambda model=None, **kw: real(TestModel(), **kw))

    message = {"id": "m1", "role": "user", "parts": [{"type": "text", "text": "triage"}]}
    r = client.post(
        f"/api/fixtures/{FIXTURE.name}/triage",
        json={"trigger": "submit-message", "id": "chat", "messages": [message]},
        headers=AUTH,
    )
    assert r.status_code == 200
    events = _events(r.text)
    kinds = [e["type"] for e in events]
    tools = {e["toolName"] for e in events if e["type"] == "tool-input-available"}
    assert {"get_logs", "get_diff", "test_history"} <= tools
    [verdict] = [e for e in events if e["type"] == "data-verdict"]
    assert verdict["data"]["route"] in {"auto_post", "human_review"}
    assert len(verdict["data"]["checks"]) == len(verdict["data"]["verdict"]["evidence"])
    assert kinds.index("data-verdict") > max(i for i, k in enumerate(kinds) if k.startswith("tool-"))

    # Watched once, answered for good: `triage()` now finds it in the cache.
    result = agent_mod.triage(FIXTURE, model=verdict["data"]["model"])
    assert result.cached


def test_a_queued_verdict_comes_back_with_each_citation_rechecked(client: TestClient):
    from ci_triage.models import Evidence, FailureCategory, Verdict
    from ci_triage.store import Status

    store = client.app.state.store  # type: ignore[attr-defined]
    invented = Evidence(job_name="j", log_path="2_build (ubuntu-22.04).txt", line_start=1, line_end=1,
                        quote="a line that is nowhere in this log", why="w")  # fmt: skip
    verdict = Verdict(category=FailureCategory.INFRA, confidence=0.9, summary="s", reasoning="r", evidence=[invented])
    rid = store.enqueue(repo="o/r", run_id=1, run_attempt=1, html_url="u")
    store.claim_next()
    store.finish(rid, Status.AWAITING_REVIEW, fixture=str(FIXTURE), verdict=verdict.model_dump(mode="json"),
                 evidence_ok=False)  # fmt: skip

    body = client.get(f"/api/runs/{rid}/verdict", headers=AUTH).json()
    assert body["evidence_ok"] is False
    assert [c["ok"] for c in body["checks"]] == [False]
    assert client.get("/api/runs/999/verdict", headers=AUTH).status_code == 404
