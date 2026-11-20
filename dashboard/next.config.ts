import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Replay mode reads its snapshot from disk; a page rendered on demand on a
  // host like Vercel only sees the files its trace names.
  outputFileTracingIncludes: {
    "/*": ["./demo/**/*.json"],
  },
};

export default nextConfig;
