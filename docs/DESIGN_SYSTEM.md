# Night-Shift Desktop: Design System

Direction: **"Mission control at night"**. Deep-space dark canvas, frosted glass panels, a slow
aurora glow behind content, crisp cyan/violet light accents, tabular monospaced numbers. Calm when
idle, alive when working. It must look like a premium 2026 desktop product, not a web admin template.

Tokens live in `design/tokens.json` and are generated into `frontend/src/design/tokens.ts` and the
Tailwind theme (`npm run tokens`). **UI code may use only semantic/component tokens.** Raw hex/rgb,
arbitrary Tailwind values (`bg-[#...]`, `p-[13px]`) and emoji are rejected by CI.

## 1. Foundations

- **Canvas:** `bg.canvas` with an animated aurora layer (three blurred radial gradients `aurora.a/b/c`
  drifting over `duration.ambient`; paused when the window is unfocused or `prefers-reduced-motion`).
  A faint 1px grid (`border.subtle`, 32px) at 40% opacity fades out toward the bottom.
- **Glass:** `surface.glass` + `backdrop-filter: blur(component.card.blur) saturate(140%)` +
  1px `border.subtle` + inner top highlight (1px white at 6%). Raised/modal = `surface.glass-strong`.
- **Type:** UI `font.sans`; numbers, ETAs, durations, log lines `font.mono` with tabular digits;
  hero numbers and page titles `font.display`. Scale = `size.*`. Body `md`, labels `sm`, captions `xs`.
  Sentence case everywhere. Max line length 72ch.
- **Spacing:** 4px grid (`space.*`) only. Card padding `space.6`, gaps between cards `space.5`.
- **Radius:** controls `md`, cards `lg`, modals `xl`, chips `pill`.
- **Motion:** enter = fade + 8px rise, `duration.base`, `easing.out`; layout changes animate with
  Framer `layout`; progress values tween, never jump; live "working" state = slow breathing glow on the
  active card border (`border.glow`, 2.4s). Respect `prefers-reduced-motion` (opacity only).
- **Icons:** lucide-react, 1.75 stroke, 16/20/24px. No emoji anywhere (text, labels, logs shown in UI).
- **Themes:** dark (default) and light, both from tokens; switch in Settings; follow system by default.
- **Contrast:** WCAG 2.2 AA minimum for all text (4.5:1) and UI boundaries (3:1), in both themes.

## 2. Shell layout

```
+--------------------------------------------------------------------------------+
| [logo] Night-Shift   project switcher v     status pill: RUNNING   [cmd K] [_][x]|  topbar 52px (custom, draggable)
+-----------+--------------------------------------------------------------------+
| Sidebar   |  Page header: title (display, 2xl) + one-line plain explanation    |
|  core     |                                                                    |
|  - Mission|  Page content (cards grid, 12 cols, max 1440px, centred)           |
|  - Plan   |                                                                    |
|  - Runs   |                                                                    |
|  - Engines|                                                                    |
|  build    |                                                                    |
|  tools    |                                                                    |
|  -------- |                                                                    |
|  Settings |                                                                    |
+-----------+--------------------------------------------------------------------+
```

- Frameless window with custom title bar (drag region, min/max/close with proper hit targets).
- Sidebar: sections from module manifests; active item = accent pill with soft glow; collapses to
  icon rail below 1100px width or by toggle; live status badge per module (e.g. "2 running").
- Command palette (Ctrl+K): every module command + navigation, fuzzy search.
- Toasts bottom-right, glass, auto-dismiss 5s, never for routine progress.

## 3. Components (each with all 8 states: default, hover, focus-visible, active, disabled, loading, error, empty where relevant)

| Component | Notes |
|---|---|
| `GlassCard` | header (icon + title + optional action), body, footer; variant `hero` has glow border when live |
| `Button` | primary (accent fill, dark text), secondary (glass), ghost, danger (only destructive); sm/md/lg; icon-left; loading spinner keeps width |
| `Toggle`, `Checkbox`, `Select`, `TextField`, `TextArea`, `CodeEditor` (Monaco-lite or CodeMirror 6 for JSON/prompt editing) | labels always visible, helper + error text |
| `StatusPill` | idle / running (breathing dot) / waiting / cooling down / failed / done; icon + text, never colour alone |
| `ProgressRing` | hero 168px with centre value + caption; tweened; accessible `aria-valuenow` |
| `ProgressBar` | segmented per phase for overall plan |
| `PhaseTimeline` | vertical timeline of phases: done (check), current (pulsing), next, blocked (lock, says why) |
| `StepFeed` | plain-language step list ("Antigravity is writing the code", "Checks passed") with relative times; "Details" expands raw log lines in mono |
| `EngineCard` | AI engine: name, model, effort chip, state (ready / cooling until HH:MM / no login), est. quota bar labelled "estimate" |
| `StatTile` | label, big mono value, delta, sparkline |
| `EmptyState` | illustration-free: icon in soft circle, one sentence, one primary action |
| `ConfirmDialog` | for destructive actions; restates what will happen |
| `Kbd`, `Tooltip`, `Tabs`, `SegmentedControl`, `Badge`, `Skeleton` | standard |

A `/gallery` route (dev builds and CI only) renders every component in every state for screenshots.

## 4. Key screens

**Mission Control (home).** Row 1: hero `GlassCard` (span 8) with `ProgressRing` (overall %),
"Now doing" sentence (lg), "Next" line, ETA line "~1 h 20 m left (estimate from past runs)", "Working
AI: Antigravity (Gemini is cooling down until 03:10)", and the always-visible **Stop After Current Phase**
button (secondary; becomes "Will stop after this phase - Undo" when armed). Right column (span 4):
Quota card ("~65% left, estimate from local ledger") per engine, and Problems card (plain English + what
the app is doing). Row 2: `PhaseTimeline` (span 5) + `StepFeed` (span 7). Nothing else.

**Plan Studio (AI Planning Council).** Step 1: a large "Describe what you want built" text area plus
project picker. Step 2: three role cards side by side (Draft - Gemini, Critique - GPT, Final - Claude),
each showing the engine actually used (and why, if substituted), live status, elapsed time and usage
estimate; the active card has the breathing glow. Step 3: plan diff view (draft -> final) with per-AI
notes as margin comments, inline edit, plain-English trade-off summary, and **Approve and send to Plan
Builder** (primary) / Regenerate (secondary).

**Plan Builder.** Left: phase list (drag to reorder, dependency chips, ladder chip, timeout). Right:
editor for the selected phase (title, prompt in CodeEditor, ladder select, branch auto-filled,
auto-merge / release toggles). Top: Validate (shows plain-English problems) and Start run. Import /
export legacy `queue.json`.

**Runs & History.** Table of runs (date, phases done/failed, duration, AI used) -> run detail with the
digest, per-phase timeline, PR links, artifacts.

**AI Engines & Quota.** `EngineCard` grid with skill scores and budget sliders; a "Router decisions" list showing who was chosen for each recent step and why; ladder editor per category (drag order, Claude pinned last
with a note); effort default "medium"; ledger history chart (estimates).

**Settings.** Projects (repo, local path, build rules text, artifact glob, output folder), theme,
autostart (per-user), notifications, keep-awake while running, data folder, about/version.

## 5. Writing (UX copy)

Plain English for a non-programmer. Say what is happening, why, and what happens next. Never show a raw
error as the main message: "Gemini hit its limit. Switching to Antigravity; Gemini is back at 03:10."
Estimates always say "estimate". Buttons are verbs. No exclamation marks, no emoji.
