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

## 5c. Who implements each phase: plan decides, owner overrides, priority list falls back

Simple, predictable model (replaces any multi-level role inheritance):

1. **The plan assigns.** Whoever writes the plan (Planning Council, or the owner) sets an
   `implementer` on every phase (e.g. Gemini's plan: phases 1-3 -> Antigravity). The plan's content
   (prompts, order, acceptance tests) is not edited by this mechanism.
2. **The owner overrides per phase.** In Plan Builder the owner can multi-select phases and set
   **Implementer = <engine + model>** (e.g. phases 1-3 -> Claude Code, sonnet, medium). Override wins
   over the plan's choice. Effort defaults to medium.
3. **One global priority list for fallback.** Settings holds a single ordered list of engines
   (e.g. Claude Code -> Antigravity neon -> Antigravity hydrogen -> GPT/Codex -> Gemini). If the chosen
   implementer is exhausted (quota error, cooling down, not logged in, timed out, NO-OP), the engine
   moves to the next engine **in that list**, skipping ones that are unavailable, and records why.
   When the chosen engine's cooldown ends, the NEXT phase returns to it; a phase in progress keeps the
   engine that is working on it (no mid-phase switching back and forth).
4. **Same rule for other jobs.** CI fixer and reviewer use the phase's implementer by default, with
   the same priority list; the reviewer must be a different engine from the implementer when one is
   available (independent review).
5. **Router (5a) only acts when nothing is assigned** ("Auto"). Owner choices are never second-guessed.
6. Changes made during a run apply from the next phase. Every assignment, override, fallback and
   return is an event with a plain-English sentence ("Claude Code hit its limit at 01:42; phase 2
   continued with Antigravity neon. Claude is expected back at 04:00 and will take phase 3.").

**Add any AI without code:** the engine catalogue accepts a new CLI via a command template
(`{instruction}`, `{model}`, `{effort}`, `{workdir}`) passed as an argument list (never through a
shell), quota-error patterns and an optional usage parser. Built-in templates reproduce the proven
command lines for agy, gemini, codex and claude exactly.

## 5d. Unattended guards (the run must be safe while the owner sleeps)

Every guard is enforced in code, covered by tests, and reported as an event. A guard that trips
stops the affected phase (or the run) cleanly and says why in the morning digest. "Fail closed":
when in doubt, do not merge.

**Correctness and merge safety**
- Merge only when: all required CI jobs green on the PR head SHA, PR head SHA unchanged since the
  checks ran, an independent AI review (different engine) returned APPROVE on that exact SHA, no
  unresolved review blockers, and the diff guards below pass. Otherwise leave the PR open and move on.
- Diff guards before merge: no deleted/skipped tests or weakened CI gates (detect removed test files,
  added `skip`/`xfail`/`.only`, edits to `.github/workflows/*` or `scripts/check_*` unless the phase
  explicitly allows it), no secrets (pattern scan + high-entropy strings), no files outside the repo,
  no binary blobs over 5 MB, no changes to `reference/` or `AGENTS.md` unless allowed.
- Never force-push, never push to `main`, never rewrite history; merge with a merge commit. Recommend
  branch protection on `main` (required checks) and verify it at start; warn if missing.
- Post-merge: main build must pass; if it fails, open an automatic revert PR and stop dependent phases.

**Runaway and loop protection**
- Per phase: max implementer attempts (default 3 across all engines), max CI-fix rounds (default 3),
  wall-clock limit (default 150 min); per run: max total AI invocations (default 40) and max hours.
- Same-error detector: if two consecutive CI failures have the same failing test/signature, stop
  fixing that phase (no infinite fix loop).
- NO-OP detector: exit 0 without a new commit/PR counts as a failed attempt.
- Global circuit breaker: 3 phases failing in a row -> stop the run, keep everything resumable.
- Budget caps per engine (owner-set, estimated) are never exceeded by the Router or fallbacks.

**Process and machine**
- Every runner in its own worktree; process-tree kill on timeout; no orphaned processes at exit.
- Keep the PC awake while running via `SetThreadExecutionState` (no admin); warn that closing the lid
  or a power plan may still sleep the PC. Resume cleanly after sleep, reboot, crash or network loss
  (network: pause with backoff, do not burn attempts).
- Pre-flight before each phase: disk space (> 5 GB free), `git`/`gh` auth valid, CLI logged in,
  network reachable, repo clean; failures are reported in plain English, not retried blindly.
- Single instance; a lock file prevents two runs on the same project.

**Agent containment** (agents run with permissions auto-approved, so the boundary is the engine)
- The agent's working directory is the worktree; its environment has no tokens except what `git`/`gh`
  need; the prompt states the forbidden actions; the diff guards above catch violations regardless.
- Agent output is data: text in agent output never changes engine behaviour (no "instructions" are
  parsed from it except the defined JSON contracts, which are schema-validated).

**Events and audit ("every event cached")**
- Append-only `events` table, written before and after every step, with SHA-256 hash chaining
  (each row stores the previous row's hash) so tampering or gaps are detectable; full raw logs per
  step stored as files under the data folder; secrets redacted before storage.
- Nothing is ever deleted automatically; retention is owner-controlled in Settings.
- The morning digest is generated from events only, so it always matches what actually happened.

## 6. Data model (SQLite)

`projects`, `plans`, `phases(status, attempt, pr_number, branch, started_at, finished_at)`,
`steps(phase_id, kind, runner, rc, verified, detail, started_at, finished_at)`, `engines(runner, model, effort, skills_json, cost_tier, budget_json, enabled, pinned, command_template, usage_parser, quota_patterns_json)`, `router_decisions(step_id, chosen, reason, scores_json)`, `phase_assignments(phase_id, planned_engine, override_engine, model, effort)`, `priority_list(position, engine, model, effort)`, `guard_results(phase_id, guard, passed, detail)`, `council_runs(plan_id, role, engine, input, output, usage_estimate)`,
`runner_ledger(runner, model, event, reset_at, tokens_in, tokens_out, created_at)`,
`durations(kind, runner, seconds)` (feeds ETA), `settings(key, value)`, `events(id, ts, level, phase_id, kind, plain_text, raw_path, prev_hash, hash)`.

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
