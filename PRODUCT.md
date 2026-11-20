# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Engineers deciding whether to trust ci-triage and add its GitHub Action to their repository. They arrive at the public demo (ci-triage.vercel.app) from the GitHub README or the author's portfolio; hiring managers who are engineers read it the same way. They know CI, GitHub Actions and LLM agents, and they are skeptical of agents that sound confident.

The author also runs the same dashboard locally against the Python service, to watch live triages and approve or reject held verdicts.

## Product Purpose

When a GitHub Actions run fails, ci-triage reads the failed jobs' logs, the PR diff and the run history, and names the cause: flaky, regression, infra, dependency or unknown. It cites the log lines that prove it. The site has to let an engineer see, within a minute, what the agent does and why its answers can be trusted, and then install it.

Success: a visitor understands the mechanism, watches a recorded triage, and copies the workflow file.

## Positioning

Every log line the agent quotes is checked mechanically against the real log, at the line numbers it cites. A model can write a confident wrong verdict; it cannot fake a quote at coordinates that don't contain it. A verdict resting on a quote the log doesn't contain is never posted. The share of quotes found is a hallucination metric that needs no labels.

## Operating Context

- Distribution: a GitHub Action (one workflow file, `workflow_run` trigger) and a FastAPI webhook service for a GitHub App with a human review queue.
- Providers: no bundled model; Groq, Anthropic, OpenAI or Google keys.
- Routing differs by entry point. The Action drops a suggested fix from what it posts and posts the rest when confident (≥ 0.70) and every quote verifies. The service holds any verdict that proposes a fix for a person.
- The public demo replays recorded triages and calls no model; the local dashboard streams live triages.

## Capabilities and Constraints

- Corpus: 43 real failed runs from open-source repos (airflow, black, home-assistant, fastapi, httpx, langchain, pandas, poetry, polars, prefect, pydantic-ai), about 370 MB, not shipped to the demo.
- Category accuracy is not scored yet: 3 of 43 runs carry a human label. Labels are written by a person only.
- Free-tier Groq quota allows about ten triages a day, so recordings are added a few at a time.

## Brand Commitments

- Name: "ci-triage", lowercase. No logo or visual brand exists; the identity is open.
- One identity for the public demo and the local dashboard: they share every component.

## Evidence on Hand

- Eval reports in `reports/eval-*.json`: gpt-oss-120b 92% of citations verified (44/48), gpt-oss-20b 52% (15/29), and 2 of 20b's confident verdicts would have posted an invented quote.
- Recorded triages in `dashboard/public/demo/recordings/`, with the log context around each quote and the exact comment the Action would post.
- A real run on a demo repo: https://github.com/yosrkharrat/ci-triage-demo/pull/1
- A demo GIF in `assets/action-demo.gif`.
- No users, testimonials or adoption numbers exist; none may be implied.

## Product Principles

- Show the proof, not the claim: every verdict is presented with the log lines it rests on, in place.
- Honest about limits: unmeasured things are labelled unmeasured.
- Engineer-grade detail one step away: plain first, technical underneath, never hidden.
- The demo never spends anyone's quota and never pretends a recording is live.
