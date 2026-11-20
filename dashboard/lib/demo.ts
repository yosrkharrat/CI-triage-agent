import "server-only";

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { agentJson } from "@/lib/agent";
import type { FixtureDetail, FixtureRow, Recording } from "@/lib/types";

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
