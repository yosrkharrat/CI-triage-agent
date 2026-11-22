import { ReviewForm } from "@/components/review-form";
import { DelegationBadge, Panel } from "@/components/ui";
import type { Delegation, Verdict } from "@/lib/types";

/**
 * One delegation as a single line: the failed run, the diagnosis, the brief
 * sent to Claude, each attempt's CI run and what the guard said, and where it
 * ended. Drawn as a branch line off the run, stop by stop, in the rail map's
 * idiom: a stop is filled once passed, ringed while the train is there.
 */

type Tone = "ok" | "warn" | "bad" | "accent" | "muted";
type Stop = { title: string; tone: Tone; current?: boolean; body?: React.ReactNode };

const TONE: Record<Tone, string> = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  bad: "var(--bad)",
  accent: "var(--accent)",
  muted: "var(--line-strong)",
};

const OUTCOME: Partial<Record<Delegation["status"], { title: string; tone: Tone }>> = {
  fixed: { title: "Fixed", tone: "ok" },
  gave_up: { title: "Gave up: over to a person", tone: "bad" },
  rejected: { title: "Rejected", tone: "muted" },
  stale: { title: "Stale: nothing came back in time", tone: "muted" },
  superseded: { title: "Superseded: the author pushed", tone: "muted" },
  closed: { title: "Closed with the pull request", tone: "muted" },
};

function gh(repo: string, path: string) {
  return `https://github.com/${repo}/${path}`;
}

function A({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="text-accent hover:underline">
      {children}
    </a>
  );
}

function Diagnosis({ v }: { v: Verdict }) {
  return (
    <p className="text-sm text-muted">
      <span className="font-medium text-fg">{v.category}</span> ({v.confidence.toFixed(2)}): {v.summary}
    </p>
  );
}

function List({ items, tone }: { items: string[]; tone: "bad" | "warn" }) {
  return (
    <ul className={`mt-1 space-y-0.5 text-xs ${tone === "bad" ? "text-bad" : "text-warn"}`}>
      {items.map((x) => (
        <li key={x} className="break-words font-mono">
          {x}
        </li>
      ))}
    </ul>
  );
}

export function DelegationChain({ d, verdict }: { d: Delegation; verdict: Verdict | null }) {
  const stops: Stop[] = [
    {
      title: "Failed run",
      tone: "bad",
      body: (
        <p className="text-sm text-muted">
          <A href={gh(d.repo, `pull/${d.pr_number}`)}>#{d.pr_number}</A> at{" "}
          <span className="font-mono text-xs">{d.head_sha.slice(0, 8)}</span>
          {d.failed_jobs.length > 0 && <>: {d.failed_jobs.join(", ")}</>}
        </p>
      ),
    },
  ];
  if (verdict) stops.push({ title: "Diagnosis, every quote found in the log", tone: "ok", body: <Diagnosis v={verdict} /> });

  const decided = d.status !== "proposed";
  stops.push({
    title: !decided
      ? "Waiting for a person to approve"
      : d.status === "rejected"
        ? `Rejected${d.reviewed_by ? ` by @${d.reviewed_by}` : ""}`
        : d.reviewed_by === "auto"
          ? "Approved automatically (auto mode)"
          : `Approved${d.reviewed_by ? ` by @${d.reviewed_by}` : ""}`,
    tone: !decided ? "warn" : d.status === "rejected" ? "muted" : "ok",
    current: !decided,
    body: !decided ? (
      <div className="mt-2">
        <p className="mb-3 text-sm text-muted">
          Approving sends the brief below to Claude, and approves the diagnosis with it.
        </p>
        <ReviewForm id={d.id} canApprove target="delegations" approveLabel="Approve and send to Claude" />
      </div>
    ) : undefined,
  });

  for (const h of d.history) {
    stops.push({
      title: `Attempt ${h.attempt}: not fixed`,
      tone: "bad",
      body: (
        <div className="text-sm text-muted">
          <p>
            <A href={h.html_url}>CI run</A> on <span className="font-mono text-xs">{h.sha.slice(0, 8)}</span>:{" "}
            {h.outcome}
          </p>
          {h.guard && h.guard.length > 0 && <List items={h.guard} tone="bad" />}
          {h.verdict && (
            <div className="mt-1">
              <Diagnosis v={h.verdict} />
            </div>
          )}
        </div>
      ),
    });
  }

  // The brief as last sent: it carries the attempts above it.
  stops.push({
    title: d.attempt > 1 ? `Brief for attempt ${d.attempt}` : "Brief",
    tone: d.attempt > 0 ? "ok" : "muted",
    body: (
      <details className="mt-1">
        <summary className="cursor-pointer text-sm text-accent">What Claude is told</summary>
        <pre className="log mt-2 max-h-[28rem] overflow-auto rounded-md bg-sunken p-3 text-xs whitespace-pre-wrap">
          {d.brief}
        </pre>
      </details>
    ),
  });

  if (d.status === "dispatched" || d.status === "retry" || d.status === "approved") {
    stops.push({
      title: `Attempt ${Math.max(d.attempt, 1)} of ${d.max_attempts}: ${
        d.status === "dispatched" ? "with Claude" : "waiting to be sent"
      }`,
      tone: "accent",
      current: true,
      body: (
        <p className="text-sm text-muted">
          Branch <A href={gh(d.repo, `tree/${d.fix_branch}`)}>{d.fix_branch}</A>
          {d.fix_pr ? (
            <>
              , draft <A href={gh(d.repo, `pull/${d.fix_pr}`)}>#{d.fix_pr}</A> into{" "}
              <span className="font-mono text-xs">{d.head_branch}</span>
            </>
          ) : (
            ", no push yet"
          )}
          {d.deadline && <>. Stale after {new Date(d.deadline).toLocaleString()}.</>}
          {d.error && <span className="mt-1 block text-bad">{d.error}</span>}
        </p>
      ),
    });
  }

  const end = OUTCOME[d.status];
  if (end) {
    stops.push({
      title: end.title,
      tone: end.tone,
      body: (
        <div className="text-sm text-muted">
          {d.closed_reason && <p>{d.closed_reason}</p>}
          {d.status === "fixed" && d.guard_result && (
            <p className="mt-1">
              Diff guard passed on {d.guard_result.files} file(s)
              {d.fix_pr && (
                <>
                  . Review and merge <A href={gh(d.repo, `pull/${d.fix_pr}`)}>#{d.fix_pr}</A>
                </>
              )}
              .
            </p>
          )}
          {d.guard_result && d.guard_result.flags.length > 0 && <List items={d.guard_result.flags} tone="warn" />}
        </div>
      ),
    });
  }

  return (
    <Panel
      title={`Fix delegation ${d.id}`}
      aside={
        <span className="flex items-center gap-3 text-xs">
          {d.comment_url && <A href={d.comment_url}>PR comment</A>}
          <DelegationBadge status={d.status} />
        </span>
      }
    >
      <ol className="relative">
        {stops.map((s, i) => (
          <li key={i} className="relative pb-5 pl-7 last:pb-0">
            {i < stops.length - 1 && (
              <span aria-hidden className="absolute top-3 bottom-0 left-[5px] w-[3px] rounded-full bg-line-strong" />
            )}
            <span
              aria-hidden
              className="absolute top-[5px] left-0 size-[13px] rounded-full border-[3px]"
              style={{
                borderColor: TONE[s.tone],
                background: s.current ? "var(--panel)" : TONE[s.tone],
              }}
            />
            <p className="text-[15px] font-medium leading-6">{s.title}</p>
            {s.body}
          </li>
        ))}
      </ol>
    </Panel>
  );
}
