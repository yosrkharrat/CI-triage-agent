import type { Category, RunStatus } from "@/lib/types";

export const CATEGORIES: Category[] = ["regression", "flaky", "infra", "dependency", "unknown"];

/** Each category is a line on the map, in its own ink. */
export const INK: Record<Category, string> = {
  regression: "var(--cat-regression)",
  flaky: "var(--cat-flaky)",
  infra: "var(--cat-infra)",
  dependency: "var(--cat-dependency)",
  unknown: "var(--cat-unknown)",
};

/** A pill's fill: the line's ink, a step deeper where porcelain type sits on it. */
export const PILL: Record<Category, string> = {
  regression: "var(--pill-regression)",
  flaky: "var(--cat-flaky)",
  infra: "var(--pill-infra)",
  dependency: "var(--cat-dependency)",
  unknown: "var(--cat-unknown)",
};

/** Type on a pill of that ink: navy on the light inks, porcelain on the dark. */
export const ON_INK: Record<Category, string> = {
  regression: "var(--fg)",
  flaky: "var(--bg)",
  infra: "var(--fg)",
  dependency: "var(--bg)",
  unknown: "var(--bg)",
};

/** Primary and secondary actions: a porcelain plate, and its outline. */
export const PLATE =
  "inline-flex items-center gap-2 rounded-sm bg-fg px-4 py-2 font-display text-[15px] font-semibold uppercase tracking-[0.06em] text-bg transition-opacity hover:opacity-90 disabled:opacity-50";
export const PLATE_STOP =
  "inline-flex items-center gap-2 rounded-sm bg-bad px-4 py-2 font-display text-[15px] font-semibold uppercase tracking-[0.06em] text-bg transition-opacity hover:opacity-90";
export const PLATE_OUTLINE =
  "inline-flex items-center gap-2 rounded-sm border border-line-strong px-4 py-2 font-display text-[15px] font-semibold uppercase tracking-[0.06em] text-fg transition-colors hover:border-fg disabled:opacity-50";

/** A category as a line pill, the way a map names its lines. */
export function CategoryBadge({ category, dim = false }: { category: Category | null; dim?: boolean }) {
  if (!category) return <span className="text-faint">—</span>;
  return (
    <span
      className="label inline-flex items-center rounded-sm px-1.5 py-px text-[11px] font-semibold leading-[18px]"
      style={{ background: PILL[category], color: ON_INK[category], opacity: dim ? 0.55 : 1 }}
      title={dim ? "differs from the human label" : undefined}
    >
      {category}
    </span>
  );
}

/** A short stroke of the category's ink, as a timetable marks a line. */
export function LineBullet({ category }: { category: Category | null }) {
  return (
    <span
      aria-hidden
      className="inline-block h-[3px] w-4 shrink-0 rounded-full"
      style={{ background: category ? INK[category] : "var(--line-strong)" }}
    />
  );
}

/** "3/4", in green only when every quote was found where cited. */
export function Citations({ verified, cited }: { verified: number | null; cited: number | null }) {
  if (!cited) return <span className="text-faint">—</span>;
  const all = verified === cited;
  return (
    <span
      className={`text-[13px] font-medium tabular-nums ${all ? "text-ok" : "text-bad"}`}
      title={`${verified} of ${cited} quotes found at the lines they cite`}
    >
      {verified}/{cited}
    </span>
  );
}

/** Verification as a station mark: filled when the quote is there, broken when it is not. */
export function Mark({ ok }: { ok: boolean }) {
  return (
    <span className={`label inline-flex items-center gap-1.5 text-[11px] font-semibold ${ok ? "text-ok" : "text-bad"}`}>
      <svg viewBox="0 0 12 12" className="size-3" aria-hidden>
        {ok ? (
          <circle cx="6" cy="6" r="4.5" fill="currentColor" />
        ) : (
          <>
            <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2.4 2" />
            <path d="M2 10L10 2" stroke="currentColor" strokeWidth="1.5" />
          </>
        )}
      </svg>
      {ok ? "Found in log" : "Not in log"}
    </span>
  );
}

export function RouteBadge({ route }: { route: string | null }) {
  if (!route) return null;
  const auto = route === "auto_post";
  return (
    <span className={`label rounded-sm border px-1.5 py-px text-[10.5px] ${auto ? "border-ok/50 text-ok" : "border-warn/50 text-warn"}`}>
      {auto ? "auto-post" : "human review"}
    </span>
  );
}

export function ErrorPanel({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-md border border-line bg-panel p-6">
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

/** A heading set like the signage: condensed caps, porcelain. */
export function SectionTitle({ children, as: Tag = "h2" }: { children: React.ReactNode; as?: "h2" | "h3" }) {
  return <Tag className="font-display text-[22px] font-semibold uppercase leading-tight tracking-[0.04em]">{children}</Tag>;
}

export function Panel({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-md border border-line bg-panel">
      <div className="flex items-center justify-between gap-4 border-b border-line px-5 py-3">
        <h2 className="label text-[15px] font-semibold text-fg">{title}</h2>
        {aside}
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

const STATUS_STYLE: Record<RunStatus, string> = {
  queued: "border-line text-muted",
  running: "border-accent/50 text-accent",
  awaiting_review: "border-warn/50 text-warn",
  approved: "border-ok/50 text-ok",
  posted: "border-ok/50 text-ok",
  dry_run: "border-line text-fg",
  no_pr: "border-line text-muted",
  rejected: "border-line text-muted",
  failed: "border-bad/50 text-bad",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`label whitespace-nowrap rounded-sm border px-1.5 py-px text-[10.5px] ${STATUS_STYLE[status]}`}>
      {status.replace("_", " ")}
    </span>
  );
}
