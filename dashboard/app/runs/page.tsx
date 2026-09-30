import Link from "next/link";

import { CategoryBadge, ErrorPanel, StatusBadge } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import type { RunRecord } from "@/lib/types";

export default async function RunsPage() {
  let runs: RunRecord[];
  try {
    runs = await agentJson<RunRecord[]>("/runs?limit=200");
  } catch (err) {
    return <ErrorPanel title="The service's runs could not be loaded" detail={(err as AgentError).message} />;
  }
  const waiting = runs.filter((r) => r.status === "awaiting_review");
  const rest = runs.filter((r) => r.status !== "awaiting_review");

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Review queue</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Live runs the webhook service triaged. A verdict waits here when its confidence is low, it proposes a
          code change, or a citation failed verification.
        </p>
      </div>
      <RunTable title={`Waiting for a human (${waiting.length})`} runs={waiting} empty="Nothing waiting." />
      <RunTable title="Everything else" runs={rest} empty="No live runs yet. Point a GitHub App's webhook at the service." />
    </div>
  );
}

function RunTable({ title, runs, empty }: { title: string; runs: RunRecord[]; empty: string }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      {runs.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line p-4 text-sm text-muted">{empty}</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-line bg-panel">
          <table className="w-full text-sm">
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-line last:border-0 hover:bg-sunken">
                  <td className="px-4 py-2">
                    <Link href={`/runs/${r.id}`} className="text-accent hover:underline">
                      {r.repo} <span className="whitespace-nowrap">#{r.run_id}</span>
                    </Link>
                    <div className="mt-1 flex flex-wrap items-center gap-2 sm:hidden">
                      <StatusBadge status={r.status} />
                      {r.verdict && <CategoryBadge category={r.verdict.category} />}
                    </div>
                  </td>
                  <td className="hidden px-4 py-2 sm:table-cell">
                    <StatusBadge status={r.status} />
                  </td>
                  <td className="hidden px-4 py-2 sm:table-cell">
                    {r.verdict && <CategoryBadge category={r.verdict.category} />}
                  </td>
                  <td className="hidden max-w-md truncate px-4 py-2 text-xs text-muted md:table-cell">
                    {r.reason ?? r.error}
                  </td>
                  <td className="hidden whitespace-nowrap px-4 py-2 text-xs text-muted md:table-cell">
                    {new Date(r.received_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
