"use client";

import { getToolName, isToolUIPart, type UIMessage } from "ai";

type ToolPart = Extract<UIMessage["parts"][number], { toolCallId: string }>;

export function isTool(part: UIMessage["parts"][number]): part is ToolPart {
  return isToolUIPart(part);
}

const LABEL: Record<string, string> = {
  get_logs: "Read the failure logs",
  get_diff: "Read the diff",
  test_history: "Checked the run history",
  reproduce: "Reproduced in the sandbox",
  overview: "Looked at the run",
  getLogs: "Read the failure logs",
  getDiff: "Read the diff",
  getHistory: "Checked the run history",
};

function argsOf(input: unknown): string {
  if (!input || typeof input !== "object") return "";
  const entries = Object.entries(input as Record<string, unknown>).filter(([, v]) => v != null && v !== "");
  return entries.map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
}

export function ToolCard({ part, compact = false }: { part: ToolPart; compact?: boolean }) {
  const name = getToolName(part);
  const args = argsOf(part.input);
  const running = part.state === "input-streaming" || part.state === "input-available";
  const failed = part.state === "output-error";
  const output =
    part.state === "output-available"
      ? typeof part.output === "string"
        ? part.output
        : JSON.stringify(part.output, null, 2)
      : null;
  const lines = output ? output.split("\n").length : 0;

  return (
    <details className="group rounded-md border border-line bg-panel" open={false}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm">
        <span
          className={`size-2 shrink-0 rounded-full ${
            running ? "animate-pulse bg-accent" : failed ? "bg-bad" : "bg-ok"
          }`}
        />
        <span className="font-medium">{LABEL[name] ?? name}</span>
        <code className="truncate font-mono text-xs text-muted">
          {name}({args})
        </code>
        <span className="ml-auto shrink-0 text-xs text-muted">
          {running ? "running…" : failed ? "failed" : `${lines} line${lines === 1 ? "" : "s"}`}
        </span>
      </summary>
      {(output || failed) && (
        <div className={`border-t border-line bg-sunken ${compact ? "max-h-64" : "max-h-[28rem]"} overflow-auto`}>
          <pre className="log p-3">{failed ? part.errorText : output}</pre>
        </div>
      )}
    </details>
  );
}
