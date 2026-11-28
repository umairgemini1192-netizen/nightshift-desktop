import { type ChangeEvent, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  CodeEditor,
  ConfirmDialog,
  EmptyState,
  GlassCard,
  Icon,
  PageHeader,
  PhaseTimeline,
  type PhaseTimelineItem,
  ProgressRing,
  StatusPill,
  type StatusPillState,
  StepFeed,
  type StepFeedItem,
  Tabs,
} from "../../design";
import { useAppStore } from "../../app/store";
import type { NeedAttentionItem } from "../../bridge/types";
import { TokenSavingsCard } from "../token-optimizer/TokenSavingsCard";

const TOOL_DISPLAY_NAMES: Record<string, string> = {
  git: "Git",
  gh: "GitHub CLI",
  agy: "Antigravity",
  gemini: "Gemini CLI",
  codex: "Codex CLI",
  claude: "Claude Code",
};

export default function MissionControlPage() {
  const snapshot = useAppStore((s) => s.snapshot);
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);
  const refreshSnapshot = useAppStore((s) => s.refreshSnapshot);
  const navigate = useNavigate();

  const [activeTab, setActiveTab] = useState<"feed" | "timeline">("feed");
  const [readinessModalOpen, setReadinessModalOpen] = useState(false);
  const [fixingTool, setFixingTool] = useState<string | null>(null);
  const [stopArmedLocal, setStopArmedLocal] = useState<boolean>(Boolean(snapshot.stopArmed));
  const [isFirstRun, setIsFirstRun] = useState(false);
  const [showFirstRunImport, setShowFirstRunImport] = useState(false);
  const [firstRunQueueJson, setFirstRunQueueJson] = useState("");
  const [importingQueue, setImportingQueue] = useState(false);
  const [pendingApproval, setPendingApproval] = useState<NeedAttentionItem | null>(null);
  const [pendingRollback, setPendingRollback] = useState<NeedAttentionItem | null>(null);
  const [governanceBusy, setGovernanceBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setStopArmedLocal(Boolean(snapshot.stopArmed));
  }, [snapshot.stopArmed]);

  useEffect(() => {
    if (snapshot.scenario !== "empty") {
      setIsFirstRun(false);
      return;
    }
    let cancelled = false;
    void bridge
      .call<{ isFirstRun?: boolean }>("app.firstRunStatus")
      .then((res) => {
        if (!cancelled) {
          setIsFirstRun(Boolean(res?.isFirstRun));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setIsFirstRun(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [bridge, snapshot.scenario]);

  const handleDismissFirstRun = async () => {
    try {
      await bridge.call("app.dismissFirstRun");
    } catch {
      // Ignore dismiss failure and hide banner locally
    }
    setIsFirstRun(false);
    setShowFirstRunImport(false);
  };

  const handleFirstRunFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      setFirstRunQueueJson(text);
    } catch (err) {
      pushToast({
        title: "Could not read file",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleFirstRunImport = async () => {
    const payload =
      firstRunQueueJson.trim() ||
      JSON.stringify({
        repo: "owner/imported-repo",
        local_repo: "D:/Projects/imported-repo",
        phases: [{ id: "p1", title: "Imported Phase 1", branch: "feat/p1", ladder: "backend" }],
      });
    setImportingQueue(true);
    try {
      const parsed = JSON.parse(payload);
      await bridge.call("plan.importQueue", { queue: parsed });
      setShowFirstRunImport(false);
      setIsFirstRun(false);
      setFirstRunQueueJson("");
      pushToast({
        title: "Legacy queue imported",
        description: "Your project, phases, and failover ladders are ready in Plan Builder.",
        tone: "success",
      });
      await refreshSnapshot();
    } catch (err) {
      pushToast({
        title: "Could not import queue.json",
        description: err instanceof Error ? err.message : String(err),
        tone: "danger",
      });
    } finally {
      setImportingQueue(false);
    }
  };

  // Re-check logins on window focus (BLUEPRINT 5f)
  useEffect(() => {
    const onFocus = () => {
      void bridge.call("logins.recheck", { repo: snapshot.projects[0]?.repo ?? "owner/repo" }).then(() => {
        void refreshSnapshot();
      });
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [bridge, refreshSnapshot, snapshot.projects]);

  const isLive = snapshot.runState === "running";
  const deck = snapshot.deck;
  const logins = snapshot.logins ?? {};

  // Inspect readiness
  const loginEntries = Object.entries(logins);
  const unreadyLogins = loginEntries.filter(
    ([, item]) => item.state !== "Ready" && item.state !== "Disabled by owner",
  );
  const readyImplementers = loginEntries.filter(
    ([name, item]) => ["agy", "gemini", "codex", "claude"].includes(name) && item.state === "Ready",
  );
  const canStartAnyway = readyImplementers.length > 0;

  // Handle Stop After Phase toggle
  const handleStopToggle = async () => {
    if (stopArmedLocal) {
      const res = await bridge.call<{ armed: boolean; message: string }>("engine.undoStop");
      setStopArmedLocal(false);
      pushToast({
        title: "Stop cancelled",
        description: res?.message ?? "Stop after current phase cancelled.",
        tone: "info",
      });
    } else {
      const res = await bridge.call<{ armed: boolean; message: string }>("engine.stopAfterPhase");
      setStopArmedLocal(true);
      pushToast({
        title: "Stop armed",
        description: res?.message ?? "Night-Shift will stop after the current phase finishes.",
        tone: "warning",
      });
    }
    await refreshSnapshot();
  };

  // Handle Fix login button
  const handleFixTool = async (tool: string) => {
    setFixingTool(tool);
    try {
      await bridge.call("logins.openFix", { tool });
      pushToast({
        title: "Terminal opened",
        description: `Opened login command for ${TOOL_DISPLAY_NAMES[tool] ?? tool}. Complete login, then return here.`,
        tone: "info",
      });
    } catch {
      pushToast({
        title: "Could not open login command",
        description: `Check that ${tool} is installed and in your PATH.`,
        tone: "danger",
      });
    } finally {
      setFixingTool(null);
    }
  };

  // Handle Start run
  const handleStartRun = () => {
    if (unreadyLogins.length > 0) {
      setReadinessModalOpen(true);
      return;
    }
    void executeStart();
  };

  const executeStart = async () => {
    setReadinessModalOpen(false);
    try {
      await bridge.call("engine.start");
      pushToast({
        title: "Run started",
        description: "Night-Shift overnight run is now active.",
        tone: "success",
      });
      await refreshSnapshot();
    } catch (err) {
      pushToast({
        title: "Could not start run",
        description: err instanceof Error ? err.message : "Engine failed to start.",
        tone: "danger",
      });
    }
  };

  // Handle Needs Attention governance & navigation actions (Phase 8e)
  const handleNeedsAttentionAction = (item: NeedAttentionItem) => {
    const kind = item.action_kind;
    const actionLower = item.action.toLowerCase();

    if (kind === "approve_task" || (item.task_id && item.head_sha && actionLower.includes("approve"))) {
      if (!item.task_id || !item.head_sha) {
        pushToast({
          title: "Cannot approve task",
          description: "Task identifier or commit SHA is missing. Inspect run logs first.",
          tone: "danger",
        });
        return;
      }
      setPendingApproval(item);
      return;
    }

    if (kind === "rollback_release" || (item.release_tag && actionLower.includes("roll back"))) {
      if (!item.release_tag) {
        pushToast({
          title: "Cannot roll back release",
          description: "Release tag is missing. Open Settings or Runs to inspect release history.",
          tone: "danger",
        });
        return;
      }
      setPendingRollback(item);
      return;
    }

    if (
      kind === "configure_smoke_test" ||
      kind === "open_settings" ||
      actionLower.includes("smoke test") ||
      actionLower.includes("settings")
    ) {
      navigate("/settings");
      return;
    }

    if (kind === "fix_login" || actionLower.includes("fix")) {
      const tool = unreadyLogins[0]?.[0];
      if (tool) {
        void handleFixTool(tool);
        return;
      }
      navigate("/settings");
      return;
    }

    if (kind === "open_runs" || actionLower.includes("inspect")) {
      navigate("/runs");
      return;
    }

    navigate("/runs");
  };

  const handleConfirmApproval = async () => {
    if (!pendingApproval?.task_id || !pendingApproval?.head_sha) return;
    setGovernanceBusy(true);
    try {
      await bridge.call("pipeline.approveTask", {
        taskId: pendingApproval.task_id,
        headSha: pendingApproval.head_sha,
        projectId: pendingApproval.project_id ?? snapshot.activeProjectId ?? undefined,
        approvedBy: "owner",
        resume: true,
      });
      pushToast({
        title: "Task approved",
        description: `Approved commit ${pendingApproval.head_sha.slice(0, 7)} and resumed the merge gate.`,
        tone: "success",
      });
      setPendingApproval(null);
      await refreshSnapshot();
    } catch (err) {
      pushToast({
        title: "Could not approve task",
        description: err instanceof Error ? err.message : String(err),
        tone: "danger",
      });
    } finally {
      setGovernanceBusy(false);
    }
  };

  const handleConfirmRollback = async () => {
    if (!pendingRollback?.release_tag) return;
    setGovernanceBusy(true);
    try {
      const res = await bridge.call<{ restored_tag?: string; detail?: string }>("pipeline.rollbackRelease", {
        tag: pendingRollback.release_tag,
        repo: pendingRollback.repo,
        projectId: pendingRollback.project_id ?? snapshot.activeProjectId ?? undefined,
      });
      pushToast({
        title: "Release rolled back",
        description:
          res?.detail ??
          `Marked ${pendingRollback.release_tag} as superseded and restored ${res?.restored_tag ?? pendingRollback.previous_good_tag ?? "previous good release"}.`,
        tone: "warning",
      });
      setPendingRollback(null);
      await refreshSnapshot();
    } catch (err) {
      pushToast({
        title: "Could not roll back release",
        description: err instanceof Error ? err.message : String(err),
        tone: "danger",
      });
    } finally {
      setGovernanceBusy(false);
    }
  };

  // Timeline items
  const timelineItems: PhaseTimelineItem[] = useMemo(() => {
    const items: PhaseTimelineItem[] = [];
    if (deck?.done_tonight) {
      for (const ph of deck.done_tonight) {
        items.push({
          id: ph.phase_id,
          title: ph.title,
          status: ph.status === "parked" ? "failed" : "done",
          detail: ph.runners ? `Completed by ${ph.runners.join(", ")}` : "Merged to main",
        });
      }
    }
    if (deck?.now?.phase) {
      items.push({
        id: "current_phase",
        title: deck.now.phase,
        status: "current",
        detail: deck.now.step ?? "In progress",
      });
    }
    if (deck?.next) {
      for (const ph of deck.next) {
        items.push({
          id: ph.phase_id,
          title: ph.title,
          status: "next",
          detail: "Scheduled",
        });
      }
    }
    return items;
  }, [deck]);

  // Step feed items
  const stepFeedItems: StepFeedItem[] = useMemo(() => {
    if (!deck) {
      return [
        {
          id: "step_idle",
          message: "Ready for overnight execution. Select a plan to begin.",
          relativeTime: "Just now",
          status: "info",
        },
      ];
    }
    const items: StepFeedItem[] = [];
    if (deck.now?.step) {
      items.push({
        id: "step_now",
        message: deck.now.step,
        relativeTime: "Active now",
        status: isLive ? "running" : "info",
        rawLog: deck.now.why ? `Router rationale: ${deck.now.why}` : undefined,
      });
    }
    if (deck.problems) {
      items.push({
        id: "step_problems",
        message: deck.problems,
        relativeTime: "Current",
        status: "warning",
      });
    }
    for (const d of deck.done_tonight ?? []) {
      items.push({
        id: `step_done_${d.phase_id}`,
        message: `${d.title} (${d.status})`,
        relativeTime: "Completed tonight",
        status: d.status === "parked" ? "danger" : "success",
        rawLog: d.pr_number ? `PR #${d.pr_number} merged cleanly. Runners: ${(d.runners ?? []).join(", ")}` : undefined,
      });
    }
    return items;
  }, [deck, isLive]);

  // Total phases and progress
  const doneCount = deck?.done_tonight?.filter((p) => p.status === "merged" || p.status === "completed").length ?? 0;
  const totalPhases = (deck?.done_tonight?.length ?? 0) + (deck?.now?.phase ? 1 : 0) + (deck?.next?.length ?? 0);
  const progressPct = totalPhases > 0 ? Math.round((doneCount / totalPhases) * 100) : 0;

  if (snapshot.scenario === "empty") {
    return (
      <div className="flex flex-col gap-5">
        <PageHeader
          title="Mission Control"
          description={snapshot.headline}
          action={<StatusPill state={snapshot.runState} />}
        />

        {isFirstRun && (
          <div id="first-run-import-card">
            <GlassCard
              variant="hero"
              title="Welcome to Night-Shift"
              icon="Upload"
              action={<Badge tone="info">First run</Badge>}
            >
              <div className="flex flex-col gap-4">
                <p className="max-w-prose text-sm text-text-secondary">
                  Already have a legacy queue.json from the previous Night-Shift script? Import it now to bring in
                  your project, phases, and failover ladders without retyping.
                </p>
                <div className="flex flex-wrap items-center gap-3">
                  <Button
                    variant="primary"
                    size="md"
                    icon="Upload"
                    onClick={() => setShowFirstRunImport(true)}
                  >
                    Import legacy queue.json
                  </Button>
                  <Button
                    variant="secondary"
                    size="md"
                    onClick={() => void handleDismissFirstRun()}
                  >
                    Start fresh
                  </Button>
                </div>
              </div>
            </GlassCard>
          </div>
        )}

        <EmptyState
          icon="Radar"
          title="Connect a project to start overnight runs"
          description="Add a repository and target branch in Settings to start autonomous nightly coding runs."
          actionLabel="Open Settings"
          onAction={() => navigate("/settings")}
        />

        {showFirstRunImport && (
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="first-run-import-title"
            className="fixed inset-0 z-50 flex items-center justify-center bg-bg-canvas/80 p-4 backdrop-blur-sm"
          >
            <div className="ns-glass-strong w-full max-w-xl rounded-xl border border-border-subtle p-6 shadow-2xl">
              <h2
                id="first-run-import-title"
                className="font-display text-lg font-semibold text-text-primary"
              >
                Import legacy queue.json
              </h2>
              <p className="mt-1 text-xs text-text-secondary">
                Choose a queue.json file from your computer or paste its contents below. Night-Shift stores all
                imported data locally in your per-user data folder.
              </p>

              <div className="mt-4 flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".json,application/json"
                    aria-label="Choose legacy queue.json file"
                    onChange={(e) => void handleFirstRunFileChange(e)}
                    className="sr-only"
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    icon="FileCode"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    Choose queue.json file
                  </Button>
                </div>

                <CodeEditor
                  id="first-run-queue-json"
                  label="Legacy queue.json content"
                  value={firstRunQueueJson}
                  onChange={setFirstRunQueueJson}
                  language="json"
                  rows={8}
                  placeholder='{"repo": "owner/project", "local_repo": "D:/Projects/project", "phases": [...]}'
                  helperText="Supports legacy nightshift_legacy.py queue.json files."
                />
              </div>

              <div className="mt-5 flex justify-end gap-2">
                <Button
                  variant="ghost"
                  onClick={() => setShowFirstRunImport(false)}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  icon="Upload"
                  loading={importingQueue}
                  onClick={() => void handleFirstRunImport()}
                >
                  Import queue
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      {/* 1. Header with live status pill and Start run button */}
      <PageHeader
        title="Mission Control"
        description={snapshot.headline}
        action={
          <div className="flex items-center gap-3">
            <StatusPill state={snapshot.runState as StatusPillState} />
            {snapshot.runState !== "running" && (
              <Button
                variant="primary"
                size="sm"
                icon="Play"
                onClick={handleStartRun}
              >
                Start overnight run
              </Button>
            )}
          </div>
        }
      />

      {/* 2b. Compact Logins Row (BLUEPRINT 5f) */}
      <section
        aria-label="Tool login readiness"
        className="ns-glass flex flex-wrap items-center justify-between gap-3 rounded-md px-4 py-3"
      >
        <div className="flex items-center gap-2 text-xs font-medium text-text-secondary">
          <Icon name="KeyRound" size={16} className="text-accent-primary shrink-0" />
          <span>Tool readiness:</span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {Object.entries(TOOL_DISPLAY_NAMES).map(([key, name]) => {
            const item = logins[key];
            const state = item?.state ?? "Checking";
            const isReady = state === "Ready";
            const isDisabledByOwner = state === "Disabled by owner";
            const isApiKeyOnly = key === "gemini";
            const tone =
              isReady
                ? "success"
                : isDisabledByOwner
                  ? "neutral"
                  : state.startsWith("Limit reached") || state === "Cooling down"
                    ? "warning"
                    : "danger";

            return (
              <div
                key={key}
                className="flex items-center gap-1.5 rounded-pill border border-border-subtle bg-surface-raised px-2.5 py-1 text-xs"
              >
                <span className="font-medium text-text-primary">{name}:</span>
                <Badge tone={tone}>
                  {state}
                  {item?.backAt ? ` (back at ${item.backAt})` : ""}
                </Badge>
                {!isReady && !isDisabledByOwner && !isApiKeyOnly && (
                  <button
                    type="button"
                    onClick={() => handleFixTool(key)}
                    disabled={fixingTool === key}
                    className="ml-1 rounded-sm border border-border-subtle bg-bg-base px-1.5 py-0.5 text-2xs font-semibold text-accent-primary hover:border-border-strong hover:bg-surface-raised focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring disabled:opacity-50"
                  >
                    Fix
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {/* 2. Needs Attention Card (max 3 items, BLUEPRINT 5e) */}
      {deck?.needs_you && deck.needs_you.length > 0 && (
        <GlassCard
          title="Needs your attention"
          icon="AlertTriangle"
          variant="raised"
          className="border-status-danger/40"
        >
          <div className="flex flex-col gap-3">
            {deck.needs_you.slice(0, 3).map((item, idx) => (
              <div
                key={idx}
                className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-md border border-border-subtle bg-surface-raised p-3"
              >
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-status-danger">{item.headline}</p>
                  <p className="mt-0.5 text-xs text-text-secondary">{item.detail}</p>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => handleNeedsAttentionAction(item)}
                >
                  {item.action}
                </Button>
              </div>
            ))}
          </div>
        </GlassCard>
      )}

      {/* 3. Hero Row: Hero Card (span 8) + Usage Tonight Card (span 4) */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        <GlassCard
          variant="hero"
          live={isLive}
          title={deck?.now?.phase ?? "Autonomous overnight orchestrator"}
          icon="Activity"
          className="lg:col-span-8 flex flex-col justify-between"
          action={
            <div className="flex items-center gap-2">
              {stopArmedLocal ? (
                <div className="flex items-center gap-2">
                  <Badge tone="warning">
                    <Icon name="PauseCircle" size={16} />
                    <span>Armed: will stop after phase finishes</span>
                  </Badge>
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="RotateCcw"
                    onClick={handleStopToggle}
                  >
                    Undo
                  </Button>
                </div>
              ) : (
                <Button
                  size="sm"
                  variant="secondary"
                  icon="PauseCircle"
                  onClick={handleStopToggle}
                >
                  Stop after current phase
                </Button>
              )}
            </div>
          }
        >
          <div className="grid grid-cols-1 md:grid-cols-12 gap-6 items-center">
            {/* ProgressRing */}
            <div className="md:col-span-4 flex justify-center py-2">
              <ProgressRing
                value={progressPct}
                max={100}
                size={144}
                label="Overall progress"
                caption="overall complete"
                tone={snapshot.runState === "failed" ? "danger" : progressPct === 100 ? "success" : "accent"}
              />
            </div>

            {/* Now doing, Working AI, Next, ETA */}
            <div className="md:col-span-8 grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <p className="text-xs uppercase tracking-wider text-text-muted">Now doing</p>
                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <p className="text-sm font-semibold text-text-primary">
                    {deck?.now?.step ?? "Waiting for next phase trigger"}
                  </p>
                  {deck?.now?.risk_class && (
                    <Badge
                      tone={
                        deck.now.risk_class === "high_risk" || deck.now.risk_class === "governance"
                          ? "danger"
                          : deck.now.risk_class === "business_logic"
                            ? "warning"
                            : "info"
                      }
                    >
                      {deck.now.risk_class === "docs_format"
                        ? "Docs"
                        : deck.now.risk_class === "ui_cosmetic"
                          ? "UI"
                          : deck.now.risk_class === "business_logic"
                            ? "Logic"
                            : deck.now.risk_class === "high_risk"
                              ? "High risk"
                              : "Governance"}
                    </Badge>
                  )}
                </div>
              </div>

              <div>
                <p className="text-xs uppercase tracking-wider text-text-muted">Working AI</p>
                <p className="mt-1 text-sm font-semibold text-text-primary">
                  {deck?.now?.engine ?? snapshot.activeEngineLabel ?? "Router assigning"}
                </p>
                {deck?.now?.why && (
                  <p className="mt-0.5 text-xs text-text-muted leading-tight">
                    {deck.now.why}
                  </p>
                )}
              </div>

              <div>
                <p className="text-xs uppercase tracking-wider text-text-muted">Next</p>
                <p className="mt-1 text-sm text-text-secondary">
                  {deck?.next?.[0]?.title ?? "All scheduled phases complete"}
                </p>
              </div>

              <div>
                <p className="text-xs uppercase tracking-wider text-text-muted">Time remaining</p>
                <p className="mt-1 font-mono text-sm text-text-primary ns-tabular">
                  {snapshot.etaEstimate ?? deck?.now?.eta ?? "No active estimate"}
                </p>
              </div>
            </div>
          </div>

          {/* Slim Guards Strip under hero */}
          <div className="mt-6 border-t border-border-subtle pt-4">
            <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
              <button
                type="button"
                onClick={() => navigate("/pipeline-ledger")}
                className="text-text-muted font-medium hover:text-accent-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded-sm"
              >
                Phase safety guards:
              </button>
              <div className="flex flex-wrap items-center gap-2">
                {(deck?.guards ?? [
                  { name: "Allowed files", status: "pass", detail: "Changes stay within allowed globs" },
                  { name: "Protected tests", status: "pass", detail: "Zero test deletions or relaxed assertions" },
                  { name: "Real CI", status: "pass", detail: "Exact-SHA checks verified" },
                  { name: "Screenshots", status: "pass", detail: "Verified-build UI evidence" },
                  { name: "Independent review", status: "pass", detail: "Cross-provider reviewer verified" },
                ]).map((guard, idx) => (
                  <button
                    key={idx}
                    type="button"
                    title={guard.detail}
                    onClick={() => navigate("/pipeline-ledger")}
                    className="rounded-pill focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring"
                  >
                    <Badge
                      tone={guard.status === "pass" ? "success" : guard.status === "warn" ? "warning" : "danger"}
                    >
                      <Icon
                        name={guard.status === "pass" ? "ShieldCheck" : "AlertTriangle"}
                        size={16}
                      />
                      <span>{guard.name}</span>
                    </Badge>
                  </button>
                ))}
              </div>
            </div>
          </div>
        </GlassCard>

        {/* AI usage tonight card (span 4) */}
        <GlassCard
          title="AI usage tonight"
          icon="Cpu"
          className="lg:col-span-4 flex flex-col justify-between"
          action={
            <span className="text-xs text-text-muted">estimate from local ledger</span>
          }
        >
          {(() => {
            const hasUsageData =
              Object.keys(deck?.usage_tonight ?? {}).length > 0 ||
              (deck?.handoff_count ?? 0) > 0 ||
              (deck?.budget_tonight?.invocations ?? 0) > 0 ||
              (deck?.gold_saved_tonight?.jobs_done ?? 0) > 0;

            if (!hasUsageData) {
              return (
                <EmptyState
                  icon="Cpu"
                  title="No data yet"
                  description="AI usage, spend, and gold savings will appear here once a run records ledger data."
                />
              );
            }

            return (
              <>
                <div className="flex flex-col gap-3">
                  {[
                    { id: "agy", name: "Antigravity", model: "auto" },
                    { id: "gemini", name: "Gemini CLI", model: "gemini-2.5-pro" },
                    { id: "codex", name: "Codex CLI", model: "o3-mini" },
                    { id: "claude", name: "Claude Code", model: "claude-3-7-sonnet" },
                  ].map((eng) => {
                    const usage = deck?.usage_tonight?.[eng.id];
                    const budgetPct = usage?.budgetPct ?? 0;
                    const barWidth = Math.min(Math.max(budgetPct, 0), 100);

                    return (
                      <div key={eng.id} className="flex flex-col gap-1">
                        <div className="flex items-center justify-between text-xs">
                          <span className="font-medium text-text-primary">{eng.name}</span>
                          <span className="font-mono text-text-muted ns-tabular">
                            ~{budgetPct}% budget used (estimate)
                          </span>
                        </div>
                        <div className="h-1.5 w-full rounded-pill bg-surface-raised overflow-hidden border border-border-subtle">
                          <div
                            className={`h-full rounded-pill ${
                              budgetPct >= 90
                                ? "bg-status-danger"
                                : budgetPct >= 70
                                  ? "bg-status-warning"
                                  : "bg-accent-primary"
                            }`}
                            style={{ width: `${barWidth}%` }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>

                <div className="mt-4 border-t border-border-subtle pt-3 flex flex-col gap-2">
                  <div className="flex items-center justify-between text-xs text-text-secondary">
                    <div className="flex items-center gap-1.5">
                      <Icon name="ArrowRightLeft" size={16} className="text-accent-primary" />
                      <span>Handoffs tonight:</span>
                    </div>
                    <span className="font-mono font-semibold text-text-primary ns-tabular">
                      {deck?.handoff_count ?? 0}
                    </span>
                  </div>

                  <div className="flex items-center justify-between text-xs text-text-secondary">
                    <div className="flex items-center gap-1.5">
                      <Icon name="ShieldCheck" size={16} className="text-accent-primary" />
                      <span>Spend tonight:</span>
                    </div>
                    <span className="font-mono font-medium text-text-primary ns-tabular">
                      {deck?.budget_tonight?.displayLine ?? "$0.00 / $0.00 (free-only mode)"}
                    </span>
                  </div>

                  {deck?.gold_saved_tonight && deck.gold_saved_tonight.jobs_done > 0 && (
                    <div className="flex items-center justify-between text-xs rounded border border-border-subtle bg-surface-raised px-2 py-1.5">
                      <div className="flex items-center gap-1.5 text-text-secondary">
                        <Icon name="Coins" size={16} className="text-accent-primary" />
                        <span>Gold saved:</span>
                      </div>
                      <span className="font-mono font-medium text-accent-primary ns-tabular">
                        {deck.gold_saved_tonight.estimate_label}
                      </span>
                    </div>
                  )}
                </div>
              </>
            );
          })()}
        </GlassCard>
      </div>

      {/* 4. Problems Card (when active) */}
      {deck?.problems && (
        <GlassCard
          title="Orchestration status"
          icon="Info"
          variant="raised"
          className="border-status-warning/40"
        >
          <div className="flex items-start gap-3">
            <Icon name="AlertCircle" size={16} className="text-status-warning mt-0.5 shrink-0" />
            <div>
              <p className="text-sm font-medium text-text-primary">{deck.problems}</p>
              <p className="mt-1 text-xs text-text-muted">
                Night-Shift is managing retry budgets, error signature tracking, and automatic failovers safely.
              </p>
            </div>
          </div>
        </GlassCard>
      )}

      {/* 4b. Token Optimizer Savings Card (BLUEPRINT 5g) */}
      {deck?.token_savings_tonight && (
        <TokenSavingsCard savings={deck.token_savings_tonight} />
      )}

      {/* 5. Lower Row: Done Tonight (span 5) + StepFeed / Timeline (span 7) */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        <GlassCard
          title="Done tonight"
          icon="CheckCircle2"
          className="lg:col-span-5"
          action={
            <span className="font-mono text-xs text-text-muted ns-tabular">
              {doneCount} merged
            </span>
          }
        >
          {deck?.done_tonight && deck.done_tonight.length > 0 ? (
            <ul className="flex flex-col gap-2.5">
              {deck.done_tonight.map((phase) => (
                <li
                  key={phase.phase_id}
                  className="flex items-center justify-between gap-3 rounded-md border border-border-subtle bg-surface-raised p-3 text-xs"
                >
                  <div className="min-w-0">
                    <p className="font-medium text-text-primary truncate">{phase.title}</p>
                    <p className="mt-0.5 text-text-muted">
                      {phase.runners && phase.runners.length > 0
                        ? `Built by ${phase.runners.join(", ")}`
                        : "Verified & committed"}
                    </p>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {phase.pr_number && (
                      <span className="font-mono text-text-secondary ns-tabular">
                        PR #{phase.pr_number}
                      </span>
                    )}
                    <Badge tone={phase.status === "parked" ? "danger" : "success"}>
                      {phase.status}
                    </Badge>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-text-muted py-4">No phases completed or merged yet tonight.</p>
          )}
        </GlassCard>

        {/* Phase Timeline & Step Feed tabs (span 7) */}
        <GlassCard
          title="Live activity"
          icon="ListTree"
          className="lg:col-span-7"
        >
          <Tabs
            label="Activity views"
            value={activeTab}
            onChange={(id) => setActiveTab(id as "feed" | "timeline")}
            items={[
              {
                id: "feed",
                label: "Steps",
                icon: "ListOrdered",
                content: <StepFeed items={stepFeedItems} emptyText="No steps logged yet tonight." />,
              },
              {
                id: "timeline",
                label: "Plan DAG",
                icon: "GitBranch",
                content: <PhaseTimeline items={timelineItems} emptyText="No plan phases scheduled." />,
              },
            ]}
          />
        </GlassCard>
      </div>

      {/* Before you sleep readiness check dialog (BLUEPRINT 5f) */}
      <ConfirmDialog
        open={readinessModalOpen}
        title="Before you sleep: Readiness check"
        description={
          unreadyLogins.length > 0
            ? `Ready to run all night: ${loginEntries.length - unreadyLogins.length} of ${loginEntries.length} tools ready. ${unreadyLogins
                .map(([tool]) => TOOL_DISPLAY_NAMES[tool] ?? tool)
                .join(", ")} is not fully authenticated.`
            : "All tool authentication and pre-flight checks are verified for overnight execution."
        }
        consequence={
          canStartAnyway
            ? `You can start anyway: the engine will use available ready tools (${readyImplementers
                .map(([t]) => TOOL_DISPLAY_NAMES[t] ?? t)
                .join(", ")}).`
            : "No AI implementer tools are logged in. Fix at least one tool before starting."
        }
        confirmLabel={canStartAnyway ? "Start anyway" : "Fix now"}
        confirmVariant={canStartAnyway ? "primary" : "danger"}
        cancelLabel="Cancel"
        onConfirm={() => {
          if (canStartAnyway) {
            void executeStart();
          } else {
            const firstUnready = unreadyLogins[0]?.[0];
            if (firstUnready) void handleFixTool(firstUnready);
            setReadinessModalOpen(false);
          }
        }}
        onCancel={() => setReadinessModalOpen(false)}
      />

      {/* Governance owner approval confirmation dialog (Phase 8e) */}
      <ConfirmDialog
        open={pendingApproval !== null}
        title="Approve change for merge"
        description={pendingApproval?.headline ?? "Confirm owner approval for this blocked change."}
        consequence={
          pendingApproval
            ? `This records explicit owner approval for commit ${pendingApproval.head_sha?.slice(0, 7) ?? "unknown"} and resumes the merge gate.`
            : undefined
        }
        confirmLabel="Approve and resume"
        confirmVariant="primary"
        cancelLabel="Cancel"
        loading={governanceBusy}
        onConfirm={() => void handleConfirmApproval()}
        onCancel={() => setPendingApproval(null)}
      />

      {/* Release rollback confirmation dialog (Phase 8e) */}
      <ConfirmDialog
        open={pendingRollback !== null}
        title="Roll back release"
        description={pendingRollback?.headline ?? "Confirm rollback of this failed release."}
        consequence={
          pendingRollback
            ? `This appends a superseded warning to ${pendingRollback.release_tag ?? "the failed release"} and restores ${pendingRollback.previous_good_tag ?? "the previous verified release"} with its SHA256SUMS into your output folder.`
            : undefined
        }
        confirmLabel="Roll back release"
        confirmVariant="danger"
        cancelLabel="Cancel"
        loading={governanceBusy}
        onConfirm={() => void handleConfirmRollback()}
        onCancel={() => setPendingRollback(null)}
      />
    </div>
  );
}
