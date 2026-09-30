import Link from "next/link";

import { ReviewForm } from "@/components/review-form";
import { VerdictCard } from "@/components/verdict-card";
import { CategoryBadge, ErrorPanel, Panel, StatusBadge } from "@/components/ui";
import { AgentError, agentJson } from "@/lib/agent";
import type { CheckedVerdict, RunRecord } from "@/lib/types";

export default async function RunPage(props: PageProps<"/runs/[id]">) {
  const { id } = await props.params;
  let r: RunRecord;
  try {
    r = await agentJson<RunRecord>(`/runs/${encodeURIComponent(id)}`);
  } catch (err) {
    return <ErrorPanel title={`Run ${id} could not be loaded`} detail={(err as AgentError).message} />;
  }
  const fixture = r.fixture?.split("/").pop();
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
