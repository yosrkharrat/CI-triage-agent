#!/usr/bin/env node
/**
 * Record real triages for the dashboard's replay mode.
 *
 *   uv run ci-triage serve                       # from the repo root, once
 *   pnpm demo:record                             # refresh the snapshot only, no tokens spent
 *   pnpm demo:record pandas__35489338041         # + record that run on every model
 *   pnpm demo:record --model groq:openai/gpt-oss-20b black__34987498453
 *   pnpm demo:record --force <run>               # re-record over an existing recording
 *
 * Each recording is one live triage (~19k tokens) streamed from the running
 * service, saved chunk by chunk with the time it arrived. A run that ends
 * without a verdict (a spent quota, a model error) is reported and not saved,
 * so a demo never replays a failure it did not mean to show.
 *
 * Every run also brings each existing recording's verdict checks up to date,
 * through the service's /check endpoint: the quotes are looked up again and
 * the log context and the Action's comment are added. No model is called.
 *
 * Writes `demo/snapshot.json` (the corpus and each run's detail, as the
 * service's API returned them) and `public/demo/recordings/<run>/<model>.json`.
 * Only corpus runs go in the snapshot: the service's own live captures may
 * come from private repos, and the demo is public.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { parseArgs, parseEnv } from "node:util";

const ROOT = path.resolve(import.meta.dirname, "..");
const RECORDINGS = path.join(ROOT, "public", "demo", "recordings");
const SNAPSHOT = path.join(ROOT, "demo", "snapshot.json");
const MODELS = ["groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b"];

const { values: opts, positionals: runs } = parseArgs({
  allowPositionals: true,
  options: {
    model: { type: "string", multiple: true },
    force: { type: "boolean", default: false },
  },
});

// The same lookup as lib/agent.ts: this process, then the repo root's .env.local.
const sharedFile = path.join(ROOT, "..", ".env.local");
const shared = existsSync(sharedFile) ? parseEnv(readFileSync(sharedFile, "utf8")) : {};
const env = (key) => process.env[key] || shared[key] || undefined;

const BASE = env("CI_TRIAGE_URL") ?? "http://127.0.0.1:8000";
const TOKEN = env("CI_TRIAGE_REVIEW_TOKEN");
if (!TOKEN) die("CI_TRIAGE_REVIEW_TOKEN is not set, here or in the repo root's .env.local");

async function api(p, init = {}) {
  let res;
  try {
    res = await fetch(`${BASE}${p}`, { ...init, headers: { ...init.headers, authorization: `Bearer ${TOKEN}` } });
  } catch {
    die(`cannot reach the ci-triage service at ${BASE}: start it with \`uv run ci-triage serve\``);
  }
  if (!res.ok) die(`${p}: ${res.status} ${await res.text()}`);
  return res;
}

const slug = (model) => model.replace(/[^a-z0-9.-]+/gi, "_");

/** One live triage, as the chunks the browser would have received and when. */
async function record(name, model) {
  const started = performance.now();
  const res = await api(`/api/fixtures/${encodeURIComponent(name)}/triage?model=${encodeURIComponent(model)}`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({
      trigger: "submit-message",
      id: `record-${name}`,
      messages: [{ id: "u1", role: "user", parts: [{ type: "text", text: "Triage this run." }] }],
    }),
  });

  const events = [];
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const bytes of res.body) {
    buffer += decoder.decode(bytes, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = block
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trimStart())
        .join("\n");
      if (!data || data === "[DONE]") continue;
      events.push({ t: Math.round(performance.now() - started), chunk: JSON.parse(data) });
    }
    process.stdout.write(`\r  ${name} · ${model}: ${events.length} chunks`);
  }
  process.stdout.write("\n");

  const error = events.find((e) => e.chunk.type === "error");
  const verdict = events.findLast((e) => e.chunk.type === "data-verdict");
  if (error || !verdict) {
    console.log(`  not saved: ${error ? error.chunk.errorText : "the stream ended without a verdict"}`);
    return;
  }
  const dir = path.join(RECORDINGS, name);
  mkdirSync(dir, { recursive: true });
  const recorded_at = new Date().toISOString();
  writeFileSync(path.join(dir, `${slug(model)}.json`), JSON.stringify({ model, fixture: name, recorded_at, events }));
  const v = verdict.chunk.data;
  const checks = v.checks.map((c) => (c.ok ? "✓" : "✗")).join("");
  console.log(`  saved: ${v.verdict.category} ${v.verdict.confidence} · citations ${checks || "none"} · ${v.route}`);
}

/** Re-check a recording's verdict against the fixture, keeping what the run spent. */
async function refresh(name, file) {
  const full = path.join(RECORDINGS, name, file);
  const r = JSON.parse(readFileSync(full, "utf8"));
  const ev = r.events.findLast((e) => e.chunk.type === "data-verdict");
  if (!ev) return;
  const old = ev.chunk.data;
  const checked = await (
    await api(`/api/fixtures/${encodeURIComponent(name)}/check`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ verdict: old.verdict }),
    })
  ).json();
  ev.chunk.data = { model: old.model, ...checked, tokens: old.tokens };
  writeFileSync(full, JSON.stringify(r));
}

/** What a recording file adds to the snapshot: enough to list it without opening it. */
function describe(name, file) {
  const r = JSON.parse(readFileSync(path.join(RECORDINGS, name, file), "utf8"));
  const v = r.events.findLast((e) => e.chunk.type === "data-verdict")?.chunk.data;
  return {
    model: r.model,
    path: `demo/recordings/${name}/${file}`,
    recorded_at: r.recorded_at,
    duration_ms: r.events.at(-1)?.t ?? 0,
    category: v?.verdict.category ?? null,
    evidence_ok: v?.evidence_ok ?? null,
    comment: v?.action?.comment ?? null,
  };
}

const fixtures = (await (await api("/api/fixtures")).json()).filter((f) => f.source === "corpus");
const known = new Set(fixtures.map((f) => f.name));
for (const name of runs) if (!known.has(name)) die(`no corpus run named ${name}`);

for (const name of runs) {
  for (const model of opts.model ?? MODELS) {
    if (!opts.force && existsSync(path.join(RECORDINGS, name, `${slug(model)}.json`))) {
      console.log(`  ${name} · ${model}: already recorded (--force to redo)`);
      continue;
    }
    await record(name, model);
  }
}

const details = {};
for (const f of fixtures) {
  details[f.name] = await (await api(`/api/fixtures/${encodeURIComponent(f.name)}`)).json();
}
const recordings = {};
for (const name of existsSync(RECORDINGS) ? readdirSync(RECORDINGS) : []) {
  if (!known.has(name)) continue;
  const files = readdirSync(path.join(RECORDINGS, name)).filter((f) => f.endsWith(".json")).sort();
  for (const f of files) await refresh(name, f);
  if (files.length) recordings[name] = files.map((f) => describe(name, f));
}

mkdirSync(path.dirname(SNAPSHOT), { recursive: true });
writeFileSync(
  SNAPSHOT,
  JSON.stringify({ captured_at: new Date().toISOString(), fixtures, details, recordings }, null, 1) + "\n",
);
const count = Object.values(recordings).reduce((n, r) => n + r.length, 0);
console.log(`snapshot: ${fixtures.length} runs, ${count} recordings across ${Object.keys(recordings).length} of them`);

function die(message) {
  console.error(message);
  process.exit(1);
}
