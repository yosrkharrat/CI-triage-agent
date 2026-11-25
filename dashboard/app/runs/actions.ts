"use server";

import { refresh } from "next/cache";

import { AgentError, agentJson } from "@/lib/agent";
import type { Delegation, RunRecord } from "@/lib/types";

export type ReviewResult = { ok: true; run?: RunRecord; delegation?: Delegation } | { ok: false; error: string };

/**
 * Approve or reject one run, or one delegation, as the named reviewer.
 *
 * The service decides whether an approval may post: a verdict whose citations
 * failed verification comes back as a 409, and that message is shown as is.
 * Approving a delegation sends it to Claude and approves its diagnosis too.
 */
export async function review(
  id: number,
  action: "approve" | "reject",
  reviewer: string,
  note: string,
  target: "runs" | "delegations" = "runs",
): Promise<ReviewResult> {
  try {
    const run = await agentJson<RunRecord & Delegation>(`/${target}/${id}/${action}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ reviewer: reviewer.trim(), note: note.trim() || null }),
    });
    refresh();
    return target === "runs" ? { ok: true, run } : { ok: true, delegation: run };
  } catch (err) {
    const e = err as AgentError;
    return { ok: false, error: e.status === 422 ? "That does not look like a GitHub login." : e.message };
  }
}
