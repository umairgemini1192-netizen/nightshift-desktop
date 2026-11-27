import { useCallback, useEffect, useMemo, useState } from "react";
import { useAppStore } from "../../app/store";
import type {
  PipelineModelScore,
  PipelineReleaseVerification,
  PipelineState,
  PipelineTaskItem,
} from "../../bridge/types";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  GlassCard,
  PageHeader,
  ProgressBar,
  type ProgressSegment,
  Skeleton,
  StatTile,
  StatusPill,
  type StatusPillState,
  StepFeed,
  type StepFeedItem,
  Tabs,
  Tooltip,
} from "../../design";

const PIPELINE_STAGES = [
  "PLAN",
  "CODE",
  "MECHANICAL",
  "TRIAGE",
  "FIX",
  "REVIEW",
  "GATES",
  "MERGE",
  "RELEASE",
] as const;

const RISK_META: Record<
  string,
  { label: string; tone: "neutral" | "info" | "warning" | "danger" | "success" }
> = {
  docs_format: { label: "Docs", tone: "neutral" },
  ui_cosmetic: { label: "UI", tone: "info" },
  business_logic: { label: "Logic", tone: "info" },
  high_risk: { label: "High risk", tone: "warning" },
  governance: { label: "Governance", tone: "danger" },
};

const MATURITY_TONE: Record<string, "neutral" | "info" | "success"> = {
  Implemented: "neutral",
  "Integration-tested": "info",
  "Production-proven": "success",
};

function mapTaskStatusToPill(status: PipelineTaskItem["status"]): StatusPillState {
  switch (status) {
    case "RUNNING":
    case "VERIFYING":
    case "REVIEWING":
      return "running";
    case "APPROVED":
    case "MERGED":
    case "RELEASED":
      return "done";
    case "BLOCKED":
      return "waiting";
    case "FAILED":
      return "failed";
    default:
      return "idle";
  }
}

function stageIndexForStatus(status: PipelineTaskItem["status"]): number {
  switch (status) {
    case "QUEUED":
      return 0;
    case "RUNNING":
      return 1;
    case "VERIFYING":
      return 2;
    case "REVIEWING":
      return 5;
    case "APPROVED":
      return 6;
    case "MERGED":
      return 7;
    case "RELEASED":
      return 8;
    case "BLOCKED":
      return 6;
    case "FAILED":
      return 4;
  }
}

function buildStageSegments(task: PipelineTaskItem): ProgressSegment[] {
  const activeIdx = stageIndexForStatus(task.status);
  return PIPELINE_STAGES.map((stage, idx) => {
    if (task.status === "RELEASED" || idx < activeIdx) {
      return { id: stage, label: stage, status: "done" };
    }
    if (idx === activeIdx) {
      if (task.status === "BLOCKED") return { id: stage, label: stage, status: "blocked" };
      if (task.status === "FAILED") return { id: stage, label: stage, status: "failed" };
      return { id: stage, label: stage, status: "running", progress: 60 };
    }
    return { id: stage, label: stage, status: "pending" };
  });
}

type SortKey = "cost_per_accepted_usd_est" | "acceptance_rate" | "sample_tasks" | "regression_count";

export default function PipelineLedgerPage() {
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);

  const [state, setState] = useState<PipelineState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState("active");
  const [showEvidenceDetails, setShowEvidenceDetails] = useState(false);
  const [riskFilter, setRiskFilter] = useState<string>("all");
  const [sortKey, setSortKey] = useState<SortKey>("cost_per_accepted_usd_est");
  const [approveTarget, setApproveTarget] = useState<PipelineTaskItem | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<PipelineReleaseVerification | null>(null);
  const [busyAction, setBusyAction] = useState(false);

  const loadState = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await bridge.call<PipelineState>("pipeline.getState");
      setState(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load pipeline ledger.");
    } finally {
      setLoading(false);
    }
  }, [bridge]);

  useEffect(() => {
    void loadState();
  }, [loadState]);

  const handleConfirmApprove = async () => {
    if (!approveTarget) return;
    setBusyAction(true);
    try {
      await bridge.call("pipeline.approveTask", { taskId: approveTarget.id, owner: "owner" });
      pushToast({
        title: "Task approved",
        description: `Approved ${approveTarget.title} for commit ${String(approveTarget.head_sha ?? "").slice(0, 7)}.`,
        tone: "success",
      });
      setApproveTarget(null);
      await loadState();
    } catch (err) {
      pushToast({
        title: "Approval failed",
        description: err instanceof Error ? err.message : "Could not approve task.",
        tone: "danger",
      });
    } finally {
      setBusyAction(false);
    }
  };

  const handleConfirmRollback = async () => {
    if (!rollbackTarget) return;
    setBusyAction(true);
    try {
      await bridge.call("pipeline.rollbackRelease", {
        tag: rollbackTarget.tag,
        archiveDir: "",
        outputDir: "",
      });
      pushToast({
        title: "Release rolled back",
        description: `Restored ${rollbackTarget.previous_good_tag ?? "previous release"} after ${rollbackTarget.tag} failed smoke verification.`,
        tone: "warning",
      });
      setRollbackTarget(null);
      await loadState();
    } catch (err) {
      pushToast({
        title: "Rollback failed",
        description: err instanceof Error ? err.message : "Could not roll back release.",
        tone: "danger",
      });
    } finally {
      setBusyAction(false);
    }
  };

  const sortedScores = useMemo(() => {
    const scores = [...(state?.modelScores ?? [])];
    scores.sort((a: PipelineModelScore, b: PipelineModelScore) => {
      if (sortKey === "acceptance_rate") {
        const ra = a.sample_tasks > 0 ? a.accepted_tasks / a.sample_tasks : 0;
        const rb = b.sample_tasks > 0 ? b.accepted_tasks / b.sample_tasks : 0;
        return rb - ra;
      }
      if (sortKey === "sample_tasks") return b.sample_tasks - a.sample_tasks;
      if (sortKey === "regression_count") return a.regression_count - b.regression_count;
      return a.cost_per_accepted_usd_est - b.cost_per_accepted_usd_est;
    });
    return scores;
  }, [state?.modelScores, sortKey]);

  const filteredTasks = useMemo(() => {
    const tasks = state?.tasks ?? [];
    if (riskFilter === "all") return tasks;
    return tasks.filter((t) => t.risk_class === riskFilter);
  }, [state?.tasks, riskFilter]);

  if (loading) {
    return (
      <div className="flex flex-col gap-5">
        <PageHeader
          title="Pipeline ledger & scores"
          description="Track active pipeline stages, evidence checks, and what each AI model costs per accepted change (estimates)."
        />
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
        <Skeleton className="h-64" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col gap-5">
        <PageHeader
          title="Pipeline ledger & scores"
          description="Track active pipeline stages, evidence checks, and what each AI model costs per accepted change (estimates)."
        />
        <GlassCard title="Could not load pipeline ledger" icon="AlertTriangle" variant="raised">
          <div className="flex flex-col gap-4">
            <p className="text-sm text-text-secondary">{error}</p>
            <div>
              <Button variant="primary" size="sm" icon="RefreshCw" onClick={() => void loadState()}>
                Retry
              </Button>
            </div>
          </div>
        </GlassCard>
      </div>
    );
  }

  const summary = state?.summary;
  const activeTask = state?.activeTask ?? null;
  const hasAnyData = (state?.tasks.length ?? 0) > 0 || (state?.modelScores.length ?? 0) > 0;

  if (!hasAnyData) {
    return (
      <div className="flex flex-col gap-5">
        <PageHeader
          title="Pipeline ledger & scores"
          description="Track active pipeline stages, evidence checks, and what each AI model costs per accepted change (estimates)."
        />
        <EmptyState
          icon="BarChart3"
          title="No pipeline tasks recorded yet"
          description="Once an overnight run starts, every task contract, evidence check, and model cost estimate will appear here."
          actionLabel="Refresh ledger"
          onAction={() => void loadState()}
        />
      </div>
    );
  }

  const monthlyPct =
    summary && summary.monthlyCapUsdEst > 0
      ? Math.min(100, Math.round((summary.monthlySpendUsdEst / summary.monthlyCapUsdEst) * 100))
      : 0;

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Pipeline ledger & scores"
        description="Track active pipeline stages, evidence checks, and what each AI model costs per accepted change (estimates)."
        action={
          <Badge tone={summary?.freeOnlyMode ? "success" : "info"}>
            {summary?.freeOnlyMode ? "Free-only mode active" : "Paid budget enabled"}
          </Badge>
        }
      />

      {/* Top summary row: 4 StatTiles */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
        <StatTile
          label="Accepted changes (30d)"
          value={String(summary?.acceptedChanges30d ?? 0)}
          delta="Verified merges"
          deltaTone="positive"
        />
        <StatTile
          label="Cost per accepted change (estimate)"
          value={`$${(summary?.costPerAcceptedUsdEst ?? 0).toFixed(2)}`}
          delta={summary?.freeOnlyMode ? "Free-only default" : "Tokens, CI, storage"}
          deltaTone="neutral"
        />
        <StatTile
          label="Regression rate"
          value={`${(summary?.regressionRate ?? 0).toFixed(1)}%`}
          delta={(summary?.regressionRate ?? 0) <= 5 ? "Within 5% threshold" : "Above 5% threshold"}
          deltaTone={(summary?.regressionRate ?? 0) <= 5 ? "positive" : "negative"}
        />
        <StatTile
          label="Gold saved (estimate)"
          value={String(summary?.goldSavedCount ?? 0)}
          delta="Graduated Economy tasks"
          deltaTone="positive"
        />
      </div>

      {/* Main tabbed workspace */}
      <GlassCard title="Assembly line telemetry" icon="Layers">
        <Tabs
          label="Pipeline ledger sections"
          value={activeTab}
          onChange={setActiveTab}
          items={[
            {
              id: "active",
              label: "Active Pipeline",
              icon: "Activity",
              content: activeTask ? (
                <div className="flex flex-col gap-5 pt-2">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="font-display text-lg font-semibold text-text-primary">
                        {activeTask.title}
                      </h2>
                      <StatusPill state={mapTaskStatusToPill(activeTask.status)} label={activeTask.status} />
                      <Badge tone={RISK_META[activeTask.risk_class]?.tone ?? "info"}>
                        {RISK_META[activeTask.risk_class]?.label ?? activeTask.risk_class}
                      </Badge>
                      {activeTask.pr_number && (
                        <Badge tone="neutral">PR #{activeTask.pr_number}</Badge>
                      )}
                    </div>

                    {activeTask.status === "BLOCKED" && (
                      <Button
                        variant="primary"
                        size="sm"
                        icon="ShieldCheck"
                        onClick={() => setApproveTarget(activeTask)}
                      >
                        Review & approve
                      </Button>
                    )}
                  </div>

                  <ProgressBar
                    label="9-stage pipeline progress"
                    segments={buildStageSegments(activeTask)}
                  />

                  <div className="grid grid-cols-1 md:grid-cols-3 gap-4 rounded-md border border-border-subtle bg-surface-raised p-4 text-xs">
                    <div>
                      <p className="text-text-muted">Contributing providers (excluded from review)</p>
                      <p className="mt-1 font-mono text-text-primary">
                        {activeTask.contributingProviders.length > 0
                          ? activeTask.contributingProviders.join(", ")
                          : "None yet"}
                      </p>
                    </div>
                    <div>
                      <p className="text-text-muted">Frozen contract digest</p>
                      <p className="mt-1 font-mono text-text-primary ns-tabular">
                        {activeTask.contract_sha256.slice(0, 16)}
                      </p>
                    </div>
                    <div>
                      <p className="text-text-muted">Head commit</p>
                      <p className="mt-1 font-mono text-text-primary ns-tabular">
                        {activeTask.head_sha ? activeTask.head_sha.slice(0, 12) : "Pending commit"}
                      </p>
                    </div>
                  </div>

                  {activeTask.block_reason && (
                    <div className="rounded-md border border-status-warning/40 bg-surface-raised p-3 text-xs">
                      <p className="font-semibold text-status-warning">
                        Blocked: {activeTask.block_reason}
                      </p>
                      {activeTask.block_detail && (
                        <p className="mt-1 text-text-secondary">{activeTask.block_detail}</p>
                      )}
                    </div>
                  )}

                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-text-secondary">
                      Verified evidence records ({activeTask.evidence.length})
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      icon={showEvidenceDetails ? "ChevronUp" : "ChevronDown"}
                      onClick={() => setShowEvidenceDetails((v) => !v)}
                    >
                      {showEvidenceDetails ? "Hide details" : "Details"}
                    </Button>
                  </div>

                  {showEvidenceDetails && (
                    <div className="flex flex-col gap-2 rounded-md border border-border-subtle bg-bg-base p-3 text-xs">
                      {activeTask.evidence.length === 0 ? (
                        <p className="text-text-muted">No evidence records attached yet.</p>
                      ) : (
                        activeTask.evidence.map((ev) => (
                          <div
                            key={ev.id}
                            className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle pb-2 last:border-b-0 last:pb-0"
                          >
                            <div className="flex items-center gap-2">
                              <Badge tone={ev.verified ? "success" : "danger"}>
                                {ev.verified ? "Verified" : "Rejected"}
                              </Badge>
                              <span className="font-mono text-text-primary">{ev.source}</span>
                            </div>
                            <span className="font-mono text-text-muted ns-tabular">
                              {ev.duration_seconds.toFixed(1)}s | {ev.commit_sha.slice(0, 7)}
                              {ev.rejection_reason ? ` | ${ev.rejection_reason}` : ""}
                            </span>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
              ) : (
                <p className="py-4 text-sm text-text-muted">No active pipeline task selected.</p>
              ),
            },
            {
              id: "scores",
              label: "Model Scores",
              icon: "Gauge",
              content: (
                <div className="flex flex-col gap-4 pt-2">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="text-text-secondary">
                      Cheaper Economy models graduate only after 10+ tasks meeting acceptance, regression, and cost thresholds.
                    </span>
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="sm"
                        variant={sortKey === "cost_per_accepted_usd_est" ? "primary" : "secondary"}
                        onClick={() => setSortKey("cost_per_accepted_usd_est")}
                      >
                        Sort by cost
                      </Button>
                      <Button
                        size="sm"
                        variant={sortKey === "acceptance_rate" ? "primary" : "secondary"}
                        onClick={() => setSortKey("acceptance_rate")}
                      >
                        Sort by acceptance
                      </Button>
                      <Button
                        size="sm"
                        variant={sortKey === "sample_tasks" ? "primary" : "secondary"}
                        onClick={() => setSortKey("sample_tasks")}
                      >
                        Sort by samples
                      </Button>
                    </div>
                  </div>

                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-xs">
                      <thead>
                        <tr className="border-b border-border-subtle text-text-muted">
                          <th className="py-2 pr-3 font-medium">Model</th>
                          <th className="py-2 pr-3 font-medium">Role</th>
                          <th className="py-2 pr-3 font-medium">Risk class</th>
                          <th className="py-2 pr-3 font-medium">Samples</th>
                          <th className="py-2 pr-3 font-medium">Acceptance</th>
                          <th className="py-2 pr-3 font-medium">Avg retries</th>
                          <th className="py-2 pr-3 font-medium">Regressions</th>
                          <th className="py-2 pr-3 font-medium">Cost / accepted (estimate)</th>
                          <th className="py-2 font-medium">Graduation</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sortedScores.map((sc) => {
                          const accPct =
                            sc.sample_tasks > 0
                              ? Math.round((sc.accepted_tasks / sc.sample_tasks) * 100)
                              : 0;
                          const gradLabel = sc.benchmark_graduated
                            ? "Graduated"
                            : sc.sample_tasks < 10
                              ? `Benchmarking (${sc.sample_tasks}/10)`
                              : "Demoted";
                          const gradTone = sc.benchmark_graduated
                            ? "success"
                            : sc.sample_tasks < 10
                              ? "info"
                              : "warning";

                          return (
                            <tr
                              key={`${sc.engine_id}-${sc.model}-${sc.role}-${sc.risk_class}`}
                              className="border-b border-border-subtle/60 text-text-primary"
                            >
                              <td className="py-2.5 pr-3 font-medium">
                                {sc.engine_id} <span className="font-mono text-text-muted">({sc.model})</span>
                              </td>
                              <td className="py-2.5 pr-3">{sc.role}</td>
                              <td className="py-2.5 pr-3">
                                <Badge tone={RISK_META[sc.risk_class]?.tone ?? "neutral"}>
                                  {RISK_META[sc.risk_class]?.label ?? sc.risk_class}
                                </Badge>
                              </td>
                              <td className="py-2.5 pr-3 font-mono ns-tabular">{sc.sample_tasks}</td>
                              <td className="py-2.5 pr-3 font-mono ns-tabular">{accPct}%</td>
                              <td className="py-2.5 pr-3 font-mono ns-tabular">{sc.avg_retries.toFixed(2)}</td>
                              <td className="py-2.5 pr-3 font-mono ns-tabular">{sc.regression_count}</td>
                              <td className="py-2.5 pr-3 font-mono ns-tabular">
                                ${sc.cost_per_accepted_usd_est.toFixed(3)}
                              </td>
                              <td className="py-2.5">
                                <Tooltip content="Requires >= 10 tasks, acceptance above risk threshold, <= 5% regressions, and <= 1.5 avg retries.">
                                  <Badge tone={gradTone}>{gradLabel}</Badge>
                                </Tooltip>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              ),
            },
            {
              id: "ledger",
              label: "Task Ledger",
              icon: "ListOrdered",
              content: (
                <div className="flex flex-col gap-4 pt-2">
                  <div className="flex flex-wrap items-center gap-2">
                    {["all", "docs_format", "ui_cosmetic", "business_logic", "high_risk", "governance"].map(
                      (rk) => (
                        <Button
                          key={rk}
                          size="sm"
                          variant={riskFilter === rk ? "primary" : "secondary"}
                          onClick={() => setRiskFilter(rk)}
                        >
                          {rk === "all" ? "All risks" : RISK_META[rk]?.label ?? rk}
                        </Button>
                      ),
                    )}
                  </div>

                  <div className="flex flex-col gap-3">
                    {filteredTasks.map((task) => {
                      const taskLedger = (state?.ledger ?? []).filter((l) => l.task_id === task.id);
                      const tokensTotal = taskLedger.reduce((acc, r) => acc + r.tokens_in + r.tokens_out, 0);
                      const ciMin = taskLedger.reduce((acc, r) => acc + r.ci_minutes, 0);
                      const feedItems: StepFeedItem[] = task.attempts.map((a) => ({
                        id: `${task.id}-${a.id}`,
                        message: `${a.stage} (${a.role}) via ${a.engine_id} [${a.provider}]: ${a.outcome}`,
                        relativeTime: `${Math.round(a.wall_seconds)}s`,
                        status: a.outcome === "ok" ? "success" : a.outcome === "blocked" ? "warning" : "info",
                        rawLog: a.decision_reason || undefined,
                      }));

                      return (
                        <div
                          key={task.id}
                          className="rounded-md border border-border-subtle bg-surface-raised p-4"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="text-sm font-semibold text-text-primary">{task.title}</span>
                              <StatusPill state={mapTaskStatusToPill(task.status)} label={task.status} />
                              <Badge tone={RISK_META[task.risk_class]?.tone ?? "neutral"}>
                                {RISK_META[task.risk_class]?.label ?? task.risk_class}
                              </Badge>
                            </div>
                            <span className="font-mono text-xs text-text-muted ns-tabular">
                              ~{tokensTotal.toLocaleString()} tokens (estimate) | {ciMin.toFixed(1)} CI min
                            </span>
                          </div>
                          <p className="mt-1 text-xs text-text-secondary">
                            Branch: <span className="font-mono">{task.branch}</span> | Coders:{" "}
                            <span className="font-mono">{task.contributingProviders.join(", ") || "none"}</span>
                            {task.reviewerProvider ? ` | Reviewer: ${task.reviewerProvider}` : ""}
                          </p>
                          {feedItems.length > 0 && (
                            <div className="mt-3">
                              <StepFeed items={feedItems} emptyText="No attempts recorded." />
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              ),
            },
            {
              id: "maturity",
              label: "Maturity & Rollback",
              icon: "ShieldCheck",
              content: (
                <div className="flex flex-col gap-5 pt-2">
                  {/* Monthly budget bar */}
                  <div className="rounded-md border border-border-subtle bg-surface-raised p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                      <span className="font-medium text-text-primary">Monthly paid API budget (estimate)</span>
                      <span className="font-mono text-text-secondary ns-tabular">
                        {summary?.freeOnlyMode
                          ? "$0.00 / $0.00 (free-only mode)"
                          : `$${(summary?.monthlySpendUsdEst ?? 0).toFixed(2)} / $${(summary?.monthlyCapUsdEst ?? 0).toFixed(2)} (estimate)`}
                      </span>
                    </div>
                    <div className="mt-2 h-2 w-full overflow-hidden rounded-pill border border-border-subtle bg-bg-base">
                      <div
                        className="h-full rounded-pill bg-accent-primary"
                        style={{ width: `${monthlyPct}%` }}
                      />
                    </div>
                  </div>

                  {/* Stage maturity labels (Amendment J) */}
                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-text-muted mb-3">
                      Pipeline stage maturity
                    </h3>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                      {(state?.stageMaturity ?? []).map((st) => (
                        <div
                          key={st.stage}
                          className="flex flex-col gap-1.5 rounded-md border border-border-subtle bg-surface-raised p-3 text-xs"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-mono font-semibold text-text-primary">{st.stage}</span>
                            <Badge tone={MATURITY_TONE[st.maturity] ?? "info"}>{st.maturity}</Badge>
                          </div>
                          <p className="text-text-secondary">{st.description}</p>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Release verifications & one-click rollback (Amendment H) */}
                  <div>
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-text-muted mb-3">
                      Release verifications & rollback
                    </h3>
                    {(state?.releases ?? []).length === 0 ? (
                      <p className="text-xs text-text-muted">No verified releases recorded yet.</p>
                    ) : (
                      <div className="flex flex-col gap-2.5">
                        {(state?.releases ?? []).map((rel) => (
                          <div
                            key={rel.tag}
                            className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border-subtle bg-surface-raised p-3 text-xs"
                          >
                            <div className="min-w-0">
                              <div className="flex flex-wrap items-center gap-2">
                                <span className="font-mono font-semibold text-text-primary">{rel.tag}</span>
                                <Badge
                                  tone={
                                    rel.smoke_status === "passed"
                                      ? "success"
                                      : rel.smoke_status === "rolled_back"
                                        ? "warning"
                                        : "danger"
                                  }
                                >
                                  Smoke: {rel.smoke_status}
                                </Badge>
                                <span className="font-mono text-text-muted">
                                  merge {rel.merge_sha.slice(0, 7)}
                                </span>
                              </div>
                              <p className="mt-1 text-text-secondary">{rel.smoke_detail}</p>
                            </div>

                            {rel.previous_good_tag && rel.smoke_status !== "rolled_back" && (
                              <Button
                                size="sm"
                                variant="danger"
                                icon="RotateCcw"
                                onClick={() => setRollbackTarget(rel)}
                              >
                                Roll back
                              </Button>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ),
            },
          ]}
        />
      </GlassCard>

      {/* Owner approval confirmation dialog */}
      <ConfirmDialog
        open={Boolean(approveTarget)}
        title="Approve task for merge"
        description={
          approveTarget
            ? `Approve ${approveTarget.title} (${RISK_META[approveTarget.risk_class]?.label ?? approveTarget.risk_class}) for commit ${String(approveTarget.head_sha ?? "").slice(0, 7)}.`
            : ""
        }
        consequence="Night-Shift will record your exact-SHA approval and allow the merge gate to proceed."
        confirmLabel="Approve exact commit"
        confirmVariant="primary"
        loading={busyAction}
        onConfirm={() => void handleConfirmApprove()}
        onCancel={() => setApproveTarget(null)}
      />

      {/* One-click rollback confirmation dialog */}
      <ConfirmDialog
        open={Boolean(rollbackTarget)}
        title="Roll back release"
        description={
          rollbackTarget
            ? `Roll back ${rollbackTarget.tag} and restore ${rollbackTarget.previous_good_tag ?? "previous good release"}.`
            : ""
        }
        consequence="The faulty release will be marked superseded and the previous verified artifact and SHA256SUMS will be restored to your output folder."
        confirmLabel="Roll back release"
        confirmVariant="danger"
        loading={busyAction}
        onConfirm={() => void handleConfirmRollback()}
        onCancel={() => setRollbackTarget(null)}
      />
    </div>
  );
}
