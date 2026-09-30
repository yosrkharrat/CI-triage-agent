import type { Category, RunStatus } from "@/lib/types";

const CATEGORY_COLOR: Record<Category, string> = {
  regression: "var(--cat-regression)",
  flaky: "var(--cat-flaky)",
  infra: "var(--cat-infra)",
  dependency: "var(--cat-dependency)",
  unknown: "var(--cat-unknown)",
};

export function CategoryBadge({ category, dim = false }: { category: Category | null; dim?: boolean }) {
  if (!category) return <span className="text-muted">—</span>;
  const color = CATEGORY_COLOR[category];
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium"
      style={{ color, borderColor: color, opacity: dim ? 0.75 : 1 }}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} />
      {category}
    </span>
  );
}

/** "3/4 verified", green only when every citation held up. */
export function Citations({ verified, cited }: { verified: number | null; cited: number | null }) {
  if (!cited) return <span className="text-muted">—</span>;
  const all = verified === cited;
  return (
    <span className={`font-mono text-xs ${all ? "text-ok" : "text-bad"}`} title="citations verified against the log">
      {verified}/{cited}
    </span>
  );
}

export function Mark({ ok }: { ok: boolean }) {
  return ok ? (
    <span className="rounded bg-ok-bg px-1.5 py-0.5 font-mono text-[11px] font-semibold text-ok">verified</span>
  ) : (
    <span className="rounded bg-bad-bg px-1.5 py-0.5 font-mono text-[11px] font-semibold text-bad">not in log</span>
  );
}

export function RouteBadge({ route }: { route: string | null }) {
  if (!route) return null;
  const auto = route === "auto_post";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${auto ? "bg-ok-bg text-ok" : "bg-warn-bg text-warn"}`}
    >
      {auto ? "auto-post" : "human review"}
    </span>
  );
}

export function ErrorPanel({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-lg border border-line bg-panel p-6">
      <p className="font-medium">{title}</p>
      <p className="mt-1 text-sm text-muted">{detail}</p>
      <p className="mt-4 text-sm text-muted">
        Start the service with <code className="font-mono">uv run ci-triage serve</code>. Both it and the
        dashboard read <code className="font-mono">CI_TRIAGE_REVIEW_TOKEN</code> from the repo root&apos;s{" "}
        <code className="font-mono">.env.local</code>.
      </p>
    </div>
  );
}

export function Panel({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-line bg-panel">
      <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold">{title}</h2>
        {aside}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

const STATUS_STYLE: Record<RunStatus, string> = {
  queued: "bg-sunken text-muted",
  running: "bg-sunken text-accent",
  awaiting_review: "bg-warn-bg text-warn",
  approved: "bg-ok-bg text-ok",
  posted: "bg-ok-bg text-ok",
  dry_run: "bg-sunken text-fg",
  no_pr: "bg-sunken text-muted",
  rejected: "bg-sunken text-muted",
  failed: "bg-bad-bg text-bad",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`whitespace-nowrap rounded px-1.5 py-0.5 font-mono text-[11px] ${STATUS_STYLE[status]}`}>
      {status.replace("_", " ")}
    </span>
  );
}

