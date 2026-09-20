# CI triage agent

An agent that reads a failed GitHub Actions run and decides *why* it is red —
`flaky`, `regression`, `infra`, `dependency` or `unknown` — citing the log lines
that prove it. Low-confidence verdicts route to a human instead of being posted.

**Status: in progress.** The capture layer, the agent and its evidence checking
work end to end against captured runs. The eval harness, the webhook service and
the dashboard are not built yet.

## The idea it is built around

A model can write a fluent, confident, wrong verdict. It cannot fake a quote at
coordinates that do not contain it.

So every citation carries a log path, a line range and the quoted text, and
every one is looked up in the source log after the run. That turns "is this
reasoning any good?" into a mechanical question, and it gives a second quality
axis — the share of citations that survive verification — next to category
accuracy.

It catches real failures. Running `sweep__30005314760` on `gpt-oss-20b`:

```
category:   regression  (confidence 0.95)   <- agrees with the human label
evidence:
  [BAD ] 3_build (macos-latest...).txt:978-990
          quote does not appear within the cited lines
```

The right answer, resting on a citation the model invented.

## How it works

```
ci-triage hunt <owner/repo>   find failed runs worth capturing
ci-triage fetch <run-url>     capture run, jobs, logs, diff and history
ci-triage inspect <fixture>   show exactly what the agent will be shown
ci-triage triage <fixture>    run the agent, print and check its verdict
ci-triage label <fixture>     record human ground truth for the eval set
```

A captured run is the unit of work. The agent's three tools — `get_logs`,
`get_diff`, `test_history` — read that capture and nothing else, so a triage run
is offline, costs no GitHub API calls, and produces the same prompt next month
as today. An eval score that moves means the agent moved.

Some decisions worth naming:

- **Routing is code, not a model field.** The model reports a category and a
  confidence; `route()` decides whether that may be auto-posted. The threshold
  can be swept in an eval without touching the prompt.
- **One definition of each category.** `CATEGORY_GUIDE` is handed to the model
  as part of the output schema *and* is the guide a human follows when
  labelling. If those two drifted, eval scores would stop meaning anything.
- **Matrix failures are collapsed.** One run here has 37 failed jobs saying the
  same thing 37 ways. They are fingerprinted on masked failure lines and shown
  as one representative — and the count is reported, because "37 jobs failing
  identically" is evidence of one cause rather than of a broken environment.
- **Verdicts are cached, routing is not.** Only the model's answer is stored,
  keyed on a hash of the prompt. Tightening the evidence check or raising the
  confidence threshold re-scores every run already paid for, for free.

## Running it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
echo 'GITHUB_TOKEN=...' >> .env.local
echo 'GROQ_API_KEY=...'  >> .env.local

uv run ci-triage fetch https://github.com/owner/repo/actions/runs/123456
uv run ci-triage triage repo__123456
```

The default model is `groq:openai/gpt-oss-120b`, on Groq's free tier. Pass
`--model` for any provider pydantic-ai supports — `anthropic:claude-opus-5`,
`google-gla:gemini-2.0-flash`, `ollama:qwen2.5:14b`. Running below the frontier
is deliberate: a weaker model is what makes the citation check measure anything.

Captured runs land in `fixtures/` and are gitignored — roughly 370 MB of logs
across 43 runs, all re-fetchable from their run URLs.

## Built with

Python 3.12 · [Pydantic AI](https://ai.pydantic.dev) for the agent loop and tool
definitions · Pydantic v2 at every boundary · [Logfire](https://logfire.dev) for
tracing · httpx · Typer · pytest · uv.

## Not done yet

- Eval harness over the labelled corpus — category accuracy and citation
  verification rate, scored per prompt revision
- Webhook service so this runs on live PRs rather than captures
- Sandbox tool to reproduce a failure and test a patch
- Dashboard streaming a triage run as it happens
