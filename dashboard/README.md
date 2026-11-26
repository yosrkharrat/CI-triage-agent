# ci-triage dashboard

Watch the agent triage a failed run as it happens, ask a second agent about the
incident, and decide the verdicts the service would not post on its own.

```bash
# in the repo root's .env.local, next to GROQ_API_KEY:
#   CI_TRIAGE_REVIEW_TOKEN=<anything long and random>
#   GITHUB_WEBHOOK_SECRET=<anything, for local use>
uv run ci-triage serve          # from the repo root
cd dashboard
pnpm install
pnpm dev                        # http://localhost:3000
```

The service and the dashboard read the same root `.env.local`, so the token is
set once. `dashboard/.env.local` is only for overriding a key for the
dashboard; an empty value there is ignored rather than shadowing the root one.

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

## Replay mode: the public demo

A public deployment can't run triages on demand. Each one spends about 19k
tokens of a free quota that covers about ten a day, and the corpus is about
400 MB of logs. So the demo replays real triages that were recorded once:

```bash
uv run ci-triage serve                          # from the repo root
cd dashboard
pnpm demo:record                                # snapshot the corpus, spends no tokens
pnpm demo:record <run> [<run> ...]              # + record each run on both models
pnpm demo:record --model groq:openai/gpt-oss-20b <run>
pnpm demo:record --force <run>                  # record that run again
pnpm build:demo && pnpm start:demo              # or pnpm dev:demo
```

The recorder saves every chunk the agent streamed, with the time it arrived, in
`public/demo/recordings/`. It saves the API responses for the corpus and each
run's detail in `demo/snapshot.json`. It skips a run that ends without a
verdict (for example, when the quota is spent), so the demo never replays a
failure by accident. Only corpus runs go in the snapshot, because the service's
live captures may come from private repos.

With `CI_TRIAGE_DEMO=replay`:

- Every page is prerendered from the snapshot.
- The browser replays each recording through `ReplayTransport` (`lib/replay.ts`),
  at the pace the run streamed. Pauses are capped at 1.2 s.
- The ask and triage routes return 404.
- The review queue becomes **Fix chains**: recorded delegation chains, each
  shown on its run page from the failed run to the fix's outcome, labelled as
  a recording. They come from `demo/chains.json`, which
  `scripts/export_demo_chains.py` writes from the database a chain ran on (from
  the repo root):

  ```bash
  uv run python scripts/export_demo_chains.py .ci-triage/e2e-plan.db 1 6 \
    --model openrouter:google/gemini-3.5-flash
  ```

  It exports only chains that have ended, on a repository listed as public in
  the script, and leaves out the App installation and webhook delivery ids.

No model is called and no key is needed, so the demo can be hosted for free as a
static site.
