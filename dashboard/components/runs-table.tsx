"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { CATEGORIES, CategoryBadge, Citations, INK, LineBullet, ON_INK, PILL } from "@/components/ui";
import { type Category, type FixtureRow, type Recording, shortModel } from "@/lib/types";

type Flag = "replay" | "disagree" | "invented";

const FLAGS: { key: Flag; label: string; title: string }[] = [
  { key: "replay", label: "Has a replay", title: "A recorded triage you can watch" },
  { key: "disagree", label: "Models disagree", title: "The models named different categories" },
  { key: "invented", label: "A quote not found", title: "Some model cited a line that is not in the log" },
];

/**
 * Every captured run with each model's last verdict, as a departures board:
 * a run's line is the category the models gave it. Filters narrow it to the
 * runs worth opening: the replays, the disagreements, the invented quotes.
 */
export function RunsTable({
  rows,
  models,
  recorded,
  demo,
}: {
  rows: FixtureRow[];
  models: string[];
  recorded: Record<string, Recording[]>;
  demo: boolean;
}) {
  const [query, setQuery] = useState("");
  const [lines, setLines] = useState<Set<Category>>(new Set());
  const [flags, setFlags] = useState<Set<Flag>>(new Set());

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      const said = Object.values(r.verdicts).map((v) => v.category).filter((c): c is Category => Boolean(c));
      if (q && !`${r.repo} ${r.workflow ?? ""} ${r.name}`.toLowerCase().includes(q)) return false;
      if (lines.size && !said.some((c) => lines.has(c))) return false;
      if (flags.has("replay") && !recorded[r.name]?.length) return false;
      if (flags.has("disagree") && new Set(said).size < 2) return false;
      if (flags.has("invented") && !Object.values(r.verdicts).some((v) => (v.verified ?? 0) < (v.cited ?? 0))) {
        return false;
      }
      return true;
    });
  }, [rows, query, lines, flags, recorded]);

  const toggle = <T,>(set: Set<T>, value: T, apply: (s: Set<T>) => void) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    apply(next);
  };
  const filtered = query || lines.size || flags.size;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
        <label className="relative w-full sm:w-64">
          <span className="sr-only">Search runs</span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search repo or workflow"
            className="w-full rounded-sm border border-line-strong bg-bg px-3 py-2 text-[14px] text-fg placeholder:text-faint focus:border-accent focus:outline-none"
          />
        </label>

        <fieldset className="flex flex-wrap items-center gap-1.5">
          <legend className="sr-only">Category</legend>
          {CATEGORIES.map((c) => {
            const on = lines.has(c);
            return (
              <button
                key={c}
                onClick={() => toggle(lines, c, setLines)}
                aria-pressed={on}
                className={`label inline-flex items-center gap-1.5 rounded-sm border px-2 py-1 text-[11.5px] font-semibold transition-colors ${
                  on ? "border-transparent" : "border-line-strong text-muted hover:border-fg hover:text-fg"
                }`}
                style={on ? { background: PILL[c], color: ON_INK[c] } : undefined}
              >
                {!on && <span aria-hidden className="h-[3px] w-2.5 rounded-full" style={{ background: INK[c] }} />}
                {c}
              </button>
            );
          })}
        </fieldset>

        <fieldset className="flex flex-wrap items-center gap-1.5">
          <legend className="sr-only">Show only</legend>
          {FLAGS.filter((f) => demo || f.key !== "replay").map((f) => {
            const on = flags.has(f.key);
            return (
              <button
                key={f.key}
                title={f.title}
                aria-pressed={on}
                onClick={() => toggle(flags, f.key, setFlags)}
                className={`label rounded-sm border px-2 py-1 text-[11.5px] font-semibold transition-colors ${
                  on ? "border-fg bg-fg text-bg" : "border-line-strong text-muted hover:border-fg hover:text-fg"
                }`}
              >
                {f.label}
              </button>
            );
          })}
        </fieldset>
      </div>

      <p className="text-[13px] tabular-nums text-faint" aria-live="polite">
        {shown.length} of {rows.length} runs
        {filtered ? (
          <button
            onClick={() => {
              setQuery("");
              setLines(new Set());
              setFlags(new Set());
            }}
            className="ml-3 text-accent hover:text-fg"
          >
            clear filters
          </button>
        ) : null}
      </p>

      <ul className="divide-y divide-line rounded-md border border-line md:hidden">
        {shown.map((r) => {
          const line = models.map((m) => r.verdicts[m]?.category).find(Boolean) ?? null;
          return (
            <li key={r.name} className="px-4 py-3.5">
              <div className="flex items-center gap-3">
                <LineBullet category={line} />
                <Link href={`/fixtures/${r.name}`} className="min-w-0 truncate font-semibold text-fg hover:text-accent">
                  {r.repo}
                </Link>
                {recorded[r.name]?.length ? <ReplayTag name={r.name} /> : null}
              </div>
              <p className="mt-0.5 pl-7 text-[13.5px] text-muted">
                {r.workflow ?? "workflow"}
                {r.label && (
                  <span className="ml-2 inline-flex items-center gap-1.5 align-middle">
                    <span className="text-[12px] text-faint">label</span> <CategoryBadge category={r.label} />
                  </span>
                )}
              </p>
              <dl className="mt-2 space-y-1 pl-7">
                {/* Only the models that answered: a column of "not reached" is noise here. */}
                {models.filter((m) => r.verdicts[m]).map((m) => {
                  const v = r.verdicts[m];
                  return (
                    <div key={m} className="flex items-center gap-3">
                      <dt className="w-28 shrink-0 truncate font-mono text-[11.5px] text-faint">{shortModel(m)}</dt>
                      <dd className="flex items-center gap-2.5">
                        {v ? (
                          <>
                            <CategoryBadge category={v.category} dim={Boolean(r.label && r.label !== v.category)} />
                            <Citations verified={v.verified} cited={v.cited} />
                          </>
                        ) : (
                          <span className="text-[13px] text-faint">not reached</span>
                        )}
                      </dd>
                    </div>
                  );
                })}
              </dl>
            </li>
          );
        })}
        {shown.length === 0 && <li className="px-4 py-8 text-center text-[14px] text-muted">No run matches these filters.</li>}
      </ul>

      <div className="hidden overflow-x-auto rounded-md border border-line md:block">
        <table className="w-full text-[14px]">
          <thead>
            <tr className="border-b border-line text-left">
              <th className="label px-4 py-2.5 text-[11.5px] font-medium text-faint">Run</th>
              <th className="label px-4 py-2.5 text-right text-[11.5px] font-medium text-faint">Failed jobs</th>
              <th className="label px-4 py-2.5 text-[11.5px] font-medium text-faint">Human label</th>
              {models.map((m) => (
                <th key={m} className="label px-4 py-2.5 text-[11.5px] font-medium text-faint">
                  {shortModel(m)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const line = models.map((m) => r.verdicts[m]?.category).find(Boolean) ?? null;
              const replay = recorded[r.name];
              return (
                <tr key={r.name} className="border-b border-line last:border-0 hover:bg-panel">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-3">
                      <LineBullet category={line} />
                      <div className="min-w-0">
                        <Link href={`/fixtures/${r.name}`} className="font-semibold text-fg hover:text-accent">
                          {r.repo}
                        </Link>
                        <span className="ml-2 text-muted">{r.workflow ?? "workflow"}</span>
                        <div className="mt-0.5 flex items-center gap-2 font-mono text-[11.5px] text-faint">
                          {r.name}
                          {r.source === "live" && <span>· live capture</span>}
                          {replay?.length ? <ReplayTag name={r.name} /> : null}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-right font-mono text-[12.5px] tabular-nums text-muted">{r.failed_jobs}</td>
                  <td className="px-4 py-3">
                    <CategoryBadge category={r.label} />
                  </td>
                  {models.map((m) => {
                    const v = r.verdicts[m];
                    return (
                      <td key={m} className="px-4 py-3">
                        {v ? (
                          <span className="inline-flex items-center gap-2.5">
                            <CategoryBadge category={v.category} dim={Boolean(r.label && r.label !== v.category)} />
                            <Citations verified={v.verified} cited={v.cited} />
                          </span>
                        ) : (
                          <span className="text-[12.5px] text-faint">not reached</span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {shown.length === 0 && (
              <tr>
                <td colSpan={3 + models.length} className="px-4 py-8 text-center text-[14px] text-muted">
                  No run matches these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ReplayTag({ name }: { name: string }) {
  return (
    <Link
      href={`/fixtures/${name}`}
      className="label inline-flex shrink-0 items-center gap-1 rounded-sm bg-fg px-1.5 py-px font-display text-[10.5px] font-semibold text-bg"
    >
      <svg viewBox="0 0 16 16" className="size-2" fill="currentColor" aria-hidden>
        <path d="M4 2.5v11l9-5.5z" />
      </svg>
      Replay
    </Link>
  );
}
