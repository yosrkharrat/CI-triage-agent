export type Category = "flaky" | "regression" | "infra" | "dependency" | "unknown";

export type Evidence = {
  job_name: string;
  log_path: string;
  line_start: number;
  line_end: number;
  quote: string;
  why: string;
};

export type Verdict = {
  category: Category;
  confidence: number;
  summary: string;
  reasoning: string;
  evidence: Evidence[];
  suggested_fix: string | null;
};

/** What the stream's closing `data-verdict` part carries. */
export type CheckedVerdict = {
  model: string;
  verdict: Verdict;
  route: "auto_post" | "human_review";
  route_reason: string;
  evidence_ok: boolean;
  checks: { ok: boolean; reason: string }[];
  tokens: number | null;
};

export type ScoredVerdict = {
  category: Category | null;
  confidence: number | null;
  cited: number | null;
  verified: number | null;
  route: string | null;
};

export type FixtureRow = {
  name: string;
  source: "corpus" | "live";
  repo: string;
  branch: string | null;
  workflow: string | null;
  conclusion: string | null;
  created_at: string;
  failed_jobs: number;
  label: Category | null;
  verdicts: Record<string, ScoredVerdict>;
};

export type Score = {
  fixture: string;
  label: Category | null;
  predicted: Category | null;
  confidence: number | null;
  route: string | null;
  cited: number;
  verified: number;
  faults: Record<string, number>;
  tokens: number | null;
  error: string | null;
  verdict: Verdict | null;
};

export type FixtureDetail = {
  name: string;
  repo: string;
  branch: string | null;
  workflow: string | null;
  html_url: string;
  head_sha: string;
  overview: string;
  failed_jobs: string[];
  label: { category: Category; root_cause: string; notes: string | null } | null;
  scores: Record<string, Score>;
};

export type RunStatus =
  | "queued"
  | "running"
  | "posted"
  | "dry_run"
  | "awaiting_review"
  | "no_pr"
  | "approved"
  | "rejected"
  | "failed";

export type RunRecord = {
  id: number;
  repo: string;
  run_id: number;
  run_attempt: number;
  html_url: string;
  status: RunStatus;
  attempts: number;
  received_at: string;
  updated_at: string;
  fixture: string | null;
  verdict: Verdict | null;
  route: string | null;
  reason: string | null;
  evidence_ok: boolean | null;
  comment: string | null;
  comment_url: string | null;
  error: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
};

/** `groq:openai/gpt-oss-120b` → `gpt-oss-120b`. */
export function shortModel(model: string): string {
  return model.split("/").pop()?.split(":").pop() ?? model;
}
