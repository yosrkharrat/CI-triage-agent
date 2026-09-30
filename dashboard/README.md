# ci-triage dashboard

Watch the agent triage a failed run as it happens, ask a second agent about the
incident, and decide the verdicts the service would not post on its own.

```bash
uv run ci-triage serve          # from the repo root; needs CI_TRIAGE_REVIEW_TOKEN
cd dashboard
cp .env.example .env.local      # same CI_TRIAGE_REVIEW_TOKEN, plus GROQ_API_KEY
pnpm install
pnpm dev                        # http://localhost:3000
```

| page | what it shows |
| --- | --- |
| `/` | the corpus, with each model's last scored verdict and its citation rate |
| `/fixtures/[name]` | a live triage, streamed tool call by tool call, and the "ask" agent |
| `/runs` | the webhook service's runs, with the ones waiting for a human first |
| `/runs/[id]` | one verdict, each citation re-checked, and approve / reject |

The live triage is the Python agent, streamed in the AI SDK's UI message
protocol by pydantic-ai and relayed untouched by `app/api/fixtures/[name]/triage`.
The ask agent lives in `app/api/fixtures/[name]/ask`: Zod-typed tools here,
served by the Python agent's own tools there, so both read the same evidence.

The review token stays on the Next.js server. The browser only ever talks to
this app.
