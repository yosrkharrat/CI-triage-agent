---
name: ci-triage
description: A transit map for failed CI runs. Every run rides one line through five stations to a terminus, and every quoted log line is checked where it claims to be.
colors:
  bg: "#0b1230"
  panel: "#0f1a3e"
  sunken: "#070c22"
  fg: "#f4f1ea"
  muted: "#a3adc9"
  faint: "#7d88ab"
  line: "rgb(244 241 234 / 0.14)"
  line-strong: "rgb(244 241 234 / 0.32)"
  accent: "#9db8ff"
  ok: "#6fdc95"
  ok-bg: "rgb(34 177 76 / 0.16)"
  warn: "#ffd257"
  warn-bg: "rgb(255 194 14 / 0.14)"
  bad: "#ff6b72"
  bad-bg: "rgb(226 29 45 / 0.18)"
  cat-regression: "#e21d2d"
  cat-flaky: "#ffc20e"
  cat-infra: "#2f6bff"
  pill-regression: "#c8192a"
  pill-infra: "#2558e0"
  cat-dependency: "#22b14c"
  cat-unknown: "#8b93ad"
typography:
  display:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "84px"
    fontWeight: 700
    lineHeight: 0.92
    letterSpacing: "0.01em"
  headline:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "40px"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "0.01em"
  lede:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "30px"
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: "normal"
  title:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "0.04em"
  numeral:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "44px"
    fontWeight: 600
    lineHeight: 1
    fontFeature: "\"tnum\" 1"
  plate:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.5
    letterSpacing: "0.06em"
  label:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "0.08em"
  label-sm:
    fontFamily: "Barlow Condensed, Arial Narrow, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.64
    letterSpacing: "0.08em"
  body:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.625
    letterSpacing: "normal"
  body-sm:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.375
    letterSpacing: "normal"
  summary:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 500
    lineHeight: 1.375
  mono:
    fontFamily: "JetBrains Mono, ui-monospace, monospace"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.6
rounded:
  sm: "4px"
  md: "6px"
  full: "9999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "20px"
  "10": "40px"
  "12": "48px"
  "20": "80px"
components:
  plate-primary:
    backgroundColor: "{colors.fg}"
    textColor: "{colors.bg}"
    typography: "{typography.plate}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  plate-outline:
    backgroundColor: "transparent"
    textColor: "{colors.fg}"
    typography: "{typography.plate}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  plate-stop:
    backgroundColor: "{colors.bad}"
    textColor: "{colors.bg}"
    typography: "{typography.plate}"
    rounded: "{rounded.sm}"
    padding: "8px 16px"
  line-pill-regression:
    backgroundColor: "{colors.pill-regression}"
    textColor: "{colors.fg}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  line-pill-flaky:
    backgroundColor: "{colors.cat-flaky}"
    textColor: "{colors.bg}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  line-pill-infra:
    backgroundColor: "{colors.pill-infra}"
    textColor: "{colors.fg}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  line-pill-dependency:
    backgroundColor: "{colors.cat-dependency}"
    textColor: "{colors.bg}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  line-pill-unknown:
    backgroundColor: "{colors.cat-unknown}"
    textColor: "{colors.bg}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  line-bullet:
    rounded: "{rounded.full}"
    height: "3px"
    width: "16px"
  replay-tag:
    backgroundColor: "{colors.fg}"
    textColor: "{colors.bg}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  status-tag:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "1px 6px"
  filter-chip:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "4px 8px"
  filter-chip-on:
    backgroundColor: "{colors.fg}"
    textColor: "{colors.bg}"
  input-search:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.fg}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  copy-button:
    backgroundColor: "transparent"
    textColor: "{colors.muted}"
    typography: "{typography.label-sm}"
    rounded: "{rounded.sm}"
    padding: "4px 10px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.fg}"
    rounded: "{rounded.md}"
    padding: "20px"
  panel-header:
    typography: "{typography.label}"
    padding: "12px 20px"
  log-well:
    backgroundColor: "{colors.sunken}"
    textColor: "{colors.fg}"
    typography: "{typography.mono}"
    rounded: "{rounded.md}"
    padding: "12px"
---

# Design System: ci-triage

## Overview

**Creative North Star: "The Transit Diagram"**

Every failed CI run rides one line through five stations (Failed run, Investigate, Verdict, Verify quotes, Route) and arrives at one of three termini: posted on the PR, held for a person, or blocked. The rail map is the explanation on the home page, the live tracker on the run page, and the status vocabulary everywhere else: categories are lines, decisions are interchanges, a quote is a segment of line between two stations in the log. The whole surface is a lit enamel mural at night: a midnight navy ground, porcelain type and track, and five saturated line inks that appear only where a category is named.

Density is that of a working transit board, not a marketing page. Type is set like signage: condensed caps for anything that names a place or an action, a plain grotesk for reading. Proof is shown in place, with real log lines in a recessed well and the cited span marked by the same rail-and-station drawing the map uses. Depth is tonal, never shadowed. The system refuses the neutral dev-tool dashboard of feature cards; the map carries the explanation instead.

One identity serves both the public demo and the local dashboard, and one theme serves both: the ground is dark on purpose (`color-scheme: dark`) and does not flip with the OS.

**Key Characteristics:**
- Midnight enamel ground with porcelain type, rules and track; one dark theme only.
- Five category inks used as line strokes, line pills and line bullets, and for nothing else.
- Barlow Condensed caps for signage (stations, section titles, buttons, labels, table heads); Barlow for prose; JetBrains Mono for machine-written strings.
- A solid porcelain line means reached or verified; a dashed or broken line means not yet, or not found.
- Flat, tonal depth: three enamel tones and porcelain hairlines, no shadows.
- Small, squared corners (4px on controls, 6px on containers); round forms are reserved for stations, track ends and dots.

## Colors

A dark enamel field with porcelain foreground and a rail map's five line inks; status signals have their own lighter tones so they never read as a category.

### Primary
- **Porcelain** (`fg`): all primary type, the trunk track and filled stations on the map, the primary plate's face, the solid quote segment in the log gutter, and the "on" state of toggles. It is both the ink of text and the material of the line.

### Secondary
- **Signal Cobalt** (`accent`): links, focus rings, input focus borders, and the active-station pulse while a triage streams. A pale cobalt chosen to read on navy; it is the only interactive color that is not porcelain.

### Tertiary (line inks)
- **Regression Scarlet** (`cat-regression`): the regression line, pill and bullet.
- **Flaky Amber** (`cat-flaky`): the flaky line, pill and bullet.
- **Infra Cobalt** (`cat-infra`): the infra line, pill and bullet.
- **Dependency Green** (`cat-dependency`): the dependency line, pill and bullet.
- **Unknown Slate** (`cat-unknown`): the unknown line, pill and bullet.

Text on a pill takes Midnight Enamel on the light inks (flaky, dependency, unknown) and Porcelain on the dark inks (regression, infra). Those two pills fill with a step-deeper shade (`pill-regression` #c8192a, `pill-infra` #2558e0) so their 11px caps clear 4.5:1; the map's lines and bullets keep the brighter inks.

### Status signals
- **Clear Green** (`ok`, wash `ok-bg`): every quote found, the "Posted on the PR" terminus, auto-post and posted tags, citation counts that fully verify.
- **Caution Amber** (`warn`, wash `warn-bg`): the "Held for a person" terminus, human-review and awaiting-review tags, the demo's "Recorded runs · no live model" dot.
- **Stop Red** (`bad`, wash `bad-bg`): the "Blocked" terminus, quotes not found, failed stations, the Stop plate, error notices on their wash, model citation rates below 80%.

### Neutral
- **Midnight Enamel** (`bg`): the page ground, the field inside the live tracker, input fields, and the navy type on porcelain plates and light-ink pills.
- **Enamel Plate** (`panel`): raised containers (panels, the verdict card, eval cards, the PR comment), and the row hover on tables.
- **Log Well** (`sunken`): recessed displays for machine output: log excerpts, the workflow YAML, tool output, the prompt.
- **Porcelain Hairline** (`line`): every border and divider on containers, tables and figure captions.
- **Strong Hairline** (`line-strong`): control outlines (outline plate, filter chips, inputs, copy button), unreached track and station strokes, blockquote rules.
- **Slate Mist** (`muted`): secondary prose, reasoning, captions, nav links at rest.
- **Faint Slate** (`faint`): table heads, line numbers, model ids, empty-state dashes, idle station names. Holds about 4.8:1 on the plate and 5.2:1 on the ground; it is the floor for readable text.

### Named Rules
**The Line Ink Rule.** The five category inks mean the five categories and nothing else. They draw category lines, pills, bullets and the run marker once a verdict names a line; they never decorate, never mark status, never tint a surface.

**The Signal Is Not the Line Rule.** Posted, held and blocked use their own signal tones (`ok`, `warn`, `bad`), lighter than the line inks, so a green terminus never reads as the dependency line and a red failure never reads as a regression.

**The One Enamel Rule.** One dark theme. The ground is midnight enamel in every context; there is no light mode and no OS flip.

## Typography

**Display Font:** Barlow Condensed (with Arial Narrow, sans-serif)
**Body Font:** Barlow (with ui-sans-serif, system-ui, sans-serif)
**Label/Mono Font:** JetBrains Mono (with ui-monospace, monospace)

**Character:** Barlow is drawn from California highway and transit signage, so its condensed cut in caps reads as station names and wayfinding, while the regular cut carries sentences calmly. JetBrains Mono appears only where a machine wrote the string.

All three load through `next/font` (Barlow 400/500/600, Barlow Condensed 500/600/700, JetBrains Mono 400/500) and are exposed as `--font-sans`, `--font-display` and `--font-mono`.

### Hierarchy
- **Display** (700, 84px from 640px up and 56px below, line-height 0.92, uppercase): the one home-page question, "Why is CI red?".
- **Headline** (600, 40px from 640px up and 34px below, line-height 1.25, sentence case): the run page's repo and workflow name; the local Corpus title uses the same face at 34px in caps.
- **Lede** (500, 30px from 640px up and 26px below, line-height 1.25, muted, max 34ch): the single sentence under the display line.
- **Title** (600, 22px, uppercase, tracking 0.04em): section titles ("What lands on the pull request", "Add it to a repo", "Eval sweeps").
- **Numeral** (Barlow Condensed 600, 44px on the verdict card and 40px on the results board, line-height 1, tabular): confidence and citation rates; the percent sign drops to half size in muted.
- **Summary** (Barlow 500, 18px, line-height 1.375, max 70ch, balanced): the verdict's one-line answer.
- **Body** (Barlow 400, 15px base, 14.5px for section prose, line-height 1.625, max 60 to 75ch): explanations, reasoning, captions in muted.
- **Body small** (Barlow 400, 13 to 13.5px, line-height 1.375): station details, notes, departures, metadata.
- **Label** (Barlow Condensed 500 or 600, 11 to 15px, uppercase, tracking 0.08em): station names (15px), panel and card headings (14 to 15px), nav links (13px), table heads, tags and pills (10.5 to 11.5px).
- **Plate** (Barlow Condensed 600, 15px, uppercase, tracking 0.06em): button faces.
- **Mono** (JetBrains Mono 400, 12px, line-height 1.6 to 1.65, never wraps mid-token): log lines, YAML, tool output, file paths, SHAs, branch names, model ids.

### Named Rules
**The Signage Rule.** Anything that names a place or an action is Barlow Condensed in caps with open tracking: stations, termini, section titles, panel headings, buttons, tags, table heads, nav. Anything meant to be read as a sentence is Barlow in sentence case.

**The Machine String Rule.** JetBrains Mono is for strings a machine wrote: log lines, YAML, tool calls and output, paths, SHAs, branches, model ids, and counts inside machine tables. Never for prose or headings; headline figures such as confidence and citation rates are Numerals.

**The Tabular Figures Rule.** Every figure that changes or is compared (confidence, citation counts, line numbers, tokens, run counts) is set with tabular numerals.

## Layout

A single centered column, 1152px max (`max-w-6xl`) with a 16px side gutter, under a full-bleed header separated by one hairline. The home page stacks sections 80px apart; the run page stacks them 40px apart. Prose is held to 60 to 75ch even inside wide columns.

The home hero is a two-column grid from 1024px up (about 1.05fr to 1fr, 48px column gap, bottom-aligned): the display question and lede on the left, the explanatory paragraph and the two plates on the right. The line map runs the full column width beneath it, 48px down. Below 1024px the columns stack.

The line map is a single 1000-unit SVG viewBox scaled to the column, with HTML labels placed at the same percentage coordinates so type stays at its real size while the drawing scales. Below 1024px the same line is redrawn as a vertical rail down the left gutter (a 28px column, a 5px rail), with the category lines entering the Verdict station as short spurs from the side.

The runs board is a full table from 768px up and a stacked list of runs below it, each with its line bullet and only the models that answered. Containers use a 20px inner padding with 12px by 20px headers; tables use 16px cells with 10 to 12px vertical padding. The local run page puts the ask panel in a sticky 22rem column beside the live triage from 1024px up.

Breakpoints are Tailwind's defaults: 640px (type steps up), 768px (table replaces the list), 1024px (map replaces the rail; hero and live layouts go two-column).

## Elevation & Depth

Flat. There are no box shadows anywhere in the system. Depth is three enamel tones and a porcelain hairline: the recessed Log Well below the ground, the Midnight Enamel ground, the raised Enamel Plate above it, each edged with a 1px `line` border. Interaction never lifts a surface; hover changes border or text color, or the row tone to `panel`.

The only layering effect is the run marker's halo on the map: an 11-unit disc of the ground color at 85% opacity under the 7-unit marker, so the train reads cleanly where it crosses the track.

### Named Rules
**The Tonal Enamel Rule.** Raise with `panel`, recess with `sunken`, edge with a hairline. Never a drop shadow, never a glow.

## Shapes

Squared-off enamel signs with small corners: 4px (`rounded.sm`) on everything a hand presses or reads as a tag (plates, pills, chips, inputs, the copy button, the replay tag, status tags), 6px (`rounded.md`) on everything that holds content (panels, cards, log wells, the runs board, notices).

Round forms belong to the rail: station circles, the Route interchange circle, line bullets, track caps, termini end bars, citation-rate bars and status dots are fully rounded. The Verdict interchange is a capsule (a 56 by 26 rect with a 13 radius), the one place where the five lines meet the trunk.

Strokes have a fixed vocabulary: 6-unit porcelain track when reached, 3-unit dashed (2 on, 9 off) `line-strong` track when not; 5-unit category lines, 7 for the line a verdict names; 3.5 to 4-unit station rims. In the log gutter the quote segment is a 3px rail with a 9px station at each end.

## Components

### Plates (buttons)
Enamel signs you press: squared, condensed caps, no ornament.
- **Shape:** gently squared (4px), 8px by 16px padding, an optional 16px inline SVG arrow after the label.
- **Primary plate:** porcelain face, navy type. One per view at most ("Replay a triage", "Replay the triage", "Run triage live").
- **Outline plate:** transparent face, porcelain type, 1px `line-strong` border that turns porcelain on hover ("Add it to a repo").
- **Stop plate:** Stop Red face, navy type; replaces the primary plate while a triage streams.
- **Hover / Focus:** primary and stop dim to 90% opacity; outline sharpens its border. Focus is the global 2px Signal Cobalt outline at a 2px offset. Disabled is 50% opacity.
- **Copy button:** a small outline tag (label caps, 11.5px, 4px by 10px), muted until hover, announcing "Copied" politely.

### Line pills, bullets and filter chips
- **Line pill:** the category name in label caps (11px, 600) on a solid block of its ink, 4px corners, 1px by 6px padding. Dimmed to 55% when a model's category differs from the human label.
- **Line bullet:** a 16 by 3px stroke of the ink, fully rounded, leading each run row as a timetable marks a line; `line-strong` when no model named one.
- **Filter chip:** an outline tag in `line-strong` with a 10px bullet of the ink; pressed, it becomes a solid pill of that ink. Flag chips ("Has a replay", "Models disagree", "A quote not found") turn solid porcelain with navy type when pressed.
- **Replay tag:** a tiny porcelain plate with a play triangle, linking straight to the recorded run.

### Status tags
Outline-only tags in label caps (10.5px), 4px corners: border at 50% of the signal tone, type in the tone. Posted and approved in Clear Green, awaiting review and human review in Caution Amber, running in Signal Cobalt, failed in Stop Red, everything neutral in `line` and muted.

### Panels and cards
- **Corner Style:** 6px.
- **Background:** Enamel Plate on Midnight Enamel.
- **Shadow Strategy:** none; see Elevation & Depth.
- **Border:** 1px `line`.
- **Internal Padding:** 20px; the header strip is 12px by 20px with a label-caps title and an optional aside, split from the body by a hairline. Sections inside a card are divided by hairlines, not gaps.

### Inputs / Fields
- **Style:** Midnight Enamel field inside a 1px `line-strong` border, 4px corners, 8px by 12px padding, 14px Barlow; placeholders in Faint Slate. The model select is the same in 12px mono.
- **Focus:** the border turns Signal Cobalt (no outline ring on the field itself).
- **Disabled:** 60% opacity.

### Navigation
A single hairline-ruled header row: the wordmark "ci-triage" in lowercase Barlow Condensed 600 at 20px, then label-caps links at 13px in muted that turn porcelain on hover. In the demo, a right-aligned outline tag with a Caution Amber dot states "Recorded runs · no live model" (hidden below 640px).

### The Line (signature)
The pipeline map and tracker are one component. At rest (home) every station is filled porcelain, every line lit, and each station carries its one-sentence detail. Tracking a run, unreached track is dashed `line-strong`, the active station pulses in Signal Cobalt, done stations fill porcelain, a failed station shows a Stop Red rim and cross. When the verdict lands, the named category line thickens to full opacity while the others fall to 14%, and the branches that were not taken fade to 30%. Termini end in a 6-unit vertical bar in their signal tone with the terminus name in label caps beside it.

### The Train (run marker)
The run itself, riding the drawn track from the station it last reached to the next (round the bend at Route too) with SVG motion: 520ms per segment, capped at 1400ms, on an expo-out curve (`0.16 1 0.3 1`). It is porcelain until a verdict names a category, then takes that line's ink. Under reduced motion it stands at the station reached.

### Log excerpt (quote as a segment)
A recessed Log Well with a caption strip (verification mark, mono path, line span "of N", job name), then the real log around the quote as a table: faint right-aligned line numbers, a 16px gutter, the line text. The cited lines carry a segment of line in the gutter: a solid 3px porcelain rail with a station at each end when the quote was found, a dashed Stop Red rule with hollow stations when it was not. Cited lines are porcelain, context lines muted; hovering the excerpt dims the context to 40% so the measured span owns the display. A footer repeats the quote on one line and why it matters.

### Verification mark
A 12px station glyph with a label-caps caption: a filled Clear Green disc for "Found in log", a dashed Stop Red ring struck through for "Not in log".

### Departures
"What happens next" is a definition list set like a departures board: the entry point in muted, the terminus in label caps in its signal tone behind a 4px vertical bar, then the reason in muted body small. Results by model use the same board: mono model id, a muted record line, a 5px rate bar (porcelain at 80% or more, Stop Red below), and the rate as a Numeral.

### PR comment
The Action's real comment rendered from its markdown on an Enamel Plate: a caption with a neutral avatar disc, "github-actions[bot]" and "commented on the pull request", then the body at 14px with muted hairline-ruled blockquotes. It never imitates GitHub's own chrome.

### Motion
Transitions are short and state-driven: track and station fill/stroke 250ms, line and terminus opacity 300ms ease-out, context dimming 200ms, chevron rotation 150ms, the active-station ping. Under `prefers-reduced-motion` the train stands still, pings are suppressed and smooth scrolling is turned off.

### Named Rules
**The Broken Line Rule.** Solid porcelain means reached or verified; dashed or broken means not yet or not found. The map's unreached track, the struck-through "Not in log" mark and the dashed quote segment all speak this one grammar.

**The Same Line Everywhere Rule.** The line on the home page, the tracker on the run page, the vertical rail on a phone and the segment in the log gutter are one drawing. A new surface that needs to show progress or proof reuses it instead of inventing a stepper, a progress bar or a check icon.

## Do's and Don'ts

### Do:
- **Do** set the ground in Midnight Enamel and raise content on Enamel Plate with a 1px Porcelain Hairline border; recess machine output in the Log Well.
- **Do** use a category's ink only to name that category: its line, its pill, its bullet, the train after the verdict.
- **Do** mark posted, held and blocked with the signal tones (`ok`, `warn`, `bad`) and a short vertical bar, never with line inks.
- **Do** set stations, section titles, buttons, tags and table heads in Barlow Condensed caps with 0.04 to 0.08em tracking, and prose in Barlow sentence case held to 60 to 75ch.
- **Do** keep JetBrains Mono for machine-written strings, at 12px, scrolling rather than wrapping mid-token.
- **Do** show proof in place: the real log lines around a quote, with the cited span marked by a rail segment and a station at each end.
- **Do** draw what has not happened yet as a dashed `line-strong` stroke, and what failed as a broken Stop Red one.
- **Do** keep corners at 4px on controls and tags and 6px on containers; round shapes belong to stations, bullets and dots.
- **Do** give every motion a reduced-motion state that shows the same end position without travel.

### Don't:
- **Don't** add drop shadows, glows or lifted hover states; depth is tonal.
- **Don't** introduce a light theme or let the palette follow the OS.
- **Don't** use a category ink as decoration, a surface tint, a status color or a link color.
- **Don't** build feature-card grids to explain the product; the line map carries the explanation.
- **Don't** use JetBrains Mono for headings or prose.
- **Don't** invent a second progress vocabulary (steppers, spinners, checkmark icons) where the line, its stations and its marks already say it.
- **Don't** imitate GitHub's interface when showing what the Action posts; show its exact markdown on an Enamel Plate.
