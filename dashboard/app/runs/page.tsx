import Link from "next/link";

import { CategoryBadge, DelegationBadge, ErrorPanel, StatusBadge } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import { DEMO } from "@/lib/demo";
import { type Delegation, OPEN_DELEGATION, type RunRecord } from "@/lib/types";

export default async function RunsPage() {
  if (DEMO) {
    return (
      <ErrorPanel
        title="The review queue needs the webhook service"
        detail="This demo replays recorded triages and runs no service. Run the dashboard locally against ci-triage serve to review live runs."
      />
    );
  }
  let runs: RunRecord[];
  let delegations: Delegation[];
  try {
    [runs, delegations] = await Promise.all([
      agentJson<RunRecord[]>("/runs?limit=200"),
      // An older service has no delegations; the queue still works without them.
      agentJson<Delegation[]>("/delegations?limit=100").catch(() => []),
    ]);
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
      {delegations.length > 0 && <DelegationTable delegations={delegations} />}
      <RunTable title="Everything else" runs={rest} empty="No live runs yet. Point a GitHub App's webhook at the service." />
    </div>
  );
}

function DelegationTable({ delegations }: { delegations: Delegation[] }) {
  const open = delegations.filter((d) => OPEN_DELEGATION.includes(d.status));
  const closed = delegations.filter((d) => !OPEN_DELEGATION.includes(d.status));
  return (
    <section>
      <h2 className="mb-1 text-sm font-semibold">Fixes handed to Claude ({open.length} open)</h2>
      <p className="mb-2 max-w-2xl text-xs text-muted">
        A proposed one waits for a person: approving it sends the brief to Claude and approves the diagnosis with
        it. ci-triage reports fixed only when the failing jobs pass, nothing else fails, and the diff guard passes.
      </p>
      <div className="overflow-x-auto rounded-md border border-line bg-panel">
        <table className="w-full text-sm">
          <tbody>
            {[...open, ...closed].map((d) => (
              <tr key={d.id} className="border-b border-line last:border-0 hover:bg-sunken">
                <td className="px-4 py-2">
                  <Link href={`/runs/${d.origin_run}`} className="text-accent hover:underline">
                    {d.repo} <span className="whitespace-nowrap">PR #{d.pr_number}</span>
                  </Link>
                  <div className="mt-1 sm:hidden">
                    <DelegationBadge status={d.status} />
                  </div>
                </td>
                <td className="hidden px-4 py-2 sm:table-cell">
                  <DelegationBadge status={d.status} />
                </td>
                <td className="hidden whitespace-nowrap px-4 py-2 text-xs text-muted sm:table-cell">
                  {d.attempt ? `attempt ${d.attempt} of ${d.max_attempts}` : "not sent yet"}
                </td>
                <td className="hidden max-w-md truncate px-4 py-2 text-xs text-muted md:table-cell">
                  {d.error ?? d.closed_reason ?? d.fix_branch}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RunTable({ title, runs, empty }: { title: string; runs: RunRecord[]; empty: string }) {
  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold">{title}</h2>
      {runs.length === 0 ? (
        <p className="rounded-md border border-dashed border-line p-4 text-sm text-muted">{empty}</p>
      ) : (
        <div className="overflow-x-auto rounded-md border border-line bg-panel">
          <table className="w-full text-sm">
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-b border-line last:border-0 hover:bg-sunken">
                  <td className="px-4 py-2">
                    <Link href={`/runs/${r.id}`} className="text-accent hover:underline">
                      {r.repo} <span className="whitespace-nowrap">#{r.run_id}</span>
                    </Link>
                    {r.kind === "follow_up" && (
                      <span className="label ml-2 text-[10.5px] text-muted">fix check</span>
                    )}
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
