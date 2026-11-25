import Link from "next/link";

import { DelegationChain } from "@/components/delegation-chain";
import { ReviewForm } from "@/components/review-form";
import { VerdictCard } from "@/components/verdict-card";
import { CategoryBadge, ErrorPanel, Panel, StatusBadge } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import { DEMO } from "@/lib/demo";
import type { CheckedVerdict, Delegation, RunRecord } from "@/lib/types";

export default async function RunPage(props: PageProps<"/runs/[id]">) {
  const { id } = await props.params;
  if (DEMO) {
    return (
      <ErrorPanel
        title="The review queue needs the webhook service"
        detail="This demo replays recorded triages and runs no service. Run the dashboard locally against ci-triage serve to review live runs."
      />
    );
  }
  let r: RunRecord;
  try {
    r = await agentJson<RunRecord>(`/runs/${encodeURIComponent(id)}`);
  } catch (err) {
    return <ErrorPanel title={`Run ${id} could not be loaded`} detail={(err as AgentError).message} />;
  }
  const fixture = r.fixture?.split("/").pop();
  const parent =
    r.kind === "follow_up" && r.delegation != null
      ? await agentJson<Delegation>(`/delegations/${r.delegation}`).catch(() => null)
      : null;
  // Re-checked against the capture, so a reviewer sees which citation failed,
  // not only that one did. Missing when the capture has since been removed.
  const checked = r.verdict
    ? await agentJson<CheckedVerdict>(`/api/runs/${r.id}/verdict`).catch(() => null)
    : null;

  return (
    <div className="space-y-6">
      <div>
        <Link href="/runs" className="text-xs text-muted hover:text-fg">
          ← Review queue
        </Link>
        <h1 className="mt-1 text-lg font-semibold">
          {r.repo} <span className="font-mono">#{r.run_id}</span>
        </h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <StatusBadge status={r.status} />
          {r.verdict && <CategoryBadge category={r.verdict.category} />}
          {r.verdict && <span className="font-mono text-xs">{r.verdict.confidence.toFixed(2)}</span>}
          <a href={r.html_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
            run on GitHub
          </a>
          {fixture && (
            <Link href={`/fixtures/${fixture}`} className="text-accent hover:underline">
              open capture
            </Link>
          )}
          {r.comment_url && (
            <a href={r.comment_url.split(" ")[0]} target="_blank" rel="noreferrer" className="text-accent hover:underline">
              posted comment
            </a>
          )}
        </div>
        {(r.reason || r.error) && <p className="mt-2 text-sm text-muted">{r.error ?? r.reason}</p>}
        {r.kind === "follow_up" && (
          <p className="mt-1 text-sm text-muted">
            A CI run on a fix branch, checked against{" "}
            {parent ? (
              <Link href={`/runs/${parent.origin_run}`} className="text-accent hover:underline">
                delegation {parent.id}
              </Link>
            ) : (
              `delegation ${r.delegation}`
            )}{" "}
            rather than triaged on its own.
          </p>
        )}
        {r.reviewed_by && (
          <p className="mt-1 text-sm text-muted">
            Reviewed by @{r.reviewed_by}
            {r.review_note ? ` — “${r.review_note}”` : ""}
          </p>
        )}
      </div>

      {r.status === "awaiting_review" && (
        <Panel title="Decide">
          <ReviewForm id={r.id} canApprove={r.evidence_ok === true} />
        </Panel>
      )}

      {r.delegations?.map((d) => <DelegationChain key={d.id} d={d} verdict={r.verdict} />)}

      {r.trail && r.trail.length > 0 && (
        <Panel title="How it was diagnosed">
          <ol className="list-decimal space-y-0.5 pl-5 text-sm text-muted">
            {r.trail.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ol>
        </Panel>
      )}

      {checked && <VerdictCard v={checked} />}

      {/* The comment says every quote was found in the log; only show it where that is true. */}
      {r.comment && r.evidence_ok && (
        <Panel title={r.status === "posted" ? "The comment posted" : "The comment it would post"}>
          <pre className="log max-h-[36rem] rounded-md bg-sunken p-4 whitespace-pre-wrap">{r.comment}</pre>
        </Panel>
      )}
    </div>
  );
}
