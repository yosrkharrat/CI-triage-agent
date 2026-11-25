import type { ChatTransport, UIMessage, UIMessageChunk } from "ai";

import type { RecordingFile } from "@/lib/types";

/**
 * Longest pause kept from a recording. A free tier's rate-limit wait can sit
 * in the middle of a triage for half a minute; the replay keeps the rhythm of
 * the run, not its dead air.
 */
const MAX_GAP_MS = 1200;

/** Below this, waiting costs more than it shows: chunks go out together. */
const FRAME_MS = 16;

/**
 * A `useChat` transport that plays a recorded triage back instead of calling
 * the service.
 *
 * The recording holds the exact UI message chunks the Python agent streamed,
 * so the page renders a replay through the same code as a live run.
 */
export class ReplayTransport implements ChatTransport<UIMessage> {
  constructor(private url: string) {}

  async sendMessages({ abortSignal }: { abortSignal: AbortSignal | undefined }): Promise<ReadableStream<UIMessageChunk>> {
    const res = await fetch(this.url, { signal: abortSignal });
    if (!res.ok) throw new Error(`The recording could not be loaded (${res.status}).`);
    const { events } = (await res.json()) as RecordingFile;

    return new ReadableStream<UIMessageChunk>({
      async start(controller) {
        let last = 0;
        let owed = 0;
        for (const { t, chunk } of events) {
          owed += Math.min(Math.max(t - last, 0), MAX_GAP_MS);
          last = t;
          if (owed >= FRAME_MS) {
            await sleep(owed, abortSignal);
            owed = 0;
          }
          if (abortSignal?.aborted) break;
          controller.enqueue(chunk as UIMessageChunk);
        }
        controller.close();
      },
    });
  }

  async reconnectToStream(): Promise<ReadableStream<UIMessageChunk> | null> {
    return null;
  }
}

function sleep(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}
