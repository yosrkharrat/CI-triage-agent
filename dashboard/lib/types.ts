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
  /** The log around each citation, numbered as cited; null for a log the run lacks. */
  context?: ({ first_line: number; lines: string[]; total_lines: number } | null)[];
  /** What the GitHub Action does with this verdict, and the comment it posts. */
  action?: { posts: boolean; reason: string; fix_withheld: boolean; comment: string | null };
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
  | "failed"
  | "checked";

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
  /** `follow_up` for a CI run on a delegation's fix branch, checked rather than triaged. */
  kind?: "triage" | "follow_up";
  delegation?: number | null;
  /** On `/runs/{id}` only: the delegations this run's diagnosis started. */
  delegations?: Delegation[];
  /** How the verdict was reached: one line per tool call the agent made. */
  trail?: string[];
};

export type DelegationStatus =
  | "proposed"
  | "approved"
  | "dispatched"
  | "retry"
  | "fixed"
  | "gave_up"
  | "rejected"
  | "stale"
  | "superseded"
  | "closed";

export const OPEN_DELEGATION: DelegationStatus[] = ["proposed", "approved", "dispatched", "retry"];

export type GuardResult = { ok: boolean; violations: string[]; flags: string[]; files: number };

/** One attempt that did not fix it, as the service records it. */
export type AttemptRecord = {
  attempt: number;
  outcome: string;
  run_id: number;
  html_url: string;
  sha: string;
  verdict?: Verdict;
  guard?: string[];
};

/** One step of a fix plan: one change to one file. */
export type PlanStep = { file: string; action: "edit" | "create"; change: string; why: string };

/** The planner's answer, checked against the code at the failing commit. */
export type FixPlan = {
  feasible: boolean;
  blocker: string | null;
  goal: string;
  steps: PlanStep[];
  verify: string[];
  risks: string | null;
};

/** What the planner's checks found, and why a plan was dropped when it was. */
export type PlanChecks = {
  ok: boolean;
  dropped: string | null;
  repairs: number;
  checks: { check: string; ok: boolean; detail: string }[];
};

export type Delegation = {
  id: number;
  repo: string;
  pr_number: number;
  origin_run: number;
  workflow_id: number;
  failed_jobs: string[];
  head_branch: string;
  head_sha: string;
  base_branch: string | null;
  fix_branch: string;
  fix_pr: number | null;
  command: string | null;
  status: DelegationStatus;
  attempt: number;
  max_attempts: number;
  brief: string;
  history: AttemptRecord[];
  last_run_id: number | null;
  guard_result: GuardResult | null;
  closed_reason: string | null;
  error: string | null;
  reviewed_by: string | null;
  comment_url: string | null;
  created_at: string;
  updated_at: string;
  deadline: string | null;
  /** The plan Claude was handed, when one passed its checks. */
  plan?: FixPlan | null;
  plan_checks?: PlanChecks | null;
  /** How the diagnosis was reached, as on the run. */
  trail?: string[];
  /** On `/delegations/{id}` only. */
  follow_ups?: RunRecord[];
  /** The diagnosis's category; a `dependency` fix may pin actions. Missing from an older service. */
  category?: Category;
};

/** One recorded triage, as `scripts/record-demo.mjs` lists it in the snapshot. */
export type Recording = {
  model: string;
  /** Under `public/`, so the browser fetches it as a static file. */
  path: string;
  recorded_at: string;
  duration_ms: number;
  category: Category | null;
  evidence_ok: boolean | null;
  /** The comment the GitHub Action would post for this verdict, if it posts one. */
  comment: string | null;
};

/** What a recording file holds: every stream chunk, with its time since the request. */
export type RecordingFile = {
  model: string;
  fixture: string;
  recorded_at: string;
  events: { t: number; chunk: unknown }[];
};

/** `groq:openai/gpt-oss-120b` → `gpt-oss-120b`. */
export function shortModel(model: string): string {
  return model.split("/").pop()?.split(":").pop() ?? model;
}
