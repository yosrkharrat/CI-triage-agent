"""GitHub REST client and fixture capture.

Everything the agent reasons about is captured to disk first, then read back
offline. That is not a detour on the way to a webhook — it is the point:

* Agent runs are reproducible. The same fixture produces the same prompt today
  and in six weeks, so an eval score change means the *agent* changed.
* The golden dataset grows as a side effect of ordinary use. Every run you look
  at is one more labelled case, instead of a miserable batch of 50 at the end.
* No network, no token, and no rate limit sits between you and a test run.

A fixture directory looks like:

    fixtures/<repo>__<run_id>/
        meta.json      capture provenance + human label
        run.json       raw workflow run payload
        jobs.json      raw jobs payload
        logs.zip       untouched archive, as GitHub served it
        logs/          extracted, one .txt per job
        diff.patch     unified diff of the commit under test
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ci_triage.models import FixtureMeta, HistoricalRun, Job, RunHistory, WorkflowRun

API_ROOT = "https://api.github.com"
_RUN_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)")
_LOG_PREFIX = re.compile(r"^\d+_")
#: Characters the log archiver cannot put in a filename. They are not handled
#: uniformly, which is the whole difficulty: most are replaced with "_", but a
#: colon is deleted outright, so `Server Tests - python:3.13, postgres:14` is
#: archived as `Server Tests - python3.13, postgres14`. Substituting "_" for it
#: produced a name matching nothing, and every job of a repo that versions its
#: matrix with colons — prefect, uv, airflow — resolved to no log at all while
#: the fixture looked healthy on disk. Measured across every captured fixture,
#: deletion matches 224 of 233 such job names and substitution matches none.
_FS_UNSAFE = re.compile(r'[/\\*?"<>|]')
_FS_DELETED = re.compile(r":")


def _sanitize_job_name(name: str) -> str:
    return _FS_UNSAFE.sub("_", _FS_DELETED.sub("", name))

#: Separate connect and read budgets. A slow first byte from blob storage is
#: normal; a slow TCP handshake means something is actually wrong.
_API_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
_BLOB_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _with_retry(call, *, attempts: int = 3, base_delay: float = 1.5, what: str = "request"):
    """Retry a call through transient network failures.

    Log archives are served from blob storage over a redirect, which is the
    flakiest hop in the whole capture path — it stalls often enough that a
    single ReadTimeout should not cost you a fixture.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return call()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in _RETRY_STATUS:
                raise
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_delay * (2**attempt))
    raise GitHubError(f"{what} failed after {attempts} attempts: {last}") from last


class GitHubError(RuntimeError):
    pass


def parse_run_ref(ref: str) -> tuple[str, str, int]:
    """Accept either a run URL or `owner/repo#run_id` / `owner/repo run_id`."""
    if m := _RUN_URL.search(ref):
        return m.group(1), m.group(2), int(m.group(3))
    if m := re.fullmatch(r"([^/\s]+)/([^/\s#]+)[#\s]+(\d+)", ref.strip()):
        return m.group(1), m.group(2), int(m.group(3))
    raise ValueError(
        f"could not parse run reference {ref!r}; expected a run URL or 'owner/repo#run_id'"
    )


class GitHubClient:
    def __init__(self, token: str, *, api_root: str = API_ROOT, timeout: httpx.Timeout | float | None = None):
        if not token:
            raise GitHubError("no GitHub token; set GITHUB_TOKEN in .env.local")
        # GitHub answers 301 for a repo that has been renamed or transferred,
        # pointing at the numeric `/repositories/<id>/...` form. Not following
        # that turns an ordinary rename into an `HTTPStatusError`, which is how
        # one moved repo (`encode/starlette`) ended a batch capture eleven repos
        # early. The log-archive download opts out per-request, since that
        # redirect goes to blob storage and must not carry the token.
        self._client = httpx.Client(
            base_url=api_root,
            follow_redirects=True,
            timeout=timeout or _API_TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ci-triage-agent",
            },
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc) -> None:
        self._client.close()

    def _get(self, path: str, **kwargs) -> httpx.Response:
        r = _with_retry(lambda: self._client.get(path, **kwargs), what=f"GET {path}")
        if r.status_code == 404:
            raise GitHubError(f"not found: {path} (private repo, or token lacks access?)")
        if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
            reset = r.headers.get("x-ratelimit-reset", "?")
            raise GitHubError(f"rate limited; resets at epoch {reset}")
        r.raise_for_status()
        return r

    def get_run(self, owner: str, repo: str, run_id: int) -> tuple[WorkflowRun, dict]:
        payload = self._get(f"/repos/{owner}/{repo}/actions/runs/{run_id}").json()
        return WorkflowRun.model_validate(payload), payload

    def get_jobs(self, owner: str, repo: str, run_id: int) -> tuple[list[Job], dict]:
        """Fetch every job, following pagination — matrix builds exceed one page."""
        jobs: list[dict] = []
        page = 1
        while True:
            payload = self._get(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                params={"per_page": 100, "page": page, "filter": "latest"},
            ).json()
            jobs.extend(payload["jobs"])
            if len(jobs) >= payload.get("total_count", len(jobs)) or not payload["jobs"]:
                break
            page += 1
        return [Job.model_validate(j) for j in jobs], {"total_count": len(jobs), "jobs": jobs}

    def get_logs_zip(self, owner: str, repo: str, run_id: int) -> bytes:
        """Download the run's log archive.

        The API answers with a 302 to blob storage. The redirect is followed
        manually and *without* the Authorization header — that storage host
        rejects requests carrying someone else's credentials, and sending a
        token to a third party is a bad habit besides.
        """
        r = self._client.get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs", follow_redirects=False
        )
        if r.status_code == 410:
            raise GitHubError("logs have expired (GitHub keeps them ~90 days)")
        if r.status_code in (301, 302, 307):
            location = r.headers["location"]
            with httpx.Client(timeout=_BLOB_TIMEOUT, follow_redirects=True) as anon:
                def _download() -> httpx.Response:
                    blob = anon.get(location)
                    blob.raise_for_status()
                    return blob

                return _with_retry(_download, what="log archive download").content
        r.raise_for_status()
        return r.content


    def get_run_history(
        self,
        owner: str,
        repo: str,
        run: WorkflowRun,
        *,
        workflow_limit: int = 20,
        max_attempts: int = 5,
    ) -> RunHistory:
        """Collect the runs around `run` that bear on why it is red.

        Scoped to the *same workflow* throughout. Mixing workflows would
        manufacture false flake signals: "CI failed but Lint passed on this
        commit" says nothing about non-determinism, yet it looks exactly like a
        pass and a fail on one tree.
        """
        truncated = False

        # Every run on this exact commit. The filter returns only the latest
        # attempt of each run, so re-runs are collected separately below.
        payload = self._get(
            f"/repos/{owner}/{repo}/actions/runs",
            params={"head_sha": run.head_sha, "per_page": 100},
        ).json()
        same_commit = [
            HistoricalRun.model_validate(r)
            for r in payload.get("workflow_runs", [])
            if run.workflow_id is None or r.get("workflow_id") == run.workflow_id
        ]

        # A re-run keeps the run id and increments the attempt, so earlier
        # attempts are invisible to the query above — and an earlier attempt
        # that went green is the single strongest flake signal there is.
        if run.run_attempt > 1:
            for n in range(1, min(run.run_attempt, max_attempts + 1)):
                try:
                    prior = self._get(
                        f"/repos/{owner}/{repo}/actions/runs/{run.id}/attempts/{n}"
                    ).json()
                except (GitHubError, httpx.HTTPError):
                    continue
                same_commit.append(HistoricalRun.model_validate(prior))
            truncated = run.run_attempt > max_attempts + 1

        # Recent runs of the same workflow on *other* commits: was this job
        # already failing before the diff under test showed up?
        same_workflow: list[HistoricalRun] = []
        if run.workflow_id is not None:
            sibling = self._get(
                f"/repos/{owner}/{repo}/actions/workflows/{run.workflow_id}/runs",
                params={"per_page": workflow_limit},
            ).json()
            same_workflow = [
                HistoricalRun.model_validate(r)
                for r in sibling.get("workflow_runs", [])
                if r.get("head_sha") != run.head_sha
            ]

        return RunHistory(
            head_sha=run.head_sha,
            workflow_name=run.name,
            same_commit=sorted(same_commit, key=lambda r: (r.created_at, r.run_attempt)),
            same_workflow=same_workflow,
            truncated=truncated,
        )

    def list_failed_runs(
        self,
        owner: str,
        repo: str,
        *,
        limit: int = 10,
        event: str | None = "pull_request",
    ) -> list[HistoricalRun]:
        """Recent failed runs, newest first — candidates for the golden set."""
        params: dict[str, object] = {"status": "failure", "per_page": min(limit, 100)}
        if event:
            params["event"] = event
        payload = self._get(f"/repos/{owner}/{repo}/actions/runs", params=params).json()
        return [HistoricalRun.model_validate(r) for r in payload.get("workflow_runs", [])[:limit]]

    def get_diff(self, owner: str, repo: str, sha: str) -> str:
        """Unified diff of the commit under test."""
        r = self._get(
            f"/repos/{owner}/{repo}/commits/{sha}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        return r.text

    # -- writing, for the webhook service ------------------------------------

    def _write(self, method: str, path: str, body: dict) -> dict:
        """Send one write, without retrying it.

        Reads go through `_with_retry`; writes do not. A POST that timed out may
        still have landed, and retrying it is how a PR collects two identical
        comments. Losing one comment is the cheaper failure.
        """
        r = self._client.request(method, path, json=body)
        if r.status_code in (403, 404):
            raise GitHubError(
                f"{method} {path}: {r.status_code} — does the token or App have write access "
                "to pull requests?"
            )
        r.raise_for_status()
        return r.json()

    def open_pulls_for_commit(self, owner: str, repo: str, sha: str) -> list[int]:
        """Open PRs whose head is `sha`.

        A run's own `pull_requests` field is empty whenever the PR comes from a
        fork, which is most PRs to a public repo, so this is the fallback.
        """
        pulls = self._get(f"/repos/{owner}/{repo}/commits/{sha}/pulls").json()
        return [p["number"] for p in pulls if p.get("state") == "open"]

    def find_comment(self, owner: str, repo: str, number: int, marker: str) -> int | None:
        """The id of the comment on a PR carrying `marker`, if there is one."""
        page = 1
        while True:
            comments = self._get(
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                params={"per_page": 100, "page": page},
            ).json()
            for c in comments:
                if marker in (c.get("body") or ""):
                    return int(c["id"])
            if len(comments) < 100:
                return None
            page += 1

    def upsert_comment(self, owner: str, repo: str, number: int, body: str, marker: str) -> str:
        """Post `body` on a PR, or edit the comment that already carries `marker`.

        Re-running a failed job produces a new run attempt and a new verdict; it
        should replace the comment about that run rather than stack a second one
        under it. Returns the comment's URL.
        """
        existing = self.find_comment(owner, repo, number, marker)
        if existing is not None:
            c = self._write("PATCH", f"/repos/{owner}/{repo}/issues/comments/{existing}", {"body": body})
        else:
            c = self._write("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body})
        return str(c["html_url"])


# --------------------------------------------------------------------------
# GitHub App authentication
# --------------------------------------------------------------------------


class AppAuth:
    """Mint installation tokens for a GitHub App.

    An App authenticates as itself with a short-lived JWT signed by its private
    key, and trades that for a token scoped to one installation — one account
    that installed it. Tokens last an hour; they are cached and replaced five
    minutes before they expire, so a burst of webhooks costs one exchange.
    """

    def __init__(self, app_id: str, private_key: str, *, api_root: str = API_ROOT):
        self.app_id = app_id
        self.private_key = private_key
        self.api_root = api_root
        self._tokens: dict[int, tuple[str, datetime]] = {}

    def _jwt(self) -> str:
        import jwt

        now = int(time.time())
        # Backdated a minute because GitHub rejects an `iat` from its future,
        # and clocks drift; ten minutes is the most GitHub accepts.
        claims = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def token(self, installation: int) -> str:
        cached = self._tokens.get(installation)
        if cached and cached[1] - datetime.now(UTC) > timedelta(minutes=5):
            return cached[0]
        r = httpx.post(
            f"{self.api_root}/app/installations/{installation}/access_tokens",
            headers={
                "Authorization": f"Bearer {self._jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ci-triage-agent",
            },
            timeout=_API_TIMEOUT,
        )
        if r.status_code != 201:
            raise GitHubError(f"installation token for {installation}: {r.status_code} {r.text[:200]}")
        payload = r.json()
        expires = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        self._tokens[installation] = (payload["token"], expires)
        return str(payload["token"])


def save_fixture(
    client: GitHubClient,
    owner: str,
    repo: str,
    run_id: int,
    *,
    root: Path = Path("fixtures"),
    overwrite: bool = False,
) -> Path:
    """Capture one workflow run to `root/<repo>__<run_id>/`."""
    run, run_raw = client.get_run(owner, repo, run_id)
    dest = root / run.slug
    if dest.exists() and not overwrite:
        raise GitHubError(f"{dest} already exists; pass --overwrite to replace it")

    # Assemble in a staging directory and move it into place only once every
    # piece is on disk. A capture that dies halfway must not leave a
    # half-populated fixture behind: a silently incomplete fixture would sit in
    # the golden set producing wrong eval scores, and an empty one blocks the
    # retry that would have fixed it.
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".{run.slug}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()

    try:
        dest = _populate(client, owner, repo, run, run_raw, staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def _populate(
    client: GitHubClient,
    owner: str,
    repo: str,
    run: WorkflowRun,
    run_raw: dict,
    dest: Path,
    final: Path,
) -> Path:
    _, jobs_raw = client.get_jobs(owner, repo, run.id)
    (dest / "run.json").write_text(json.dumps(run_raw, indent=2))
    (dest / "jobs.json").write_text(json.dumps(jobs_raw, indent=2))

    try:
        blob = client.get_logs_zip(owner, repo, run.id)
        (dest / "logs.zip").write_bytes(blob)
        logs_dir = dest / "logs"
        logs_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            zf.extractall(logs_dir)
    except GitHubError as exc:
        (dest / "logs_unavailable.txt").write_text(str(exc))

    # `GitHubError` belongs here alongside `httpx.HTTPError`: `_get` converts a
    # 404 into `GitHubError`, and a commit can be unreachable (force-pushed,
    # deleted fork) while the run itself is perfectly capturable. Catching only
    # the httpx family let that abort the whole capture and discard the staging
    # directory — losing the logs, which are the part with a 90-day clock on them.
    try:
        (dest / "diff.patch").write_text(client.get_diff(owner, repo, run.head_sha))
    except (GitHubError, httpx.HTTPError) as exc:
        (dest / "diff_unavailable.txt").write_text(str(exc))

    try:
        history = client.get_run_history(owner, repo, run)
        (dest / "history.json").write_text(history.model_dump_json(indent=2))
    except (GitHubError, httpx.HTTPError) as exc:
        (dest / "history_unavailable.txt").write_text(str(exc))

    meta = FixtureMeta(
        repo=run.repository.full_name,
        run_id=run.id,
        run_attempt=run.run_attempt,
        fetched_at=datetime.now(UTC),
        html_url=run.html_url,
        label=None,
    )
    (dest / "meta.json").write_text(meta.model_dump_json(indent=2))

    if final.exists():
        shutil.rmtree(final)
    os.replace(dest, final)
    return final


def load_fixture(path: Path) -> tuple[WorkflowRun, list[Job], FixtureMeta | None]:
    """Read a captured run back from disk."""
    run = WorkflowRun.model_validate(json.loads((path / "run.json").read_text()))
    jobs_payload = json.loads((path / "jobs.json").read_text())
    jobs = [Job.model_validate(j) for j in jobs_payload["jobs"]]
    meta_path = path / "meta.json"
    meta = FixtureMeta.model_validate_json(meta_path.read_text()) if meta_path.exists() else None
    return run, jobs, meta


def load_history(path: Path) -> RunHistory | None:
    """Read `history.json` back, or None for fixtures captured before it existed."""
    f = path / "history.json"
    return RunHistory.model_validate_json(f.read_text()) if f.exists() else None


def log_path_for_job(fixture: Path, job: Job) -> Path | None:
    """Find the top-level `.txt` log that belongs to a job.

    GitHub names them `<n>_<job name>.txt`, where n is the job's position in the
    archive rather than anything stable — so the name has to carry the match.

    The job name is not used verbatim: characters that cannot appear in a
    filename are replaced with `_` first. Matrix jobs are exactly where this
    bites, because their generated names contain ` / ` almost by convention
    (`Test macos-latest / 3.13` becomes `24_Test macos-latest _ 3.13.txt`).
    Comparing against the raw name silently finds nothing, and a fixture whose
    logs all fail to resolve still looks perfectly healthy on disk.

    Very long names are truncated by the archiver, so an exact match is tried
    first and a prefix match only as a fallback.
    """
    logs = fixture / "logs"
    if not logs.is_dir():
        return None

    wanted = _sanitize_job_name(job.name)
    candidates = sorted(logs.glob("*.txt"))
    stems = {c: _LOG_PREFIX.sub("", c.stem) for c in candidates}

    for candidate, stem in stems.items():
        if stem == wanted:
            return candidate
    # Truncated name: the file stem is a prefix of the full job name. Require
    # some length so a short stem cannot match an unrelated job.
    for candidate, stem in stems.items():
        if len(stem) >= 8 and wanted.startswith(stem):
            return candidate
    return None
