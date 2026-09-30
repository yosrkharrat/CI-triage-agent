"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport, type UIMessage } from "ai";
import { useState } from "react";

import { isTool, ToolCard } from "@/components/tool-card";
import { VerdictCard } from "@/components/verdict-card";
import type { CheckedVerdict } from "@/lib/types";

const MODELS = ["groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b"];

/**
 * A triage run, rendered as it happens.
 *
 * Each tool call appears when the agent makes it and fills in when it returns;
 * the model's reasoning streams alongside. The verdict at the end comes with
 * what the model cannot say about itself: whether each quote is really in the
 * log, and where routing sends it.
 */
export function LiveTriage({ name }: { name: string }) {
  const [model, setModel] = useState(MODELS[0]);
  const { messages, sendMessage, status, stop, error, setMessages } = useChat({
    id: `triage-${name}-${model}`,
    transport: new DefaultChatTransport({
      api: `/api/fixtures/${encodeURIComponent(name)}/triage?model=${encodeURIComponent(model)}`,
    }),
  });
  const busy = status === "submitted" || status === "streaming";
  const run = messages.findLast((m) => m.role === "assistant");

  function start() {
    // Each run is a fresh triage: the agent's prompt is the run itself, and
    // an earlier attempt must not leak into the next as conversation history.
    setMessages([]);
    void sendMessage({ text: "Triage this run." });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={busy ? () => void stop() : start}
          className={`rounded-md px-3.5 py-1.5 text-sm font-medium text-white ${busy ? "bg-bad" : "bg-accent"} hover:opacity-90`}
        >
          {busy ? "Stop" : run ? "Run again" : "Run triage live"}
        </button>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          disabled={busy}
          className="rounded-md border border-line bg-panel px-2 py-1.5 font-mono text-xs"
        >
          {MODELS.map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
        <span className="text-xs text-muted">
          {status === "submitted" ? "starting…" : status === "streaming" ? "the agent is working" : ""}
        </span>
      </div>

      {error && (
        <p className="rounded-md bg-bad-bg px-3 py-2 text-sm text-bad">
          {error.message.includes("429") || /rate limit/i.test(error.message)
            ? "The model's free-tier quota is used up for now. Try the other model, or come back later."
            : error.message}
        </p>
      )}

      {run ? (
        <RunParts message={run} />
      ) : (
        !busy && (
          <p className="max-w-prose text-sm text-muted">
            The agent reads the failed jobs&apos; logs, the diff and the run history, then answers with a category,
            a confidence and log citations. Each tool call appears here as it happens; each citation is then looked
            up in the log.
          </p>
        )
      )}
    </div>
  );
}

function RunParts({ message }: { message: UIMessage }) {
  const verdict = message.parts.find((p) => p.type === "data-verdict") as { data: CheckedVerdict } | undefined;
  return (
    <div className="space-y-2">
      {message.parts.map((part, i) => {
        if (part.type === "reasoning" && part.text.trim()) {
          return (
            <p key={i} className="border-l-2 border-line pl-3 text-sm italic leading-relaxed text-muted">
              {part.text}
            </p>
          );
        }
        if (isTool(part)) {
          // The structured answer arrives as pydantic-ai's output tool; the
          // verdict card below shows it with its checks, so skip the raw call.
          if (part.type === "tool-final_result") return null;
          return <ToolCard key={part.toolCallId} part={part} />;
        }
        if (part.type === "text" && part.text.trim()) {
          return (
            <p key={i} className="text-sm leading-relaxed">
              {part.text}
            </p>
          );
        }
        return null;
      })}
      {verdict && (
        <div className="pt-3">
          <VerdictCard v={verdict.data} />
        </div>
      )}
    </div>
  );
}
