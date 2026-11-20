"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { useMemo, useState } from "react";

import { Chevron, Pipeline, STAGES, type StageState, type Terminus } from "@/components/pipeline";
import { isMalformed, isTool, ToolCard } from "@/components/tool-card";
import { PLATE, PLATE_STOP } from "@/components/ui";
import { actionTerminus, VerdictCard } from "@/components/verdict-card";
import { explainModelError } from "@/lib/model-errors";
import { ReplayTransport } from "@/lib/replay";
import type { Category, CheckedVerdict, Recording } from "@/lib/types";

const MODELS = ["groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b"];

type Part = UIMessage["parts"][number];
type Restart = { type: "data-triage_restart"; data: { attempt: number; reason: string } };

/**
 * A triage run, rendered as it happens.
 *
 * The five-stage tracker on top says where the run is; the verdict, when it
 * lands, says what the agent concluded and whether its quotes hold up; the
 * steps below say how it got there.
 *
 * Given `recordings`, it replays those instead: the same chunks the agent
 * streamed when it was recorded, at the pace it streamed them.
 */
export function LiveTriage({ name, recordings }: { name: string; recordings?: Recording[] }) {
  const replay = recordings !== undefined;
  const models = replay ? recordings.map((r) => r.model) : MODELS;
  const [model, setModel] = useState(models[0]);
  const recording = recordings?.find((r) => r.model === model);
  const transport = useMemo(
    () =>
      recording
        ? new ReplayTransport(`/${recording.path}`)
        : new DefaultChatTransport({
            api: `/api/fixtures/${encodeURIComponent(name)}/triage?model=${encodeURIComponent(model)}`,
          }),
    [name, model, recording],
  );
  const { messages, sendMessage, status, stop, error, setMessages } = useChat({
    id: `triage-${name}-${model}`,
    transport,
    // A run streams a couple of thousand reasoning deltas; render at most
    // every 50 ms rather than once per delta, or the page falls behind.
    throttle: 50,
  });
  const busy = status === "submitted" || status === "streaming";
  const run = messages.findLast((m) => m.role === "assistant");
  const attempt = run ? latestAttempt(run) : null;
  const { states, notes, category, terminus } = track(attempt?.parts ?? null, busy);

  function start() {
    // Each run is a fresh triage: the agent's prompt is the run itself, and
    // an earlier attempt must not leak into the next as conversation history.
    setMessages([]);
    void sendMessage({ text: "Triage this run." });
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={busy ? () => void stop() : start}
          className={busy ? PLATE_STOP : PLATE}
        >
          {busy ? "Stop" : replay ? (run ? "Replay again" : "Replay the triage") : run ? "Run again" : "Run triage live"}
        </button>
        <label className="flex items-center gap-2 text-xs text-muted">
          <span className="sr-only">Model</span>
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={busy}
            className="rounded-sm border border-line-strong bg-bg px-2 py-2 font-mono text-xs text-fg disabled:opacity-60"
          >
            {models.map((m) => (
              <option key={m}>{m}</option>
            ))}
          </select>
        </label>
        <span className="text-xs text-muted">
          {replay && recording
            ? `Recorded ${new Date(recording.recorded_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })} against the live model; long pauses shortened.`
            : busy
              ? "The agent is working."
              : ""}
        </span>
      </div>

      <div className="rounded-md border border-line bg-bg px-5 py-5">
        <Pipeline states={states} notes={notes} category={category} terminus={terminus} />
      </div>

      {error && (
        <p className="rounded-md bg-bad-bg px-3 py-2 text-sm text-bad">
          {replay ? error.message : explainModelError(error, model)}
        </p>
      )}

      {attempt && <RunParts parts={attempt.parts} restart={attempt.restart} busy={busy} />}
    </div>
  );
}

/**
 * A run the service restarted from scratch (a 413, or a tool call the
 * provider could not parse) streams a marker before its next attempt. What
 * came before it is an abandoned conversation, so only the latest attempt
 * counts, with a line saying why the earlier one was dropped.
 */
function latestAttempt(message: UIMessage): { parts: Part[]; restart?: Restart } {
  const at = message.parts.findLastIndex((p) => p.type === "data-triage_restart");
  return {
    parts: message.parts.slice(at + 1),
    restart: at >= 0 ? (message.parts[at] as unknown as Restart) : undefined,
  };
}

function verdictOf(parts: Part[]): CheckedVerdict | undefined {
  return (parts.find((p) => p.type === "data-verdict") as { data: CheckedVerdict } | undefined)?.data;
}

type Track = {
  states: StageState[];
  notes: (string | null)[];
  category: Category | null;
  terminus: Terminus | null;
};

/** Where a run is on the line, read off the parts it has streamed so far. */
function track(parts: Part[] | null, busy: boolean): Track {
  const states: StageState[] = STAGES.map(() => "idle");
  const notes: (string | null)[] = STAGES.map(() => null);
  if (!parts) {
    if (busy) states[0] = "active";
    return { states, notes, category: null, terminus: null };
  }
  states[0] = "done";

  const tools = parts.filter(isTool);
  const calls = tools.filter((p) => p.type !== "tool-final_result" && !isMalformed(p)).length;
  notes[1] = calls ? `${calls} tool call${calls === 1 ? "" : "s"}` : "thinking";
  const v = verdictOf(parts);

  if (v) {
    const found = v.checks.filter((c) => c.ok).length;
    states[1] = states[2] = "done";
    notes[2] = `${v.verdict.category} · ${Math.round(v.verdict.confidence * 100)}%`;
    states[3] = states[4] = v.evidence_ok ? "done" : "failed";
    notes[3] = `${found} of ${v.checks.length} found`;
    const terminus = actionTerminus(v);
    notes[4] = terminus === "posted" ? "posted by the Action" : terminus;
    return { states, notes, category: v.verdict.category, terminus };
  }

  // No verdict yet. Once the agent reaches for its answer it is past
  // investigating; a run that stopped here ends on a failed stage.
  const answering = tools.some((p) => p.type === "tool-final_result" || isMalformed(p));
  const at = answering ? 2 : 1;
  for (let i = 1; i < at; i++) states[i] = "done";
  states[at] = busy ? "active" : "failed";
  if (answering) notes[2] = busy ? "writing the answer" : "stopped";
  else if (!busy) notes[1] = "stopped";
  return { states, notes, category: null, terminus: null };
}

function RunParts({ parts, restart, busy }: { parts: Part[]; restart?: Restart; busy: boolean }) {
  const verdict = verdictOf(parts);
  // The steps are the show while the agent works and a footnote once it has
  // answered: open until the verdict lands, unless the reader says otherwise.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = toggled ?? !verdict;
  const steps = parts.filter(isStep);

  return (
    <div className="space-y-4">
      {verdict && <VerdictCard v={verdict} />}

      <section className="rounded-md border border-line bg-panel">
        <button
          onClick={() => setToggled(!open)}
          className="flex w-full items-center gap-2 rounded-md px-4 py-3 text-left text-sm hover:bg-sunken"
          aria-expanded={open}
        >
          <Chevron open={open} />
          <span className="label text-[14px] font-semibold">How the agent got there</span>
          <span className="text-xs text-muted">
            {steps.length} step{steps.length === 1 ? "" : "s"}
            {busy ? " · working" : ""}
          </span>
        </button>
        {open && (
          <div className="space-y-2 border-t border-line p-3">
            {restart && (
              <p className="rounded-md bg-sunken px-3 py-2 text-xs text-muted">
                Attempt {restart.data.attempt}: the previous attempt stopped because {restart.data.reason}, so the
                agent started again from scratch.
              </p>
            )}
            {steps.map((part, i) => (
              <Step key={isTool(part) ? part.toolCallId : i} part={part} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function isStep(part: Part): boolean {
  if (part.type === "reasoning" || part.type === "text") return Boolean(part.text.trim());
  // The structured answer arrives as pydantic-ai's output tool; the verdict
  // card shows it with its checks, so the raw call is not a step.
  return isTool(part) && part.type !== "tool-final_result";
}

function Step({ part }: { part: Part }) {
  if (isTool(part)) return <ToolCard part={part} />;
  if (part.type === "reasoning") return <Thought text={part.text} live={part.state === "streaming"} />;
  if (part.type === "text") return <p className="px-1 text-sm leading-relaxed">{part.text}</p>;
  return null;
}

/**
 * The model's thinking: a scrolling tail while it streams, one line once done.
 * It is the agent talking to itself, worth a look but never worth a screen.
 */
function Thought({ text, live }: { text: string; live: boolean }) {
  const words = text.trim().split(/\s+/).length;
  if (live) {
    return (
      <div className="flex max-h-24 flex-col justify-end overflow-hidden rounded-md bg-sunken px-3 py-2 text-sm italic leading-relaxed text-muted [mask-image:linear-gradient(to_bottom,transparent,black_40%)]">
        <p>{text}</p>
      </div>
    );
  }
  return (
    <details className="group rounded-md border border-line">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm">
        <span className="size-2 shrink-0 rounded-full bg-line" />
        <span className="shrink-0 font-medium">Thinking</span>
        <span className="truncate text-xs italic text-muted group-open:hidden">{text.trim().slice(0, 160)}</span>
        <span className="ml-auto shrink-0 text-xs text-muted">{words} words</span>
      </summary>
      <p className="border-t border-line px-3 py-2 text-sm italic leading-relaxed text-muted">{text}</p>
    </details>
  );
}
