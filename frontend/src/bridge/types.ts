export type ScenarioName =
  | "idle"
  | "running"
  | "failure"
  | "empty"
  | "gemini-logged-out"
  | "claude-limit"
  | "gh-logged-out"
  | "handoff"
  | "ci-healing"
  | "finished";

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

export interface NeedAttentionItem {
  phase_id?: string;
  headline: string;
  detail: string;
  action: string;
}

export interface DoneTonightItem {
  phase_id: string;
  title: string;
  status: string;
  runners?: string[];
  pr_number?: number | null;
}

export interface UpcomingPhaseItem {
  phase_id: string;
  title: string;
  status: string;
}

export interface EngineUsageTonight {
  invocations: number;
  tokens: number;
  budgetPct?: number;
}

export interface GuardStatusItem {
  name: string;
  status: "pass" | "warn" | "fail";
  detail: string;
}

export interface DeckPayload {
  project_id: string;
  project_name: string;
  state: "idle" | "working" | "good" | "attention";
  headline: string;
  needs_you: NeedAttentionItem[];
  done_tonight: DoneTonightItem[];
  now: {
    phase?: string | null;
    step?: string | null;
    engine?: string | null;
    why?: string | null;
    eta?: string | null;
  };
  next: UpcomingPhaseItem[];
  usage_tonight: Record<string, EngineUsageTonight>;
  handoff_count: number;
  guards?: GuardStatusItem[];
  problems?: string | null;
  updated_at: string;
}

export interface LoginCheckItem {
  tool: string;
  state: string; // "Ready" | "Logged out" | "Limit reached" | "Not installed" | "Unknown (network)"
  detail: string;
  backAt?: string | null;
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
  deck?: DeckPayload;
  stopArmed?: boolean;
  logins?: Record<string, LoginCheckItem>;
}

export type BridgeEventCallback = (payload: unknown) => void;

export interface Bridge {
  call<T = unknown>(method: string, args?: Record<string, unknown>): Promise<T>;
  subscribe(event: string, cb: BridgeEventCallback): () => void;
}
