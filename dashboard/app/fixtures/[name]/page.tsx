import Link from "next/link";

import { AskPanel } from "@/components/ask-panel";
import { LiveTriage } from "@/components/live-triage";
import { CategoryBadge, Citations, ErrorPanel, Panel, RouteBadge, SectionTitle } from "@/components/ui";
import { DEMO, demoNames, getFixture, getRecordings } from "@/lib/demo";
import { type FixtureDetail, shortModel } from "@/lib/types";

/** In replay mode every run in the snapshot is built ahead of time; live, none is. */
export function generateStaticParams() {
  return demoNames().map((name) => ({ name }));
}

export default async function FixturePage(props: PageProps<"/fixtures/[name]">) {
  const { name } = await props.params;
  let d: FixtureDetail;
  try {
    d = await getFixture(name);
  } catch (err) {
    return <ErrorPanel title={`Run ${name} could not be loaded`} detail={(err as Error).message} />;
  }
  const recordings = getRecordings()[name] ?? [];
  const jobs = d.failed_jobs.length;

  return (
    <div className="space-y-10">
      <header>
        <Link href="/" className="label text-[12px] text-muted hover:text-fg">
          ← All runs
        </Link>
        <div className="mt-3">
          <h1 className="font-display text-[34px] font-semibold leading-tight tracking-[0.01em] sm:text-[40px]">
            {d.repo}
            {d.workflow && <span className="font-medium text-muted"> · {d.workflow}</span>}
          </h1>
        </div>
        <p className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-[14px] text-muted">
          <span>
            {jobs} failed job{jobs === 1 ? "" : "s"}: {d.failed_jobs.join(", ")}
          </span>
          {d.branch && <span className="font-mono text-xs">{d.branch}</span>}
          <span className="font-mono text-xs">{d.head_sha.slice(0, 10)}</span>
          <a href={d.html_url} className="text-accent underline-offset-4 hover:underline" target="_blank" rel="noreferrer">
            Open the run on GitHub
          </a>
        </p>
      </header>

      {DEMO ? (
        <Panel title="Watch the agent triage this run">
          {recordings.length > 0 ? (
            <LiveTriage name={d.name} recordings={recordings} />
          ) : (
            <p className="max-w-[65ch] text-sm text-muted">
              No triage of this run was recorded for the demo. The verdicts below are from the last eval sweep; runs
              with a replay are marked on the <Link href="/" className="text-accent hover:underline">home page</Link>.
            </p>
          )}
        </Panel>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <Panel title="Live triage">
            <LiveTriage name={d.name} />
          </Panel>
          <div className="lg:sticky lg:top-4 lg:h-[calc(100vh-2rem)]">
            <Panel title="Ask about this incident">
              <div className="h-[28rem] lg:h-[calc(100vh-7rem)]">
                <AskPanel name={d.name} />
              </div>
            </Panel>
          </div>
        </div>
      )}

      <section>
        <SectionTitle>Eval sweeps</SectionTitle>
        <p className="mt-2 text-[14.5px] text-muted">
          What each model concluded about this run in its last scored sweep, next to the human label.
        </p>
        <div className="mt-4 grid gap-4 md:grid-cols-3">
          <div className="rounded-md border border-line bg-panel p-4">
            <p className="label text-[11.5px] text-faint">Human label</p>
            {d.label ? (
              <>
                <div className="mt-2">
                  <CategoryBadge category={d.label.category} />
                </div>
                <p className="mt-2 text-sm">{d.label.root_cause}</p>
              </>
            ) : (
              <p className="mt-2 text-sm text-muted">Not labelled yet.</p>
            )}
          </div>
          {Object.entries(d.scores).map(([model, s]) => (
            <div key={model} className="rounded-md border border-line bg-panel p-4">
              <p className="font-mono text-[12px] text-faint">{shortModel(model)}</p>
              {s.predicted ? (
                <>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <CategoryBadge category={s.predicted} />
                    <span className="text-[13px] tabular-nums">{Math.round((s.confidence ?? 0) * 100)}%</span>
                    <Citations verified={s.verified} cited={s.cited} />
                    <RouteBadge route={s.route} />
                  </div>
                  <p className="mt-2 line-clamp-3 text-[14px] leading-snug text-muted">{s.verdict?.summary}</p>
                </>
              ) : (
                <p className="mt-2 text-sm text-muted">{s.error ?? "No verdict."}</p>
              )}
            </div>
          ))}
        </div>
      </section>

      <details className="group rounded-md border border-line bg-panel">
        <summary className="cursor-pointer list-none px-4 py-3 text-sm">
          <span className="label text-[14px] font-semibold">The prompt</span>
          <span className="ml-2 text-muted">what the agent sees before its first tool call</span>
        </summary>
        <pre className="log border-t border-line bg-sunken p-4 text-muted">{d.overview}</pre>
      </details>
    </div>
  );
}
