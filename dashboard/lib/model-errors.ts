/**
 * What went wrong with a model call, in words a person can act on.
 *
 * The AI SDK masks every stream error as "An error occurred." by default,
 * which hides the one failure a free tier makes routine: a spent quota.
 * Provider messages carry an organisation id, so the raw text is never sent.
 */
export function explainModelError(error: unknown, model: string): string {
  const text = error instanceof Error ? `${error.message} ${String(error.cause ?? "")}` : String(error);
  const m = text.match(/try again in ((?:\d+h)?(?:\d+m)?)([\d.]+)s/i);
  const wait = m ? `${m[1]}${Math.round(Number(m[2]))}s` : null;
  const after = wait ? ` It frees up again in about ${wait}.` : "";
  if (/tokens per day|\(TPD\)/i.test(text)) {
    return `Groq's daily token limit for ${model} is used up — eval sweeps draw on the same quota.${after}`;
  }
  if (/rate limit|429/i.test(text)) {
    return `Groq is rate-limiting ${model} for the moment.${after} Try again shortly.`;
  }
  if (/request too large|413/i.test(text)) {
    return `The conversation has grown past what ${model} accepts per minute on the free tier. Start a new question.`;
  }
  if (/api key|401|unauthorized/i.test(text)) {
    return "The Groq API key is missing or rejected. Set GROQ_API_KEY in the repo root's .env.local.";
  }
  return "The model call failed. The dashboard's server log has the details.";
}
