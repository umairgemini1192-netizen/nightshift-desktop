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
  | "finished"
  | "council-drafting"
  | "council-critique"
  | "council-final-ready"
  | "council-substitution";

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

export type NeedAttentionActionKind =
  | "approve_task"
  | "rollback_release"
  | "configure_smoke_test"
  | "open_settings"
  | "open_runs"
  | "fix_login";

export interface NeedAttentionItem {
  phase_id?: string;
  task_id?: string;
  head_sha?: string;
  risk_class?: string;
  pr_number?: number;
  project_id?: string;
  repo?: string;
  release_tag?: string;
  previous_good_tag?: string;
  headline: string;
  detail: string;
  action: string;
  action_kind?: NeedAttentionActionKind;
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
  gold_saved_tonight?: {
    jobs_done: number;
    cost_saved_usd: number;
    tokens_saved: number;
    estimate_label: string;
  };
  token_savings_tonight?: TokenSavingsTonight;
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

export interface PlanPhase {
  id: string;
  title: string;
  branch: string;
  prompt: string;
  depends_on: string[];
  ladder: string;
  timeout_minutes: number;
  auto_merge: boolean;
  release_after_merge: boolean;
  force_rerun?: boolean;
  position?: number;
  implementer?: string;
  overrideImplementer?: string;
}

export interface RunSummary {
  id: string;
  title: string;
  projectId: string;
  status: string;
  phasesCount: number;
  phasesDone: number;
  phasesFailed: number;
  duration: string;
  aiUsed: string[];
  createdAt: string;
  updatedAt: string;
}

export interface RunPhaseItem {
  id: string;
  title: string;
  status: string;
  runner: string;
  pr_number?: number | null;
  pr_url?: string | null;
  branch?: string;
  duration?: string;
}

export interface RunArtifact {
  name: string;
  size: string;
  sha256?: string;
}

export interface RunDetail {
  run: RunSummary;
  digest: string;
  phases: RunPhaseItem[];
  artifacts: RunArtifact[];
}

export interface EngineItem {
  id: string;
  runner: string;
  name: string;
  model: string;
  effort: "low" | "medium" | "high";
  costTier: "economy" | "standard" | "gold";
  state: "ready" | "cooling" | "no-login" | "unavailable";
  coolingUntil?: string | null;
  quotaUsedPct: number;
  skills: Record<string, number>;
  budget?: Record<string, number>;
}

export interface RouterDecision {
  id: number;
  step: string;
  timestamp: string;
  chosen: string;
  reason: string;
}

export interface ProjectConfigItem {
  id: string;
  name: string;
  repo: string;
  local_path: string;
  build_rules?: string;
  artifact_glob?: string;
  output_dir?: string;
  smoke_command?: string;
}

export interface CouncilRoleState {
  role: "draft" | "critique" | "final";
  engineId: string;
  name: string;
  model: string;
  state: "waiting" | "active" | "done" | "skipped";
  substituted: boolean;
  substitutionReason?: string;
  elapsedSeconds: number;
  tokensEstimate: number;
  costUsdEstimate: number;
  notes: string[];
  issues?: string[];
  tradeoffSummary?: string;
  phases: Array<{
    id: string;
    title: string;
    prompt: string;
    branch: string;
    depends_on: string[];
    size: string;
    timeout_minutes: number;
  }>;
}

export interface CouncilSessionData {
  goal: string;
  planId: string;
  roles: {
    draft: CouncilRoleState;
    critique: CouncilRoleState;
    final: CouncilRoleState;
  };
  diffSummary: string;
  tradeoffSummary: string;
}

export interface PhoneStatusSettings {
  enabled: boolean;
  url: string;
  hasSecret: boolean;
  lastResult: string;
  lastResultAt: string;
}

export interface PhoneStatusTestResult {
  ok: boolean;
  lastResult: string;
  lastResultAt: string;
}

export interface TokenSavingsToolItem {
  tool: string;
  enabled: boolean;
  autoDisabled: boolean;
  tokensSavedTonight: number;
  estimateLabel: string;
  abAction: "keep_enabled" | "auto_disable" | "keep_measuring";
  abReason: string;
  warning: string;
}

export interface TokenSavingsTonight {
  totalTokensSaved: number;
  estimateLabel: string;
  perTool: TokenSavingsToolItem[];
  warnings: string[];
  isEstimate: boolean;
}

export interface TokenOptimizerToolItem {
  name: string;
  displayName: string;
  role: string;
  version: string;
  artifact: string;
  sha256: string;
  license: string;
  runners: string[];
  catalogueOnly: boolean;
  isolationGateRequired: boolean;
  isolationPassed: boolean;
  isolationMethod: string;
  sha256Verified: boolean;
  enabled: boolean;
  autoDisabled: boolean;
  status:
    | "enabled"
    | "disabled"
    | "auto_disabled"
    | "isolation_blocked"
    | "hash_mismatch"
    | "catalogue_only";
  warning: string;
  tokensSavedTonight: number;
  estimateLabel: string;
  abAction: "keep_enabled" | "auto_disable" | "keep_measuring";
  abReason: string;
}

export interface TokenOptimizerState {
  tools: TokenOptimizerToolItem[];
  totalTokensSaved: number;
  estimateLabel: string;
  warnings: string[];
  toolsRoot: string;
  telemetryOff: boolean;
  isEstimate: boolean;
  probeResult?: {
    passed: boolean;
    method: string;
    reason: string;
    globalConfigModified: boolean;
    candidatesTried: string[];
  };
}

export interface Bridge {
  call<T = unknown>(method: string, args?: Record<string, unknown>): Promise<T>;
  subscribe(event: string, cb: BridgeEventCallback): () => void;
}
