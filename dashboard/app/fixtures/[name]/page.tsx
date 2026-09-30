import Link from "next/link";

import { AskPanel } from "@/components/ask-panel";
import { LiveTriage } from "@/components/live-triage";
import { CategoryBadge, Citations, ErrorPanel, Panel, RouteBadge } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import { type FixtureDetail, shortModel } from "@/lib/types";

export default async function FixturePage(props: PageProps<"/fixtures/[name]">) {
  const { name } = await props.params;
  let d: FixtureDetail;
  try {
    d = await agentJson<FixtureDetail>(`/api/fixtures/${encodeURIComponent(name)}`);
  } catch (err) {
    const e = err as AgentError;
    return <ErrorPanel title={`Run ${name} could not be loaded`} detail={e.message} />;
  }

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-xs text-muted hover:text-fg">
          ← Corpus
        </Link>
        <h1 className="mt-1 font-mono text-lg font-semibold">{d.name}</h1>
        <p className="mt-1 text-sm text-muted">
          <a href={d.html_url} className="text-accent hover:underline" target="_blank" rel="noreferrer">
            {d.repo}
          </a>
          {d.workflow ? ` · ${d.workflow}` : ""}
          {d.branch ? ` · ${d.branch}` : ""} · <span className="font-mono">{d.head_sha.slice(0, 10)}</span> ·{" "}
          {d.failed_jobs.length} failed job{d.failed_jobs.length === 1 ? "" : "s"}
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        <div className="rounded-lg border border-line bg-panel p-4">
          <p className="text-xs text-muted">Human label</p>
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
          <div key={model} className="rounded-lg border border-line bg-panel p-4">
            <p className="font-mono text-xs text-muted">{shortModel(model)} · last sweep</p>
            {s.predicted ? (
              <>
                <div className="mt-2 flex items-center gap-2">
                  <CategoryBadge category={s.predicted} />
                  <span className="font-mono text-xs tabular-nums">{s.confidence?.toFixed(2)}</span>
                  <Citations verified={s.verified} cited={s.cited} />
                  <RouteBadge route={s.route} />
                </div>
                <p className="mt-2 line-clamp-3 text-sm">{s.verdict?.summary}</p>
              </>
            ) : (
              <p className="mt-2 text-sm text-muted">{s.error ?? "No verdict."}</p>
            )}
          </div>
        ))}
      </div>

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

      <details className="rounded-lg border border-line bg-panel">
        <summary className="cursor-pointer px-4 py-2.5 text-sm font-semibold">What the agent is shown first</summary>
        <pre className="log border-t border-line bg-sunken p-4">{d.overview}</pre>
      </details>
    </div>
  );
}
