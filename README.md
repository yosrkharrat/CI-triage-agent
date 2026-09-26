# CI triage agent

An agent that reads a failed GitHub Actions run and decides *why* it is red —
`flaky`, `regression`, `infra`, `dependency` or `unknown` — citing the log lines
that prove it. Low-confidence verdicts route to a human instead of being posted.

**Status: in progress.** Capture, the agent, its evidence checking and the eval
harness work end to end against 43 captured runs, and a webhook service runs the
same agent on live runs. The dashboard and the sandbox are not built yet.

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
ci-triage eval                score the agent over the whole corpus
ci-triage serve               triage live runs as GitHub reports them
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
  keyed on a hash of everything the model was shown — the prompt, and the
  reduced logs, diff and history under it. Editing the anchors or the failure
  fingerprint invalidates the cache as surely as editing the prompt does, so a
  score can never come from answers to a question no longer being asked.
  Tightening the evidence check or raising the confidence threshold re-scores
  every run already paid for, for free.

## Scoring it

```bash
uv run ci-triage eval                # every captured run
uv run ci-triage eval --labelled     # only the ones carrying ground truth
uv run ci-triage eval --cached-only  # re-score what is already answered, free
```

Two metrics, and only one of them needs a human:

| metric | needs labels? | covers |
| --- | --- | --- |
| citation verification rate | no | all 43 fixtures |
| category accuracy | yes | the labelled subset |

That asymmetry is why the harness was not gated on labelling. Whether a quoted
line is really at the coordinates the model gave is a question `verify_evidence`
answers against the log, with no human in it anywhere — so the citation number
covered the whole corpus from the first sweep, while accuracy waited on hand
labelling and grows as it arrives.

It is also the more particular of the two. Category accuracy is table stakes for
anything that classifies. A number for how often a model invents the evidence
under a verdict, produced by a harness that proves it line by line, is not.

Reported beside them, and needing no labels either, is the number that is a
liability rather than a metric: **auto-posted verdicts carrying a citation that
fails verification**. Those are the comments that would have landed on someone's
pull request quoting a line that is not in the log.

Sweeps are resumable and re-scoring is free. Only the model's answer is cached,
keyed on a hash of the prompt, so a sweep stopped by a daily quota — the normal
way a free-tier sweep ends — resumes for the price of what it never reached, and
tightening the evidence check or moving the confidence threshold re-scores every
verdict already paid for without spending anything. The threshold sweep in the
report is re-derived from stored verdicts for exactly that reason.

### What this corpus can and cannot teach

`flaky` is defined here as one commit producing both outcomes, which puts a hard
floor under how many flaky fixtures can exist: a run only qualifies if its
history records a pass *and* a fail on the same SHA. Across the 43 captured
runs, two do — `pandas__35496694897` and `pandas__35499323117`. Six more have a
second run on that commit and none of them passed: four were `action_required`,
one was `cancelled`, one failed again.

So the flaky class stays small however long the labelling runs, and its accuracy
will be read off a handful of fixtures. That is a fact about how CI reruns get
recorded, not about the labelling, and the honest response is to report the
per-class counts beside the accuracy rather than to hunt for flakes until the
class looks balanced.

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

Captured runs land in `fixtures/`. The logs are gitignored — roughly 370 MB
across 43 runs, all re-fetchable from their run URLs. Each fixture's `meta.json`
is not: a run URL regenerates its logs, and nothing regenerates a human reading
them and deciding what broke.

## Running it on live runs

`ci-triage serve` is a small FastAPI service for a GitHub App's webhook. It
takes failed `workflow_run` deliveries, captures each run to disk exactly as
`fetch` would, triages the capture, and decides what to do with the verdict:

| outcome | when |
| --- | --- |
| `posted` | auto-post route **and** every citation verified; comment on the PR |
| `dry_run` | the same, while posting is switched off (the default) |
| `awaiting_review` | low confidence, `unknown`, a proposed fix, or any citation that failed |
| `no_pr` | postable, but the commit has no open pull request |

The rule that matters is the second half of the first row. The eval reports
auto-posts carrying a failed citation as a liability; the service does not
count them, it refuses them. A comment is posted only when every quote in it
was found where it claims to be, and it edits its own earlier comment on a
re-run rather than stacking another.

The handler only checks the signature and queues the run in SQLite, keyed on
`(repo, run, attempt)`, so a redelivered webhook changes nothing. A run
interrupted by a restart is picked up again on the next start. A run that hits
the model's quota waits and retries instead of failing. Every verdict, posted
or not, is at `GET /runs` along with the comment it would have posted.

```bash
echo 'GITHUB_WEBHOOK_SECRET=...' >> .env.local     # required; unsigned requests get 401
echo 'GITHUB_APP_ID=...' >> .env.local             # or rely on GITHUB_TOKEN
echo 'GITHUB_APP_PRIVATE_KEY_FILE=app.pem' >> .env.local
uv run ci-triage serve                             # dry run; add CI_TRIAGE_POST_COMMENTS=1 to post
```

The App needs **Actions: read**, **Contents: read** and **Pull requests:
write**, and a subscription to the **Workflow run** event. The service binds to
localhost; expose it with a tunnel (`cloudflared`, `ngrok`) rather than a
public interface, since `/runs` has no authentication.

## Built with

Python 3.12 · [Pydantic AI](https://ai.pydantic.dev) for the agent loop and tool
definitions · FastAPI and SQLite for the webhook service · Pydantic v2 at every
boundary · [Logfire](https://logfire.dev) for tracing · httpx · Typer · pytest ·
uv.

## Not done yet

- Labelling the rest of the corpus. This is the slow part and only a human can
  do it, which is why the harness reports what it can without one.
- A human approval action for `awaiting_review` verdicts; the queue exists,
  the button does not
- Sandbox tool to reproduce a failure and test a patch
- Dashboard streaming a triage run as it happens
