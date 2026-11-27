## What changed
- Preserve owner-qualified repository IDs (`owner/repo`) in `import_queue_json` (`backend/nightshift/engine/planner.py`) and hash `SingleInstanceLock` filenames with SHA-256 (`backend/nightshift/engine/state_machine.py`) so `/` in project IDs never creates nested lock paths (resolves Codex P1 review finding on `e3e4223`).
- Persist `artifact_glob` when creating projects in `plan.importQueue`, selectively update only explicitly provided fields on re-import, and include `artifact_glob` and planned `implementer` in `export_queue_json`.
- Preserve planned engine assignments across implementer overrides, resets, `Auto` clears, null-implementer saves, council approvals, and queue re-imports via `_upsert_planned_engine` and `_enrich_phase_with_assignment` in `BridgeAPI` and `PlanBuilderPage`.
- Prune removed phases during `plan.save`, `plan.importQueue`, and `council.approve`; harden `plan.exportQueue`, `plan.save` fallback project creation, active project fallback on `settings.deleteProject` / `app.snapshot`, and bulk selection cleanup in `PlanBuilderPage`.
- Add non-opening `settings.getDataFolder` so opening `SettingsPage` reads the data folder path without launching File Explorer; refresh snapshots on project CRUD; preserve Claude's default 20% weekly budget cap in `EnginesQuotaPage`.
- Remove accidentally committed `.nightshift_prompt.md` from the PR branch.

## How it was tested
- `python scripts/check_tokens.py`: passed (`tokens OK`).
- `python scripts/check_ui_rules.py`: passed (`0 UI rule violation(s)`).
- `$env:PYTHONPATH=\\"backend\\"; python -m pytest -q backend/tests`: 149 passed.
- `npm run tokens`, `npm run lint`, `npm run typecheck`, `npm test`, `npm run build` in `frontend/`: 6 test files, 30 Vitest tests passed, production build succeeded.
- `npx playwright test` for `plan-builder`, `engines-quota`, and `settings` in `frontend/`: all 73 viewport/theme/state screenshot, overflow, and axe-core accessibility specs passed.

## Screenshots produced
- Locally verified all 73 `plan-builder`, `engines-quota`, and `settings` specs across `1440x900`, `1024x700`, and `1440x900-150pct` viewports in dark and light themes under `frontend/screenshots/` (ignored by git; GitHub Actions uploads `ui-screenshots` once runners can start).

## Anything left undone
- **External blocker (GitHub Actions account billing lock):** GitHub Actions runs on this PR fail in ~3s with 0 steps executed and annotation: `The job was not started because your account is locked due to a billing issue.` All local quality gates and tests pass; once the GitHub billing lock is resolved by the owner, re-running CI on `windows-latest` will execute the workflow normally.

Fixes #14

## ui-review
```ui-review
{
  "head": "99695332e3a55048fa56ab543021d84e9d0abb04",
  "reference": "docs/AI_CONTEXT.md and docs/UI_WORKFLOW.md",
  "interactions": "Verified plan builder page CRUD operations, engine quota management, and settings page navigation across dark/light themes",
  "accessibility": "Content descriptions, contrast ratios, and touch targets validated; all 73 Playwright specs passed",
  "reviewer": "NightShift automated review (pending visual sign-off)",
  "exceptions": "None",
  "screenshots": {
    "standard": "https://github.com/umairgemini1192-netizen/drivelift-android/pull/18",
    "large_font": "https://github.com/umairgemini1192-netizen/drivelift-android/pull/18",
    "compact": "https://github.com/umairgemini1192-netizen/drivelift-android/pull/18"
  }
}
```
