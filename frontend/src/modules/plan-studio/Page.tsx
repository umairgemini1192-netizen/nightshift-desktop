import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  GlassCard,
  Icon,
  PageHeader,
  Select,
  TextArea,
  TextField,
} from "../../design";
import type { CouncilSessionData } from "../../bridge/types";
import { useAppStore } from "../../app/store";

export default function PlanStudioPage() {
  const navigate = useNavigate();
  const bridge = useAppStore((s) => s.bridge);
  const snapshot = useAppStore((s) => s.snapshot);
  const pushToast = useAppStore((s) => s.pushToast);

  const [goal, setGoal] = useState(
    "Implement smart quota-aware router and AI Planning Council for Night-Shift Desktop.",
  );
  const [selectedProjectId, setSelectedProjectId] = useState<string>(
    snapshot.activeProjectId ?? "nightshift-desktop",
  );
  const [running, setRunning] = useState(false);
  const [session, setSession] = useState<CouncilSessionData | null>(null);
  const [editingPhases, setEditingPhases] = useState<
    Array<{
      id: string;
      title: string;
      prompt: string;
      branch: string;
      depends_on: string[];
      size: string;
      timeout_minutes: number;
    }>
  >([]);

  // Load initial session from mock or bridge
  const loadInitialSession = useCallback(async () => {
    try {
      const res = await bridge.call<CouncilSessionData>("council.session", { goal });
      if (res) {
        setSession(res);
        const latestPhases =
          res.roles.final?.phases?.length > 0
            ? res.roles.final.phases
            : res.roles.critique?.phases?.length > 0
              ? res.roles.critique.phases
              : res.roles.draft?.phases ?? [];
        setEditingPhases(structuredClone(latestPhases));
      }
    } catch {
      // Clean slate if no active session
    }
  }, [bridge, goal]);

  useEffect(() => {
    void loadInitialSession();
  }, [loadInitialSession]);

  const handleStartCouncil = async () => {
    if (!goal.trim()) {
      pushToast({
        title: "Goal required",
        description: "Describe what you want built before convening the Planning Council.",
        tone: "warning",
      });
      return;
    }
    setRunning(true);
    try {
      const res = await bridge.call<CouncilSessionData>("council.run", {
        goal,
        projectId: selectedProjectId,
      });
      if (res) {
        setSession(res);
        const latestPhases =
          res.roles.final?.phases?.length > 0
            ? res.roles.final.phases
            : res.roles.critique?.phases?.length > 0
              ? res.roles.critique.phases
              : res.roles.draft?.phases ?? [];
        setEditingPhases(structuredClone(latestPhases));
        pushToast({
          title: "Planning Council complete",
          description: "All 3 roles completed. Inspect the diff and trade-offs below.",
          tone: "success",
        });
      }
    } catch (err) {
      pushToast({
        title: "Planning Council failed",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setRunning(false);
    }
  };

  const handleApprovePlan = async () => {
    if (!session || editingPhases.length === 0) return;
    try {
      await bridge.call("council.approve", {
        planId: session.planId,
        phases: editingPhases,
        goal: session.goal,
      });
      await bridge.call("plan.save", {
        plan: {
          id: session.planId,
          title: `Council Plan: ${goal.slice(0, 35)}`,
        },
        phases: editingPhases,
      });
      pushToast({
        title: "Plan approved",
        description: "Council plan sent to Plan Builder. Review order and assignments.",
        tone: "success",
      });
      navigate("/plan-builder");
    } catch (err) {
      pushToast({
        title: "Could not approve plan",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const projectOptions = (snapshot.projects ?? [
    { id: "nightshift-desktop", name: "Night-Shift Desktop", repo: "owner/nightshift-desktop" },
    { id: "drivelift-android", name: "DriveLift Android", repo: "owner/drivelift-android" },
  ]).map((p) => ({ value: p.id, label: `${p.name} (${p.repo})` }));

  const roleConfigs = [
    {
      key: "draft" as const,
      defaultName: "Draft - Gemini",
      roleData: session?.roles?.draft,
      icon: "Layers",
    },
    {
      key: "critique" as const,
      defaultName: "Critique - GPT",
      roleData: session?.roles?.critique,
      icon: "Radar",
    },
    {
      key: "final" as const,
      defaultName: "Final - Claude",
      roleData: session?.roles?.final,
      icon: "Sparkles",
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Plan Studio"
        description="Describe your goal in plain words. Three specialized AI roles draft, critique, and finalize your multi-phase plan."
      />

      {/* Step 1: Goal Input & Project Picker */}
      <GlassCard title="1. Describe what you want built" icon="FileCode">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <div className="lg:col-span-8 flex flex-col gap-2">
            <TextArea
              id="council-goal-input"
              label="Implementation goal"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="e.g. Implement offline database migrations with self-healing tests..."
              rows={4}
              className="font-sans text-sm"
            />
          </div>

          <div className="lg:col-span-4 flex flex-col justify-between gap-4">
            <Select
              id="council-project-select"
              label="Target project"
              value={selectedProjectId}
              onChange={(val) => setSelectedProjectId(val)}
              options={projectOptions}
            />

            <div className="flex items-center gap-2">
              <Button
                variant="primary"
                size="md"
                icon="Sparkles"
                loading={running}
                onClick={() => void handleStartCouncil()}
                className="w-full"
              >
                Convene Planning Council
              </Button>
            </div>
          </div>
        </div>
      </GlassCard>

      {/* Step 2: Three Role Cards Side by Side */}
      <section aria-labelledby="roles-heading" className="flex flex-col gap-3">
        <h2 id="roles-heading" className="text-sm font-semibold uppercase tracking-wider text-text-secondary">
          2. Council Roles & Deliberation
        </h2>
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          {roleConfigs.map(({ key, defaultName, roleData, icon }) => {
            const state = roleData?.state ?? "waiting";
            const isActive = state === "active";
            const isDone = state === "done";
            const isSkipped = state === "skipped";

            return (
              <GlassCard
                key={key}
                title={roleData?.name ?? defaultName}
                icon={icon}
                variant="raised"
                className={`flex flex-col justify-between transition-all ${
                  isActive
                    ? "border-accent-primary ring-1 ring-accent-primary animate-pulse"
                    : isDone
                      ? "border-status-success/30"
                      : isSkipped
                        ? "border-status-warning/40"
                        : "opacity-80"
                }`}
                action={
                  <Badge
                    tone={
                      isActive
                        ? "info"
                        : isDone
                          ? "success"
                          : isSkipped
                            ? "warning"
                            : "neutral"
                    }
                  >
                    {isActive
                      ? "Working"
                      : isDone
                        ? "Deliberated"
                        : isSkipped
                          ? "Skipped (Budget)"
                          : "Waiting"}
                  </Badge>
                }
              >
                <div className="flex flex-col gap-3 text-xs">
                  {/* Engine & Model metadata */}
                  <div className="flex items-center justify-between border-b border-border-subtle pb-2">
                    <span className="text-text-muted">Assigned AI:</span>
                    <span className="font-semibold text-text-primary">
                      {roleData?.engineId ? `${roleData.name} (${roleData.model})` : defaultName}
                    </span>
                  </div>

                  {/* Substitution note if applicable */}
                  {roleData?.substituted && (
                    <div className="flex items-start gap-1.5 rounded border border-status-warning/40 bg-status-warning/10 p-2 text-status-warning">
                      <Icon name="AlertTriangle" size={16} className="mt-0.5 shrink-0" />
                      <span>{roleData.substitutionReason ?? "Substituted via Router"}</span>
                    </div>
                  )}

                  {/* Skip note if skipped */}
                  {isSkipped && (
                    <div className="flex items-start gap-1.5 rounded border border-status-warning/40 bg-status-warning/10 p-2 text-text-secondary">
                      <Icon name="Info" size={16} className="mt-0.5 shrink-0 text-status-warning" />
                      <span>Preserved gold quota: accepted critique revisions directly.</span>
                    </div>
                  )}

                  {/* Elapsed time & Token usage estimate */}
                  <div className="grid grid-cols-2 gap-2 rounded bg-surface-elevated/40 p-2">
                    <div>
                      <span className="text-text-muted">Elapsed:</span>
                      <p className="font-mono font-medium text-text-primary ns-tabular">
                        {roleData?.elapsedSeconds ? `${roleData.elapsedSeconds}s` : "0s"}
                      </p>
                    </div>
                    <div>
                      <span className="text-text-muted">Usage estimate:</span>
                      <p className="font-mono font-medium text-text-primary ns-tabular">
                        {roleData?.tokensEstimate
                          ? `~${roleData.tokensEstimate.toLocaleString()} tokens`
                          : "0 tokens"}
                      </p>
                    </div>
                  </div>

                  {/* Notes / Issues */}
                  {roleData?.issues && roleData.issues.length > 0 && (
                    <div className="flex flex-col gap-1">
                      <span className="font-semibold text-status-warning">Critique issues:</span>
                      <ul className="list-disc pl-4 space-y-0.5 text-text-secondary">
                        {roleData.issues.map((iss, i) => (
                          <li key={i}>{iss}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {roleData?.notes && roleData.notes.length > 0 && !roleData.issues && (
                    <p className="text-text-secondary line-clamp-3">
                      {roleData.notes.join(" ")}
                    </p>
                  )}
                </div>
              </GlassCard>
            );
          })}
        </div>
      </section>

      {/* Step 3: Plan Diff View & Inline Edit */}
      {session && (
        <section aria-labelledby="diff-heading" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="diff-heading" className="text-sm font-semibold uppercase tracking-wider text-text-secondary">
              3. Plan Diff & Interactive Review
            </h2>
            <div className="flex items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                icon="RefreshCw"
                loading={running}
                onClick={() => void handleStartCouncil()}
              >
                Regenerate
              </Button>
              <Button
                variant="primary"
                size="sm"
                icon="CheckCircle2"
                onClick={() => void handleApprovePlan()}
              >
                Approve and send to Plan Builder
              </Button>
            </div>
          </div>

          {/* Trade-off summary strip */}
          <div className="flex items-start gap-2.5 rounded-lg border border-accent-primary/40 bg-accent-primary/5 p-3.5 text-xs">
            <Icon name="Sparkles" size={16} className="shrink-0 text-accent-primary mt-0.5" />
            <div className="flex flex-col gap-1">
              <span className="font-semibold text-text-primary">Council Trade-off Analysis:</span>
              <p className="text-text-secondary leading-relaxed">
                {session.tradeoffSummary || session.diffSummary}
              </p>
            </div>
          </div>

          {/* Draft -> Critique -> Final Diff & Per-AI Margin Notes */}
          <GlassCard title="Plan diff (Draft to Final) and per-AI notes" icon="GitBranch">
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 text-xs">
              <div className="lg:col-span-8 flex flex-col gap-2">
                {editingPhases.map((finalPhase) => {
                  const draftMatch = session.roles.draft.phases.find(
                    (d) => d.id === finalPhase.id || d.branch === finalPhase.branch,
                  );
                  const critiqueMatch = session.roles.critique.phases.find(
                    (c) => c.id === finalPhase.id || c.branch === finalPhase.branch,
                  );
                  const statusLabel = !draftMatch
                    ? "Added in critique"
                    : draftMatch.title !== finalPhase.title || draftMatch.prompt !== finalPhase.prompt
                      ? "Refined"
                      : "Unchanged from draft";
                  const statusTone = !draftMatch
                    ? "success"
                    : statusLabel === "Refined"
                      ? "warning"
                      : "neutral";

                  return (
                    <div
                      key={`diff-${finalPhase.id}`}
                      className="rounded-lg border border-border-subtle bg-surface-elevated/40 p-3 flex flex-col gap-1.5"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-semibold text-text-primary">
                          {finalPhase.id}: {finalPhase.title}
                        </span>
                        <Badge tone={statusTone}>{statusLabel}</Badge>
                      </div>
                      {draftMatch && draftMatch.prompt !== finalPhase.prompt && (
                        <p className="text-text-muted line-through">
                          Draft: {draftMatch.prompt}
                        </p>
                      )}
                      <p className="text-text-secondary">
                        Final: {finalPhase.prompt}
                      </p>
                      {critiqueMatch && !draftMatch && (
                        <p className="text-text-muted">
                          Added after critique to strengthen dependency boundaries.
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>

              <aside aria-label="Per-AI margin notes" className="lg:col-span-4 flex flex-col gap-2.5">
                <div className="rounded-lg border border-border-subtle bg-surface-raised p-3">
                  <p className="font-semibold text-text-primary">Draft notes ({session.roles.draft.name})</p>
                  <p className="mt-1 text-text-secondary">
                    {session.roles.draft.notes.join(" ") || "Initial phased draft produced."}
                  </p>
                </div>
                <div className="rounded-lg border border-border-subtle bg-surface-raised p-3">
                  <p className="font-semibold text-text-primary">Critique notes ({session.roles.critique.name})</p>
                  {session.roles.critique.issues && session.roles.critique.issues.length > 0 ? (
                    <ul className="mt-1 list-disc pl-4 space-y-0.5 text-text-secondary">
                      {session.roles.critique.issues.map((issue, idx) => (
                        <li key={idx}>{issue}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1 text-text-secondary">
                      {session.roles.critique.notes.join(" ") || "Waiting for critique."}
                    </p>
                  )}
                </div>
                <div className="rounded-lg border border-border-subtle bg-surface-raised p-3">
                  <p className="font-semibold text-text-primary">Final notes ({session.roles.final.name})</p>
                  <p className="mt-1 text-text-secondary">
                    {session.roles.final.notes.join(" ") || "Waiting for final review."}
                  </p>
                </div>
              </aside>
            </div>
          </GlassCard>

          {/* Phases list with inline editing */}
          <div className="flex flex-col gap-3">
            {editingPhases.map((phase, idx) => (
              <GlassCard
                key={phase.id}
                title={`${phase.id}: ${phase.title}`}
                icon="Layers"
                action={
                  <div className="flex items-center gap-2">
                    <Badge tone="neutral">size: {phase.size}</Badge>
                    <Badge tone="info">{phase.timeout_minutes}m limit</Badge>
                  </div>
                }
              >
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-12 text-xs">
                  <div className="lg:col-span-4">
                    <TextField
                      id={`phase-title-${phase.id}`}
                      label="Phase title"
                      value={phase.title}
                      onChange={(e) => {
                        const val = e.target.value;
                        setEditingPhases((prev) =>
                          prev.map((p, i) => (i === idx ? { ...p, title: val } : p)),
                        );
                      }}
                    />
                  </div>

                  <div className="lg:col-span-4">
                    <TextField
                      id={`phase-branch-${phase.id}`}
                      label="Git branch"
                      value={phase.branch}
                      onChange={(e) => {
                        const val = e.target.value;
                        setEditingPhases((prev) =>
                          prev.map((p, i) => (i === idx ? { ...p, branch: val } : p)),
                        );
                      }}
                    />
                  </div>

                  <div className="lg:col-span-4">
                    <TextField
                      id={`phase-deps-${phase.id}`}
                      label="Dependencies (comma-separated)"
                      value={phase.depends_on.join(", ")}
                      onChange={(e) => {
                        const val = e.target.value
                          .split(",")
                          .map((d) => d.trim())
                          .filter(Boolean);
                        setEditingPhases((prev) =>
                          prev.map((p, i) => (i === idx ? { ...p, depends_on: val } : p)),
                        );
                      }}
                    />
                  </div>

                  <div className="lg:col-span-12">
                    <TextArea
                      id={`phase-prompt-${phase.id}`}
                      label="Implementation instructions for AI coder"
                      value={phase.prompt}
                      onChange={(e) => {
                        const val = e.target.value;
                        setEditingPhases((prev) =>
                          prev.map((p, i) => (i === idx ? { ...p, prompt: val } : p)),
                        );
                      }}
                      rows={2}
                    />
                  </div>
                </div>
              </GlassCard>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
