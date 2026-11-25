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

No network calls except: GitHub (`gh`, `git`), the AI CLIs the owner already uses, and the optional
phone status bridge (section 5i: off by default, only the owner's own Apps Script URL). No telemetry.

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
7. Merge per section 5d (risk-based review, self-healing; never waits idle) (if auto_merge), wait for post-merge main build, release (if release_after_merge) by
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

## 5c. Who implements each phase: plan decides, owner overrides, priority list keeps it moving

Owner's principle: **no time is ever lost waiting for one AI.** The run keeps moving all night.

1. **The plan assigns** an `implementer` per phase (e.g. Gemini's plan: phases 1-3 -> Antigravity).
   The plan's content is not edited by this mechanism.
2. **The owner overrides per phase** in Plan Builder (multi-select -> Set implementer, e.g. phases 1-3
   -> Claude Code, sonnet, medium). Override wins. Effort defaults to medium.
3. **One global priority list** in Settings (default follows the tiers of 5h: Economy -> Standard -> Gold) decides who takes over, skipping unavailable engines.
4. **Mid-phase handoff (switch immediately, lose nothing).** When the working AI is exhausted
   (quota error, cooling down, timeout, NO-OP, crash) in the middle of a phase:
   - The engine checkpoints first: commits all work in the worktree as a WIP commit
     (`nightshift: checkpoint <phase> <engine> <time>`) and pushes it to the phase branch.
   - It writes `.nightshift/handoff.md` in the worktree (excluded from the PR diff via
     `.git/info/exclude`): goal, done so far (from `git diff --stat` and the AI's last output tail),
     remaining acceptance tests, last CI failure if any.
   - The next available engine on the priority list continues **the same phase on the same branch**
     with the original prompt + the handoff note. WIP commits are squashed by the merge-commit PR.
5. **Fast switch-back.** The preferred engine (owner override, else plan choice) is re-checked at every
   invocation boundary (each implement attempt, each CI-fix round, each review-fix round). As soon as
   its cooldown has ended it takes over again via the same checkpoint + handoff. A working process is
   never killed just to switch back (that would waste the tokens already spent); boundaries come
   often because each fix round is a separate invocation. Optional setting "aggressive switch-back":
   cap a fallback engine's single invocation at 30 min while the preferred engine is cooling down,
   so control returns sooner.
6. **Same rule for other jobs.** CI fixer and reviewer follow the priority list; the reviewer is a
   different engine from the last implementer when one is available.
7. **Router (5a) only acts when nothing is assigned** ("Auto"). Owner choices are never second-guessed.
8. Every assignment, override, handoff and switch-back is an event with a plain-English sentence
   ("Claude Code hit its limit at 01:42; Antigravity continued phase 2 from Claude's checkpoint.
   Claude came back at 04:00 and took over at the next fix round.").

**Add any AI without code:** the engine catalogue accepts a new CLI via a command template
(`{instruction}`, `{model}`, `{effort}`, `{workdir}`) passed as an argument list (never through a
shell), quota-error patterns and an optional usage parser. Built-in templates reproduce the proven
command lines for agy, gemini, codex and claude exactly.

## 5d. Self-healing delivery and guards (speed first, maximum caution where it matters)

Principle: **solve, don't wait.** A problem triggers an automatic fix path; the run only parks a phase
after every fix path is exhausted, and even then keeps working on everything that does not depend on it.

**Self-healing merge pipeline**
- CI fails -> CI fixer (priority list) gets the failing log and fixes; repeat up to the limit,
  rotating to a different engine after 2 failed rounds by the same engine (fresh eyes).
- Merge conflict with `main` -> automatic rebase-free update: merge `origin/main` into the branch,
  let the implementer resolve conflicts, re-run CI.
- Review (when required, see risk-based review) returns CHANGES -> the implementer fixes exactly those
  findings, then a quick re-review of the new diff only.
- Flaky test (fails then passes on re-run with no code change) -> re-run once automatically, record
  it as flaky in the digest, do not burn a fix round.
- **Never block the night:** if a phase is still not mergeable after all fix paths, it is **parked**
  (PR left open with a plain-English reason and the best next step), and:
  - independent phases continue immediately;
  - dependent phases continue as **stacked PRs** on top of the parked branch (based on it, not on
    `main`), so work keeps progressing; they merge automatically, in order, once the parked phase is
    fixed (by a later fix round, the next night, or the owner).

**Risk-based review (fast by default, careful where it counts)**
- Every diff gets a risk score from changed paths and content. Project setting
  `critical_paths` (glob list) plus built-in critical signals: auth/login, encryption/keys,
  permissions (e.g. AndroidManifest permissions), payments, data deletion/migrations, file-system
  writes outside app data, network/security config, build and CI config, dependency additions.
- **Low risk** (no critical signal): merge as soon as all required CI checks are green on the head SHA
  and the diff guards pass. No AI review, no waiting.
- **Critical**: one independent AI review (single call, medium effort, different engine) limited to the
  critical files; APPROVE -> merge; CHANGES -> self-heal loop above.
- The digest and the deck list every critical change and its review result in one line each.

**Hard guards (always on, never skipped)**
- Nothing pushes to `main` directly; no force-push; no history rewrite. GitHub branch protection on
  `main` (required CI checks, no force-push, no deletion) is verified at start; the run warns on the
  deck if it is missing.
- Merge only with all required checks green on the exact head SHA.
- Diff guards: no deleted/skipped tests or weakened CI gates (removed test files, new
  `skip`/`xfail`/`.only`, edits to `.github/workflows/*` or `scripts/check_*` unless the phase allows
  it), no secrets (patterns + high entropy), no files outside the repo, no binaries over 5 MB.
  A tripped diff guard sends the phase back to the implementer with the exact finding (self-heal),
  it is not silently merged.
- Post-merge `main` build fails -> automatic revert PR (merged on green) + the phase re-queued with
  the failure log; dependent phases rebase onto the reverted `main`.

**Runaway protection**
- Per phase: max implementer attempts across all engines (default 4), max CI-fix rounds (default 4),
  wall-clock limit (default 180 min); per run: max total AI invocations (default 60) and max hours.
- Same-error detector: identical failure signature twice in a row from the same engine -> rotate engine;
  identical across 3 engines -> park the phase.
- NO-OP detector: exit 0 without a new commit/PR counts as a failed attempt.
- Circuit breaker: 3 phases parked in a row -> stop starting new phases (stacked work stays resumable).
- Owner-set budget caps per engine (estimated) are never exceeded by the Router or fallbacks.

**Process and machine**
- Every runner in its own worktree; process-tree kill on timeout; no orphaned processes at exit.
- Keep the PC awake while running via `SetThreadExecutionState` (no admin); the deck warns that closing
  the lid may still sleep the PC. Resume cleanly after sleep, reboot, crash or network loss
  (network loss: pause with backoff, does not burn attempts).
- Pre-flight before each phase: disk space (> 5 GB free), `git`/`gh` auth valid, CLI logged in,
  network reachable, repo clean.
- Single instance; a lock file prevents two runs on the same project.

**Agent containment** (agents run with permissions auto-approved, so the boundary is the engine)
- Working directory = the worktree; environment carries only what `git`/`gh` need; forbidden actions
  stated in the prompt; diff guards catch violations regardless.
- Agent output is data: only the defined JSON contracts are parsed (schema-validated); free text in
  agent output never changes engine behaviour.

**Events and audit**
- Append-only `events` table, written before and after every step, SHA-256 hash-chained; full raw logs
  per step as files; secrets redacted; nothing deleted automatically.
- The morning digest is generated from events only.

## 5e. The deck: what the owner sees (simple words, no reading required)

The owner has no time to review details. Mission Control and the morning digest show only what
matters, in plain words, in this order:

1. **One-line headline**: "All good - 4 of 6 phases merged, 2 still running, finishing ~05:30 (estimate)."
   or "Needs you - 1 phase parked: login change failed review twice."
2. **Needs your attention** (only if non-empty, max 3 items, each one sentence + one button):
   parked phases, critical changes merged, budget caps reached, machine warnings.
3. **Done tonight**: one line per merged phase ("Phase 3 Widget fixes - merged, built by Claude +
   Antigravity, release preview.44 ready").
4. **Now / Next / ETA** with the working AI and why.
5. **AI usage tonight**: per engine one bar + "~% of budget used (estimate)" and handoff count.
6. Everything else (raw logs, every event, every guard result) sits behind "Details".

Notifications (Windows toast) only for: run finished, a phase parked, circuit breaker tripped, PC about
to lose power or network for more than 10 min. Never for routine progress.

## 5f. Login health: every CLI and account is checked, all night

The app checks every tool it depends on and never discovers a logged-out AI halfway through the night
by surprise.

**What is checked and how** (verified against the CLIs installed on the owner's PC, 2026-11-24):

| Tool | Free check (no quota used) | Tiny probe (only when no free check exists) |
|---|---|---|
| git | `git ls-remote <repo> HEAD` succeeds (auth + network) | - |
| GitHub CLI | `gh auth status` and `gh api user` | - |
| Claude Code | `claude auth status` -> JSON `loggedIn: true` | - |
| Codex (GPT) | `codex login status` -> "Logged in" | - |
| Gemini CLI | binary present + Gemini config files exist (names only, contents never read or logged) | `gemini -p "Reply with OK" --output-format json`, short timeout |
| Antigravity (agy) | binary present + `~/.gemini/antigravity-cli` exists | `agy -p "Reply with OK" --output-format json --print-timeout 60s` |

- Each result is classified as **Ready**, **Logged out**, **Limit reached (back at HH:MM)**,
  **Not installed**, or **Unknown (network)**. A quota/limit error during a probe is a cooldown, never
  "logged out". Probe outputs are parsed by known patterns only; tokens and credential contents are
  never read, stored or shown.
- **When:** at app start; right before a run starts; before each phase for the engines that phase may
  use (cached for 60 min, so probes cost almost nothing); and every 30 min during a run for engines in
  the priority list. A probe for an engine with an active cooldown is skipped.
- **During the night:** if an engine becomes logged out or its login expires, it is marked
  unavailable, the priority list takes over immediately (5c handoff), and the deck gets one
  "Needs your attention" line. If `git` or `gh` fails (nothing can be pushed or merged), the run
  pauses with network/auth backoff instead of burning attempts, and a Windows toast is shown.

**On the deck**
- A compact **Logins** row on Mission Control: one chip per tool (icon + name + state in words, e.g.
  "Claude: ready", "Gemini: limit reached, back 03:10", "Antigravity: logged out"). Never colour alone.
- Each problem chip has a **Fix** button that opens a terminal window with the right login command
  (`claude` then `/login`; `gemini`; `agy`; `codex login`; `gh auth login`), then re-checks when the
  owner returns to the app. The app never types or stores passwords itself.
- **Before you sleep:** pressing Start shows a one-screen readiness summary: "Ready to run all night:
  5 of 6 tools ready. Gemini is logged out; the night will continue with Antigravity, GPT and Claude.
  Fix now or Start anyway." Start anyway is always allowed when at least one implementer is ready.
- AI Engines & Quota shows the full history of checks per tool (Details).

## 5g. Token optimizer (agreed stance: Claude + Gemini, Gemini verdict AGREE 2026-11-25, see docs/reviews/token-optimizer-gemini.json)

Goal: cut tokens per phase without hurting unattended reliability. Every tool is opt-in, pinned,
isolated, and kept only if measurement on the owner's own runs proves a net saving.

**Rules for every tool**
1. Pinned exact version or commit; no self-update; upgrades only when the owner approves in the app.
2. No `curl | sh` / `irm | iex`. The app downloads each artifact once into
   `%LOCALAPPDATA%\NightShift\tools\<name>\<version>\`, verifies a recorded SHA-256, then always runs
   that local copy. Hash mismatch = tool disabled + plain-English warning.
3. No global installs or global config edits (no `rtk init -g`, no writes to
   `~/.gemini/config/plugins` or `~/.gemini/config/mcp_config.json`). Config is per run / per worktree,
   git-excluded like `.nightshift_prompt.md`. The owner's IDE and desktop apps stay untouched.
4. No telemetry: `DO_NOT_TRACK=1` and offline flags where supported.
5. Measure first, then enable one tool at a time with an A/B comparison (same phase type, multiple runs,
   tokens AND turns AND success rate). A tool that does not cut net tokens, or that raises retry turns
   or failure rate, is switched off automatically and reported on the deck.

**Rollout order (highest ROI first)**

| # | Tool | Role | Keep only if |
|---|---|---|---|
| 1 | ccusage (pinned, `--offline --json`) | Per-engine, per-model usage from local logs; feeds the quota ledger and A/B numbers. Antigravity support must be verified with a real run before the Router relies on it. | Always kept if its numbers match the CLIs' own usage output within a reasonable margin; otherwise the ledger falls back to CLI JSON usage + run counts |
| 2 | repomix (pinned, `--compress` + token budget) | Compact repo map injected into the prompt at phase start, replacing the agent's first exploration turns | Measured first-phase turns/tokens go down without more failed phases |
| 3 | agy-fast (pinned commit, auto-update OFF) | Batched search/outline/edit/check tools for agy runners | **Gate:** only if agy can load it per run without touching global config. Verified on this PC 2026-11-24: `agy mcp add` and `agy plugin install` have no project/per-run scope (they write global config). Remaining candidates to test: `agy --settings <session-only json>`, a project-local plugin/MCP folder, or a scoped config dir that keeps the login. If none works, agy-fast is skipped (repomix covers most of the cold-start saving). If it works: keep only if agy turns drop by at least 15% with no rise in failed edits |
| 4 | rtk-oss (pinned portable Windows binary from GitHub Releases + SHA-256, not winget) | Compresses shell output (test/build/git) for Gemini CLI, Claude and Codex runners | Net tokens drop AND retry turns do not rise. Expect a small saving on DriveLift (builds run on GitHub) and possibly more on the Windows app (local `npm test` / `pytest` output). Asset verified 2026-11-25 on rtk-ai/rtk v1.2.1: use **`rtk-oss-x86_64-pc-windows-msvc.zip`** (Apache-2.0 OSS build), NOT `rtk-x86_64-pc-windows-msvc.zip` (the non-OSS engine build). Some rtk filters call ripgrep: `rg` is NOT on PATH on the owner's PC, so bundle a pinned portable `rg.exe` (ripgrep, MIT/Unlicense, SHA-256 verified) in the same tools folder and put it first on PATH for runner processes only |
| 5 | ast-grep-mcp (pinned) | Structural code search with compact output | Only added if ccusage shows search is still a top cost after 1-4 |

**Expectations (to avoid over-promising):** savings are estimates until measured; rtk's 60-90% applies to
command output, not the total bill; agy-fast's -31% is the author's small-sample claim. The deck shows
"tokens saved (estimate)" per tool and per night.

## 5h. Economy first: small jobs never burn the gold (owner rule, 2026-11-25)

Owner's principle: **use the cheapest capable AI for each job; premium quota is gold and is spent
only where it is needed.** Sending code to Chinese-hosted model services is explicitly allowed by the
owner. The machine has 8 GB RAM, so no local models (Ollama) are used.

**Engine tiers** (owner can re-order inside a tier; defaults below, all effort medium):

| Tier | Engines | Use for |
|---|---|---|
| **Economy** | Qwen Code (Qwen Coder, free sign-in tier), DeepSeek, Kimi K2, GLM, MiniMax | Small and medium jobs (see sizing) |
| **Standard** | Antigravity (neon, hydrogen), Gemini CLI, Codex/GPT | Large jobs, and escalation from Economy |
| **Gold** | Claude Code | Only critical/large jobs that Standard could not finish, or final plan review |

**How economy engines run (no admin, per-run only):**
- Qwen Code: its own CLI (`qwen -p ...` headless, YOLO approval), installed by the app's tool manager
  into `%LOCALAPPDATA%\NightShift\tools\` at a pinned version (rules of section 5g); free-tier login is
  done once by the owner via the Logins "Fix" button.
- DeepSeek, Kimi, GLM, MiniMax: run through the already-installed Claude Code CLI pointed at the
  provider's Anthropic-compatible endpoint **for that one process only** via environment variables
  (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`).
  Never written to `~/.claude` settings, so the owner's normal Claude Code is untouched. Endpoint and
  model names are entries in the engine catalogue (editable, verified with the Test button); API keys
  are entered by the owner in Settings and stored with Windows DPAPI (current user), never in the repo,
  logs or prompts.
- Each economy engine is checked by Login health (5f) like any other.

**Job sizing (decides the starting tier):**
- **Small** -> Economy: CI-fix rounds with a short failing log; diff-guard findings; review-findings
  fixes; docs, copy and PR-description fixes; UI-contract evidence; a phase whose plan estimates
  <= 3 files or <= ~200 changed lines; low-risk reviews; plan critique.
- **Medium** -> Economy first, escalate on failure: phases estimating <= 8 files with no critical paths.
- **Large / critical** -> Standard first: multi-module features, UI overhauls, anything touching
  critical paths (5d risk list), dependency or build-system changes, security-sensitive code.
- The planner writes `size` (small/medium/large) per phase; the engine re-estimates from the actual
  diff after the first attempt. Owner override per phase (5c) always wins.

**Escalation ladder (no time lost):** start tier -> next engine in the same tier -> next tier.
Escalate immediately on: quota/limit, NO-OP, the same failure twice, a tripped diff guard that the
economy engine could not fix, or wall-clock budget for that tier (default 45 min for small, 90 min for
medium). Escalation uses the checkpoint + handoff of 5c, so the stronger engine continues from the
economy engine's work. Gold is reached only after Standard is exhausted or failed, or for final plan
review, and only within the owner's Gold budget.

**Measured, not assumed:** per engine the ledger tracks success rate, attempts, time and estimated cost
per job size. If an economy engine's success rate for a size falls below 60% over the last 10 jobs,
the Router starts that size one tier higher and says so on the deck. The deck shows
"gold saved tonight (estimate)": jobs finished by Economy that would otherwise have used Standard/Gold.

## 5i. Phone status bridge (owner rule, 2026-11-25)

Purpose: the owner checks an unattended run from the phone (open one Google Doc, or ask Gemini
"Update") without RDP, Google Cloud credentials or admin rights. It is a module (section 4),
`phone_status`, off by default.

**Path:** engine -> HTTPS POST -> the owner's own Google Apps Script web app (deployed under the
owner's account, "Execute as: me") -> redraws the Google Doc `NIGHTSHIFT_LIVE_STATUS`. No other host.

**What is sent: the deck, never raw data.** The payload is the section 5e deck only: project name,
state (`working` / `attention` / `good` / `stopped`), one-line headline, now (phase, step, working AI,
ETA estimate), needs you (max 3), done this run, next (max 3), and a phase strip (short title + state
per phase). No log lines, file paths, diffs, prompts or command output. Every string passes the 5d
secret redaction before sending; the web app also whitelists and length-caps every field.

**Rules:**
1. **Secret required.** The web app is reachable anonymously, so the URL alone is not enough: every
   POST carries a shared secret in the JSON body (not the URL; Apps Script cannot read headers). The
   web app compares it in constant time and rejects anything else. URL and secret live in `settings`,
   the secret encrypted with Windows DPAPI; neither is ever logged or shown after entry.
2. **Never blocks a phase.** One background sender, 20 s timeout, every error swallowed and recorded
   as one line on the Settings card ("last phone update: ok 10:45" / "failed: timeout").
3. **Debounce + urgent.** At most one post per 60 s; run start/stop, a phase merging, completing or
   parking goes out at once.
4. **Heartbeat.** Every 10 min while running. The Doc shows "last heard" per project; older than
   25 min means the PC is off, asleep or offline (the Doc says so in plain words).
5. **Ordered writes.** Each post carries `seq` (epoch ms); the web app serialises writes with
   `LockService` and drops a post older than the stored one.
6. **Apps Script quirks.** A POST to `/exec` answers with a 302 after `doPost` has run; the client
   follows it and does not treat it as a failure.
7. **One card per project.** DriveLift and Night-Shift Desktop each keep their own card in the same
   Doc (running projects first, cards older than 7 days dropped), so two builders never overwrite
   each other.

**Doc look:** one dark card per project in the design-system colours (`design/tokens.json` dark theme):
large status word (WORKING cyan, NEEDS YOU red, ALL GOOD green, STOPPED/SILENT amber), the headline,
a coloured strip with one block per phase (green done, cyan working, red stopped, slate waiting),
then NOW / NEEDS YOU / DONE THIS RUN / NEXT and "last heard". Readable in one glance on a phone.

**Settings card:** toggle, URL, secret (masked), "Send test update" button, last result. The Apps
Script source ships in `tools/phone-bridge/Code.gs` with a one-page setup guide.

Reference implementation (legacy, running since 2026-11-25): `Gemini-Supervisor/status_bridge.py`
(client) and `Gemini-Supervisor/gas-bridge/Code.gs` (web app v6).

## 6. Data model (SQLite)

`projects`, `plans`, `phases(status, attempt, pr_number, branch, started_at, finished_at)`,
`steps(phase_id, kind, runner, rc, verified, detail, started_at, finished_at)`, `engines(runner, model, effort, skills_json, cost_tier, budget_json, enabled, pinned, command_template, usage_parser, quota_patterns_json)`, `router_decisions(step_id, chosen, reason, scores_json)`, `phase_assignments(phase_id, planned_engine, override_engine, model, effort)`, `priority_list(position, engine, model, effort)`, `guard_results(phase_id, guard, passed, detail)`, `handoffs(phase_id, from_engine, to_engine, reason, checkpoint_sha, ts)`, `parked(phase_id, reason, next_step, stacked_children_json)`, `login_checks(tool, state, detail, back_at, checked_at)`, `engine_stats(engine, size, attempts, successes, minutes, est_cost)`, `council_runs(plan_id, role, engine, input, output, usage_estimate)`,
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
