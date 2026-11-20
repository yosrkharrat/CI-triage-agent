import { Mark } from "@/components/ui";
import type { Evidence } from "@/lib/types";

type Context = { first_line: number; lines: string[]; total_lines: number };

/**
 * A citation in its place: the real log around it, with the cited lines run
 * through by a segment of line between two stations, solid when the quote was
 * found there and broken when it was not. Pointing at the excerpt dims the
 * lines around the quote, so the measured span owns the display.
 */
export function LogExcerpt({ ev, ok, context }: { ev: Evidence; ok: boolean | undefined; context: Context | null | undefined }) {
  const span = ev.line_start === ev.line_end ? `line ${ev.line_start}` : `lines ${ev.line_start}–${ev.line_end}`;

  return (
    <figure className="group overflow-hidden rounded-md border border-line bg-sunken">
      <figcaption className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-2">
        {ok !== undefined && <Mark ok={ok} />}
        <span className="font-mono text-[12px] text-fg">{ev.log_path}</span>
        <span className="font-mono text-[12px] text-muted">
          {span}
          {context ? ` of ${context.total_lines}` : ""}
        </span>
        <span className="text-[12.5px] text-muted">{ev.job_name}</span>
      </figcaption>

      {context ? (
        <div className="overflow-x-auto py-2">
          <table className="font-mono text-[12px] leading-[1.65]">
            <tbody>
              {context.lines.map((text, i) => {
                const n = context.first_line + i;
                const cited = n >= ev.line_start && n <= ev.line_end;
                const first = n === ev.line_start;
                const last = n === ev.line_end;
                return (
                  <tr key={n} className={cited ? "" : "transition-opacity duration-200 group-hover:opacity-40"}>
                    <td className="select-none pl-3 pr-2 text-right align-top tabular-nums text-faint">{n}</td>
                    <td className="relative w-4 select-none" aria-hidden>
                      {cited && <Segment ok={ok !== false} first={first} last={last} />}
                    </td>
                    <td className={`whitespace-pre pr-4 pl-2 ${cited ? "text-fg" : "text-muted"}`}>{text || " "}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <pre className="log p-3 text-fg">{ev.quote}</pre>
      )}

      {context && (
        <div className="border-t border-line px-3 py-2">
          <p className="text-[12.5px] text-muted">
            <span className="label mr-2 text-[11px] text-faint">Quoted</span>
            <span className="font-mono text-[12px] text-fg">{oneLine(ev.quote)}</span>
          </p>
          <p className="mt-1 text-[13px] leading-snug text-muted">{ev.why}</p>
        </div>
      )}
    </figure>
  );
}

/** A piece of the citation's line: a rail through the cited lines, a station at each end. */
function Segment({ ok, first, last }: { ok: boolean; first: boolean; last: boolean }) {
  const color = ok ? "var(--fg)" : "var(--bad)";
  return (
    <>
      <span
        className="absolute left-1/2 -translate-x-1/2"
        style={{
          top: first ? "50%" : 0,
          bottom: last ? "50%" : 0,
          width: ok ? 3 : 0,
          background: ok ? color : undefined,
          borderLeft: ok ? undefined : `2px dashed ${color}`,
        }}
      />
      {(first || last) && (
        <span
          className="absolute left-1/2 top-1/2 size-[9px] -translate-x-1/2 -translate-y-1/2 rounded-full border-2"
          style={{ borderColor: color, background: ok ? color : "var(--sunken)" }}
        />
      )}
    </>
  );
}

function oneLine(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > 140 ? `${flat.slice(0, 140)}…` : flat;
}
