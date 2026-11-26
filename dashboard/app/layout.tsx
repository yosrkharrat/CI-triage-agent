import type { Metadata } from "next";
import { Barlow, Barlow_Condensed, JetBrains_Mono } from "next/font/google";
import Link from "next/link";

import { DEMO } from "@/lib/demo";
import "./globals.css";

// Barlow is drawn from California's highway and transit signage: the map's
// station labels are set in its condensed cut, everything else in the regular.
const barlow = Barlow({ variable: "--font-barlow", subsets: ["latin"], weight: ["400", "500", "600"] });
const barlowCondensed = Barlow_Condensed({
  variable: "--font-barlow-condensed",
  subsets: ["latin"],
  weight: ["500", "600", "700"],
});
const mono = JetBrains_Mono({ variable: "--font-jetbrains-mono", subsets: ["latin"], weight: ["400", "500"] });

export const metadata: Metadata = {
  title: "ci-triage: why is CI red?",
  description: "An agent that diagnoses failed GitHub Actions runs and checks every log line it quotes.",
};

const REPO = "https://github.com/yosrkharrat/CI-triage-agent";

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${barlow.variable} ${barlowCondensed.variable} ${mono.variable} antialiased`}>
      <body className="min-h-screen font-sans text-[15px]">
        <header className="border-b border-line">
          <nav className="mx-auto flex max-w-6xl items-center gap-6 px-4 py-3.5 text-sm">
            <Link href="/" className="font-display text-xl font-semibold tracking-tight">
              ci-triage
            </Link>
            {DEMO ? (
              <>
                <Link href="/runs" className="label text-[13px] text-muted hover:text-fg">
                  Fix chains
                </Link>
                <Link href="/#install" className="label text-[13px] text-muted hover:text-fg">
                  Install
                </Link>
                <a href={REPO} className="label text-[13px] text-muted hover:text-fg" target="_blank" rel="noreferrer">
                  Source
                </a>
                <span className="label ml-auto hidden items-center gap-2 rounded-sm border border-line px-2.5 py-1 text-[11px] text-muted sm:inline-flex">
                  <span className="size-1.5 rounded-full bg-warn" />
                  Recorded runs · no live model
                </span>
              </>
            ) : (
              <>
                <Link href="/" className="label text-[13px] text-muted hover:text-fg">
                  Corpus
                </Link>
                <Link href="/runs" className="label text-[13px] text-muted hover:text-fg">
                  Review queue
                </Link>
              </>
            )}
          </nav>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-10">{children}</main>
      </body>
    </html>
  );
}
