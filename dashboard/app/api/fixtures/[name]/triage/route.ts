import { UI_MESSAGE_STREAM_HEADERS } from "ai";
import type { NextRequest } from "next/server";

import { AgentError, agentFetch } from "@/lib/agent";

// A triage is several model round trips; on a free tier, a rate-limit wait
// can sit in the middle of one.
export const maxDuration = 300;

/**
 * Relay a live triage from the Python agent to the browser.
 *
 * The stream is already in the AI SDK's UI message protocol — pydantic-ai
 * speaks it — so this adds the token and passes the bytes through untouched.
 */
export async function POST(req: NextRequest, ctx: RouteContext<"/api/fixtures/[name]/triage">) {
  const { name } = await ctx.params;
  const model = req.nextUrl.searchParams.get("model");
  const query = model ? `?model=${encodeURIComponent(model)}` : "";
  let upstream: Response;
  try {
    upstream = await agentFetch(`/api/fixtures/${encodeURIComponent(name)}/triage${query}`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: await req.text(),
      signal: req.signal,
    });
  } catch (err) {
    const status = err instanceof AgentError ? err.status : 502;
    return new Response(err instanceof Error ? err.message : "service unreachable", { status });
  }
  if (!upstream.ok || !upstream.body) {
    return new Response(await upstream.text(), { status: upstream.status || 502 });
  }
  return new Response(upstream.body, { headers: UI_MESSAGE_STREAM_HEADERS });
}
