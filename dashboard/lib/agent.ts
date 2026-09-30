import "server-only";

/**
 * The dashboard's one door into the Python service.
 *
 * Every call carries the review token, and every call is made from the
 * Next.js server: the token lets its holder spend model quota and post on
 * pull requests, so it never reaches the browser.
 */
const BASE = process.env.CI_TRIAGE_URL ?? "http://127.0.0.1:8000";

export class AgentError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export function agentFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = process.env.CI_TRIAGE_REVIEW_TOKEN;
  if (!token) {
    throw new AgentError(503, "CI_TRIAGE_REVIEW_TOKEN is not set for the dashboard");
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
