import { LogExcerpt } from "@/components/log-excerpt";
import { TERMINI, type Terminus } from "@/components/pipeline";
import { PrComment } from "@/components/pr-comment";
import { CategoryBadge } from "@/components/ui";
import { type CheckedVerdict, shortModel } from "@/lib/types";

/** Where the GitHub Action takes this verdict: the terminus the tracker lights. */
export function actionTerminus(v: CheckedVerdict): Terminus {
  if (!v.evidence_ok) return "blocked";
  if (v.action) return v.action.posts ? "posted" : "held";
  return v.route === "auto_post" ? "posted" : "held";
}

/** Where the webhook service takes it: it holds any verdict that proposes a fix. */
function serviceTerminus(v: CheckedVerdict): Terminus {
  if (!v.evidence_ok) return "blocked";
  return v.route === "auto_post" ? "posted" : "held";
}

/**
 * What the agent concluded, in the order a reader needs it: the answer, what
 * to do about it, the proof in the log itself, and where each entry point
 * takes the verdict next.
 */
export function VerdictCard({ v }: { v: CheckedVerdict }) {
  const { verdict } = v;
  const found = v.checks.filter((c) => c.ok).length;
  return (
    <article className="overflow-hidden rounded-md border border-line bg-panel">
      <section className="grid gap-5 p-5 sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-7">
        <div className="flex items-center gap-4 sm:flex-col sm:items-start sm:gap-2">
          <CategoryBadge category={verdict.category} />
          <p className="font-display text-[44px] font-semibold leading-none tabular-nums">
            {Math.round(verdict.confidence * 100)}
            <span className="text-[22px] text-muted">%</span>
          </p>
          <p className="label text-[11px] text-faint">confidence</p>
        </div>
        <div className="min-w-0">
          <p className="max-w-[70ch] text-[18px] font-medium leading-snug text-balance">{verdict.summary}</p>
          <p className="mt-2 max-w-[75ch] text-[14px] leading-relaxed text-muted">{verdict.reasoning}</p>
          <p className="mt-3 text-[13px] tabular-nums text-faint">
            {shortModel(v.model)}
            {v.tokens != null ? ` · ${v.tokens.toLocaleString()} tokens` : ""}
          </p>
        </div>
      </section>

      {verdict.suggested_fix && (
        <section className="border-t border-line px-5 py-4">
          <Heading>Suggested fix</Heading>
          <p className="mt-2 max-w-[75ch] text-[15px] leading-relaxed">{verdict.suggested_fix}</p>
        </section>
      )}

      <section className="space-y-3 border-t border-line px-5 py-4">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <Heading>Evidence from the log</Heading>
          <span className={`text-[13.5px] tabular-nums ${found === v.checks.length ? "text-ok" : "text-bad"}`}>
            {found} of {v.checks.length} quotes found at the lines they cite
          </span>
        </div>
        {verdict.evidence.map((ev, i) => (
          <LogExcerpt key={i} ev={ev} ok={v.checks[i]?.ok} context={v.context?.[i]} />
        ))}
      </section>

      <section className="border-t border-line px-5 py-4">
        <Heading>What happens next</Heading>
        <dl className="mt-2 divide-y divide-line">
          <Departure entry="GitHub Action" terminus={actionTerminus(v)} why={actionWhy(v)} />
          <Departure entry="Webhook service" terminus={serviceTerminus(v)} why={serviceWhy(v)} />
        </dl>
        {v.action?.comment && (
          <details className="group mt-3">
            <summary className="label cursor-pointer list-none text-[13px] text-accent hover:text-fg">
              <span className="group-open:hidden">Show the comment the Action posts</span>
              <span className="hidden group-open:inline">Hide the comment</span>
            </summary>
            <div className="mt-3">
              <PrComment body={v.action.comment} />
            </div>
          </details>
        )}
      </section>
    </article>
  );
}

function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="label text-[14px] font-semibold text-fg">{children}</h3>;
}

function Departure({ entry, terminus, why }: { entry: string; terminus: Terminus; why: string }) {
  const t = TERMINI.find((x) => x.key === terminus)!;
  return (
    <div className="grid gap-x-4 gap-y-1 py-2.5 sm:grid-cols-[9rem_11rem_minmax(0,1fr)] sm:items-baseline">
      <dt className="text-[13.5px] text-muted">{entry}</dt>
      <dd className="label flex items-center gap-2 text-[13.5px] font-semibold" style={{ color: t.tone }}>
        <span className="h-3.5 w-[4px] rounded-full" style={{ background: t.tone }} />
        {t.title}
      </dd>
      <dd className="text-[13.5px] leading-snug text-muted">{why}</dd>
    </div>
  );
}

function actionWhy(v: CheckedVerdict): string {
  if (!v.evidence_ok) return "A quoted line is not in the log where the verdict says, so nothing is posted, whatever the confidence.";
  if (v.action?.posts) {
    return v.action.fix_withheld
      ? "The suggested fix is left out of the comment and kept in the job summary."
      : "Confident, and every quote checks out against the log.";
  }
  return v.action ? `Not posted: ${v.action.reason}. The verdict stays in the job summary.` : v.route_reason;
}

function serviceWhy(v: CheckedVerdict): string {
  if (!v.evidence_ok) return "Queued for a person with the failed quote marked.";
  if (v.route === "auto_post") return "Confident, and every quote checks out against the log.";
  if (v.route_reason.includes("code change")) return "It proposes a code change, so a reviewer approves it before anything is posted.";
  if (v.route_reason.includes("below")) return `Confidence below the bar: ${v.route_reason}.`;
  if (v.route_reason.includes("category")) return "The agent could not name a category.";
  return v.route_reason;
}
