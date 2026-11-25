import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

/**
 * A model's answer: lists, emphasis and code, without raw HTML.
 *
 * `html` admits the few tags the Action's PR comment uses (details, summary,
 * sub), through GitHub's own sanitizing schema: the comment is shown as
 * GitHub would render it, and anything a model wrote into it stays inert.
 */
export function Markdown({ children, html = false }: { children: string; html?: boolean }) {
  return (
    <div className="space-y-2 text-sm leading-relaxed [&_a]:text-accent [&_a]:underline [&_li]:ml-4 [&_ol]:list-decimal [&_ol]:space-y-1 [&_strong]:font-semibold [&_ul]:list-disc [&_ul]:space-y-1">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={html ? [rehypeRaw, rehypeSanitize] : []}
        components={{
          pre: ({ children }) => <pre className="log rounded-md bg-sunken p-3">{children}</pre>,
          code: ({ className, children }) =>
            className ? (
              <code className={className}>{children}</code>
            ) : (
              <code className="rounded bg-sunken px-1 py-0.5 font-mono text-[12px]">{children}</code>
            ),
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="text-xs [&_td]:border [&_td]:border-line [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-line [&_th]:px-2 [&_th]:py-1">
                {children}
              </table>
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
