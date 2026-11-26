import Link from "next/link";

import { DelegationChain } from "@/components/delegation-chain";
import { ReviewForm } from "@/components/review-form";
import { VerdictCard } from "@/components/verdict-card";
import { CategoryBadge, ErrorPanel, Panel, StatusBadge } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import { DEMO, getRecordedRun } from "@/lib/demo";
import type { CheckedVerdict, Delegation, RunRecord } from "@/lib/types";

export default async function RunPage(props: PageProps<"/runs/[id]">) {
  const { id } = await props.params;
  let r: RunRecord;
  let parent: Delegation | null = null;
  let checked: CheckedVerdict | null = null;
  if (DEMO) {
    // The demo runs no service: it shows a chain recorded on ci-triage-demo.
    const recorded = getRecordedRun(id);
    if (!recorded) {
      return (
        <ErrorPanel
          title={`No recorded chain ${id}`}
          detail="This demo replays recorded runs and runs no service. The recorded fix chains are listed under Fix chains."
        />
      );
    }
    r = recorded.run;
    checked = recorded.verdict;
  } else {
    try {
      r = await agentJson<RunRecord>(`/runs/${encodeURIComponent(id)}`);
    } catch (err) {
      return <ErrorPanel title={`Run ${id} could not be loaded`} detail={(err as AgentError).message} />;
    }
    parent =
      r.kind === "follow_up" && r.delegation != null
        ? await agentJson<Delegation>(`/delegations/${r.delegation}`).catch(() => null)
        : null;
    // Re-checked against the capture, so a reviewer sees which citation failed,
    // not only that one did. Missing when the capture has since been removed.
    checked = r.verdict
      ? await agentJson<CheckedVerdict>(`/api/runs/${r.id}/verdict`).catch(() => null)
      : null;
  }
  // A live capture is not in the demo's corpus, so it gets no link there.
  const fixture = DEMO ? undefined : r.fixture?.split("/").pop();

  return (
    <div className="space-y-6">
      <div>
        <Link href="/runs" className="text-xs text-muted hover:text-fg">
          {DEMO ? "← Fix chains" : "← Review queue"}
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
        {DEMO && (
          <p className="mt-2 max-w-2xl text-sm text-muted">
            A recording of a real run on the public ci-triage-demo repository, from failed CI to the fix&apos;s
            outcome, shown as the service recorded it. Nothing here runs live.
          </p>
        )}
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

      {!DEMO && r.status === "awaiting_review" && (
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
