export type ScenarioName = "idle" | "running" | "failure" | "empty";

export type AppRunState = "idle" | "running" | "waiting" | "cooling" | "failed" | "done";

export interface ProjectSummary {
  id: string;
  name: string;
  repo: string;
}

export interface ModuleBadgeState {
  label: string;
  tone: "neutral" | "info" | "success" | "warning" | "danger";
}

export interface AppSnapshot {
  scenario: ScenarioName;
  runState: AppRunState;
  headline: string;
  activeProjectId: string | null;
  projects: ProjectSummary[];
  activePhaseTitle: string | null;
  activeEngineLabel: string | null;
  etaEstimate: string | null;
  moduleBadges: Record<string, ModuleBadgeState>;
}

export type BridgeEventCallback = (payload: unknown) => void;

export interface Bridge {
  call<T = unknown>(method: string, args?: Record<string, unknown>): Promise<T>;
  subscribe(event: string, cb: BridgeEventCallback): () => void;
}
