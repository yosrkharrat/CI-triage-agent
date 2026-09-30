import "server-only";

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { parseEnv } from "node:util";

/**
 * The dashboard's one door into the Python service.
 *
 * Every call carries the review token, and every call is made from the
 * Next.js server: the token lets its holder spend model quota and post on
 * pull requests, so it never reaches the browser.
 */

/**
 * A setting from this app's environment, or else from the repo root's
 * `.env.local`, which the Python service reads too.
 *
 * The service and the dashboard share their secrets, so they share the file
 * rather than making the token something to copy between two and keep in
 * step. A value set here wins; an empty one does not, since copying
 * `.env.example` leaves every key present and blank.
 */
export function env(key: string): string | undefined {
  return process.env[key] || shared()[key] || undefined;
}

let sharedCache: Record<string, string> | undefined;
function shared(): Record<string, string> {
  if (sharedCache === undefined) {
    const file = path.join(process.cwd(), "..", ".env.local");
    sharedCache = existsSync(file) ? (parseEnv(readFileSync(file, "utf8")) as Record<string, string>) : {};
  }
  return sharedCache;
}

const BASE = env("CI_TRIAGE_URL") ?? "http://127.0.0.1:8000";

export class AgentError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export function agentFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = env("CI_TRIAGE_REVIEW_TOKEN");
  if (!token) {
    throw new AgentError(503, "CI_TRIAGE_REVIEW_TOKEN is not set, here or in the repo root's .env.local");
  }
  const headers = new Headers(init.headers);
  headers.set("authorization", `Bearer ${token}`);
  return fetch(`${BASE}${path}`, { ...init, headers, cache: "no-store" });
}

export async function agentJson<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await agentFetch(path, init);
  } catch (err) {
    if (err instanceof AgentError) throw err;
    throw new AgentError(502, `cannot reach the ci-triage service at ${BASE}`);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new AgentError(res.status, String(detail));
  }
  return res.json() as Promise<T>;
}
