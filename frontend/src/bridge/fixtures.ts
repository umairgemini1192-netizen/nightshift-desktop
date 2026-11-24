import type { AppSnapshot, ScenarioName } from "./types";

export const SCENARIO_NAMES: readonly ScenarioName[] = ["idle", "running", "failure", "empty"] as const;

export const SCENARIO_FIXTURES: Record<ScenarioName, AppSnapshot> = {
  idle: {
    scenario: "idle",
    runState: "idle",
    headline: "Ready for tonight. Select a plan to start an unattended run.",
    activeProjectId: "nightshift-desktop",
    projects: [
      { id: "nightshift-desktop", name: "Night-Shift Desktop", repo: "owner/nightshift-desktop" },
      { id: "drivelift-android", name: "DriveLift Android", repo: "owner/drivelift-android" },
    ],
    activePhaseTitle: null,
    activeEngineLabel: null,
    etaEstimate: null,
    moduleBadges: {
      "plan-builder": { label: "6 phases", tone: "neutral" },
      "engines-quota": { label: "4 ready", tone: "success" },
    },
  },
  running: {
    scenario: "running",
    runState: "running",
    headline: "All good - 2 of 6 phases merged, 1 running, finishing ~04:15 (estimate).",
    activeProjectId: "nightshift-desktop",
    projects: [
      { id: "nightshift-desktop", name: "Night-Shift Desktop", repo: "owner/nightshift-desktop" },
      { id: "drivelift-android", name: "DriveLift Android", repo: "owner/drivelift-android" },
    ],
    activePhaseTitle: "Phase 3: Core shell and design tokens",
    activeEngineLabel: "Antigravity (medium effort)",
    etaEstimate: "~1 h 25 m left (estimate)",
    moduleBadges: {
      "mission-control": { label: "Running", tone: "info" },
      "plan-builder": { label: "3 / 6", tone: "info" },
      "engines-quota": { label: "1 cooling", tone: "warning" },
    },
  },
  failure: {
    scenario: "failure",
    runState: "failed",
    headline: "Needs you - 1 phase parked after checks failed twice. Independent phases continue.",
    activeProjectId: "nightshift-desktop",
    projects: [
      { id: "nightshift-desktop", name: "Night-Shift Desktop", repo: "owner/nightshift-desktop" },
      { id: "drivelift-android", name: "DriveLift Android", repo: "owner/drivelift-android" },
    ],
    activePhaseTitle: "Phase 2: Login health checks",
    activeEngineLabel: "Codex (continuing from Gemini checkpoint)",
    etaEstimate: "Paused on parked phase",
    moduleBadges: {
      "mission-control": { label: "1 parked", tone: "danger" },
      "runs-history": { label: "Attention", tone: "warning" },
      "engines-quota": { label: "1 logged out", tone: "danger" },
    },
  },
  empty: {
    scenario: "empty",
    runState: "idle",
    headline: "No project connected yet. Add a repository in Settings to begin.",
    activeProjectId: null,
    projects: [],
    activePhaseTitle: null,
    activeEngineLabel: null,
    etaEstimate: null,
    moduleBadges: {},
  },
};

export function parseScenarioParam(search: string): ScenarioName {
  const params = new URLSearchParams(search);
  const value = params.get("scenario");
  if (value === "idle" || value === "running" || value === "failure" || value === "empty") {
    return value;
  }
  return "idle";
}
