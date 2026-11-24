# Rules for AI agents working in this repo (Antigravity, Gemini CLI, Codex, Claude)

Read `docs/BLUEPRINT.md` and `docs/DESIGN_SYSTEM.md` before writing any code. They are the contract.

1. Work only on the branch you were given. One PR per phase. Never push to `main`.
2. Do not run builds that need admin rights. Local `npm ci`, `npm run build`, `pytest` are fine;
   GitHub Actions (`windows-latest`) is the source of truth.
3. Pin every dependency to an exact version. Commit `package-lock.json`. No `latest`, no `^`/`~`.
   No packages that self-update, phone home, or need global installs.
4. UI: use only tokens (`frontend/src/design/tokens.ts` / Tailwind theme generated from
   `design/tokens.json`) and the components in `frontend/src/design/`. No raw colours, no arbitrary
   Tailwind values, no emoji, no inline styles except CSS variables from tokens. Every interactive
   component implements all states in DESIGN_SYSTEM section 3 and is keyboard accessible.
5. Every feature is a module (BLUEPRINT section 4). Do not hard-code modules in the shell.
6. Every new behaviour ships with tests: pytest for the engine (use `runners/fake.py` and a fake `gh`),
   Vitest for UI logic, a Playwright screenshot spec for every new page/state using `MockBridge` fixtures.
7. Keep `npm run lint`, `npm run typecheck`, `npm test`, `pytest -q`, `python scripts/check_ui_rules.py`
   and `python scripts/check_tokens.py` green before opening the PR.
8. User-facing text follows DESIGN_SYSTEM section 5 (plain English, estimates labelled).
9. The PR description lists: what changed, how it was tested, screenshots produced, and anything left undone.
10. Never commit secrets, tokens, `.env` files, or personal data.
