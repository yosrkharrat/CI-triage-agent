"use client";

import { useState, useTransition } from "react";

import { review } from "@/app/runs/actions";

export function ReviewForm({ id, canApprove }: { id: number; canApprove: boolean }) {
  const [reviewer, setReviewer] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function act(action: "approve" | "reject") {
    setError(null);
    startTransition(async () => {
      const r = await review(id, action, reviewer, note);
      if (!r.ok) setError(r.error);
    });
  }

  return (
    <div className="space-y-3">
      <div className="grid gap-2 sm:grid-cols-[12rem_minmax(0,1fr)]">
        <input
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
          placeholder="your GitHub login"
          className="rounded-md border border-line bg-panel px-3 py-1.5 font-mono text-sm outline-none focus:border-accent"
        />
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="note (optional, kept with the run)"
          className="rounded-md border border-line bg-panel px-3 py-1.5 text-sm outline-none focus:border-accent"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => act("approve")}
          disabled={pending || !reviewer.trim() || !canApprove}
          className="rounded-md bg-ok px-3.5 py-1.5 text-sm font-medium text-white disabled:opacity-40"
          title={canApprove ? undefined : "A citation failed verification; this can only be rejected"}
        >
          Approve and post
        </button>
        <button
          onClick={() => act("reject")}
          disabled={pending || !reviewer.trim()}
          className="rounded-md border border-line px-3.5 py-1.5 text-sm font-medium disabled:opacity-40"
        >
          Reject
        </button>
        {!canApprove && (
          <span className="text-xs text-bad">
            A quote in this verdict is not in the log. It can be rejected, not posted.
          </span>
        )}
      </div>
      {error && <p className="rounded-md bg-bad-bg px-3 py-2 text-sm text-bad">{error}</p>}
    </div>
  );
}
