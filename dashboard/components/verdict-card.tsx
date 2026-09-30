import { CategoryBadge, Mark, RouteBadge } from "@/components/ui";
import { type CheckedVerdict, shortModel } from "@/lib/types";

export function VerdictCard({ v }: { v: CheckedVerdict }) {
  const { verdict } = v;
  const bad = v.checks.filter((c) => !c.ok).length;
  return (
    <div className="rounded-lg border border-line bg-panel">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <CategoryBadge category={verdict.category} />
        <span className="font-mono text-sm tabular-nums">{verdict.confidence.toFixed(2)}</span>
        <RouteBadge route={v.route} />
        <span className="ml-auto font-mono text-xs text-muted">
          {shortModel(v.model)}
          {v.tokens != null ? ` · ${v.tokens.toLocaleString()} tokens` : ""}
        </span>
      </div>
      <div className="space-y-4 p-4">
        <p className="text-[15px] leading-relaxed">{verdict.summary}</p>

        {!v.evidence_ok && (
          <p className="rounded-md bg-bad-bg px-3 py-2 text-sm text-bad">
            {bad} of {v.checks.length} citation{v.checks.length === 1 ? "" : "s"} could not be found where the
            model says. The service would not post this, whatever its confidence.
          </p>
        )}

        <div className="space-y-3">
          {verdict.evidence.map((ev, i) => {
            const check = v.checks[i];
            const span = ev.line_start === ev.line_end ? `${ev.line_start}` : `${ev.line_start}–${ev.line_end}`;
            return (
              <div key={i} className="rounded-md border border-line">
                <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs">
                  {check && <Mark ok={check.ok} />}
                  <span className="font-mono">
                    {ev.log_path}:{span}
                  </span>
                  <span className="text-muted">{ev.why}</span>
                </div>
                <pre className="log border-t border-line bg-sunken p-3">{ev.quote}</pre>
                {check && !check.ok && <p className="px-3 py-2 text-xs text-bad">{check.reason}</p>}
              </div>
            );
          })}
        </div>

        {verdict.suggested_fix && (
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-muted">Suggested fix</p>
            <p className="mt-1 text-sm leading-relaxed">{verdict.suggested_fix}</p>
          </div>
        )}

        <details className="text-sm">
          <summary className="cursor-pointer text-muted">Reasoning</summary>
          <p className="mt-2 leading-relaxed text-muted">{verdict.reasoning}</p>
        </details>

        <p className="text-xs text-muted">Routing: {v.route_reason}</p>
      </div>
    </div>
  );
}
