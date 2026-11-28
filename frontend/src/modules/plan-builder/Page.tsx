import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  Checkbox,
  CodeEditor,
  EmptyState,
  GlassCard,
  Icon,
  PageHeader,
  Select,
  TextField,
  Toggle,
} from "../../design";
import type { PlanPhase } from "../../bridge/types";
import { useAppStore } from "../../app/store";

const LADDER_OPTIONS = [
  { value: "backend", label: "Backend (Antigravity -> Gemini -> Codex -> Claude)" },
  { value: "ui", label: "UI & Design (Antigravity -> Gemini -> Claude)" },
  { value: "review", label: "Risk Review (Gemini -> Codex -> Claude)" },
  { value: "ci_fix", label: "CI Self-Healing (Codex -> Gemini -> Claude)" },
  { value: "docs", label: "Documentation (Gemini -> Codex)" },
  { value: "default", label: "Default Failover Ladder" },
];

const ENGINE_OPTIONS = [
  { value: "Qwen Code", label: "Qwen Code (Economy)" },
  { value: "DeepSeek", label: "DeepSeek (Economy)" },
  { value: "Antigravity", label: "Antigravity (Standard)" },
  { value: "Gemini CLI", label: "Gemini CLI (Standard)" },
  { value: "Codex CLI", label: "Codex CLI (Standard)" },
  { value: "Claude Code", label: "Claude Code (Gold, Token-Metered)" },
];

const EFFORT_OPTIONS = [
  { value: "low", label: "Low effort" },
  { value: "medium", label: "Medium effort (default)" },
  { value: "high", label: "High effort" },
];

export default function PlanBuilderPage() {
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);
  const refreshSnapshot = useAppStore((s) => s.refreshSnapshot);

  const [plan, setPlan] = useState<{ id: string; title: string; project_id?: string; goal?: string } | null>(null);
  const [phases, setPhases] = useState<PlanPhase[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedPhases, setSelectedPhases] = useState<Set<string>>(new Set());
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);

  const [validationProblems, setValidationProblems] = useState<string[] | null>(null);
  const [isValidated, setIsValidated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [customLadderNames, setCustomLadderNames] = useState<string[]>([]);

  // Modals
  const [showImportModal, setShowImportModal] = useState(false);
  const [importJson, setImportJson] = useState("");
  const [showExportModal, setShowExportModal] = useState(false);
  const [exportJson, setExportJson] = useState("");

  // Bulk implementer assignment
  const [bulkEngine, setBulkEngine] = useState("Antigravity");
  const [bulkModel, setBulkModel] = useState("gemini-4-neon");
  const [bulkEffort, setBulkEffort] = useState("medium");

  const loadPlan = useCallback(async () => {
    try {
      setLoading(true);
      const [res, laddersRes] = await Promise.all([
        bridge.call<{ plan?: { id: string; title: string; project_id?: string; goal?: string }; phases?: PlanPhase[] }>("plan.get"),
        bridge.call<{ ladders?: Record<string, unknown> }>("engines.getLadders").catch(() => null),
      ]);
      if (laddersRes?.ladders) {
        setCustomLadderNames(Object.keys(laddersRes.ladders));
      }
      setPlan(res?.plan ?? null);
      const loaded = (res?.phases ?? []).map((phase) => ({
        ...phase,
        prompt: phase.prompt ?? "",
        depends_on: phase.depends_on ?? JSON.parse((phase as PlanPhase & { depends_on_json?: string }).depends_on_json ?? "[]"),
        auto_merge: Boolean(phase.auto_merge),
        release_after_merge: Boolean(phase.release_after_merge),
        force_rerun: Boolean(phase.force_rerun),
      }));
      setPhases(loaded);
      setSelectedId((current) => current ?? loaded[0]?.id ?? null);
    } catch (err) {
      pushToast({
        title: "Could not load plan",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setLoading(false);
    }
  }, [bridge, pushToast]);

  useEffect(() => {
    void loadPlan();
  }, [loadPlan]);

  const activePhase = phases.find((p) => p.id === selectedId) ?? phases[0] ?? null;

  const updateActivePhase = (updates: Partial<PlanPhase>) => {
    if (!activePhase) return;
    setPhases((prev) =>
      prev.map((p) => (p.id === activePhase.id ? { ...p, ...updates } : p)),
    );
  };

  const movePhase = (fromIndex: number, toIndex: number) => {
    if (toIndex < 0 || toIndex >= phases.length) return;
    setPhases((prev) => {
      const next = [...prev];
      const [item] = next.splice(fromIndex, 1);
      next.splice(toIndex, 0, item);
      return next.map((p, idx) => ({ ...p, position: idx }));
    });
  };

  const handleValidate = async () => {
    try {
      const res = await bridge.call<{ valid: boolean; problems: string[] }>("plan.validate", {
        phases,
      });
      setIsValidated(true);
      setValidationProblems(res?.problems ?? []);
      if (res?.valid) {
        pushToast({
          title: "Plan is valid",
          description: "All phase dependencies, ladders, and branches checked cleanly.",
          tone: "success",
        });
      } else {
        pushToast({
          title: "Plan validation findings",
          description: `Found ${res?.problems?.length ?? 0} issue(s) in the plan DAG.`,
          tone: "warning",
        });
      }
    } catch (err) {
      pushToast({
        title: "Validation failed",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleStartRun = async () => {
    try {
      const valRes = await bridge.call<{ valid: boolean; problems: string[] }>("plan.validate", {
        phases,
      });
      if (!valRes?.valid) {
        setIsValidated(true);
        setValidationProblems(valRes?.problems ?? []);
        pushToast({
          title: "Cannot start run",
          description: "Fix validation problems before starting the overnight run.",
          tone: "danger",
        });
        return;
      }
      const targetPlan = plan ?? { id: "active_plan", title: "Continuous Delivery Plan" };
      const saved = await bridge.call<{ planId?: string; projectId?: string }>("plan.save", {
        plan: targetPlan,
        phases,
      });
      const resolvedPlan = {
        ...targetPlan,
        id: saved?.planId ?? targetPlan.id,
        project_id: saved?.projectId ?? targetPlan.project_id,
      };
      setPlan(resolvedPlan);
      await bridge.call("engine.start", {
        planId: resolvedPlan.id,
        projectId: resolvedPlan.project_id,
        phases,
      });
      pushToast({
        title: "Overnight run started",
        description: "Continuous failover orchestrator is active.",
        tone: "success",
      });
    } catch (err) {
      pushToast({
        title: "Could not start run",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleSavePlan = async () => {
    try {
      const targetPlan = plan ?? { id: "active_plan", title: "Continuous Delivery Plan" };
      const saved = await bridge.call<{ planId?: string; projectId?: string }>("plan.save", {
        plan: targetPlan,
        phases,
      });
      setPlan((current) => {
        const base = current ?? targetPlan;
        return {
          ...base,
          id: saved?.planId ?? base.id,
          project_id: saved?.projectId ?? base.project_id,
        };
      });
      pushToast({
        title: "Plan saved",
        description: "All phases and dependency configurations persisted.",
        tone: "success",
      });
    } catch (err) {
      pushToast({
        title: "Save failed",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleImportQueue = async () => {
    try {
      const res = await bridge.call<{ plan: { id: string; title: string; project_id?: string; goal?: string }; phases: PlanPhase[] }>(
        "plan.importQueue",
        { queue: importJson },
      );
      if (res?.phases) {
        setPlan(res.plan);
        setPhases(
          res.phases.map((phase) => ({
            ...phase,
            prompt: phase.prompt ?? "",
            depends_on: phase.depends_on ?? [],
            timeout_minutes: phase.timeout_minutes ?? 90,
            auto_merge: Boolean(phase.auto_merge),
            release_after_merge: Boolean(phase.release_after_merge),
            force_rerun: Boolean(phase.force_rerun),
          })),
        );
        setSelectedPhases(new Set());
        setSelectedId(res.phases[0]?.id ?? null);
        await refreshSnapshot();
        setShowImportModal(false);
        setImportJson("");
        pushToast({
          title: "Queue imported",
          description: `Imported ${res.phases.length} phase(s) from legacy queue.json.`,
          tone: "success",
        });
      }
    } catch (err) {
      pushToast({
        title: "Import failed",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleExportQueue = async () => {
    try {
      const res = await bridge.call<Record<string, unknown>>("plan.exportQueue", {
        planId: plan?.id,
      });
      setExportJson(JSON.stringify(res, null, 2));
      setShowExportModal(true);
    } catch (err) {
      pushToast({
        title: "Export failed",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleAddPhase = () => {
    let nextIdx = phases.length + 1;
    while (phases.some((p) => p.id === `p${nextIdx}`)) {
      nextIdx += 1;
    }
    const newId = `p${nextIdx}`;
    const newPhase: PlanPhase = {
      id: newId,
      title: `Phase ${nextIdx}: New Feature`,
      branch: `feat/${newId}`,
      prompt: "Describe implementation requirements...",
      depends_on: phases.length > 0 ? [phases[phases.length - 1].id] : [],
      ladder: "backend",
      timeout_minutes: 90,
      auto_merge: true,
      release_after_merge: false,
      implementer: "Antigravity",
    };
    setPhases((prev) => [...prev, newPhase]);
    setSelectedId(newId);
  };

  const handleDeletePhase = (id: string) => {
    setPhases((prev) => prev.filter((p) => p.id !== id));
    setSelectedPhases((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
    if (selectedId === id) {
      const remaining = phases.filter((p) => p.id !== id);
      setSelectedId(remaining[0]?.id ?? null);
    }
  };

  const toggleSelectAll = () => {
    if (selectedPhases.size === phases.length) {
      setSelectedPhases(new Set());
    } else {
      setSelectedPhases(new Set(phases.map((p) => p.id)));
    }
  };

  const toggleSelectPhase = (id: string) => {
    setSelectedPhases((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const updateBulkImplementer = async (reset: boolean) => {
    const ids = Array.from(selectedPhases);
    try {
      const targetPlan = plan ?? { id: "active_plan", title: "Continuous Delivery Plan" };
      const saved = await bridge.call<{ planId?: string; projectId?: string }>("plan.save", {
        plan: targetPlan,
        phases,
      });
      setPlan((current) => {
        const base = current ?? targetPlan;
        return {
          ...base,
          id: saved?.planId ?? base.id,
          project_id: saved?.projectId ?? base.project_id,
        };
      });
      await bridge.call("plan.setImplementer", reset
        ? { phaseIds: ids, reset: true }
        : { phaseIds: ids, engine: bulkEngine, model: bulkModel, effort: bulkEffort });
      setPhases((prev) => prev.map((p) => ids.includes(p.id)
        ? reset
          ? { ...p, overrideImplementer: undefined, overrideModel: undefined, overrideEffort: undefined }
          : { ...p, overrideImplementer: bulkEngine, overrideModel: bulkModel, overrideEffort: bulkEffort }
        : p));
      pushToast({
        title: reset ? "Reset to plan" : "Implementer overridden",
        description: reset ? "The selected phases use the plan's implementer." : "The selected phases have been assigned.",
        tone: "info",
      });
    } catch (err) {
      pushToast({ title: "Could not update implementer", description: String(err), tone: "danger" });
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Plan Builder"
        description="Order phases, check dependencies, and choose which AI handles each phase."
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              icon="Upload"
              onClick={() => setShowImportModal(true)}
            >
              Import queue.json
            </Button>
            <Button
              variant="secondary"
              icon="Download"
              onClick={() => void handleExportQueue()}
            >
              Export
            </Button>
            <Button
              variant="secondary"
              icon="CheckCircle"
              onClick={() => void handleValidate()}
            >
              Validate
            </Button>
            <Button
              variant="secondary"
              icon="Save"
              onClick={() => void handleSavePlan()}
            >
              Save
            </Button>
            <Button
              variant="primary"
              icon="Play"
              onClick={() => void handleStartRun()}
            >
              Start run
            </Button>
          </div>
        }
      />

      {/* Validation results banner */}
      {isValidated && validationProblems && validationProblems.length > 0 && (
        <div
          role="alert"
          className="ns-glass rounded-lg border border-status-danger/40 bg-status-danger/10 p-4"
        >
          <div className="flex items-center gap-2 text-sm font-semibold text-status-danger">
            <Icon name="AlertCircle" size={16} />
            <span>Plan DAG validation findings ({validationProblems.length})</span>
          </div>
          <ul className="mt-2 list-inside list-disc space-y-1 text-xs text-text-primary">
            {validationProblems.map((p, idx) => (
              <li key={idx}>{p}</li>
            ))}
          </ul>
        </div>
      )}

      {isValidated && validationProblems && validationProblems.length === 0 && (
        <div
          role="status"
          className="ns-glass rounded-lg border border-status-success/40 bg-status-success/10 p-3"
        >
          <div className="flex items-center gap-2 text-xs font-semibold text-status-success">
            <Icon name="CheckCircle2" size={16} />
            <span>Plan is valid: DAG is acyclic, branches are verified, and failover ladders are recognized.</span>
          </div>
        </div>
      )}

      {phases.length === 0 && !loading ? (
        <EmptyState
          icon="ListChecks"
          title="No phases loaded yet"
          description="Create a phased plan or import a legacy queue.json file to configure prompts and timeouts."
          actionLabel="Create first phase"
          onAction={handleAddPhase}
        />
      ) : (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
          {/* Left Column: Phase list */}
          <div className="flex flex-col gap-3 lg:col-span-5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Checkbox
                  id="select-all-phases"
                  label={`All (${phases.length})`}
                  checked={
                    selectedPhases.size === phases.length && phases.length > 0
                  }
                  onChange={toggleSelectAll}
                />
              </div>
              <Button
                size="sm"
                variant="ghost"
                icon="Plus"
                onClick={handleAddPhase}
              >
                Add phase
              </Button>
            </div>

            {/* Bulk action bar */}
            {selectedPhases.size > 0 && (
              <GlassCard className="p-3">
                <div className="flex flex-col gap-2">
                  <span className="text-xs font-medium text-text-secondary">
                    {selectedPhases.size} phase(s) selected
                  </span>
                  <div className="flex flex-wrap items-center gap-2">
                    <Select
                      id="bulk-engine-select"
                      label="Assign AI engine"
                      value={bulkEngine}
                      onChange={(val) => setBulkEngine(val)}
                      options={ENGINE_OPTIONS}
                      className="min-w-36 text-xs"
                    />
                    <TextField
                      id="bulk-model-input"
                      label="Model"
                      value={bulkModel}
                      onChange={(e) => setBulkModel(e.target.value)}
                      placeholder="auto"
                      className="min-w-32 text-xs"
                    />
                    <Select
                      id="bulk-effort-select"
                      label="Effort level"
                      value={bulkEffort}
                      onChange={(val) => setBulkEffort(val)}
                      options={EFFORT_OPTIONS}
                      className="min-w-32 text-xs"
                    />
                    <Button size="sm" variant="secondary" onClick={() => void updateBulkImplementer(false)}>
                      Set implementer
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => void updateBulkImplementer(true)}>
                      Reset to plan
                    </Button>
                  </div>
                </div>
              </GlassCard>
            )}

            {/* Draggable & keyboard-accessible phase list */}
            <div className="flex flex-col gap-2">
              {phases.map((phase, idx) => {
                const isSelected = selectedId === phase.id;
                const isChecked = selectedPhases.has(phase.id);
                return (
                  <article
                    key={phase.id}
                    draggable
                    onDragStart={() => setDraggedIndex(idx)}
                    onDragOver={(e) => {
                      e.preventDefault();
                    }}
                    onDrop={() => {
                      if (draggedIndex !== null && draggedIndex !== idx) {
                        movePhase(draggedIndex, idx);
                        setDraggedIndex(null);
                      }
                    }}
                    className={`ns-glass flex flex-col gap-2 rounded-lg p-3 transition ${
                      isSelected
                        ? "border-accent-primary/60 bg-surface-panel/90"
                        : "hover:bg-surface-elevated/60"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <Checkbox
                          id={`check-${phase.id}`}
                          label={`Select ${phase.id}`}
                          checked={isChecked}
                          onChange={() => toggleSelectPhase(phase.id)}
                          className="[&_span.flex-1]:sr-only"
                        />
                        <button
                          type="button"
                          onClick={() => setSelectedId(phase.id)}
                          className="flex items-center gap-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring rounded"
                        >
                          <Badge tone="neutral">{phase.id}</Badge>
                          <span className="text-xs font-semibold text-text-primary line-clamp-1 hover:text-accent-primary">
                            {phase.title}
                          </span>
                        </button>
                      </div>

                      {/* Reorder buttons */}
                      <div className="flex items-center gap-1">
                        <button
                          type="button"
                          disabled={idx === 0}
                          onClick={() => movePhase(idx, idx - 1)}
                          aria-label={`Move phase ${phase.id} up`}
                          className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-30"
                        >
                          <Icon name="ChevronUp" size={16} />
                        </button>
                        <button
                          type="button"
                          disabled={idx === phases.length - 1}
                          onClick={() => movePhase(idx, idx + 1)}
                          aria-label={`Move phase ${phase.id} down`}
                          className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-30"
                        >
                          <Icon name="ChevronDown" size={16} />
                        </button>
                      </div>
                    </div>

                    {/* Chips metadata row */}
                    <div
                      onClick={() => setSelectedId(phase.id)}
                      className="flex cursor-pointer flex-wrap items-center gap-1.5 text-xs text-text-muted"
                    >
                      {phase.depends_on.length > 0 ? (
                        phase.depends_on.map((dep) => (
                          <span
                            key={dep}
                            className="rounded bg-surface-elevated px-1.5 py-0.5 text-xs text-text-secondary"
                          >
                            dep: {dep}
                          </span>
                        ))
                      ) : (
                        <span className="rounded bg-surface-elevated px-1.5 py-0.5 text-xs text-text-muted">
                          root phase
                        </span>
                      )}

                      <span className="rounded bg-surface-elevated px-1.5 py-0.5 text-xs text-accent-primary">
                        ladder: {phase.ladder}
                      </span>

                      <span className="rounded bg-surface-elevated px-1.5 py-0.5 text-xs text-text-secondary">
                        {phase.timeout_minutes}m limit
                      </span>

                      {/* Implementer column */}
                      <div className="ml-auto flex items-center gap-1">
                        <span className="text-xs text-text-secondary">
                          {phase.overrideImplementer || phase.implementer || "Antigravity"}
                        </span>
                        {phase.overrideImplementer && (
                          <Badge tone="warning">Overridden</Badge>
                        )}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </div>

          {/* Right Column: Selected phase editor */}
          <div className="lg:col-span-7">
            {activePhase ? (
              <GlassCard
                title={`Edit Phase: ${activePhase.id}`}
                icon="FileCode"
                action={
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="Trash2"
                    onClick={() => handleDeletePhase(activePhase.id)}
                  >
                    Delete
                  </Button>
                }
              >
                <div className="flex flex-col gap-4">
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <TextField
                      id="phase-title"
                      label="Title"
                      value={activePhase.title}
                      onChange={(e) => updateActivePhase({ title: e.target.value })}
                    />
                    <TextField
                      id="phase-branch"
                      label="Git branch"
                      value={activePhase.branch}
                      onChange={(e) => updateActivePhase({ branch: e.target.value })}
                      helperText="Branch will be created under isolated worktree."
                    />
                  </div>

                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Select
                      id="phase-ladder"
                      label="Failover ladder"
                      value={activePhase.ladder}
                      onChange={(val) => updateActivePhase({ ladder: val })}
                      options={(() => {
                        const known = new Set(LADDER_OPTIONS.map((o) => o.value));
                        const extras = [...customLadderNames, activePhase.ladder]
                          .filter((name): name is string => Boolean(name) && !known.has(name))
                          .filter((name, idx, arr) => arr.indexOf(name) === idx)
                          .map((name) => ({ value: name, label: name }));
                        return [...LADDER_OPTIONS, ...extras];
                      })()}
                    />
                    <TextField
                      id="phase-timeout"
                      label="Timeout (minutes)"
                      type="number"
                      value={String(activePhase.timeout_minutes)}
                      onChange={(e) =>
                        updateActivePhase({
                          timeout_minutes: Number.parseInt(e.target.value, 10) || 60,
                        })
                      }
                      helperText="Process tree is cleanly killed on timeout."
                    />
                  </div>

                  <div className="flex flex-col gap-2">
                    <TextField
                      id="phase-deps"
                      label="Dependencies (comma-separated phase IDs)"
                      value={activePhase.depends_on.join(", ")}
                      onChange={(e) =>
                        updateActivePhase({
                          depends_on: e.target.value
                            .split(",")
                            .map((s) => s.trim())
                            .filter(Boolean),
                        })
                      }
                      helperText="Phase will only run when all parent phases are merged."
                    />
                  </div>

                  <CodeEditor
                    id="phase-prompt"
                    label="Implementation prompt (.nightshift_prompt.md)"
                    value={activePhase.prompt ?? ""}
                    onChange={(val) => updateActivePhase({ prompt: val })}
                    language="prompt"
                    rows={8}
                    helperText="Instructions provided directly to the AI runner."
                  />

                  <div className="flex flex-col gap-3 rounded-lg bg-surface-elevated/40 p-4">
                    <Toggle
                      id="toggle-automerge"
                      label="Auto-merge to main on green CI"
                      helperText="Merges pull request cleanly when all CI checks pass."
                      checked={activePhase.auto_merge}
                      onChange={(checked) => updateActivePhase({ auto_merge: checked })}
                    />

                    <Toggle
                      id="toggle-release"
                      label="Publish release preview after merge"
                      helperText="Uploads artifact matching glob pattern and tags release."
                      checked={activePhase.release_after_merge}
                      onChange={(checked) =>
                        updateActivePhase({ release_after_merge: checked })
                      }
                    />

                    <Toggle
                      id="toggle-force-rerun"
                      label="Force rerun if already merged"
                      helperText="Redo implementation even if branch was previously merged."
                      checked={Boolean(activePhase.force_rerun)}
                      onChange={(checked) => updateActivePhase({ force_rerun: checked })}
                    />
                  </div>
                </div>
              </GlassCard>
            ) : (
              <GlassCard title="No phase selected">
                <p className="text-xs text-text-secondary">
                  Select a phase from the list to view and edit its prompt, ladder, and dependencies.
                </p>
              </GlassCard>
            )}
          </div>
        </div>
      )}

      {/* Import queue.json modal overlay */}
      {showImportModal && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="import-dialog-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
        >
          <div className="ns-glass w-full max-w-xl rounded-xl border border-border-subtle bg-surface-panel p-6 shadow-2xl">
            <h2 id="import-dialog-title" className="font-display text-lg font-semibold text-text-primary">
              Import legacy queue.json
            </h2>
            <p className="mt-1 text-xs text-text-secondary">
              Paste your existing queue.json configuration to import projects, ladders, and phases.
            </p>
            <div className="mt-4">
              <CodeEditor
                id="import-json-editor"
                label="queue.json content"
                value={importJson}
                onChange={setImportJson}
                language="json"
                rows={10}
                placeholder='{ "repo": "owner/repo", "phases": [...] }'
              />
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setShowImportModal(false);
                  setImportJson("");
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => void handleImportQueue()}
              >
                Import Plan
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Export queue.json modal overlay */}
      {showExportModal && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="export-dialog-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
        >
          <div className="ns-glass w-full max-w-xl rounded-xl border border-border-subtle bg-surface-panel p-6 shadow-2xl">
            <h2 id="export-dialog-title" className="font-display text-lg font-semibold text-text-primary">
              Exported queue.json
            </h2>
            <p className="mt-1 text-xs text-text-secondary">
              Configuration compatible with the legacy queue format and CLI runners.
            </p>
            <div className="mt-4">
              <CodeEditor
                id="export-json-editor"
                label="JSON payload"
                value={exportJson}
                onChange={() => {}}
                readOnly
                language="json"
                rows={10}
              />
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="primary"
                onClick={() => setShowExportModal(false)}
              >
                Done
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
