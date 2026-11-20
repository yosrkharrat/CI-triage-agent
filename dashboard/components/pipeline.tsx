import { Train } from "@/components/train";
import { CATEGORIES, CategoryBadge, INK } from "@/components/ui";
import type { Category } from "@/lib/types";

/**
 * The line every failed run rides, drawn as a rail map.
 *
 * Five stations on one trunk; the five categories are lines that meet it at
 * the Verdict interchange; at Route the trunk branches to its three termini.
 * The home page shows the map as the explanation, the run page as a tracker
 * that fills in while a triage streams, so what a visitor read about is what
 * they then watch happen.
 */

export type StageState = "idle" | "active" | "done" | "failed";
export type Terminus = "posted" | "held" | "blocked";

export const STAGES = [
  {
    title: "Failed run",
    detail: "A workflow_run event hands over the failed jobs' logs, the PR diff and the run history.",
  },
  {
    title: "Investigate",
    detail: "The agent calls get_logs, get_diff and test_history until it can explain the failure.",
  },
  {
    title: "Verdict",
    detail: "A typed answer: one of five categories, a confidence, and the log lines that prove it.",
  },
  {
    title: "Verify quotes",
    detail: "Each quote is looked up at the lines it cites. String matching, no model in the loop.",
  },
  {
    title: "Route",
    detail: "Posted on the PR when every quote holds and confidence ≥ 0.70. A suggested fix waits for a person.",
  },
] as const;

export const TERMINI: { key: Terminus; title: string; tone: string }[] = [
  { key: "posted", title: "Posted on the PR", tone: "var(--ok)" },
  { key: "held", title: "Held for a person", tone: "var(--warn)" },
  { key: "blocked", title: "Blocked", tone: "var(--bad)" },
];

type Props = {
  states?: StageState[];
  notes?: (string | null)[];
  category?: Category | null;
  terminus?: Terminus | null;
  showDetail?: boolean;
};

export function Pipeline(props: Props) {
  return (
    <>
      <div className="hidden lg:block">
        <MapView {...props} />
      </div>
      <div className="lg:hidden">
        <RailView {...props} />
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// The map, for wide screens. One viewBox; labels are HTML placed in the same
// coordinates, so type stays at its real size while the drawing scales.

const W = 1000;
const TRUNK = 185;
const STATION_X = [30, 235, 430, 610, 770];
const VX = STATION_X[2]; // the Verdict interchange
const RX = STATION_X[4]; // the Route interchange
const LINE_X = CATEGORIES.map((_, i) => VX - 18 + i * 9); // where each line meets the interchange
const PILL_X = CATEGORIES.map((_, i) => VX - 156 + i * 78); // where each line is named
const BRANCH_Y: Record<Terminus, number> = { posted: 145, held: 185, blocked: 225 };
const END_X = 975;

const pct = (x: number, total: number) => `${(x / total) * 100}%`;

function MapView({ states, notes, category, terminus, showDetail }: Props) {
  // Room under the stations for a sentence each on the home page, a note on the tracker.
  const H = showDetail ? 340 : 262;
  const tracking = states !== undefined;
  const state = (i: number): StageState => states?.[i] ?? (tracking ? "idle" : "done");
  const left = (i: number) => state(i) === "done";

  // The run's own position: the station it is at, or the end of its branch.
  const route = STATION_X.map((x) => ({ x, y: TRUNK }));
  let at = -1;
  STAGES.forEach((_, i) => {
    if (state(i) !== "idle") at = i;
  });
  if (tracking && terminus) {
    const y = BRANCH_Y[terminus];
    route.push({ x: RX + Math.abs(y - TRUNK), y }, { x: END_X - 14, y });
    at = route.length - 1;
  }

  return (
    <div className="relative w-full" style={{ aspectRatio: `${W} / ${H}` }}>
      <svg viewBox={`0 0 ${W} ${H}`} className="absolute inset-0 size-full overflow-visible" aria-hidden>
        {/* The category lines, each from its name down into the interchange. */}
        {CATEGORIES.map((c, i) => {
          const dx = Math.abs(PILL_X[i] - LINE_X[i]);
          const lit = !tracking ? 1 : category ? (category === c ? 1 : 0.14) : 0.35;
          return (
            <path
              key={c}
              d={`M${PILL_X[i]} 30 V${TRUNK - 15 - dx} L${LINE_X[i]} ${TRUNK - 15} V${TRUNK}`}
              fill="none"
              stroke={INK[c]}
              strokeWidth={category === c ? 7 : 5}
              strokeLinejoin="round"
              strokeLinecap="round"
              style={{ opacity: lit, transition: "opacity 300ms ease-out" }}
            />
          );
        })}

        {/* The trunk, built station by station: dashed where the run has not been yet. */}
        {STATION_X.slice(0, -1).map((x, i) => (
          <Track key={i} d={`M${x} ${TRUNK} H${STATION_X[i + 1]}`} on={!tracking || left(i)} />
        ))}

        {/* The three branches out of Route. */}
        {TERMINI.map((t) => {
          const y = BRANCH_Y[t.key];
          const dy = y - TRUNK;
          const d = dy === 0 ? `M${RX} ${TRUNK} H${END_X}` : `M${RX} ${TRUNK} L${RX + Math.abs(dy)} ${y} H${END_X}`;
          const on = !tracking || terminus === t.key;
          return (
            <g key={t.key} style={{ opacity: tracking && terminus && terminus !== t.key ? 0.3 : 1, transition: "opacity 300ms" }}>
              <Track d={d} on={on} />
              <path d={`M${END_X} ${y - 11} V${y + 11}`} stroke={on ? t.tone : "var(--line-strong)"} strokeWidth={6} strokeLinecap="round" />
            </g>
          );
        })}

        {/* Stations; the two decision points are interchanges. */}
        {STATION_X.map((x, i) =>
          i === 2 ? (
            <Interchange key={i} state={state(i)} capsule />
          ) : i === 4 ? (
            <Interchange key={i} state={state(i)} />
          ) : (
            <Station key={i} x={x} state={state(i)} />
          ),
        )}

        {tracking && at >= 0 && <Train route={route} at={at} color={category ? INK[category] : "var(--fg)"} />}
      </svg>

      {/* Line names. */}
      {CATEGORIES.map((c, i) => (
        <div
          key={c}
          className="absolute -translate-x-1/2 transition-opacity duration-300"
          style={{
            left: pct(PILL_X[i], W),
            top: pct(8, H),
            opacity: !tracking ? 1 : category ? (category === c ? 1 : 0.3) : 0.6,
          }}
        >
          <CategoryBadge category={c} />
        </div>
      ))}

      {/* Station names, with what each one did or does. */}
      {STAGES.map((s, i) => (
        <div
          key={s.title}
          className={`absolute ${i === 0 ? "w-[134px]" : "w-[152px] -translate-x-1/2 text-center"}`}
          style={{ left: pct(i === 0 ? STATION_X[0] - 12 : STATION_X[i], W), top: pct(TRUNK + 24, H) }}
        >
          <p className={`label text-[15px] font-semibold ${state(i) === "idle" ? "text-faint" : "text-fg"}`}>{s.title}</p>
          {showDetail && <p className="mt-1 text-[13px] leading-snug text-muted">{s.detail}</p>}
          {notes?.[i] && (
            <p className={`mt-0.5 text-[13px] tabular-nums ${state(i) === "failed" ? "text-bad" : "text-muted"}`}>
              {notes[i]}
            </p>
          )}
        </div>
      ))}

      {/* Termini. */}
      {TERMINI.map((t) => (
        <p
          key={t.key}
          className="label absolute -translate-y-full text-right text-[13px] font-semibold transition-opacity duration-300"
          style={{
            right: pct(W - END_X + 8, W),
            top: pct(BRANCH_Y[t.key] - 6, H),
            color: t.tone,
            opacity: tracking && terminus !== t.key ? 0.35 : 1,
          }}
        >
          {t.title}
        </p>
      ))}
    </div>
  );
}

function Track({ d, on }: { d: string; on: boolean }) {
  return (
    <path
      d={d}
      fill="none"
      stroke={on ? "var(--fg)" : "var(--line-strong)"}
      strokeWidth={on ? 6 : 3}
      strokeDasharray={on ? undefined : "2 9"}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ transition: "stroke 250ms ease-out" }}
    />
  );
}

const STROKE: Record<StageState, string> = {
  idle: "var(--line-strong)",
  active: "var(--accent)",
  done: "var(--fg)",
  failed: "var(--bad)",
};

function Station({ x, state }: { x: number; state: StageState }) {
  return (
    <g>
      {state === "active" && <Pulse cx={x} cy={TRUNK} r={10} />}
      <circle
        cx={x}
        cy={TRUNK}
        r={8}
        fill={state === "done" ? "var(--fg)" : "var(--bg)"}
        stroke={STROKE[state]}
        strokeWidth={3.5}
        style={{ transition: "fill 250ms, stroke 250ms" }}
      />
      {state === "failed" && <Cross cx={x} cy={TRUNK} />}
    </g>
  );
}

function Interchange({ state, capsule = false }: { state: StageState; capsule?: boolean }) {
  const filled = state === "done";
  const shape = capsule ? (
    <rect x={VX - 28} y={TRUNK - 13} width={56} height={26} rx={13} />
  ) : (
    <circle cx={RX} cy={TRUNK} r={13} />
  );
  return (
    <g>
      {state === "active" &&
        (capsule ? <Pulse cx={VX} cy={TRUNK} r={20} /> : <Pulse cx={RX} cy={TRUNK} r={15} />)}
      <g
        fill={filled ? "var(--fg)" : "var(--bg)"}
        stroke={state === "idle" ? "var(--line-strong)" : STROKE[state]}
        strokeWidth={4}
        style={{ transition: "fill 250ms, stroke 250ms" }}
      >
        {shape}
      </g>
      {state === "failed" && <Cross cx={capsule ? VX : RX} cy={TRUNK} />}
    </g>
  );
}

function Pulse({ cx, cy, r }: { cx: number; cy: number; r: number }) {
  return (
    <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--accent)" strokeWidth={2} className="motion-safe:animate-ping" style={{ transformOrigin: `${cx}px ${cy}px`, transformBox: "view-box" }} />
  );
}

function Cross({ cx, cy }: { cx: number; cy: number }) {
  return <path d={`M${cx - 4} ${cy - 4}l8 8m0-8l-8 8`} stroke="var(--bad)" strokeWidth={2.5} strokeLinecap="round" />;
}

// ---------------------------------------------------------------------------
// The same line, set vertically for narrow screens: one continuous rail down
// the page, the category lines entering the Verdict station from the side.

function RailView({ states, notes, category, terminus, showDetail }: Props) {
  const tracking = states !== undefined;
  const state = (i: number): StageState => states?.[i] ?? (tracking ? "idle" : "done");
  const reached = TERMINI.find((t) => t.key === terminus);

  return (
    <ol>
      {STAGES.map((s, i) => {
        const st = state(i);
        const first = i === 0;
        const last = i === STAGES.length - 1;
        const interchange = i === 2 || i === 4;
        // The rail into this station is lit once the run left the previous one.
        const inLit = !tracking || (i > 0 && state(i - 1) === "done");
        const outLit = !tracking || st === "done";
        return (
          <li key={s.title} className="flex">
            <div className="relative flex w-7 shrink-0 justify-center">
              {!first && (
                <span aria-hidden className={`absolute top-0 h-4 w-[5px] ${inLit ? "bg-fg" : "bg-line-strong"}`} />
              )}
              {!last && (
                <span aria-hidden className={`absolute top-4 bottom-0 w-[5px] ${outLit ? "bg-fg" : "bg-line-strong"}`} />
              )}
              <span
                className={`relative z-10 mt-1 grid shrink-0 place-items-center rounded-full border-[3px] transition-colors duration-200 ${
                  interchange ? "size-6 -mt-0.5" : "size-[18px]"
                } ${st === "done" ? "border-fg bg-fg" : st === "active" ? "border-accent bg-bg" : st === "failed" ? "border-bad bg-bg" : "border-line-strong bg-bg"}`}
              >
                {st === "active" && <span className="absolute inset-[-5px] rounded-full border-2 border-accent motion-safe:animate-ping" />}
                <span className="sr-only">{st === "idle" ? "not reached" : st}</span>
              </span>
              {i === 2 && <Spurs category={tracking ? (category ?? null) : null} dim={tracking && !category} />}
            </div>
            <div className={`min-w-0 pl-7 ${last ? "pb-1" : "pb-6"}`}>
              <p className={`label text-[15px] font-semibold ${st === "idle" && tracking ? "text-faint" : ""}`}>{s.title}</p>
              {showDetail && <p className="mt-1 text-[13.5px] leading-snug text-muted">{s.detail}</p>}
              {notes?.[i] && (
                <p className={`mt-0.5 text-[13px] tabular-nums ${st === "failed" ? "text-bad" : "text-muted"}`}>{notes[i]}</p>
              )}
              {i === 2 && !tracking && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {CATEGORIES.map((c) => (
                    <CategoryBadge key={c} category={c} />
                  ))}
                </div>
              )}
              {last && (!tracking ? (
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
                  {TERMINI.map((t) => (
                    <span key={t.key} className="label flex items-center gap-1.5 text-[12px] font-semibold" style={{ color: t.tone }}>
                      <span className="h-3 w-[4px] rounded-full" style={{ background: t.tone }} />
                      {t.title}
                    </span>
                  ))}
                </div>
              ) : reached ? (
                <p className="label mt-1.5 flex items-center gap-1.5 text-[13px] font-semibold" style={{ color: reached.tone }}>
                  <span className="h-3 w-[4px] rounded-full" style={{ background: reached.tone }} />
                  {reached.title}
                </p>
              ) : null)}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** The five category lines, short, running out of the gutter into the Verdict station. */
function Spurs({ category, dim }: { category: Category | null; dim: boolean }) {
  return (
    <svg viewBox="0 0 26 26" className="absolute left-[24px] top-0 h-[26px] w-[26px]" aria-hidden>
      {CATEGORIES.map((c, i) => {
        const y = 3 + i * 5;
        return (
          <path
            key={c}
            d={`M26 ${y} H15 L2 13`}
            fill="none"
            stroke={INK[c]}
            strokeWidth={category === c ? 3.5 : 2.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            style={{ opacity: category ? (category === c ? 1 : 0.15) : dim ? 0.4 : 1, transition: "opacity 300ms ease-out" }}
          />
        );
      })}
    </svg>
  );
}

export function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={`size-3.5 shrink-0 text-muted transition-transform duration-150 ${open ? "rotate-90" : ""}`}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      aria-hidden
    >
      <path d="M6 3.5l4.5 4.5L6 12.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
