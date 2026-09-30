import Link from "next/link";

import { CategoryBadge, Citations, ErrorPanel } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import { type FixtureRow, shortModel } from "@/lib/types";

export default async function CorpusPage() {
  let rows: FixtureRow[];
  try {
    rows = await agentJson<FixtureRow[]>("/api/fixtures");
  } catch (err) {
    const e = err as AgentError;
    return <ErrorPanel title="The corpus could not be loaded" detail={e.message} />;
  }

  const models = [...new Set(rows.flatMap((r) => Object.keys(r.verdicts)))].sort();
  const labelled = rows.filter((r) => r.label).length;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Corpus</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          {rows.length} captured runs, {labelled} labelled. Verdicts are each model&apos;s last scored sweep;
          open a run to watch the agent triage it live.
        </p>
      </div>

      {models.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2">
          {models.map((m) => (
            <ModelSummary key={m} model={m} rows={rows} />
          ))}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-line bg-panel">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th className="px-4 py-2 font-medium">Run</th>
              <th className="px-4 py-2 font-medium">Failed jobs</th>
              <th className="px-4 py-2 font-medium">Label</th>
              {models.map((m) => (
                <th key={m} className="px-4 py-2 font-medium">
                  {shortModel(m)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name} className="border-b border-line last:border-0 hover:bg-sunken">
                <td className="px-4 py-2">
                  <Link href={`/fixtures/${r.name}`} className="font-mono text-[13px] text-accent hover:underline">
                    {r.name}
                  </Link>
                  <div className="text-xs text-muted">
                    {r.repo}
                    {r.workflow ? ` · ${r.workflow}` : ""}
                    {r.source === "live" ? " · live capture" : ""}
                  </div>
                </td>
                <td className="px-4 py-2 font-mono text-xs">{r.failed_jobs}</td>
                <td className="px-4 py-2">
                  <CategoryBadge category={r.label} />
                </td>
                {models.map((m) => {
                  const v = r.verdicts[m];
                  return (
                    <td key={m} className="px-4 py-2">
                      {v ? (
                        <span className="inline-flex items-center gap-2">
                          <CategoryBadge category={v.category} dim={Boolean(r.label && r.label !== v.category)} />
                          <Citations verified={v.verified} cited={v.cited} />
                        </span>
                      ) : (
                        <span className="text-xs text-muted">not reached</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ModelSummary({ model, rows }: { model: string; rows: FixtureRow[] }) {
  const answered = rows.map((r) => r.verdicts[model]).filter(Boolean);
  const cited = answered.reduce((n, v) => n + (v.cited ?? 0), 0);
  const verified = answered.reduce((n, v) => n + (v.verified ?? 0), 0);
  const unsound = answered.filter((v) => v.route === "auto_post" && (v.verified ?? 0) < (v.cited ?? 0)).length;
  const rate = cited ? Math.round((verified / cited) * 100) : null;
  return (
    <div className="rounded-lg border border-line bg-panel p-4">
      <p className="font-mono text-xs text-muted">{model}</p>
      <div className="mt-3 flex items-end gap-8">
        <div>
          <p className="text-2xl font-semibold tabular-nums">{rate === null ? "—" : `${rate}%`}</p>
          <p className="text-xs text-muted">
            citations verified ({verified}/{cited})
          </p>
        </div>
        <div>
          <p className="text-2xl font-semibold tabular-nums">{answered.length}</p>
          <p className="text-xs text-muted">runs answered</p>
        </div>
        <div>
          <p className={`text-2xl font-semibold tabular-nums ${unsound ? "text-bad" : ""}`}>{unsound}</p>
          <p className="text-xs text-muted">auto-posts on an invented quote</p>
        </div>
      </div>
    </div>
  );
}
