"use client";

import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { useState } from "react";

import { isTool, ToolCard } from "@/components/tool-card";

const SUGGESTIONS = [
  "Is this failure flaky, or would it fail again?",
  "Which line in the diff most likely caused it?",
  "Did every failed job fail the same way?",
];

export function AskPanel({ name }: { name: string }) {
  const [input, setInput] = useState("");
  const { messages, sendMessage, status, error } = useChat({
    id: `ask-${name}`,
    transport: new DefaultChatTransport({ api: `/api/fixtures/${encodeURIComponent(name)}/ask` }),
  });
  const busy = status === "submitted" || status === "streaming";

  function ask(text: string) {
    if (!text.trim() || busy) return;
    void sendMessage({ text });
    setInput("");
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto">
        {messages.length === 0 && (
          <div className="space-y-2">
            <p className="text-sm text-muted">
              A second agent, reading the same logs, diff and history through the same tools.
            </p>
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                onClick={() => ask(s)}
                className="block w-full rounded-md border border-line px-3 py-2 text-left text-sm hover:bg-sunken"
              >
                {s}
              </button>
            ))}
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={m.role === "user" ? "flex justify-end" : "space-y-2"}>
            {m.parts.map((part, i) => {
              if (part.type === "text" && part.text.trim()) {
                return m.role === "user" ? (
                  <p key={i} className="max-w-[85%] rounded-lg bg-sunken px-3 py-2 text-sm">
                    {part.text}
                  </p>
                ) : (
                  <p key={i} className="whitespace-pre-wrap text-sm leading-relaxed">
                    {part.text}
                  </p>
                );
              }
              if (isTool(part)) return <ToolCard key={part.toolCallId} part={part} compact />;
              return null;
            })}
          </div>
        ))}
        {status === "submitted" && <p className="text-xs text-muted">thinking…</p>}
        {error && <p className="rounded-md bg-bad-bg px-3 py-2 text-sm text-bad">{error.message}</p>}
      </div>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          ask(input);
        }}
        className="flex gap-2"
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about this incident…"
          className="min-w-0 flex-1 rounded-md border border-line bg-panel px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
        <button
          disabled={busy || !input.trim()}
          className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
        >
          Ask
        </button>
      </form>
    </div>
  );
}
