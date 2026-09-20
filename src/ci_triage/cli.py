"""Command line entry points.

    ci-triage hunt <owner/repo>    find failed runs worth capturing
    ci-triage fetch <run-url>      capture a failed run to fixtures/
    ci-triage inspect <fixture>    show exactly what the agent will be shown
    ci-triage triage <fixture>     run the agent and print its verdict
    ci-triage label <fixture>      record human ground truth for the eval set
    ci-triage eval                 score the agent over the whole corpus
    ci-triage ls                   list fixtures and their labels
    ci-triage backfill             add history.json to older fixtures
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from ci_triage.github import (
    GitHubClient,
    GitHubError,
    load_fixture,
    load_history,
    log_path_for_job,
    parse_run_ref,
    save_fixture,
)
from ci_triage.tools import TriageContext
from ci_triage.models import FailureCategory, FixtureMeta, Label

app = typer.Typer(add_completion=False, help="Triage failed CI runs.")
console = Console()
FIXTURES = Path("fixtures")


def _token() -> str:
    load_dotenv(".env.local")
    load_dotenv(".env")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        console.print("[red]GITHUB_TOKEN not set[/red] — add it to .env.local")
        raise typer.Exit(1)
    return token


def _resolve(fixture: str) -> Path:
    """Accept a full path or a bare fixture name."""
    path = Path(fixture)
    if not path.exists():
        path = FIXTURES / fixture
    if not path.is_dir():
        console.print(f"[red]no such fixture:[/red] {fixture}")
        raise typer.Exit(1)
    return path


@app.command()
def fetch(
    run_ref: str = typer.Argument(..., help="Run URL, or owner/repo#run_id"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing fixture"),
) -> None:
    """Capture a workflow run — payloads, logs and diff — to fixtures/."""
    owner, repo, run_id = parse_run_ref(run_ref)
    with GitHubClient(_token()) as client:
        try:
            dest = save_fixture(client, owner, repo, run_id, root=FIXTURES, overwrite=overwrite)
        except GitHubError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    console.print(f"[green]saved[/green] {dest}")
    console.print(f"next: [bold]uv run ci-triage inspect {dest.name}[/bold]")


@app.command()
def inspect(
    fixture: str = typer.Argument(..., help="Fixture directory or name"),
    show_logs: bool = typer.Option(False, "--logs", help="Print the reduced log excerpts"),
    max_lines: int = typer.Option(300, help="Line budget per log"),
) -> None:
    """Show the run, its failures, and how far the logs reduce."""
    path = _resolve(fixture)
    run, jobs, meta = load_fixture(path)

    console.print(f"[bold]{run.repository.full_name}[/bold] run {run.id} — {run.name}")
    console.print(f"  {run.event} on {run.head_branch} @ {run.head_sha[:8]} — [red]{run.conclusion}[/red]")
    console.print(f"  {run.html_url}")
    if meta and meta.label:
        console.print(f"  label: [cyan]{meta.label.category.value}[/cyan] — {meta.label.root_cause}")
    else:
        console.print("  label: [yellow]unlabelled[/yellow]")

    ctx = TriageContext(path, max_lines=max_lines)

    # One row per *distinct* failure, because that is what the agent is handed.
    # Listing all 37 of pydantic's identically-failing jobs would describe the
    # fixture accurately and the agent's view not at all.
    table = Table("job", "jobs", "failed step", "log lines", "kept", "cut", title="\nfailures")
    total_raw = total_kept = 0
    for group in ctx.groups:
        job = group.representative
        ex = group.excerpt
        step = job.failed_steps[0].name if job.failed_steps else "—"
        total_raw += ex.total_lines
        total_kept += ex.kept_lines
        table.add_row(
            job.name,
            str(group.size) if group.size > 1 else "—",
            step,
            str(ex.total_lines),
            str(ex.kept_lines),
            f"{ex.reduction:.0%}",
        )
    for job in ctx.jobs_without_logs:
        step = job.failed_steps[0].name if job.failed_steps else "—"
        table.add_row(job.name, "—", step, "[yellow]no log[/yellow]", "—", "—")
    console.print(table)

    shown = sum(g.size for g in ctx.groups)
    if total_raw:
        rendered = ctx.get_logs()
        console.print(
            f"\n{shown} failed job(s) -> {len(ctx.groups)} distinct failure(s); "
            f"{total_raw:,} lines -> {total_kept:,} "
            f"([bold green]{1 - total_kept / total_raw:.0%} reduction[/bold green]), "
            f"~{len(rendered) // 4:,} tokens to the model"
        )

    history = load_history(path)
    if history is None:
        console.print("\n[yellow]no history.json[/yellow] — run: ci-triage backfill " + path.name)
    else:
        console.print()
        console.print(history.summary(), highlight=False, markup=False)

    if show_logs:
        console.print()
        console.print(ctx.get_logs(), highlight=False, markup=False)
        console.print("\n[bold]diff[/bold]")
        console.print(ctx.get_diff()[:2000], highlight=False, markup=False)


@app.command()
def triage(
    fixture: str = typer.Argument(..., help="Fixture directory or name"),
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help=(
            "Provider:model, e.g. groq:openai/gpt-oss-20b, "
            "anthropic:claude-opus-5, google-gla:gemini-2.0-flash, ollama:qwen2.5:14b"
        ),
    ),
    max_lines: int = typer.Option(300, help="Line budget per log excerpt"),
    trace: bool = typer.Option(False, "--trace", help="Emit Logfire spans for each step"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore any cached verdict and ask the model again"
    ),
) -> None:
    """Run the agent over a captured run and print its verdict.

    Prints the human label underneath when the fixture has one, so a
    disagreement is visible immediately rather than only in an eval report.
    """
    path = _resolve(fixture)
    load_dotenv(".env.local")
    load_dotenv(".env")

    # Imported here so the rest of the CLI — capture, inspect, label — keeps
    # working without the agent dependencies installed.
    from pydantic_ai.exceptions import ModelHTTPError, UserError

    from ci_triage.agent import MODEL, triage as run_triage

    console.print(f"triaging [bold]{path.name}[/bold] with {model or MODEL}...")
    # Which credential is needed depends on the provider in the model string,
    # so the check is left to pydantic-ai rather than hardcoded here — the
    # harness is model-agnostic, and a check that named one provider would
    # refuse to run every other one.
    try:
        result = run_triage(
            path, model=model or MODEL, max_lines=max_lines, trace=trace, cache=not refresh
        )
    except UserError as exc:
        console.print(f"[red]{exc}[/red]")
        console.print("add the key to [bold].env.local[/bold], or pass --model for another provider")
        raise typer.Exit(1)
    except ModelHTTPError as exc:
        # A free tier is a quota you will hit, not an edge case, and a 429 in
        # the middle of an eval sweep is the normal way a sweep ends. A traceback
        # here buries the one thing that matters — which budget ran out and when
        # it returns — under a stack of pydantic-ai frames.
        detail = ""
        body = exc.body if isinstance(exc.body, dict) else {}
        if isinstance(err := body.get("error"), dict):
            detail = str(err.get("message", ""))
        if exc.status_code == 429:
            console.print(f"[red]rate limited by {exc.model_name}[/red]")
            console.print(detail or "no detail returned")
            console.print(
                "\nwait for the window to reset, or pass [bold]--model[/bold] to use another "
                "provider. Groq's free tier caps tokens per minute and per day, and both "
                "are consumed by the whole conversation on every turn, not just the logs."
            )
        else:
            console.print(f"[red]{exc.model_name} returned {exc.status_code}[/red]")
            console.print(detail or str(exc))
        raise typer.Exit(1)
    console.print()
    console.print(result.render(), highlight=False, markup=False)

    _, _, meta = load_fixture(path)
    if meta and meta.label:
        agrees = meta.label.category is result.verdict.category
        mark = "[green]agrees[/green]" if agrees else "[red]disagrees[/red]"
        console.print(f"\nhuman label: [cyan]{meta.label.category.value}[/cyan] — {mark}")
        console.print(f"  {meta.label.root_cause}")
    else:
        console.print("\n[yellow]fixture is unlabelled[/yellow] — nothing to score against")
    if not result.evidence_ok:
        console.print("\n[red]some citations could not be verified against the logs[/red]")


@app.command()
def label(
    fixture: str = typer.Argument(..., help="Fixture directory or name"),
    category: FailureCategory = typer.Option(..., "--category", "-c", help="Ground-truth category"),
    root_cause: str = typer.Option(..., "--root-cause", "-r", help="One sentence: what broke"),
    notes: str | None = typer.Option(None, "--notes", "-n"),
) -> None:
    """Record human ground truth. This is the eval set's source of truth."""
    path = _resolve(fixture)
    run, _, meta = load_fixture(path)
    if meta is None:
        meta = FixtureMeta(
            repo=run.repository.full_name,
            run_id=run.id,
            run_attempt=run.run_attempt,
            fetched_at=datetime.now(timezone.utc),
            html_url=run.html_url,
        )
    meta = meta.model_copy(
        update={
            "label": Label(
                category=category,
                root_cause=root_cause,
                notes=notes,
                labeled_at=datetime.now(timezone.utc),
            )
        }
    )
    (path / "meta.json").write_text(meta.model_dump_json(indent=2))
    console.print(f"[green]labelled[/green] {path.name} as [cyan]{category.value}[/cyan]")


@app.command("eval")
def run_eval_command(
    fixtures: list[str] = typer.Argument(None, help="Fixture names; all of them when omitted"),
    model: str | None = typer.Option(None, "--model", "-m", help="Provider:model to score"),
    max_lines: int = typer.Option(300, help="Line budget per log excerpt"),
    limit: int | None = typer.Option(None, "--limit", "-n", help="Stop after this many fixtures"),
    labelled: bool = typer.Option(
        False, "--labelled", help="Only fixtures carrying a human label"
    ),
    cached_only: bool = typer.Option(
        False, "--cached-only", help="Score answered verdicts only; never call the model"
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-ask the model for every fixture; pays for the sweep again"
    ),
    report: Path | None = typer.Option(None, "--json", help="Write the full report here"),
) -> None:
    """Score the agent over the captured corpus.

    Reports two numbers. The citation verification rate needs no ground truth —
    it asks whether each quoted line is really at the coordinates the model gave
    — so it covers every fixture from the first day. Category accuracy needs a
    label and covers the labelled subset, which widens as you label.

    Verdicts are cached, so an interrupted sweep resumes for the price of what
    it never reached, and re-scoring costs nothing at all.
    """
    load_dotenv(".env.local")
    load_dotenv(".env")

    # pydantic-ai prints a multi-line setup banner the first time an agent is
    # constructed. Harmless once; in a 43-fixture sweep it lands in the middle
    # of the progress line and makes the run log unreadable. Set before the
    # import, which is when the flag is read.
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    # Deferred with the rest of the agent imports, so capture and labelling keep
    # working in an environment that never installed the model providers.
    from ci_triage.agent import MODEL
    from ci_triage.eval import find_fixtures, run_eval, write_report

    if cached_only and refresh:
        # One says never call the model, the other says call it for everything.
        # Silently honouring either would spend a day's quota or report on
        # nothing, so neither is a safe default to pick.
        console.print("[red]--cached-only and --refresh contradict each other[/red]")
        raise typer.Exit(1)

    paths = find_fixtures(FIXTURES, names=fixtures or (), labelled_only=labelled)
    missing = [p for p in paths if not (p / "run.json").exists()]
    if missing:
        console.print(f"[red]not a captured fixture:[/red] {', '.join(p.name for p in missing)}")
        raise typer.Exit(1)
    if not paths:
        console.print("[yellow]no fixtures to score[/yellow]")
        raise typer.Exit(1)
    if limit is not None:
        paths = paths[:limit]

    chosen = model or MODEL
    console.print(
        f"scoring [bold]{len(paths)}[/bold] fixture(s) with {chosen}"
        + ("  [dim](cache only — no model calls)[/dim]" if cached_only else "")
    )

    def on_start(i: int, path: Path) -> None:
        console.print(f"  [{i}/{len(paths)}] {path.name}", end=" ")

    def on_score(score) -> None:
        if score.result is None:
            console.print(f"[red]— {score.error}[/red]")
            return
        cites = f"{score.verified}/{score.cited} cites"
        mark = "" if score.correct is None else (" [green]ok[/green]" if score.correct else " [red]WRONG[/red]")
        cached = " [dim](cached)[/dim]" if score.cached else ""
        console.print(f"— {score.predicted.value} {cites}{mark}{cached}")

    result = run_eval(
        paths,
        model=chosen,
        max_lines=max_lines,
        cache=not refresh,
        cached_only=cached_only,
        on_start=on_start,
        on_score=on_score,
    )

    console.print()
    # `soft_wrap` so the fixture table survives being piped to a file or a
    # pager: rich otherwise hard-wraps at 80 columns when stdout is not a tty,
    # which folds every row onto two.
    console.print(result.render(), highlight=False, markup=False, soft_wrap=True)
    if report is not None:
        write_report(result, report)
        console.print(f"\n[green]wrote[/green] {report}")
    # A sweep that stopped on a quota is not a passing run: the numbers above
    # describe a subset, and a CI step or a shell loop should be able to see
    # that without parsing the report.
    if result.stopped_early:
        raise typer.Exit(2)


@app.command("ls")
def list_fixtures() -> None:
    """List captured runs and their labels."""
    if not FIXTURES.is_dir():
        console.print("[yellow]no fixtures/ directory yet[/yellow]")
        return
    table = Table("fixture", "repo", "conclusion", "label", "root cause")
    counts: dict[str, int] = {}
    for path in sorted(FIXTURES.iterdir()):
        if not (path / "run.json").exists():
            continue
        run, _, meta = load_fixture(path)
        lbl = meta.label if meta else None
        if lbl:
            counts[lbl.category.value] = counts.get(lbl.category.value, 0) + 1
        table.add_row(
            path.name,
            run.repository.full_name,
            run.conclusion or "—",
            f"[cyan]{lbl.category.value}[/cyan]" if lbl else "[yellow]—[/yellow]",
            (lbl.root_cause[:60] if lbl else ""),
        )
    console.print(table)
    if counts:
        spread = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        console.print(f"\nlabelled: {sum(counts.values())}   {spread}")


#: GitHub discards Actions logs after ~90 days. A run older than that yields a
#: fixture with no logs in it, which is worse than no fixture: it sits in the
#: golden set looking capturable.
LOG_RETENTION_DAYS = 90


@app.command()
def hunt(
    repos: list[str] = typer.Argument(..., help="One or more owner/repo"),
    limit: int = typer.Option(5, "--limit", "-n", help="Candidates per repo"),
    event: str = typer.Option(
        "pull_request", "--event", help="Trigger to filter on; empty string for any"
    ),
    max_age_days: int = typer.Option(
        LOG_RETENTION_DAYS, "--max-age-days", help="Skip runs whose logs have likely expired"
    ),
    capture: bool = typer.Option(
        False, "--fetch", help="Capture each candidate instead of only listing it"
    ),
) -> None:
    """Find failed runs worth capturing, skipping any already in fixtures/.

    `--fetch` captures each candidate as it is found. Capture is the only part
    of building the golden set that has a deadline on it: GitHub discards
    Actions logs after ~LOG_RETENTION_DAYS, so a run not captured this month
    cannot be captured at all, while labelling the result can wait indefinitely.
    That asymmetry is why this grinds unattended and records no label — it is
    meant to run wide and early, ahead of knowing which fixtures you want.
    """
    have = {p.name.split("__")[-1] for p in FIXTURES.iterdir()} if FIXTURES.is_dir() else set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    # `owner/repo#run_id` rather than the URL: `parse_run_ref` accepts both, and
    # only this one survives a narrow terminal intact enough to copy.
    table = Table("age", "workflow", "run", title="candidates")
    found = skipped = captured = failed = 0
    with GitHubClient(_token()) as client:
        for ref in repos:
            try:
                owner, repo = ref.split("/", 1)
            except ValueError:
                console.print(f"[red]expected owner/repo, got[/red] {ref}")
                continue
            # Same reasoning as the per-run handler below: a batch that walks
            # eighteen repos must survive one of them being renamed, private or
            # simply broken.
            try:
                runs = client.list_failed_runs(owner, repo, limit=limit, event=event or None)
            except (GitHubError, httpx.HTTPError) as exc:
                console.print(f"[red]{ref}: {exc}[/red]")
                continue
            for r in runs:
                if str(r.id) in have or r.created_at < cutoff:
                    skipped += 1
                    continue
                age = (datetime.now(timezone.utc) - r.created_at).days
                found += 1
                if not capture:
                    table.add_row(f"{age}d", (r.name or "—")[:24], f"{ref}#{r.id}")
                    continue

                # One bad run must not end the grind: a repo can 404 mid-list,
                # a log archive can 410, a blob download can stall past its
                # retries. Each of those costs one fixture, not the batch.
                try:
                    dest = save_fixture(client, owner, repo, r.id, root=FIXTURES)
                except (GitHubError, httpx.HTTPError) as exc:
                    failed += 1
                    console.print(f"[red]x[/red] {ref}#{r.id}: {exc}")
                    continue
                have.add(str(r.id))
                captured += 1
                console.print(f"[green]+[/green] {dest.name} ({age}d, {(r.name or '—')[:24]})")

    if not capture:
        console.print(table)
    console.print(
        f"\n{found} candidate(s), {skipped} skipped (already captured or logs expired)"
    )
    if capture:
        console.print(f"[green]{captured} captured[/green], {failed} failed")
        unlabelled = sum(
            1
            for p in FIXTURES.iterdir()
            if (p / "run.json").exists()
            and '"label": null' in (p / "meta.json").read_text()
        ) if FIXTURES.is_dir() else 0
        console.print(f"next: [bold]uv run ci-triage label <fixture>[/bold] — {unlabelled} unlabelled")
    elif found:
        console.print("next: [bold]uv run ci-triage hunt --fetch <repos>[/bold]")


@app.command()
def prune(
    fixture: str | None = typer.Argument(None, help="One fixture, or all when omitted"),
    apply: bool = typer.Option(False, "--apply", help="Actually delete; otherwise dry run"),
) -> None:
    """Drop the logs of jobs that did not fail.

    A run's archive contains every job, but only a failed job's log can be
    evidence for why the run is red — those are the only ones `get_logs` can
    reach, and the only ones a citation can point at. On the current fixture set
    that is the difference between ~330 MB committed to git and ~56 MB.

    Irreversible once a run passes GitHub's ~90-day log retention, so this is a
    dry run unless `--apply` is passed. Per-step subdirectories are kept for
    failed jobs, which is the only part a future tool could still want.
    """
    paths = [_resolve(fixture)] if fixture else sorted(
        p for p in FIXTURES.iterdir() if (p / "run.json").exists()
    )
    freed = kept = 0
    doomed: list[Path] = []
    for path in paths:
        logs = path / "logs"
        if not logs.is_dir():
            continue
        _, jobs, _ = load_fixture(path)
        keep: set[Path] = set()
        for job in jobs:
            if not job.failed:
                continue
            if (log := log_path_for_job(path, job)) is not None:
                keep.add(log)
                # The per-step directory shares the sanitised job name.
                if (d := logs / log.stem.split("_", 1)[-1]).is_dir():
                    keep.add(d)
        for entry in logs.iterdir():
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file()) \
                if entry.is_dir() else entry.stat().st_size
            if entry in keep:
                kept += size
            else:
                freed += size
                doomed.append(entry)

    verb = "deleting" if apply else "would delete"
    console.print(
        f"{verb} {len(doomed)} entr(ies), {freed / 1e6:,.0f} MB; "
        f"keeping {kept / 1e6:,.0f} MB of failed-job logs"
    )
    if not apply:
        console.print("re-run with [bold]--apply[/bold] to delete")
        return
    for entry in doomed:
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    console.print(f"[green]freed {freed / 1e6:,.0f} MB[/green]")


@app.command()
def backfill(
    fixture: str | None = typer.Argument(None, help="One fixture, or all when omitted"),
) -> None:
    """Re-fetch the pieces a fixture is missing.

    Fixtures captured by an earlier version of this tool are missing whatever it
    did not collect yet, and a capture that hit a transient error wrote a
    `*_unavailable.txt` instead. Both look identical on disk to a fixture that
    is simply complete, which is how `sweep__30005725094` sat in the golden set
    with no diff at all.

    Only the pieces that are *still* fetchable are repaired: logs expire after
    ~90 days, so a missing log is usually gone for good, while payloads, history
    and the commit diff have no such clock.
    """
    paths = [_resolve(fixture)] if fixture else sorted(
        p for p in FIXTURES.iterdir() if (p / "run.json").exists()
    )
    todo = [p for p in paths if not (p / "history.json").exists() or not (p / "diff.patch").exists()]
    if not todo:
        console.print("[green]every fixture is complete[/green]")
        return

    with GitHubClient(_token()) as client:
        for path in todo:
            run, _, _ = load_fixture(path)
            owner, repo = run.repository.full_name.split("/", 1)
            got: list[str] = []

            if not (path / "history.json").exists():
                try:
                    history = client.get_run_history(owner, repo, run)
                except (GitHubError, httpx.HTTPError) as exc:
                    console.print(f"[red]{path.name}: history: {exc}[/red]")
                else:
                    (path / "history.json").write_text(history.model_dump_json(indent=2))
                    flake = "both outcomes seen" if history.passed_and_failed_on_same_commit else "consistent"
                    got.append(f"history ({flake})")

            if not (path / "diff.patch").exists():
                try:
                    diff = client.get_diff(owner, repo, run.head_sha)
                except (GitHubError, httpx.HTTPError) as exc:
                    (path / "diff_unavailable.txt").write_text(str(exc))
                    console.print(f"[yellow]{path.name}: diff unavailable: {exc}[/yellow]")
                else:
                    (path / "diff.patch").write_text(diff)
                    (path / "diff_unavailable.txt").unlink(missing_ok=True)
                    got.append(f"diff ({len(diff.splitlines())} lines)")

            if got:
                console.print(f"[green]+[/green] {path.name} — {', '.join(got)}")


if __name__ == "__main__":
    app()
