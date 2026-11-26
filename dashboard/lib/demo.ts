import "server-only";

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { agentJson } from "@/lib/agent";
import type { CheckedVerdict, FixtureDetail, FixtureRow, Recording, RunRecord } from "@/lib/types";

/**
 * Replay mode: the dashboard without the Python service.
 *
 * A public deployment cannot run triages on demand: each one spends ~19k
 * tokens of a free quota that lasts about ten of them a day, and the corpus is
 * ~400 MB of logs. So `scripts/record-demo.mjs` records real triages once,
 * with the timing of every event, and a snapshot of what the service's API
 * returned. With `CI_TRIAGE_DEMO=replay` the pages read that snapshot and the
 * browser replays the recordings; nothing calls a model.
 */
export const DEMO = process.env.CI_TRIAGE_DEMO === "replay";

export type Snapshot = {
  captured_at: string;
  fixtures: FixtureRow[];
  details: Record<string, FixtureDetail>;
  recordings: Record<string, Recording[]>;
};

const FILE = path.join(process.cwd(), "demo", "snapshot.json");

let cache: Snapshot | undefined;
function snapshot(): Snapshot {
  if (cache === undefined) {
    if (!existsSync(FILE)) {
      throw new Error("demo/snapshot.json is missing: run `pnpm demo:record` with the service up");
    }
    cache = JSON.parse(readFileSync(FILE, "utf8")) as Snapshot;
  }
  return cache;
}

export async function getFixtures(): Promise<FixtureRow[]> {
  return DEMO ? snapshot().fixtures : agentJson<FixtureRow[]>("/api/fixtures");
}

export async function getFixture(name: string): Promise<FixtureDetail> {
  if (!DEMO) return agentJson<FixtureDetail>(`/api/fixtures/${encodeURIComponent(name)}`);
  const d = snapshot().details[name];
  if (!d) throw new Error(`no run ${name} in this demo`);
  return d;
}

/** The recorded triages of each run, by fixture name; empty outside replay mode. */
export function getRecordings(): Record<string, Recording[]> {
  return DEMO ? snapshot().recordings : {};
}

export function demoNames(): string[] {
  return DEMO ? Object.keys(snapshot().details) : [];
}

/**
 * Recorded delegation chains: real runs on the public ci-triage-demo repo,
 * from failed CI to the fix's outcome, as the service's API returned them.
 * Written by `scripts/export_demo_chains.py` from the database they ran on.
 */
export type Chains = {
  captured_at: string;
  source: string;
  runs: Record<string, RunRecord>;
  verdicts: Record<string, CheckedVerdict>;
};

const CHAINS = path.join(process.cwd(), "demo", "chains.json");

let chains: Chains | undefined;
function recordedChains(): Chains {
  if (chains === undefined) {
    chains = existsSync(CHAINS)
      ? (JSON.parse(readFileSync(CHAINS, "utf8")) as Chains)
      : { captured_at: "", source: "", runs: {}, verdicts: {} };
  }
  return chains;
}

/** The recorded runs that started a chain, oldest first; empty outside replay mode. */
export function getRecordedRuns(): RunRecord[] {
  return DEMO ? Object.values(recordedChains().runs).sort((a, b) => a.received_at.localeCompare(b.received_at)) : [];
}

export function getRecordedRun(id: string): { run: RunRecord; verdict: CheckedVerdict | null } | null {
  if (!DEMO) return null;
  const c = recordedChains();
  const run = c.runs[id];
  return run ? { run, verdict: c.verdicts[id] ?? null } : null;
}
