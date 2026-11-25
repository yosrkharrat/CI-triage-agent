import { Markdown } from "@/components/markdown";

/**
 * The comment the GitHub Action posts on the pull request, rendered from the
 * exact markdown it would send: the same text a reviewer would read, without
 * dressing it up as GitHub's own interface. With the default `github.token`
 * the comment's author is the github-actions bot, so that is the name shown.
 */
export function PrComment({ body, note }: { body: string; note?: string }) {
  return (
    <figure className="overflow-hidden rounded-md border border-line bg-panel">
      <figcaption className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-4 py-2.5">
        <span aria-hidden className="size-6 shrink-0 rounded-full bg-line-strong" />
        <span className="whitespace-nowrap text-[14px] font-semibold">github-actions[bot]</span>
        <span className="text-[13px] text-muted">commented on the pull request</span>
      </figcaption>
      <div className="comment-body px-5 py-4 [&_blockquote]:border-l-2 [&_blockquote]:border-line-strong [&_blockquote]:pl-3 [&_blockquote]:text-muted [&_details]:space-y-2 [&_p]:text-[14px]">
        <Markdown html>{body}</Markdown>
      </div>
      {note && <p className="border-t border-line px-4 py-2 text-[12.5px] text-muted">{note}</p>}
    </figure>
  );
}
