# Night-Shift Desktop: Blueprint

A Windows desktop app that runs a phased software plan unattended (overnight), using AI coding CLIs
(Antigravity `agy`, Gemini CLI, Codex, Claude Code) with automatic failover, verified deliverables,
CI watching, merge and release. It is the successor of `reference/nightshift_legacy.py` and the
platform the owner will keep extending with app-development modules.

Owner profile: not a programmer. The UI must explain itself at a glance. No admin rights on the PC.

## 1. Non-negotiables

1. **No admin rights.** Runs from `%LOCALAPPDATA%\NightShift`. No services, no drivers, no HKLM, no
   global installs. Autostart only via per-user Task Scheduler or the per-user Startup folder.
2. **Unattended and resumable.** Every step is persisted in SQLite before and after it runs. Killing the
   app mid-phase and restarting continues from the last durable step; nothing is redone that is already
   verifiably done (merged PR, open PR, pushed branch).
3. **Verify, never trust exit codes.** A runner "succeeded" only if its deliverable exists (PR opened,
   new commits on the remote branch, CI fix pushed). Exit 0 without a deliverable = NO-OP -> next runner.
4. **Quota-friendly.** Medium effort for every model by default. Claude is always last in every ladder
   (and absent unless the owner adds it). Quota numbers are estimates and are labelled as such.
5. **Futuristic, consistent UI.** Only the design system in `docs/DESIGN_SYSTEM.md` and
   `design/tokens.json`. CI gates reject raw colours, emoji, and off-system spacing.
6. **Extensible.** Every feature is a module (section 4). New features never require rewriting the shell.

## 2. Stack (pinned, exact versions, lockfiles committed)

| Layer | Choice | Why |
|---|---|---|
| Engine | Python 3.12, `asyncio`, stdlib `sqlite3` (WAL) | Reuses legacy logic; no native build tools needed |
| Desktop shell | `pywebview` (EdgeChromium / WebView2, present on Windows 11) | Native window, no admin, no Rust/MSVC |
| UI | React 18 + TypeScript + Vite | Full design freedom |
| Styling | Tailwind CSS 3 with theme generated from `design/tokens.json` | Tokens are the single source |
| Motion | Framer Motion | Futuristic, physics-based transitions |
| Icons | `lucide-react` only | Consistent stroke icons; emoji are banned |
| State | Zustand | Small, typed |
| Tests | pytest (engine), Vitest + Testing Library (UI), Playwright (screenshots + a11y via axe) | All run on GitHub Actions `windows-latest` |
| Packaging | PyInstaller `--onedir` -> `NightShift.exe` + zipped folder | Per-user, no installer needed |

No network calls except: GitHub (`gh`, `git`) and the AI CLIs the owner already uses. No telemetry.

## 3. Repository layout

```
backend/nightshift/
  app.py            # entry: starts pywebview window + engine thread
  bridge.py         # js_api exposed to the UI (typed, versioned)
  events.py         # event bus -> pushes JSON events to the UI
  store.py          # SQLite schema + migrations
  engine/           # planner, scheduler, phase state machine, verify, ci, release
  runners/          # agy.py, gemini.py, codex.py, claude.py (+ fake.py for tests)
  quota/            # ledger: parses quota errors + reset times, per-run usage, estimates
  modules/          # backend side of each module (registry + manifests)
backend/tests/
frontend/src/
  app/              # shell: window chrome, sidebar, command palette, toasts, routing
  design/           # tokens.ts (generated), primitives, components
  modules/<id>/     # one folder per module (UI side)
  bridge/           # Bridge interface + PyWebviewBridge + MockBridge (fixtures for CI)
frontend/tests/     # vitest + playwright specs
design/tokens.json  # DTCG design tokens (source of truth)
scripts/            # CI gates (no raw colours, no emoji, token validation)
reference/nightshift_legacy.py   # the working legacy orchestrator to port (read-only)
```

## 4. Module system (the extension point)

A module = backend part (optional) + UI part + manifest. The shell builds the sidebar, routes,
command-palette entries and settings sections from the registry; it never hard-codes a module.

```ts
// frontend/src/modules/<id>/manifest.ts
export const manifest: ModuleManifest = {
  id: "mission-control", title: "Mission Control", icon: "Radar",   // lucide name
  order: 10, route: "/", section: "core",                           // core | build | tools
  page: () => import("./Page"),                                     // lazy
  statusBadge?: (s) => ...,                                         // optional live badge in sidebar
  settings?: () => import("./Settings"),
  commands?: [{ id: "stop-after-phase", title: "Stop after current phase", run: (b) => b.call("engine.stopAfterPhase") }],
};
```

```python
# backend/nightshift/modules/<id>.py
class Module(ModuleBase):
    id = "mission-control"
    def api(self) -> dict[str, Callable]: ...      # exposed as bridge.call("<id>.<name>", args)
    def on_event(self, event: Event) -> None: ...  # optional
```

Modules are discovered from `frontend/src/modules/*/manifest.ts` (Vite glob import) and
`backend/nightshift/modules/*.py`. Adding a module = adding one folder on each side + tests.

**v1 modules (core):** Mission Control, Plan Studio (AI Planning Council), Plan Builder, Runs & History, AI Engines & Quota, Settings.
**Planned later (owner will queue them):** CI Monitor, APK/Build Artifacts, Device Installer (adb),
Logcat Viewer, Release Notes Writer, Play Console Upload, Repo Health, Cost/Token Analytics.

## 5. Engine behaviour (port from legacy, improved)

Per project (a project = repo + local path + build rules + artifact rules + ladders):

1. Load plan (phases with ids, prompts, ladder, branch, depends_on, timeout_minutes, auto_merge,
   release_after_merge). Phases form a DAG; a phase runs only when its dependencies are merged.
2. Skip phases already merged (lookup by branch), unless `force_rerun`.
3. If a PR is already open for the branch -> skip implementation and resume at CI watch.
4. Prepare a git worktree under `%TEMP%\nightshift-worktrees` (base = remote branch if it exists,
   else `origin/main`).
5. Run the ladder: for each runner, run with timeout (kill the whole process tree on timeout), then
   verify the deliverable. Quota/rate errors -> record in ledger with reset time -> cooldown -> next runner.
6. Watch CI; on failure, ask the ladder to fix (max N retries), verify that a new commit was pushed.
7. Merge (if auto_merge), wait for post-merge main build, release (if release_after_merge) by
   downloading the build artifact matching the project's `artifact_glob`, publishing a GitHub release
   with SHA256SUMS, copying it to the project's output folder.
8. Write a plain-language morning digest.
9. Honour "Stop after current phase" at every phase boundary.

Runner command lines must match legacy exactly where proven (e.g. `agy -p ... --dangerously-skip-permissions`,
gemini `--skip-trust`, medium effort).

## 5a. Smart, quota-aware routing (every model is used according to its quota)

Ladders are a fallback; the **Router** decides who works on each step.

- **Engine catalogue** (`engines` table, editable in AI Engines & Quota): runner (`agy`, `gemini`,
  `codex` = GPT, `claude`), model, effort (default medium), capabilities with a 1-5 skill score
  (`plan`, `review`, `ui`, `backend`, `fix_ci`, `docs`), cost tier, and an owner-entered budget
  (e.g. "Claude Pro: use at most 20% of the weekly limit for Night-Shift").
- **Availability** per engine: CLI installed + logged in, cooling down until (from the ledger's parsed
  quota errors and reset times), recent NO-OP/failure rate, and estimated budget used (from per-run
  usage in JSON output where the CLI provides it, else run counts x historical average). All estimates.
- **Score** = skill for the step kind x reliability x (1 - budget pressure) - cost tier penalty.
  Claude carries a strong cost penalty so it is chosen only when it is clearly best for the step
  (e.g. final plan review) and its budget allows, or everything else is unavailable.
- Deterministic and explainable: every choice is logged with a one-line reason shown in the UI
  ("GPT chosen: Gemini cooling down until 03:10, Antigravity used 80% of tonight's budget").
- Owner can pin an engine for a phase or exclude an engine globally; pins override the score.

## 5b. AI Planning Council (Gemini, GPT and Claude write the plan)

The owner describes the goal in plain words in **Plan Studio**; three AIs turn it into a phased plan:

1. **Draft - Gemini** (Gemini CLI, medium): reads the goal + a compact repo snapshot (repomix
   `--compress`, token budget capped) and drafts phases (id, title, prompt, depends_on, ladder/kind,
   acceptance tests, timeout) as JSON matching the plan schema.
2. **Critique - GPT** (Codex CLI, medium): reviews the draft for missing steps, unsafe ordering,
   phases too big for one PR (>~1 day of work), vague acceptance tests; returns a JSON list of issues
   and a revised plan.
3. **Final review - Claude** (Claude CLI, medium, single short call, skipped automatically when its
   budget is low): resolves disagreements, checks against BLUEPRINT/AGENTS rules, returns the final
   plan + a plain-English summary of trade-offs.
4. The owner sees a side-by-side diff (draft -> critique -> final) with each AI's notes, edits
   anything, then **Approve** sends it to Plan Builder. Nothing runs without approval.

Each council role is routed through 5a (if an assigned AI is unavailable the next best planner takes
the role, and the UI says so). Outputs are schema-validated; invalid JSON is retried once with the
validation error, then shown to the owner as a problem in plain English. All three outputs and the
token/usage estimate per role are stored with the plan for history.

## 6. Data model (SQLite)

`projects`, `plans`, `phases(status, attempt, pr_number, branch, started_at, finished_at)`,
`steps(phase_id, kind, runner, rc, verified, detail, started_at, finished_at)`, `engines(runner, model, effort, skills_json, cost_tier, budget_json, enabled, pinned)`, `router_decisions(step_id, chosen, reason, scores_json)`, `council_runs(plan_id, role, engine, input, output, usage_estimate)`,
`runner_ledger(runner, model, event, reset_at, tokens_in, tokens_out, created_at)`,
`durations(kind, runner, seconds)` (feeds ETA), `settings(key, value)`, `events(id, ts, level, plain_text, raw)`.

## 7. Plain-language contract (Mission Control)

Always visible on the main card: **Now doing** (one sentence), **Next**, **Overall progress**,
**ETA** ("~1 h 20 m left (estimate from past runs)" or actual), **Which AI and why**
("Antigravity, because Gemini is cooling down until 03:10"), **Quota left (estimate)**, and the
**Stop After Current Phase** control. Problems are written in plain English with what the app is doing
about them. Raw logs only behind a "Details" toggle.

## 8. Quality gates (CI must pass before merge)

- `pytest -q` (engine with fake runners and fake `gh`), coverage of every state transition.
- `npm run lint && npm run typecheck && npm run test` (Vitest).
- `python scripts/check_tokens.py` and `python scripts/check_ui_rules.py` (no raw colours outside
  tokens, no emoji, no inline px except tokens).
- Playwright against `vite preview` with `MockBridge`: screenshot every module page in 3 viewports
  (1440x900, 1024x700, 1440x900 at 150% text) x states (idle, running, failure, empty), axe a11y with
  zero serious violations, no horizontal overflow. Screenshots uploaded as the `ui-screenshots` artifact.
- Windows build job: PyInstaller -> `NightShift-windows.zip` uploaded as artifact `nightshift-windows`.
