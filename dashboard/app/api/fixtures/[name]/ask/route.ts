import { createGroq } from "@ai-sdk/groq";
import {
  convertToModelMessages,
  createUIMessageStreamResponse,
  isStepCount,
  streamText,
  tool,
  toUIMessageStream,
  type UIMessage,
} from "ai";
import type { NextRequest } from "next/server";
import { z } from "zod";

import { agentJson, env } from "@/lib/agent";
import type { FixtureDetail } from "@/lib/types";

export const maxDuration = 120;

const MODEL = env("CI_TRIAGE_ASK_MODEL") ?? "openai/gpt-oss-120b";

/**
 * "Ask about this incident": a second agent, next to the one that triages.
 *
 * It lives here rather than in Python because it is a conversation with the
 * person looking at the page, and the AI SDK is built for that. Its tools are
 * Zod-typed on this side and served by the Python agent's own Pydantic-typed
 * tools on the other, so both agents read exactly the same evidence.
 */
export async function POST(req: NextRequest, ctx: RouteContext<"/api/fixtures/[name]/ask">) {
  const { name } = await ctx.params;
  const { messages }: { messages: UIMessage[] } = await req.json();
  const base = `/api/fixtures/${encodeURIComponent(name)}`;

  const groq = createGroq({ apiKey: env("GROQ_API_KEY") });
  const result = streamText({
    model: groq(MODEL),
    instructions: [
      `You answer questions about one failed CI run, the fixture "${name}".`,
      "Ground every claim in what your tools return. When you rely on a log line, quote it",
      "exactly and name the log file and line numbers shown next to it.",
      "If the evidence does not settle a question, say so plainly rather than guessing.",
      "Be brief: a few sentences, or a short list.",
    ].join(" "),
    messages: await convertToModelMessages(messages),
    abortSignal: req.signal,
    stopWhen: isStepCount(6),
    tools: {
      overview: tool({
        description:
          "The run: repository, commit, failed jobs, the human label if it has one, and each model's last verdict.",
        inputSchema: z.object({}),
        execute: async () => {
          const d = await agentJson<FixtureDetail>(base);
          return {
            overview: d.overview,
            label: d.label,
            verdicts: Object.fromEntries(
              Object.entries(d.scores).map(([m, s]) => [m, s.verdict && { ...s.verdict, verified: s.verified, cited: s.cited }]),
            ),
          };
        },
      }),
      getLogs: tool({
        description:
          "Reduced log excerpts for the failed jobs, with line numbers. Omit job for one representative per distinct failure.",
        inputSchema: z.object({ job: z.string().optional().describe("A failed job's exact name") }),
        execute: async ({ job }) =>
          (await agentJson<{ text: string }>(`${base}/logs${job ? `?job=${encodeURIComponent(job)}` : ""}`)).text,
      }),
      getDiff: tool({
        description: "The unified diff of the commit under test, when one was captured.",
        inputSchema: z.object({}),
        execute: async () => (await agentJson<{ text: string }>(`${base}/diff`)).text,
      }),
      getHistory: tool({
        description: "Outcomes of this workflow on this commit and recent others. The evidence for or against flaky.",
        inputSchema: z.object({}),
        execute: async () => (await agentJson<{ text: string }>(`${base}/history`)).text,
      }),
    },
  });

  return createUIMessageStreamResponse({
    stream: toUIMessageStream({ stream: result.stream, originalMessages: messages }),
  });
}
