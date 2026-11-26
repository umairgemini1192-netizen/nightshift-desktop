import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  CodeEditor,
  ConfirmDialog,
  EmptyState,
  GlassCard,
  Icon,
  PageHeader,
  SegmentedControl,
  Skeleton,
  TextField,
  Toggle,
} from "../../design";
import type { ProjectConfigItem } from "../../bridge/types";
import type { ThemeMode } from "../../design/tokens";
import { SETTINGS_MODULES } from "../../app/registry";
import { useAppStore } from "../../app/store";

const THEME_OPTIONS = [
  { value: "system", label: "System", icon: "Monitor" },
  { value: "dark", label: "Dark", icon: "Moon" },
  { value: "light", label: "Light", icon: "Sun" },
] as const;

interface PriorityItem {
  position: number;
  engine: string;
  model: string;
  effort: string;
  availability?: string;
  backAt?: string | null;
}

const ECONOMY_KEY_ENGINES = [
  { id: "deepseek", label: "DeepSeek", runner: "claude-compatible", model: "deepseek-chat" },
  { id: "kimi", label: "Kimi K2", runner: "claude-compatible", model: "kimi-k2" },
  { id: "glm", label: "GLM", runner: "claude-compatible", model: "glm-4" },
  { id: "minimax", label: "MiniMax", runner: "claude-compatible", model: "minimax-text-01" },
] as const;

interface GuardLimits {
  maxAttempts: number;
  maxCiRetries: number;
  maxRunInvocations: number;
  runnerTimeoutMinutes: number;
}

const DEFAULT_PROJECT: ProjectConfigItem = {
  id: "",
  name: "",
  repo: "",
  local_path: "",
  build_rules: "npm run lint && npm test",
  artifact_glob: "*.apk",
  output_dir: "",
};

export default function SettingsPage() {
  const themeMode = useAppStore((s) => s.themeMode);
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const bridge = useAppStore((s) => s.bridge);
  const snapshot = useAppStore((s) => s.snapshot);
  const pushToast = useAppStore((s) => s.pushToast);

  // Projects CRUD state
  const [projects, setProjects] = useState<ProjectConfigItem[]>([]);
  const [showProjectModal, setShowProjectModal] = useState(false);
  const [editingProject, setEditingProject] = useState<ProjectConfigItem>(DEFAULT_PROJECT);
  const [isEditing, setIsEditing] = useState(false);
  const [projectToDelete, setProjectToDelete] = useState<string | null>(null);

  // Engine priority list state
  const [priorityList, setPriorityList] = useState<PriorityItem[]>([
    { position: 1, engine: "agy", model: "auto", effort: "medium" },
    { position: 2, engine: "gemini", model: "gemini-2.5-pro", effort: "medium" },
    { position: 3, engine: "codex", model: "o3-mini", effort: "medium" },
    { position: 4, engine: "claude", model: "claude-3-7-sonnet", effort: "medium" },
  ]);
  const [draggedPriorityIdx, setDraggedPriorityIdx] = useState<number | null>(null);

  // Economy API keys (Windows DPAPI)
  const [economyKeys, setEconomyKeys] = useState<Record<string, string>>({});
  const [testingEconomyId, setTestingEconomyId] = useState<string | null>(null);

  // Guard limits state
  const [guardLimits, setGuardLimits] = useState<GuardLimits>({
    maxAttempts: 3,
    maxCiRetries: 4,
    maxRunInvocations: 30,
    runnerTimeoutMinutes: 90,
  });

  const moduleSettingsCards = useMemo(
    () =>
      SETTINGS_MODULES.map((mod) => ({
        id: mod.id,
        Component: lazy(mod.settings),
      })),
    [],
  );

  // System automation state
  const [keepAwake, setKeepAwake] = useState(true);
  const [notifications, setNotifications] = useState(true);
  const [autostart, setAutostart] = useState(false);
  const [dataFolder, setDataFolder] = useState<string>("Loading...");

  const loadSettingsData = useCallback(async () => {
    try {
      const [projRes, prioRes, guardsRes, folderRes] = await Promise.all([
        bridge.call<ProjectConfigItem[]>("settings.getProjects"),
        bridge.call<PriorityItem[]>("settings.getPriorityList"),
        bridge.call<GuardLimits>("settings.getGuardLimits"),
        bridge.call<{ path: string }>("settings.openDataFolder"),
      ]);

      setProjects(projRes ?? []);
      if (prioRes && prioRes.length > 0) {
        setPriorityList(prioRes);
      }
      if (guardsRes) {
        setGuardLimits(guardsRes);
      }
      if (folderRes?.path) {
        setDataFolder(folderRes.path);
      }
    } catch (err) {
      pushToast({
        title: "Could not load settings",
        description: String(err),
        tone: "danger",
      });
    }
  }, [bridge, pushToast]);

  useEffect(() => {
    void loadSettingsData();
  }, [loadSettingsData]);

  const handleOpenAddProject = () => {
    setEditingProject({ ...DEFAULT_PROJECT });
    setIsEditing(false);
    setShowProjectModal(true);
  };

  const handleOpenEditProject = (proj: ProjectConfigItem) => {
    setEditingProject({ ...proj });
    setIsEditing(true);
    setShowProjectModal(true);
  };

  const handleSaveProject = async () => {
    if (!editingProject.id.trim() || !editingProject.repo.trim()) {
      pushToast({
        title: "Missing required fields",
        description: "Please specify both Project ID and GitHub Repository (owner/repo).",
        tone: "warning",
      });
      return;
    }

    try {
      await bridge.call("settings.saveProject", { project: editingProject });
      pushToast({
        title: isEditing ? "Project updated" : "Project added",
        description: `${editingProject.name || editingProject.id} saved successfully.`,
        tone: "success",
      });
      setShowProjectModal(false);
      void loadSettingsData();
    } catch (err) {
      pushToast({
        title: "Could not save project",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleDeleteProject = async (id: string) => {
    try {
      await bridge.call("settings.deleteProject", { id });
      pushToast({
        title: "Project removed",
        description: `Project ${id} has been removed.`,
        tone: "info",
      });
      setProjectToDelete(null);
      void loadSettingsData();
    } catch (err) {
      pushToast({
        title: "Could not delete project",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const movePriority = (fromIdx: number, toIdx: number) => {
    if (toIdx < 0 || toIdx >= priorityList.length) return;
    const next = [...priorityList];
    const [item] = next.splice(fromIdx, 1);
    next.splice(toIdx, 0, item);
    const reordered = next.map((it, idx) => ({ ...it, position: idx + 1 }));
    setPriorityList(reordered);
    void bridge.call("settings.setPriorityList", { items: reordered });
  };

  const handleSaveGuardLimits = async () => {
    try {
      await bridge.call("settings.setGuardLimits", guardLimits as unknown as Record<string, unknown>);
      pushToast({
        title: "Guard limits saved",
        description: "Updated retry, invocation, and timeout thresholds.",
        tone: "success",
      });
    } catch (err) {
      pushToast({
        title: "Could not save guard limits",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleToggleKeepAwake = async (enabled: boolean) => {
    setKeepAwake(enabled);
    try {
      await bridge.call("settings.setKeepAwake", { enabled });
      pushToast({
        title: enabled ? "Keep-awake enabled" : "Keep-awake disabled",
        description: enabled
          ? "Windows sleep disabled during active overnight runs."
          : "Standard Windows power settings apply.",
        tone: "info",
      });
    } catch (err) {
      pushToast({
        title: "Could not change keep-awake",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleToggleNotifications = (enabled: boolean) => {
    setNotifications(enabled);
    pushToast({
      title: enabled ? "Notifications enabled" : "Notifications muted",
      description: enabled
        ? "Desktop alerts active for run finished, phase parked, and circuit breaker."
        : "Notifications silenced.",
      tone: "info",
    });
  };

  const handleOpenDataFolder = async () => {
    try {
      const res = await bridge.call<{ opened: boolean; path: string }>("settings.openDataFolder");
      pushToast({
        title: "Data folder opened",
        description: res?.path ? `Located at: ${res.path}` : "Opened in system file manager.",
        tone: "info",
      });
    } catch (err) {
      pushToast({
        title: "Could not open data folder",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleSaveEconomyKey = async (engineId: string, label: string) => {
    const key = (economyKeys[engineId] ?? "").trim();
    if (!key) {
      pushToast({
        title: "API key required",
        description: `Enter an API key for ${label} before saving.`,
        tone: "warning",
      });
      return;
    }
    try {
      await bridge.call("engines.saveKey", { engineId, key });
      setEconomyKeys((prev) => ({ ...prev, [engineId]: "" }));
      pushToast({
        title: `${label} key saved`,
        description: "Encrypted with Windows DPAPI for your account and redacted from logs.",
        tone: "success",
      });
    } catch (err) {
      pushToast({
        title: `Could not save ${label} key`,
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleTestEconomyEngine = async (engineId: string, runner: string, label: string) => {
    setTestingEconomyId(engineId);
    try {
      const res = await bridge.call<{ ok: boolean; output?: string; error?: string }>("engines.test", {
        runner,
        engineId,
      });
      pushToast({
        title: res?.ok ? `${label} verified` : `${label} test failed`,
        description: res?.ok
          ? (res.output ?? "CLI ready for isolated execution.")
          : (res?.error ?? "Check CLI installation and API key."),
        tone: res?.ok ? "success" : "danger",
      });
    } catch (err) {
      pushToast({
        title: `${label} test failed`,
        description: String(err),
        tone: "danger",
      });
    } finally {
      setTestingEconomyId(null);
    }
  };

  const engineNameMap: Record<string, string> = {
    qwen: "Qwen Code (Economy)",
    deepseek: "DeepSeek (Economy)",
    kimi: "Kimi K2 (Economy)",
    glm: "GLM (Economy)",
    minimax: "MiniMax (Economy)",
    agy: "Antigravity (Standard)",
    gemini: "Gemini CLI (Standard)",
    codex: "Codex CLI (Standard)",
    claude: "Claude Code (Gold, Token-Metered)",
  };

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Settings"
        description="Configure appearance, connected projects, engine fallback priority, guard limits, and system automation."
      />

      {/* Appearance */}
      <GlassCard title="Appearance" icon="Sparkles">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-text-primary">Theme</p>
            <p className="mt-1 text-xs text-text-secondary">
              Follows your Windows setting by default. Currently showing {resolvedTheme} theme.
            </p>
          </div>
          <SegmentedControl<ThemeMode>
            label="Theme preference"
            value={themeMode}
            options={THEME_OPTIONS}
            onChange={(mode) => setThemeMode(mode)}
          />
        </div>
      </GlassCard>

      {/* Connected Projects CRUD */}
      <GlassCard
        title="Connected Projects"
        icon="FolderGit2"
        action={
          <Button
            variant="secondary"
            size="sm"
            icon="Plus"
            onClick={handleOpenAddProject}
          >
            Add project
          </Button>
        }
      >
        <p className="text-xs text-text-secondary">
          Projects define your repository path, build commands, and release artifact patterns for unattended runs.
        </p>

        {projects.length === 0 ? (
          <div className="mt-4">
            <EmptyState
              icon="FolderGit2"
              title="No projects configured"
              description="Add a local repository, build rules, and release artifact pattern to run Night-Shift unattended."
              actionLabel="Add first project"
              onAction={handleOpenAddProject}
            />
          </div>
        ) : (
          <div className="mt-4 flex flex-col gap-3">
            {projects.map((proj) => (
              <div
                key={proj.id}
                className="ns-glass flex flex-col gap-2 rounded-lg p-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex flex-col gap-1">
                  <div className="flex items-center gap-2">
                    <Badge tone="info">{proj.id}</Badge>
                    <span className="font-semibold text-text-primary">{proj.name}</span>
                    <span className="font-mono text-xs text-accent-primary">{proj.repo}</span>
                  </div>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary font-mono">
                    <span>Path: {proj.local_path || "Not set"}</span>
                    <span>Artifact: {proj.artifact_glob || "*.apk"}</span>
                  </div>
                </div>

                <div className="flex items-center gap-2 self-end sm:self-center">
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="FileCode"
                    onClick={() => handleOpenEditProject(proj)}
                  >
                    Edit
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="Trash2"
                    onClick={() => setProjectToDelete(proj.id)}
                  >
                    Delete
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </GlassCard>

      {/* Engine Fallback Priority */}
      <GlassCard title="Engine Fallback Priority" icon="Layers">
        <p className="text-xs text-text-secondary">
          Global fallback order when an AI engine encounters a quota limit or error. Drag rows or use buttons to reorder; changes apply from the next phase.
        </p>

        <div className="mt-3 flex flex-col gap-2">
          {priorityList.map((item, idx) => {
            const loginInfo = snapshot.logins?.[item.engine];
            const availability = item.availability ?? loginInfo?.state ?? "Ready";
            const backAt = item.backAt ?? loginInfo?.backAt ?? null;
            const isReady = availability === "Ready";
            const isCooling = availability === "Cooling down" || availability === "Limit reached";
            return (
              <div
                key={item.engine}
                draggable
                onDragStart={() => setDraggedPriorityIdx(idx)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => {
                  if (draggedPriorityIdx !== null && draggedPriorityIdx !== idx) {
                    movePriority(draggedPriorityIdx, idx);
                    setDraggedPriorityIdx(null);
                  }
                }}
                className="ns-glass flex items-center justify-between rounded-lg p-2.5 text-xs"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono font-semibold text-text-muted">#{item.position}</span>
                  <span className="font-semibold text-text-primary">
                    {engineNameMap[item.engine] || item.engine}
                  </span>
                  <Badge tone={isReady ? "success" : isCooling ? "warning" : "danger"}>
                    {availability}
                    {backAt ? ` (back at ${backAt})` : ""}
                  </Badge>
                </div>

                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    disabled={idx === 0}
                    onClick={() => movePriority(idx, idx - 1)}
                    aria-label={`Move ${item.engine} priority up`}
                    className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-20"
                  >
                    <Icon name="ChevronUp" size={16} />
                  </button>
                  <button
                    type="button"
                    disabled={idx === priorityList.length - 1}
                    onClick={() => movePriority(idx, idx + 1)}
                    aria-label={`Move ${item.engine} priority down`}
                    className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-20"
                  >
                    <Icon name="ChevronDown" size={16} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </GlassCard>

      {/* Economy Engine API Keys (Windows DPAPI, BLUEPRINT 5h) */}
      <GlassCard title="Economy Engine API Keys (Windows DPAPI)" icon="KeyRound">
        <p className="text-xs text-text-secondary">
          Small and medium jobs use Economy engines first so premium quota is saved for critical work. Keys are encrypted with Windows DPAPI for your user account, injected per process without modifying ~/.claude, and redacted from all logs.
        </p>
        <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
          {ECONOMY_KEY_ENGINES.map((eng) => (
            <div
              key={eng.id}
              className="ns-glass flex flex-col gap-2.5 rounded-lg p-3"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold text-text-primary">{eng.label}</span>
                <Badge tone="success">Economy</Badge>
              </div>
              <TextField
                id={`economy-key-${eng.id}`}
                label={`${eng.label} API key`}
                type="password"
                value={economyKeys[eng.id] ?? ""}
                onChange={(e) =>
                  setEconomyKeys((prev) => ({ ...prev, [eng.id]: e.target.value }))
                }
                placeholder="Enter API key (stored encrypted)"
              />
              <div className="flex items-center justify-end gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  icon="Terminal"
                  loading={testingEconomyId === eng.id}
                  onClick={() => void handleTestEconomyEngine(eng.id, eng.runner, eng.label)}
                >
                  Test
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  icon="Save"
                  onClick={() => void handleSaveEconomyKey(eng.id, eng.label)}
                >
                  Save key
                </Button>
              </div>
            </div>
          ))}
        </div>
      </GlassCard>

      {/* Guard & Circuit Breaker Limits */}
      <GlassCard
        title="Guard & Circuit Breaker Limits"
        icon="ShieldCheck"
        action={
          <Button
            size="sm"
            variant="secondary"
            icon="Save"
            onClick={() => void handleSaveGuardLimits()}
          >
            Save limits
          </Button>
        }
      >
        <p className="text-xs text-text-secondary">
          Prevent runaway loops and runaway token expenditure with safety thresholds.
        </p>

        <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <TextField
            id="guard-attempts"
            label="Max attempts per phase"
            type="number"
            value={String(guardLimits.maxAttempts)}
            onChange={(e) =>
              setGuardLimits((prev) => ({
                ...prev,
                maxAttempts: Number.parseInt(e.target.value, 10) || 1,
              }))
            }
            helperText="Default: 3 attempts"
          />

          <TextField
            id="guard-ci-retries"
            label="Max CI auto-fix rounds"
            type="number"
            value={String(guardLimits.maxCiRetries)}
            onChange={(e) =>
              setGuardLimits((prev) => ({
                ...prev,
                maxCiRetries: Number.parseInt(e.target.value, 10) || 1,
              }))
            }
            helperText="Default: 4 rounds"
          />

          <TextField
            id="guard-invocations"
            label="Max AI calls per run"
            type="number"
            value={String(guardLimits.maxRunInvocations)}
            onChange={(e) =>
              setGuardLimits((prev) => ({
                ...prev,
                maxRunInvocations: Number.parseInt(e.target.value, 10) || 10,
              }))
            }
            helperText="Default: 30 calls"
          />

          <TextField
            id="guard-timeout"
            label="Runner timeout (minutes)"
            type="number"
            value={String(guardLimits.runnerTimeoutMinutes)}
            onChange={(e) =>
              setGuardLimits((prev) => ({
                ...prev,
                runnerTimeoutMinutes: Number.parseInt(e.target.value, 10) || 30,
              }))
            }
            helperText="Default: 90 minutes"
          />
        </div>
      </GlassCard>

      {/* Module settings sections discovered from registry (BLUEPRINT section 4) */}
      {moduleSettingsCards.map(({ id, Component }) => (
        <Suspense
          key={id}
          fallback={
            <GlassCard>
              <Skeleton lines={2} />
            </GlassCard>
          }
        >
          <Component />
        </Suspense>
      ))}

      {/* Automation & System */}
      <GlassCard title="Automation & System" icon="Cpu">
        <div className="flex flex-col gap-4">
          <Toggle
            id="toggle-keep-awake"
            label="Keep Windows awake while running"
            helperText="Prevents system sleep and standby during overnight Continuous Delivery runs."
            checked={keepAwake}
            onChange={(c) => void handleToggleKeepAwake(c)}
          />

          <Toggle
            id="toggle-notifications"
            label="Desktop notifications"
            helperText="Sends toasts for run finished, phase parked, and circuit breaker. (TODO: Elevated Windows COM registration skipped in non-admin mode; uses standard notification API)."
            checked={notifications}
            onChange={handleToggleNotifications}
          />

          <Toggle
            id="toggle-autostart"
            label="Start Night-Shift on Windows login (per-user)"
            helperText="Launches minimized to system tray when logging into Windows."
            checked={autostart}
            onChange={setAutostart}
          />

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border-subtle pt-3">
            <div>
              <p className="text-xs font-semibold text-text-primary">Data folder</p>
              <p className="mt-0.5 font-mono text-xs text-text-muted">{dataFolder}</p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              icon="FolderGit2"
              onClick={() => void handleOpenDataFolder()}
            >
              Open data folder
            </Button>
          </div>
        </div>
      </GlassCard>

      {/* About & Version */}
      <GlassCard title="About Night-Shift Desktop" icon="Info">
        <div className="grid grid-cols-2 gap-4 text-xs sm:grid-cols-4">
          <div>
            <p className="text-text-secondary">Application version</p>
            <p className="mt-1 font-mono font-semibold text-text-primary">v0.1.0-preview</p>
          </div>
          <div>
            <p className="text-text-secondary">Python Engine</p>
            <p className="mt-1 font-mono font-semibold text-text-primary">Python 3.12+</p>
          </div>
          <div>
            <p className="text-text-secondary">Bridge API</p>
            <p className="mt-1 font-mono font-semibold text-text-primary">v1 (pywebview)</p>
          </div>
          <div>
            <p className="text-text-secondary">Design Tokens</p>
            <p className="mt-1 font-mono font-semibold text-text-primary">DTCG compliant</p>
          </div>
        </div>
      </GlassCard>

      {/* Add / Edit Project Modal */}
      {showProjectModal && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="project-modal-title"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
        >
          <div className="ns-glass w-full max-w-xl rounded-xl border border-border-subtle bg-surface-panel p-6 shadow-2xl">
            <h2 id="project-modal-title" className="font-display text-lg font-semibold text-text-primary">
              {isEditing ? `Edit Project: ${editingProject.id}` : "Add New Project"}
            </h2>
            <p className="mt-1 text-xs text-text-secondary">
              Configure repository details, build and test verification commands, and output directories.
            </p>

            <div className="mt-4 flex flex-col gap-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <TextField
                  id="proj-id"
                  label="Project ID"
                  disabled={isEditing}
                  value={editingProject.id}
                  onChange={(e) =>
                    setEditingProject((p) => ({ ...p, id: e.target.value.toLowerCase().trim() }))
                  }
                  helperText="Unique key (e.g. drivelift-android)"
                />
                <TextField
                  id="proj-name"
                  label="Project Display Name"
                  value={editingProject.name}
                  onChange={(e) => setEditingProject((p) => ({ ...p, name: e.target.value }))}
                  helperText="User-friendly name"
                />
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <TextField
                  id="proj-repo"
                  label="GitHub Repository"
                  value={editingProject.repo}
                  onChange={(e) => setEditingProject((p) => ({ ...p, repo: e.target.value }))}
                  helperText="owner/repository"
                />
                <TextField
                  id="proj-local-path"
                  label="Local Repository Path"
                  value={editingProject.local_path}
                  onChange={(e) => setEditingProject((p) => ({ ...p, local_path: e.target.value }))}
                  helperText="Full path on disk"
                />
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <TextField
                  id="proj-artifact-glob"
                  label="Release Artifact Glob"
                  value={editingProject.artifact_glob || ""}
                  onChange={(e) =>
                    setEditingProject((p) => ({ ...p, artifact_glob: e.target.value }))
                  }
                  helperText="e.g. *.apk, *.exe"
                />
                <TextField
                  id="proj-output-dir"
                  label="Output Directory"
                  value={editingProject.output_dir || ""}
                  onChange={(e) => setEditingProject((p) => ({ ...p, output_dir: e.target.value }))}
                  helperText="Destination for downloaded artifacts"
                />
              </div>

              <CodeEditor
                id="proj-build-rules"
                label="Build & Verification Rules"
                value={editingProject.build_rules || ""}
                onChange={(val) => setEditingProject((p) => ({ ...p, build_rules: val }))}
                language="prompt"
                rows={4}
                helperText="Commands or instructions passed to CI and runner verification."
              />
            </div>

            <div className="mt-5 flex justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => setShowProjectModal(false)}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={() => void handleSaveProject()}
              >
                {isEditing ? "Save changes" : "Add project"}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* Delete Confirmation Dialog */}
      <ConfirmDialog
        open={projectToDelete !== null}
        title="Delete project"
        description={`Are you sure you want to remove project '${projectToDelete}'?`}
        consequence="This will remove the project configuration and associated plans from the local database."
        confirmLabel="Delete Project"
        confirmVariant="danger"
        onConfirm={() => {
          if (projectToDelete) {
            void handleDeleteProject(projectToDelete);
          }
        }}
        onCancel={() => setProjectToDelete(null)}
      />
    </div>
  );
}
