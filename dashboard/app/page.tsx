import Link from "next/link";

import { CopyButton } from "@/components/copy-button";
import { Pipeline } from "@/components/pipeline";
import { PrComment } from "@/components/pr-comment";
import { RunsTable } from "@/components/runs-table";
import { ErrorPanel, PLATE, PLATE_OUTLINE, SectionTitle } from "@/components/ui";
import { DEMO, getFixtures, getRecordings } from "@/lib/demo";
import type { FixtureRow } from "@/lib/types";

const REPO = "https://github.com/yosrkharrat/CI-triage-agent";

const WORKFLOW = `name: CI triage
on:
  workflow_run:
    workflows: [CI]          # the workflow(s) to watch
    types: [completed]

permissions:
  actions: read              # read the failed run's logs
  contents: read             # read the diff under test
  pull-requests: write       # comment on the PR

jobs:
  triage:
    if: github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    steps:
      - uses: yosrkharrat/CI-triage-agent@v1
        with:
          groq-api-key: \${{ secrets.GROQ_API_KEY }}
`;

export default async function HomePage() {
  let rows: FixtureRow[];
  try {
    rows = await getFixtures();
  } catch (err) {
    return <ErrorPanel title="The corpus could not be loaded" detail={(err as Error).message} />;
  }
  const recorded = getRecordings();
  const models = [...new Set(rows.flatMap((r) => Object.keys(r.verdicts)))].sort();
  const labelled = rows.filter((r) => r.label).length;
  // Recorded runs first: in the demo they are the ones a visitor can watch.
  const ordered = DEMO ? [...rows].sort((a, b) => Number(!recorded[a.name]) - Number(!recorded[b.name])) : rows;
  const firstReplay = ordered.find((r) => recorded[r.name]);
  const comment = Object.entries(recorded)
    .flatMap(([name, rs]) => rs.map((r) => ({ name, comment: r.comment })))
    .find((r) => r.comment);

  return (
    <div className="space-y-20">
      {DEMO && (
        <>
          <section>
            <div className="grid gap-x-12 gap-y-6 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)] lg:items-end">
              <div>
                <h1 className="font-display text-[56px] font-bold uppercase leading-[0.92] tracking-[0.01em] sm:text-[84px]">
                  Why is CI red?
                </h1>
                <p className="mt-4 max-w-[34ch] font-display text-[26px] font-medium leading-tight text-muted sm:text-[30px]">
                  An agent answers, and every log line it quotes is checked against the real log.
                </p>
              </div>
              <div>
                <p className="max-w-[60ch] text-[15.5px] leading-relaxed text-muted">
                  It reads a failed GitHub Actions run (logs, diff, test history) and names the cause. A model can write
                  a confident wrong answer, but it cannot fake a quote at line numbers that don&apos;t contain it, so
                  nothing reaches the pull request on a quote the log doesn&apos;t hold. This demo replays recorded
                  triages; no model is called.
                </p>
                <div className="mt-7 flex flex-wrap items-center gap-3">
                  {firstReplay && (
                    <Link href={`/fixtures/${firstReplay.name}`} className={PLATE}>
                      Replay a triage <Arrow />
                    </Link>
                  )}
                  <Link href="/runs" className={PLATE_OUTLINE}>
                    See a fix chain
                  </Link>
                  <a href="#install" className={PLATE_OUTLINE}>
                    Add it to a repo
                  </a>
                </div>
              </div>
            </div>

            <figure className="mt-12" aria-label="The line every failed run rides: five stations, then posted, held or blocked">
              <Pipeline showDetail />
            </figure>
          </section>

          <section className="grid gap-10 lg:grid-cols-2">
            <div>
              <SectionTitle>What lands on the pull request</SectionTitle>
              <p className="mt-2 max-w-[60ch] text-[14.5px] leading-relaxed text-muted">
                The comment the Action posts for the recorded run, from the exact markdown it sends. Every quote is
                shown with its file and lines, so a reviewer can check it in one click.
              </p>
              <div className="mt-5">
                {comment?.comment ? (
                  <PrComment
                    body={comment.comment}
                    note="The suggested fix is left out of the comment; it waits for a person, in the job summary."
                  />
                ) : (
                  <p className="text-sm text-muted">No recorded run has a comment yet.</p>
                )}
              </div>
            </div>
            <div>
              <SectionTitle>Results on {rows.length} real failures</SectionTitle>
              <p className="mt-2 max-w-[60ch] text-[14.5px] leading-relaxed text-muted">
                Captured from open-source repos such as pandas, fastapi and langchain. A quote that is not at the lines
                it cites was invented, so the share of quotes found is a hallucination rate that needs no labels.
                Category accuracy is not scored yet: {labelled} of {rows.length} runs carry a human label.
              </p>
              <Results models={models} rows={rows} />
            </div>
          </section>

          <section id="install" className="scroll-mt-8">
            <SectionTitle>Add it to a repo</SectionTitle>
            <p className="mt-2 max-w-[70ch] text-[14.5px] leading-relaxed text-muted">
              One workflow file, with your CI workflow&apos;s{" "}
              <code className="font-mono text-[13px] text-fg">name:</code> in{" "}
              <code className="font-mono text-[13px] text-fg">workflows:</code>. It brings no model of its own: pass a
              key for Groq, Anthropic, OpenAI or Google as a repo secret. It never checks out or runs the PR&apos;s
              code, so it is safe on pull requests from forks.
            </p>
            <figure className="mt-5 overflow-hidden rounded-md border border-line bg-sunken">
              <figcaption className="flex items-center justify-between border-b border-line px-4 py-2">
                <span className="font-mono text-[12px] text-muted">.github/workflows/ci-triage.yml</span>
                <CopyButton text={WORKFLOW} />
              </figcaption>
              <pre className="log p-4 text-fg">{WORKFLOW}</pre>
            </figure>
            <p className="mt-3 text-[14px] text-muted">
              Inputs, outputs and the CLI are in the{" "}
              <a
                href={`${REPO}#add-it-to-your-repo-in-two-minutes`}
                className="text-accent underline-offset-4 hover:underline"
              >
                README
              </a>
              .
            </p>
          </section>
        </>
      )}

      <section>
        {DEMO ? (
          <>
            <SectionTitle>All runs</SectionTitle>
            <p className="mt-2 max-w-[70ch] text-[14.5px] leading-relaxed text-muted">
              Each model&apos;s last verdict on every run, coloured by the line it named. Runs marked Replay can be
              watched step by step.
            </p>
          </>
        ) : (
          <>
            <h1 className="font-display text-[34px] font-semibold uppercase tracking-[0.02em]">Corpus</h1>
            <p className="mt-1 max-w-2xl text-[14.5px] text-muted">
              {rows.length} captured runs, {labelled} labelled. Verdicts are each model&apos;s last scored sweep; open a
              run to watch the agent triage it live.
            </p>
            {models.length > 0 && <Results models={models} rows={rows} />}
          </>
        )}
        <div className="mt-6">
          <RunsTable rows={ordered} models={models} recorded={recorded} demo={DEMO} />
        </div>
      </section>
    </div>
  );
}

function Arrow() {
  return (
    <svg viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path d="M3 8h9.5M8.5 4l4 4-4 4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Each model's citation record across the corpus, set like a departures board. */
function Results({ models, rows }: { models: string[]; rows: FixtureRow[] }) {
  return (
    <div className="mt-5 divide-y divide-line rounded-md border border-line">
      {models.map((model) => {
        const answered = rows.map((r) => r.verdicts[model]).filter(Boolean);
        const cited = answered.reduce((n, v) => n + (v.cited ?? 0), 0);
        const verified = answered.reduce((n, v) => n + (v.verified ?? 0), 0);
        const unsound = answered.filter((v) => v.route === "auto_post" && (v.verified ?? 0) < (v.cited ?? 0)).length;
        const rate = cited ? verified / cited : null;
        return (
          <div key={model} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-6 gap-y-2 px-4 py-4">
            <div className="min-w-0">
              <p className="truncate font-mono text-[12.5px] text-fg">{model}</p>
              <p className="mt-1 text-[13px] text-muted">
                {answered.length} run{answered.length === 1 ? "" : "s"} answered · {verified} of {cited} quotes found
                {unsound > 0 && (
                  <span className="text-bad">
                    {" "}
                    · {unsound} would have posted an invented quote, blocked by the check
                  </span>
                )}
              </p>
              {rate !== null && (
                <span className="mt-2.5 block h-[5px] w-full max-w-72 overflow-hidden rounded-full bg-line" aria-hidden>
                  <span
                    className={`block h-full rounded-full ${rate >= 0.8 ? "bg-fg" : "bg-bad"}`}
                    style={{ width: `${rate * 100}%` }}
                  />
                </span>
              )}
            </div>
            <p
              className={`font-display text-[40px] font-semibold leading-none tabular-nums ${rate !== null && rate < 0.8 ? "text-bad" : ""}`}
            >
              {rate === null ? "—" : Math.round(rate * 100)}
              {rate !== null && <span className="text-[20px] text-muted">%</span>}
            </p>
          </div>
        );
      })}
    </div>
  );
}
