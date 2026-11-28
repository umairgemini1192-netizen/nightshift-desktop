import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  type BadgeTone,
  BudgetMeter,
  Button,
  ConfirmDialog,
  EmptyState,
  GlassCard,
  Icon,
  PageHeader,
  ProgressBar,
  Select,
  Skeleton,
  StatTile,
  StatusPill,
  type StatusPillState,
  StepFeed,
  type StepFeedItem,
  Tabs,
  Tooltip,
} from "../../design";
import { useAppStore } from "../../app/store";
import type {
  MaturityLabel,
  PipelineLedgerOverview,
  PipelineModelScore,
  PipelineReleaseVerification,
  PipelineTaskItem,
} from "../../bridge/types";

type TabId = "active" | "scores" | "ledger" | "maturity";
type SortField = "costPerAcceptedUsdEst" | "acceptanceRatePct" | "regressionRatePct" | "sampleTasks" | "avgRetries";

const RISK_META: Record<string, { label: string; tone: BadgeTone }> = {
  docs_format: { label: "Docs", tone: "info" },
  ui_cosmetic: { label: "UI", tone: "info" },
  business_logic: { label: "Logic", tone: "warning" },
  high_risk: { label: "High risk", tone: "danger" },
  governance: { label: "Governance", tone: "danger" },
};

const MATURITY_TONE: Record<MaturityLabel, BadgeTone> = {
  Implemented: "neutral",
  "Integration-tested": "info",
  "Production-proven": "success",
};

function mapTaskStatusToPill(status: string): StatusPillState {
  const s = status.toUpperCase();
  if (s === "RELEASED" || s === "MERGED" || s === "APPROVED") return "done";
  if (s === "RUNNING" || s === "VERIFYING" || s === "REVIEWING") return "running";
  if (s === "FAILED" || s === "BLOCKED") return "failed";
  if (s === "QUEUED") return "waiting";
  return "idle";
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0 s";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const mins = Math.floor(seconds / 60);
  const rem = Math.round(seconds % 60);
  return rem > 0 ? `${mins} m ${rem} s` : `${mins} m`;
}

export default function PipelineLedgerPage() {
  const bridge = useAppStore((s) => s.bridge);
  const snapshot = useAppStore((s) => s.snapshot);
  const pushToast = useAppStore((s) => s.pushToast);
  const navigate = useNavigate();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [overview, setOverview] = useState<PipelineLedgerOverview | null>(null);
  const [activeTab, setActiveTab] = useState<TabId>("active");
  const [expandedEvidence, setExpandedEvidence] = useState<Record<string, boolean>>({});
  const [expandedTaskLogs, setExpandedTaskLogs] = useState<Record<string, boolean>>({});

  // Model Scores sorting
  const [sortField, setSortField] = useState<SortField>("costPerAcceptedUsdEst");
  const [sortAsc, setSortAsc] = useState(true);

  // Task Ledger filters
  const [statusFilter, setStatusFilter] = useState("all");
  const [riskFilter, setRiskFilter] = useState("all");

  // Rollback ConfirmDialog state
  const [pendingRollback, setPendingRollback] = useState<PipelineReleaseVerification | null>(null);
  const [rollbackBusy, setRollbackBusy] = useState(false);

  const loadOverview = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await bridge.call<PipelineLedgerOverview>("pipeline_ledger.getOverview", {
        projectId: snapshot.activeProjectId ?? undefined,
      });
      setOverview(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load pipeline ledger data.");
      setOverview(null);
    } finally {
      setLoading(false);
    }
  }, [bridge, snapshot.activeProjectId]);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  const sortedScores = useMemo(() => {
    const list = [...(overview?.modelScores ?? [])];
    list.sort((a: PipelineModelScore, b: PipelineModelScore) => {
      const av = Number(a[sortField] ?? 0);
      const bv = Number(b[sortField] ?? 0);
      return sortAsc ? av - bv : bv - av;
    });
    return list;
  }, [overview?.modelScores, sortField, sortAsc]);

  const filteredLedgerTasks = useMemo(() => {
    return (overview?.taskLedger ?? []).filter((task) => {
      if (statusFilter !== "all" && task.status.toUpperCase() !== statusFilter) return false;
      if (riskFilter !== "all" && task.riskClass !== riskFilter) return false;
      return true;
    });
  }, [overview?.taskLedger, statusFilter, riskFilter]);

  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortAsc((prev) => !prev);
    } else {
      setSortField(field);
      setSortAsc(field === "costPerAcceptedUsdEst" || field === "regressionRatePct" || field === "avgRetries");
    }
  };

  const handleConfirmRollback = async () => {
    if (!pendingRollback) return;
    setRollbackBusy(true);
    try {
      const res = await bridge.call<{ restored_tag?: string; detail?: string }>("pipeline.rollbackRelease", {
        tag: pendingRollback.tag,
        repo: pendingRollback.repo,
        projectId: snapshot.activeProjectId ?? undefined,
      });
      pushToast({
        title: "Release rolled back",
        description:
          res?.detail ??
          `Marked ${pendingRollback.tag} as superseded and restored ${res?.restored_tag ?? pendingRollback.previousGoodTag ?? "previous good release"}.`,
        tone: "warning",
      });
      setPendingRollback(null);
      await loadOverview();
    } catch (err) {
      pushToast({
        title: "Could not roll back release",
        description: err instanceof Error ? err.message : String(err),
        tone: "danger",
      });
    } finally {
      setRollbackBusy(false);
    }
  };

  const summary = overview?.summary;
  const isEmpty =
    !loading &&
    !error &&
    overview !== null &&
    overview.activeTasks.length === 0 &&
    overview.modelScores.length === 0 &&
    overview.taskLedger.length === 0;

  const monthlyPct =
    summary && summary.monthlyBudget.capUsdEst > 0
      ? Math.min(100, Math.round((summary.monthlyBudget.spentUsdEst / summary.monthlyBudget.capUsdEst) * 100))
      : 0;

  const renderTaskAttemptsFeed = (task: PipelineTaskItem): StepFeedItem[] =>
    task.attempts.map((a) => ({
      id: `${task.id}-att-${a.id}`,
      message: `${a.stage} (${a.role}) via ${a.engineId} (${a.provider}) - ${a.outcome}`,
      relativeTime: formatDuration(a.wallSeconds),
      status: a.outcome === "ok" ? "success" : a.outcome === "blocked" ? "warning" : "danger",
      rawLog: [a.decisionReason, a.outputRef ? `Redacted log: ${a.outputRef}` : ""].filter(Boolean).join("\n"),
    }));

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Pipeline ledger & scores"
        description="Track active pipeline stages, evidence checks, and what each AI model costs per accepted change (estimates)."
        action={
          <div className="flex flex-wrap items-center gap-2">
            {summary && (
              <Badge tone={summary.freeOnlyMode ? "success" : "warning"}>
                <Icon name="ShieldCheck" size={16} />
                <span>{summary.freeOnlyMode ? "Free-only mode" : "Paid caps active"}</span>
              </Badge>
            )}
            <Button variant="secondary" size="sm" icon="RefreshCw" onClick={() => void loadOverview()}>
              Refresh
            </Button>
          </div>
        }
      />

      {/* Error state */}
      {error && (
        <GlassCard variant="raised" title="Could not load pipeline ledger" icon="AlertTriangle" className="border-status-danger/40">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <p className="text-sm text-text-secondary">{error}</p>
            <Button variant="primary" size="sm" icon="RefreshCw" onClick={() => void loadOverview()}>
              Retry
            </Button>
          </div>
        </GlassCard>
      )}

      {/* Top 4 KPI StatTiles (12-col grid) */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-12 gap-5">
        <StatTile
          className="lg:col-span-3"
          label="Accepted changes (30d)"
          value={loading ? "—" : String(summary?.acceptedChanges30d ?? 0)}
          delta={loading ? undefined : `${summary?.tonightBudget.invocations ?? 0} AI calls tonight`}
          deltaTone="positive"
          loading={loading}
        />
        <StatTile
          className="lg:col-span-3"
          label="Cost per accepted change (estimate)"
          value={loading ? "—" : `$${(summary?.costPerAcceptedUsdEst ?? 0).toFixed(2)}`}
          delta={loading ? undefined : summary?.tonightBudget.displayLine ?? "$0.00 / $0.00 (free-only mode)"}
          deltaTone="neutral"
          loading={loading}
        />
        <StatTile
          className="lg:col-span-3"
          label="Regression rate"
          value={loading ? "—" : `${(summary?.regressionRatePct ?? 0).toFixed(1)}%`}
          delta={loading ? undefined : "Target <= 5.0% for graduation"}
          deltaTone={(summary?.regressionRatePct ?? 0) > 5 ? "negative" : "positive"}
          loading={loading}
        />
        <StatTile
          className="lg:col-span-3"
          label="Gold saved (estimate)"
          value={loading ? "—" : `${summary?.goldSavedTasks ?? 0} tasks`}
          delta={loading ? undefined : "Completed by graduated Economy models"}
          deltaTone="positive"
          loading={loading}
        />
      </div>

      {/* Loading skeleton */}
      {loading && (
        <GlassCard title="Loading pipeline data" icon="Activity">
          <div className="flex flex-col gap-3">
            <Skeleton variant="text" lines={2} />
            <Skeleton variant="rect" />
            <Skeleton variant="text" lines={2} />
          </div>
        </GlassCard>
      )}

      {/* Empty state */}
      {isEmpty && (
        <EmptyState
          icon="BarChart3"
          title="No pipeline tasks recorded yet"
          description="Start an overnight run from Mission Control to track 9-stage pipeline progress, model graduation scores, and estimated cost per accepted change."
          actionLabel="Open Mission Control"
          onAction={() => navigate("/")}
        />
      )}

      {/* Main 4-Tab workspace */}
      {!loading && !error && !isEmpty && overview && (
        <GlassCard title="Pipeline governance & economics" icon="BarChart3">
          <Tabs
            label="Pipeline ledger sections"
            value={activeTab}
            onChange={(id) => setActiveTab(id as TabId)}
            items={[
              {
                id: "active",
                label: "Active Pipeline",
                icon: "Activity",
                content: (
                  <div className="flex flex-col gap-4">
                    {overview.activeTasks.length === 0 ? (
                      <p className="py-4 text-sm text-text-muted">
                        No active tasks in progress. Completed tasks appear in the Task Ledger tab.
                      </p>
                    ) : (
                      overview.activeTasks.map((task) => {
                        const risk = RISK_META[task.riskClass] ?? { label: task.riskClass, tone: "neutral" as BadgeTone };
                        const detailsOpen = Boolean(expandedEvidence[task.id]);
                        return (
                          <div
                            key={task.id}
                            className="rounded-lg border border-border-subtle bg-surface-raised p-4 flex flex-col gap-4"
                          >
                            <div className="flex flex-wrap items-start justify-between gap-3">
                              <div className="min-w-0">
                                <div className="flex flex-wrap items-center gap-2">
                                  <h3 className="text-sm font-semibold text-text-primary">{task.title}</h3>
                                  <StatusPill state={mapTaskStatusToPill(task.status)} label={task.status} />
                                  <Badge tone={risk.tone}>{risk.label}</Badge>
                                  <Badge tone={MATURITY_TONE[task.maturity] ?? "info"}>{task.maturity}</Badge>
                                </div>
                                <p className="mt-1 font-mono text-xs text-text-muted ns-tabular">
                                  {task.branch}
                                  {task.prNumber ? ` · PR #${task.prNumber}` : ""}
                                  {task.headSha ? ` · ${task.headSha.slice(0, 7)}` : ""}
                                  {task.contractSha256 ? ` · contract ${task.contractSha256}` : ""}
                                </p>
                              </div>
                              <Button
                                variant="ghost"
                                size="sm"
                                icon={detailsOpen ? "ChevronUp" : "ChevronDown"}
                                onClick={() =>
                                  setExpandedEvidence((prev) => ({ ...prev, [task.id]: !prev[task.id] }))
                                }
                              >
                                {detailsOpen ? "Hide details" : "Details"}
                              </Button>
                            </div>

                            {/* 9-stage segmented ProgressBar */}
                            <ProgressBar
                              label={`9-stage pipeline for ${task.id}`}
                              segments={task.stages.map((s) => ({
                                id: s.id,
                                label: `${s.label} (${s.executor})`,
                                status: s.status,
                              }))}
                            />

                            <div className="flex flex-wrap items-center gap-4 text-xs text-text-secondary">
                              <span>
                                Contributing providers:{" "}
                                <strong className="text-text-primary">
                                  {task.contributingProviders.length > 0 ? task.contributingProviders.join(", ") : "None yet"}
                                </strong>
                              </span>
                              <span>
                                Independent reviewer:{" "}
                                <strong className="text-text-primary">{task.reviewerProvider ?? "Pending"}</strong>
                              </span>
                              <span className="font-mono ns-tabular">
                                ~${task.costUsdEstimate.toFixed(2)} (estimate)
                              </span>
                            </div>

                            {detailsOpen && (
                              <div className="border-t border-border-subtle pt-3 flex flex-col gap-2">
                                <p className="text-xs font-medium text-text-secondary">
                                  Verified evidence & artifacts
                                </p>
                                {task.evidence.length === 0 ? (
                                  <p className="text-xs text-text-muted">No evidence records captured yet.</p>
                                ) : (
                                  <ul className="flex flex-col gap-2">
                                    {task.evidence.map((ev) => (
                                      <li
                                        key={ev.id}
                                        className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border-subtle bg-bg-base px-3 py-2 text-xs"
                                      >
                                        <div className="flex items-center gap-2">
                                          <Badge tone={ev.verified ? "success" : "danger"}>
                                            {ev.verified ? "Verified" : "Rejected"}
                                          </Badge>
                                          <span className="font-medium text-text-primary">{ev.kind}</span>
                                          <span className="font-mono text-text-muted">{ev.source}</span>
                                        </div>
                                        <span className="font-mono text-text-muted ns-tabular">
                                          {ev.commitSha} · {ev.durationSeconds.toFixed(1)} s
                                          {ev.rejectionReason ? ` · ${ev.rejectionReason}` : ""}
                                        </span>
                                      </li>
                                    ))}
                                  </ul>
                                )}
                              </div>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>
                ),
              },
              {
                id: "scores",
                label: "Model Scores",
                icon: "Cpu",
                content: (
                  <div className="flex flex-col gap-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text-muted">
                      <span>
                        Economy models must pass 10 benchmark tasks with acceptance above class threshold, regression &lt;= 5%, avg retries &lt;= 1.5, and lower cost per accepted change (estimate) than Standard.
                      </span>
                    </div>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-xs border-collapse">
                        <thead>
                          <tr className="border-b border-border-subtle text-text-muted">
                            <th className="py-2 pr-3 font-medium">Model</th>
                            <th className="py-2 pr-3 font-medium">Role</th>
                            <th className="py-2 pr-3 font-medium">Risk class</th>
                            <th className="py-2 pr-3 font-medium">
                              <button
                                type="button"
                                onClick={() => toggleSort("sampleTasks")}
                                className="inline-flex items-center gap-1 hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded-sm"
                              >
                                <span>Sample tasks</span>
                                <Icon name="ArrowUpDown" size={16} />
                              </button>
                            </th>
                            <th className="py-2 pr-3 font-medium">
                              <button
                                type="button"
                                onClick={() => toggleSort("acceptanceRatePct")}
                                className="inline-flex items-center gap-1 hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded-sm"
                              >
                                <span>Acceptance rate</span>
                                <Icon name="ArrowUpDown" size={16} />
                              </button>
                            </th>
                            <th className="py-2 pr-3 font-medium">
                              <button
                                type="button"
                                onClick={() => toggleSort("avgRetries")}
                                className="inline-flex items-center gap-1 hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded-sm"
                              >
                                <span>Avg retries</span>
                                <Icon name="ArrowUpDown" size={16} />
                              </button>
                            </th>
                            <th className="py-2 pr-3 font-medium">
                              <button
                                type="button"
                                onClick={() => toggleSort("regressionRatePct")}
                                className="inline-flex items-center gap-1 hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded-sm"
                              >
                                <span>Regression rate</span>
                                <Icon name="ArrowUpDown" size={16} />
                              </button>
                            </th>
                            <th className="py-2 pr-3 font-medium">
                              <button
                                type="button"
                                onClick={() => toggleSort("costPerAcceptedUsdEst")}
                                className="inline-flex items-center gap-1 hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded-sm"
                              >
                                <span>Cost / accepted (estimate)</span>
                                <Icon name="ArrowUpDown" size={16} />
                              </button>
                            </th>
                            <th className="py-2 font-medium">Graduation</th>
                          </tr>
                        </thead>
                        <tbody>
                          {sortedScores.map((row) => {
                            const risk = RISK_META[row.riskClass] ?? { label: row.riskClass, tone: "neutral" as BadgeTone };
                            const gradTone: BadgeTone =
                              row.graduationStatus === "graduated"
                                ? "success"
                                : row.graduationStatus === "demoted"
                                  ? "danger"
                                  : "warning";
                            return (
                              <tr
                                key={`${row.engineId}-${row.model}-${row.role}-${row.riskClass}`}
                                className="border-b border-border-subtle/60"
                              >
                                <td className="py-2.5 pr-3">
                                  <div className="font-medium text-text-primary">{row.engineId}</div>
                                  <div className="font-mono text-2xs text-text-muted">{row.model}</div>
                                </td>
                                <td className="py-2.5 pr-3 text-text-secondary">{row.role}</td>
                                <td className="py-2.5 pr-3">
                                  <Badge tone={risk.tone}>{risk.label}</Badge>
                                </td>
                                <td className="py-2.5 pr-3 font-mono ns-tabular text-text-primary">
                                  {row.sampleTasks}
                                </td>
                                <td className="py-2.5 pr-3 font-mono ns-tabular text-text-primary">
                                  {row.acceptanceRatePct.toFixed(1)}%
                                </td>
                                <td className="py-2.5 pr-3 font-mono ns-tabular text-text-primary">
                                  {row.avgRetries.toFixed(2)}
                                </td>
                                <td className="py-2.5 pr-3 font-mono ns-tabular text-text-primary">
                                  {row.regressionRatePct.toFixed(1)}%
                                </td>
                                <td className="py-2.5 pr-3 font-mono ns-tabular text-text-primary">
                                  ${row.costPerAcceptedUsdEst.toFixed(2)} (estimate)
                                </td>
                                <td className="py-2.5">
                                  <Tooltip
                                    content={`Requires >= 10 samples, >= ${row.thresholdPct}% acceptance, <= 5% regression, avg retries <= 1.5, and lower cost than Standard.`}
                                  >
                                    <Badge tone={gradTone}>{row.graduationLabel}</Badge>
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
                icon: "ListChecks",
                content: (
                  <div className="flex flex-col gap-4">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 max-w-lg">
                      <Select
                        id="ledger-status-filter"
                        label="Filter by status"
                        value={statusFilter}
                        onChange={setStatusFilter}
                        options={[
                          { value: "all", label: "All statuses" },
                          { value: "REVIEWING", label: "Reviewing" },
                          { value: "APPROVED", label: "Approved" },
                          { value: "RELEASED", label: "Released" },
                          { value: "BLOCKED", label: "Blocked" },
                          { value: "FAILED", label: "Failed" },
                        ]}
                      />
                      <Select
                        id="ledger-risk-filter"
                        label="Filter by risk class"
                        value={riskFilter}
                        onChange={setRiskFilter}
                        options={[
                          { value: "all", label: "All risk classes" },
                          { value: "docs_format", label: "Docs" },
                          { value: "ui_cosmetic", label: "UI" },
                          { value: "business_logic", label: "Logic" },
                          { value: "high_risk", label: "High risk" },
                          { value: "governance", label: "Governance" },
                        ]}
                      />
                    </div>

                    {filteredLedgerTasks.length === 0 ? (
                      <p className="py-4 text-sm text-text-muted">No tasks match the selected filters.</p>
                    ) : (
                      <div className="flex flex-col gap-3">
                        {filteredLedgerTasks.map((task) => {
                          const risk = RISK_META[task.riskClass] ?? { label: task.riskClass, tone: "neutral" as BadgeTone };
                          const logsOpen = Boolean(expandedTaskLogs[task.id]);
                          return (
                            <div
                              key={task.id}
                              className="rounded-lg border border-border-subtle bg-surface-raised p-4 flex flex-col gap-3"
                            >
                              <div className="flex flex-wrap items-start justify-between gap-3">
                                <div className="min-w-0">
                                  <div className="flex flex-wrap items-center gap-2">
                                    <span className="text-sm font-semibold text-text-primary">{task.title}</span>
                                    <StatusPill state={mapTaskStatusToPill(task.status)} label={task.status} />
                                    <Badge tone={risk.tone}>{risk.label}</Badge>
                                  </div>
                                  <p className="mt-1 text-xs text-text-secondary">
                                    Coder/Fixer: {task.contributingProviders.join(", ") || "none"} · Reviewer:{" "}
                                    {task.reviewerProvider ?? "none"}
                                  </p>
                                </div>
                                <div className="flex items-center gap-3 text-xs font-mono text-text-muted ns-tabular">
                                  <span>~{task.tokensEstimate.toLocaleString()} tokens (estimate)</span>
                                  <span>~{task.ciMinutesEstimate.toFixed(1)} CI min (estimate)</span>
                                  <span>{formatDuration(task.wallSeconds)}</span>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={logsOpen ? "ChevronUp" : "ChevronDown"}
                                    onClick={() =>
                                      setExpandedTaskLogs((prev) => ({ ...prev, [task.id]: !prev[task.id] }))
                                    }
                                  >
                                    {logsOpen ? "Hide steps" : "Steps"}
                                  </Button>
                                </div>
                              </div>

                              {logsOpen && (
                                <div className="border-t border-border-subtle pt-3">
                                  <StepFeed
                                    items={renderTaskAttemptsFeed(task)}
                                    emptyText="No stage attempts recorded for this task."
                                  />
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ),
              },
              {
                id: "maturity",
                label: "Maturity & Rollback",
                icon: "ShieldCheck",
                content: (
                  <div className="flex flex-col gap-5">
                    {/* Monthly budget bar */}
                    {summary && (
                      <BudgetMeter
                        label={`Monthly budget (${summary.monthlyBudget.monthLabel})`}
                        valueText={`~$${summary.monthlyBudget.spentUsdEst.toFixed(2)} / $${summary.monthlyBudget.capUsdEst.toFixed(2)} (estimate)`}
                        percent={monthlyPct}
                      />
                    )}

                    {/* Stage maturity matrix (Amendment J) */}
                    <div>
                      <h3 className="text-sm font-semibold text-text-primary mb-2">
                        Pipeline stage maturity (Amendment J)
                      </h3>
                      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                        {overview.stageMaturity.map((st) => (
                          <div
                            key={st.stage}
                            className="rounded-md border border-border-subtle bg-surface-raised p-3 flex flex-col gap-1.5"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-xs font-semibold text-text-primary">
                                {st.stage} · {st.executor}
                              </span>
                              <Badge tone={MATURITY_TONE[st.maturity] ?? "info"}>{st.maturity}</Badge>
                            </div>
                            <p className="text-xs text-text-muted">{st.evidence}</p>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Recent release verifications & one-click rollback */}
                    <div>
                      <h3 className="text-sm font-semibold text-text-primary mb-2">
                        Recent release verifications & rollback
                      </h3>
                      {overview.releaseVerifications.length === 0 ? (
                        <p className="text-xs text-text-muted">No release verifications recorded yet.</p>
                      ) : (
                        <div className="flex flex-col gap-2.5">
                          {overview.releaseVerifications.map((rel) => {
                            const canRollback =
                              Boolean(rel.previousGoodTag) && rel.smokeStatus !== "rolled_back";
                            const smokeTone: BadgeTone =
                              rel.smokeStatus === "passed"
                                ? "success"
                                : rel.smokeStatus === "failed"
                                  ? "danger"
                                  : "warning";
                            return (
                              <div
                                key={`${rel.repo}-${rel.tag}`}
                                className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-md border border-border-subtle bg-surface-raised p-3 text-xs"
                              >
                                <div className="min-w-0">
                                  <div className="flex flex-wrap items-center gap-2">
                                    <span className="font-mono font-semibold text-text-primary">{rel.tag}</span>
                                    <Badge tone={smokeTone}>Smoke: {rel.smokeStatus}</Badge>
                                    <span className="font-mono text-text-muted">{rel.mergeSha}</span>
                                  </div>
                                  <p className="mt-1 text-text-secondary">{rel.smokeDetail}</p>
                                  {rel.previousGoodTag && (
                                    <p className="mt-0.5 text-text-muted">
                                      Rollback target: <span className="font-mono">{rel.previousGoodTag}</span>
                                    </p>
                                  )}
                                </div>
                                {canRollback && (
                                  <Button
                                    variant="danger"
                                    size="sm"
                                    icon="RotateCcw"
                                    onClick={() => setPendingRollback(rel)}
                                  >
                                    Roll back
                                  </Button>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  </div>
                ),
              },
            ]}
          />
        </GlassCard>
      )}

      {/* One-click rollback ConfirmDialog */}
      <ConfirmDialog
        open={pendingRollback !== null}
        title="Roll back release"
        description={
          pendingRollback
            ? `Roll back ${pendingRollback.tag} to ${pendingRollback.previousGoodTag ?? "previous good release"}?`
            : "Confirm release rollback."
        }
        consequence={
          pendingRollback
            ? `This appends a superseded warning to ${pendingRollback.tag} and restores ${pendingRollback.previousGoodTag ?? "the previous verified release"} with its SHA256SUMS into your output folder.`
            : undefined
        }
        confirmLabel="Roll back release"
        confirmVariant="danger"
        cancelLabel="Cancel"
        loading={rollbackBusy}
        onConfirm={() => void handleConfirmRollback()}
        onCancel={() => setPendingRollback(null)}
      />
    </div>
  );
}
