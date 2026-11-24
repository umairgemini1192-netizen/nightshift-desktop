import { useState } from "react";
import { useAppStore } from "../../app/store";
import {
  Badge,
  Button,
  Checkbox,
  CodeEditor,
  ConfirmDialog,
  EmptyState,
  EngineCard,
  GlassCard,
  Kbd,
  PageHeader,
  PhaseTimeline,
  ProgressBar,
  ProgressRing,
  SegmentedControl,
  Select,
  Skeleton,
  StatTile,
  StatusPill,
  StepFeed,
  Tabs,
  TextArea,
  TextField,
  Toggle,
  Tooltip,
  type PhaseTimelineItem,
  type ProgressSegment,
  type StatusPillState,
  type StepFeedItem,
} from "../../design";
import type { ResolvedTheme } from "../../design/tokens";

const ALL_STATUSES: readonly StatusPillState[] = [
  "idle",
  "running",
  "waiting",
  "cooling",
  "failed",
  "done",
];

const SELECT_OPTIONS = [
  { value: "agy", label: "Antigravity (neon)", description: "Standard tier · medium effort" },
  { value: "gemini", label: "Gemini CLI", description: "Standard tier · medium effort" },
  { value: "codex", label: "Codex (GPT)", description: "Standard tier · medium effort" },
  { value: "claude", label: "Claude Code", description: "Gold tier · final review", disabled: true },
] as const;

const PROGRESS_SEGMENTS: readonly ProgressSegment[] = [
  { id: "p1", label: "Phase 1 Foundation", status: "done" },
  { id: "p2", label: "Phase 2 Components", status: "done" },
  { id: "p3", label: "Phase 3 Engine Core", status: "running", progress: 65 },
  { id: "p4", label: "Phase 4 Mission Control", status: "blocked" },
  { id: "p5", label: "Phase 5 Packaging", status: "pending" },
];

const TIMELINE_ITEMS: readonly PhaseTimelineItem[] = [
  {
    id: "t1",
    title: "Phase 1 · App shell and design tokens",
    status: "done",
    detail: "Merged PR #12 with green checks.",
    duration: "14m 20s",
  },
  {
    id: "t2",
    title: "Phase 2 · Design-system components",
    status: "current",
    detail: "Antigravity is running UI checks and screenshots.",
    duration: "08m 45s",
  },
  {
    id: "t3",
    title: "Phase 3 · Engine state machine",
    status: "blocked",
    blockedReason: "Waiting for Phase 2 to merge before starting.",
  },
  {
    id: "t4",
    title: "Phase 4 · Release packaging",
    status: "next",
    detail: "Queued for overnight execution.",
  },
  {
    id: "t5",
    title: "Phase 5 · Smoke check",
    status: "failed",
    detail: "One check failed twice and needs your review.",
  },
];

const STEP_FEED_ITEMS: readonly StepFeedItem[] = [
  {
    id: "s1",
    message: "Antigravity is writing the component tests",
    relativeTime: "12s ago",
    status: "running",
    rawLog: "vitest run tests/design-components.test.tsx\n  18 passed (18)",
  },
  {
    id: "s2",
    message: "Checks passed on pull request #14",
    relativeTime: "3m ago",
    status: "success",
    rawLog: "tokens OK\n0 UI rule violation(s)\nbuild succeeded in 2.41s",
  },
  {
    id: "s3",
    message: "Gemini hit its limit. Switching to Antigravity; Gemini is back at 03:10.",
    relativeTime: "11m ago",
    status: "warning",
    rawLog: "quota_reset=03:10Z runner=gemini handoff_to=agy checkpoint=9f4a21c",
  },
];

const SAMPLE_JSON = `{
  "id": "p2-design-components",
  "implementer": "agy",
  "effort": "medium",
  "auto_merge": true
}`;

export default function GalleryPage() {
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const setThemeMode = useAppStore((s) => s.setThemeMode);

  const [autoMerge, setAutoMerge] = useState(true);
  const [keepAwake, setKeepAwake] = useState(false);
  const [releaseCheck, setReleaseCheck] = useState(true);
  const [selectedEngine, setSelectedEngine] = useState("agy");
  const [branchName, setBranchName] = useState("feat/p2-design-components");
  const [promptText, setPromptText] = useState(
    "Implement every design component with keyboard navigation and accessible states.",
  );
  const [jsonText, setJsonText] = useState(SAMPLE_JSON);
  const [activeTab, setActiveTab] = useState("overview");
  const [effort, setEffort] = useState("medium");
  const [dialogOpen, setDialogOpen] = useState(false);

  return (
    <div className="flex flex-col gap-6 pb-8">
      <PageHeader
        title="Component Gallery"
        description="Development preview of every Night-Shift design-system component across states and themes."
        action={
          <SegmentedControl<ResolvedTheme>
            label="Gallery theme"
            value={resolvedTheme}
            options={[
              { value: "dark", label: "Dark", icon: "Moon" },
              { value: "light", label: "Light", icon: "Sun" },
            ]}
            onChange={(mode) => setThemeMode(mode)}
          />
        }
      />

      {/* Buttons */}
      <GlassCard title="Buttons" icon="Zap">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" icon="Play">
              Start run
            </Button>
            <Button variant="secondary" icon="RefreshCw">
              Re-check logins
            </Button>
            <Button variant="ghost" icon="Settings">
              Configure
            </Button>
            <Button variant="danger" icon="Trash2" onClick={() => setDialogOpen(true)}>
              Park phase
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm">Small</Button>
            <Button size="md">Medium</Button>
            <Button size="lg">Large</Button>
            <Button loading>Saving plan</Button>
            <Button disabled>Disabled</Button>
            <Tooltip content="Press Ctrl+K anywhere to search commands">
              <Button variant="secondary" icon="Command">
                Hover or focus me
              </Button>
            </Tooltip>
          </div>
        </div>
      </GlassCard>

      {/* Status pills, badges, kbd */}
      <GlassCard title="Status pills, badges, and shortcuts" icon="Activity">
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3">
            {ALL_STATUSES.map((state) => (
              <StatusPill key={state} state={state} />
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <Badge tone="neutral">Standard tier</Badge>
            <Badge tone="info">2 running</Badge>
            <Badge tone="success">Checks green</Badge>
            <Badge tone="warning">Cooldown 03:10</Badge>
            <Badge tone="danger">Needs attention</Badge>
            <Kbd>Ctrl K</Kbd>
            <Kbd>Esc</Kbd>
          </div>
        </div>
      </GlassCard>

      {/* GlassCard states */}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-4">
        <GlassCard title="Hero live card" icon="Radar" variant="hero" live>
          <p className="text-sm text-text-secondary">
            Active card with breathing glow border and inner glass highlight.
          </p>
        </GlassCard>
        <GlassCard title="Raised glass" icon="Layers" variant="raised">
          <p className="text-sm text-text-secondary">
            Stronger backdrop surface for elevated panels and modals.
          </p>
        </GlassCard>
        <GlassCard title="Loading card" icon="Clock" loading />
        <GlassCard
          title="Error card"
          icon="AlertTriangle"
          error="Could not read repository status. Retrying automatically."
        />
      </div>

      {/* Progress & Stats */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <GlassCard title="Progress ring and bar" icon="Activity" className="lg:col-span-5">
          <div className="flex flex-col items-center gap-5">
            <div className="flex flex-wrap items-center justify-center gap-5">
              <ProgressRing value={68} label="Overall run progress" caption="4 of 6 phases" />
              <ProgressRing
                value={100}
                size={120}
                strokeWidth={8}
                tone="success"
                label="Checks progress"
                caption="All green"
              />
            </div>
            <ProgressBar
              label="Tonight's plan"
              segments={PROGRESS_SEGMENTS}
              className="w-full"
            />
          </div>
        </GlassCard>

        <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:col-span-7">
          <StatTile
            label="Phases merged tonight"
            value="4 / 6"
            delta="+2 vs last run"
            deltaTone="positive"
            sparkline={[1, 2, 2, 3, 4, 4]}
          />
          <StatTile
            label="Estimated time left"
            value="~42m"
            delta="Estimate from past runs"
            deltaTone="neutral"
            sparkline={[90, 78, 65, 54, 48, 42]}
          />
          <StatTile
            label="Gold quota saved (estimate)"
            value="64%"
            delta="-12% spend"
            deltaTone="positive"
            sparkline={[20, 35, 42, 50, 58, 64]}
          />
          <StatTile label="Loading metric" value="--" loading />
        </div>
      </div>

      {/* Form controls */}
      <GlassCard title="Form controls" icon="ListChecks">
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <div className="flex flex-col gap-4">
            <TextField
              label="Phase branch"
              icon="FolderGit2"
              mono
              value={branchName}
              onChange={(e) => setBranchName(e.target.value)}
              helperText="Automatically created from origin/main."
            />
            <TextField
              label="Repository path (error state)"
              value="Z:/missing-repo"
              onChange={() => {}}
              error="Folder does not exist on this computer."
            />
            <TextField
              label="Checking connection (loading state)"
              value="Checking git remote..."
              onChange={() => {}}
              loading
            />
            <Select
              label="Preferred implementer"
              value={selectedEngine}
              options={SELECT_OPTIONS}
              onChange={setSelectedEngine}
              helperText="Overrides the plan's default engine for this phase."
            />
            <Select
              label="Fallback engine (error state)"
              value=""
              options={[]}
              onChange={() => {}}
              error="Select at least one fallback engine."
            />
          </div>

          <div className="flex flex-col gap-4">
            <Toggle
              label="Auto-merge when checks pass"
              helperText="Low-risk pull requests merge automatically on green."
              checked={autoMerge}
              onChange={setAutoMerge}
            />
            <Toggle
              label="Keep PC awake while running"
              helperText="Prevents idle sleep during overnight runs."
              checked={keepAwake}
              onChange={setKeepAwake}
            />
            <Toggle
              label="Disabled setting"
              checked={false}
              onChange={() => {}}
              disabled
            />
            <Checkbox
              label="Publish preview release after merge"
              helperText="Downloads build artifact and writes SHA256SUMS."
              checked={releaseCheck}
              onChange={setReleaseCheck}
            />
            <Checkbox
              label="Required confirmation (error state)"
              checked={false}
              onChange={() => {}}
              error="Confirm branch protection before enabling auto-merge."
            />
            <TextArea
              label="Phase goal"
              value={promptText}
              onChange={(e) => setPromptText(e.target.value)}
              showCount
              maxLength={240}
              helperText="Plain-English description sent to the AI."
            />
          </div>
        </div>

        <div className="mt-5">
          <CodeEditor
            label="Plan JSON"
            language="json"
            value={jsonText}
            onChange={setJsonText}
            helperText="Press Tab to indent with two spaces."
          />
        </div>
      </GlassCard>

      {/* Engine cards */}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
        <EngineCard
          name="Antigravity"
          model="neon-medium"
          effort="Medium"
          state="ready"
          quotaUsedPct={42}
          live
        />
        <EngineCard
          name="Gemini CLI"
          model="gemini-2.5-pro"
          effort="Medium"
          state="cooling"
          coolingUntil="03:10"
          quotaUsedPct={84}
        />
        <EngineCard
          name="Claude Code"
          model="claude-sonnet"
          effort="Medium"
          state="no-login"
          quotaUsedPct={15}
          actionLabel="Fix login"
          onAction={() => {}}
        />
      </div>

      {/* Timeline and StepFeed */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <GlassCard title="Phase timeline" icon="History" className="lg:col-span-5">
          <PhaseTimeline items={TIMELINE_ITEMS} />
        </GlassCard>
        <GlassCard title="Step feed" icon="Terminal" className="lg:col-span-7">
          <StepFeed items={STEP_FEED_ITEMS} />
        </GlassCard>
      </div>

      {/* Tabs, SegmentedControl, Skeleton, EmptyState */}
      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <GlassCard title="Tabs and segmented controls" icon="Layers">
          <div className="flex flex-col gap-5">
            <SegmentedControl
              label="Default effort"
              value={effort}
              options={[
                { value: "low", label: "Low" },
                { value: "medium", label: "Medium" },
                { value: "high", label: "High", disabled: true },
              ]}
              onChange={setEffort}
            />
            <Tabs
              label="Gallery tabs"
              value={activeTab}
              onChange={setActiveTab}
              items={[
                {
                  id: "overview",
                  label: "Overview",
                  icon: "Radar",
                  content: (
                    <p className="text-sm text-text-secondary">
                      Keyboard accessible tabs with arrow navigation and Framer layout highlight.
                    </p>
                  ),
                },
                {
                  id: "skeleton",
                  label: "Skeletons",
                  icon: "Clock",
                  content: (
                    <div className="flex items-center gap-4">
                      <Skeleton variant="circle" />
                      <Skeleton variant="text" lines={2} className="flex-1" />
                    </div>
                  ),
                },
                {
                  id: "disabled",
                  label: "Disabled tab",
                  disabled: true,
                  content: null,
                },
              ]}
            />
          </div>
        </GlassCard>

        <EmptyState
          icon="Sparkles"
          title="Nothing parked tonight"
          description="When a phase needs your attention, it will appear here with one plain-English sentence and a single action."
          actionLabel="Open confirm dialog"
          onAction={() => setDialogOpen(true)}
        />
      </div>

      <ConfirmDialog
        open={dialogOpen}
        title="Park current phase?"
        description="Night-Shift will leave the pull request open and move on to independent phases."
        consequence="No commits will be lost. You can resume or fix this phase in the morning."
        confirmLabel="Park phase"
        cancelLabel="Keep running"
        onConfirm={() => setDialogOpen(false)}
        onCancel={() => setDialogOpen(false)}
      />
    </div>
  );
}
