import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EmptyState,
  GlassCard,
  Icon,
  PageHeader,
  PhaseTimeline,
  type PhaseTimelineItem,
  type PhaseTimelineStatus,
  StatusPill,
} from "../../design";
import type { AppRunState, RunDetail, RunSummary } from "../../bridge/types";
import { useAppStore } from "../../app/store";

export default function RunsHistoryPage() {
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadRuns = useCallback(async () => {
    try {
      setLoading(true);
      const res = await bridge.call<RunSummary[]>("runs.list");
      setRuns(res ?? []);
    } catch (err) {
      pushToast({
        title: "Could not load runs",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setLoading(false);
    }
  }, [bridge, pushToast]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const loadRunDetail = useCallback(
    async (runId: string) => {
      try {
        setDetailLoading(true);
        const res = await bridge.call<RunDetail>("runs.get", { runId });
        setDetail(res ?? null);
      } catch (err) {
        pushToast({
          title: "Could not load run detail",
          description: String(err),
          tone: "danger",
        });
      } finally {
        setDetailLoading(false);
      }
    },
    [bridge, pushToast],
  );

  useEffect(() => {
    if (selectedRunId) {
      void loadRunDetail(selectedRunId);
    } else {
      setDetail(null);
    }
  }, [selectedRunId, loadRunDetail]);

  const handleOpenArtifactFolder = async (projectId?: string) => {
    try {
      const res = await bridge.call<{ opened: boolean; path: string }>(
        "runs.openArtifactFolder",
        { projectId },
      );
      pushToast({
        title: "Artifacts folder opened",
        description: res?.path ? `Located at: ${res.path}` : "Opened in system file manager.",
        tone: "info",
      });
    } catch (err) {
      pushToast({
        title: "Could not open folder",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const mapTimelineStatus = (status: string): PhaseTimelineStatus => {
    switch (status.toLowerCase()) {
      case "completed":
      case "merged":
      case "done":
        return "done";
      case "running":
      case "implementing":
        return "current";
      case "failed":
        return "failed";
      case "parked":
      case "blocked":
        return "blocked";
      default:
        return "next";
    }
  };

  const mapPillState = (status: string): AppRunState => {
    switch (status.toLowerCase()) {
      case "completed":
      case "merged":
        return "done";
      case "running":
        return "running";
      case "failed":
      case "parked":
        return "failed";
      default:
        return "idle";
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Runs & History"
        description="Review past overnight runs, morning digests, pull requests, and release artifacts."
        action={
          <div className="flex items-center gap-2">
            {selectedRunId && (
              <Button
                variant="ghost"
                icon="ChevronLeft"
                onClick={() => setSelectedRunId(null)}
              >
                Back to runs table
              </Button>
            )}
            <Button
              variant="secondary"
              icon="RefreshCw"
              onClick={() => void loadRuns()}
            >
              Refresh history
            </Button>
          </div>
        }
      />

      {selectedRunId && detail ? (
        /* Run Detail View */
        <div className="flex flex-col gap-5">
          {/* Hero Summary Card */}
          <GlassCard
            title={detail.run.title}
            icon="History"
            loading={detailLoading}
            action={
              <Button
                variant="primary"
                icon="FolderGit2"
                onClick={() => void handleOpenArtifactFolder(detail.run.projectId)}
              >
                Open artifact folder
              </Button>
            }
          >
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div>
                <p className="text-xs text-text-secondary">Status</p>
                <div className="mt-1">
                  <StatusPill state={mapPillState(detail.run.status)} />
                </div>
              </div>
              <div>
                <p className="text-xs text-text-secondary">Duration</p>
                <p className="mt-1 text-sm font-semibold text-text-primary ns-tabular">
                  {detail.run.duration || "1 h 42 m"}
                </p>
              </div>
              <div>
                <p className="text-xs text-text-secondary">Phases completed</p>
                <p className="mt-1 text-sm font-semibold text-text-primary ns-tabular">
                  {detail.run.phasesDone} / {detail.run.phasesCount}
                </p>
              </div>
              <div>
                <p className="text-xs text-text-secondary">AI Engines used</p>
                <div className="mt-1 flex flex-wrap gap-1">
                  {detail.run.aiUsed.map((ai) => (
                    <Badge key={ai} tone="info">
                      {ai}
                    </Badge>
                  ))}
                </div>
              </div>
            </div>
          </GlassCard>

          <div className="grid grid-cols-1 gap-5 lg:grid-cols-12">
            {/* Morning Digest Card */}
            <div className="lg:col-span-6">
              <GlassCard title="Morning Digest" icon="FileCode">
                <div className="whitespace-pre-wrap font-mono text-xs leading-relaxed text-text-primary">
                  {detail.digest}
                </div>
              </GlassCard>
            </div>

            {/* Per-phase Timeline & PR Links */}
            <div className="flex flex-col gap-5 lg:col-span-6">
              <GlassCard title="Per-Phase Timeline" icon="Layers">
                <PhaseTimeline
                  items={detail.phases.map<PhaseTimelineItem>((p) => ({
                    id: p.id,
                    title: p.title,
                    status: mapTimelineStatus(p.status),
                    detail: `Runner: ${p.runner}${p.pr_number ? ` · PR #${p.pr_number}` : ""}`,
                    duration: p.duration,
                  }))}
                />

                {/* Pull requests link section */}
                <div className="mt-4 border-t border-border-subtle pt-3">
                  <h3 className="text-xs font-semibold text-text-secondary uppercase tracking-wider">
                    Pull Requests
                  </h3>
                  <div className="mt-2 flex flex-col gap-2">
                    {detail.phases
                      .filter((p) => p.pr_number)
                      .map((p) => (
                        <div
                          key={p.id}
                          className="flex items-center justify-between rounded bg-surface-elevated/40 p-2 text-xs"
                        >
                          <div className="flex items-center gap-2">
                            <Icon name="CheckCircle2" size={16} className="text-status-success" />
                            <span className="font-medium text-text-primary">{p.title}</span>
                          </div>
                          {p.pr_url ? (
                            <a
                              href={p.pr_url}
                              target="_blank"
                              rel="noreferrer"
                              className="font-medium text-accent-primary hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring"
                            >
                              PR #{p.pr_number}
                            </a>
                          ) : (
                            <span className="text-text-muted">PR #{p.pr_number}</span>
                          )}
                        </div>
                      ))}
                  </div>
                </div>
              </GlassCard>

              {/* Release artifacts */}
              <GlassCard title="Release Artifacts" icon="ShieldCheck">
                <div className="flex flex-col gap-2">
                  {detail.artifacts && detail.artifacts.length > 0 ? (
                    detail.artifacts.map((art, idx) => (
                      <div
                        key={idx}
                        className="flex items-center justify-between rounded bg-surface-elevated/40 p-2.5 text-xs"
                      >
                        <div className="flex items-center gap-2">
                          <Icon name="FolderGit2" size={16} className="text-accent-primary" />
                          <div>
                            <p className="font-semibold text-text-primary">{art.name}</p>
                            {art.sha256 && (
                              <p className="font-mono text-text-muted line-clamp-1">
                                SHA256: {art.sha256}
                              </p>
                            )}
                          </div>
                        </div>
                        <span className="text-xs text-text-secondary ns-tabular">{art.size}</span>
                      </div>
                    ))
                  ) : (
                    <p className="text-xs text-text-muted">No release artifacts found for this run.</p>
                  )}
                </div>
              </GlassCard>
            </div>
          </div>
        </div>
      ) : runs.length === 0 && !loading ? (
        <EmptyState
          icon="History"
          title="No completed runs recorded"
          description="Completed and parked runs will appear here with their timeline, PR links, and plain-English digest."
          actionLabel="Refresh history"
          onAction={() => void loadRuns()}
        />
      ) : (
        /* Runs Table View */
        <GlassCard title="Past Runs History" icon="History">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-border-subtle text-text-secondary">
                <tr>
                  <th scope="col" className="pb-3 font-medium">Date & Time</th>
                  <th scope="col" className="pb-3 font-medium">Run Title</th>
                  <th scope="col" className="pb-3 font-medium">Status</th>
                  <th scope="col" className="pb-3 font-medium">Phases</th>
                  <th scope="col" className="pb-3 font-medium">Duration</th>
                  <th scope="col" className="pb-3 font-medium">AI Engines Used</th>
                  <th scope="col" className="pb-3 text-right font-medium">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border-subtle text-text-primary">
                {runs.map((run) => (
                  <tr
                    key={run.id}
                    className="hover:bg-surface-elevated/40 transition"
                  >
                    <td className="py-3 text-text-muted font-mono ns-tabular">
                      {run.createdAt
                        ? new Date(run.createdAt).toLocaleString(undefined, {
                            month: "short",
                            day: "numeric",
                            hour: "2-digit",
                            minute: "2-digit",
                          })
                        : "Nov 25, 22:15"}
                    </td>
                    <td className="py-3 font-medium">
                      <div className="font-semibold text-text-primary">{run.title}</div>
                      <div className="text-text-muted font-mono">{run.id}</div>
                    </td>
                    <td className="py-3">
                      <StatusPill state={mapPillState(run.status)} />
                    </td>
                    <td className="py-3 ns-tabular">
                      <span className="font-semibold text-text-primary">
                        {run.phasesDone} / {run.phasesCount}
                      </span>
                      {run.phasesFailed > 0 && (
                        <span className="ml-1.5 text-status-danger font-medium">
                          ({run.phasesFailed} failed)
                        </span>
                      )}
                    </td>
                    <td className="py-3 text-text-secondary ns-tabular">
                      {run.duration || "1 h 42 m"}
                    </td>
                    <td className="py-3">
                      <div className="flex flex-wrap gap-1">
                        {run.aiUsed?.map((engine) => (
                          <span
                            key={engine}
                            className="rounded bg-surface-elevated px-1.5 py-0.5 text-xs text-text-secondary"
                          >
                            {engine}
                          </span>
                        )) ?? <span className="text-text-muted">None</span>}
                      </div>
                    </td>
                    <td className="py-3 text-right">
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => setSelectedRunId(run.id)}
                      >
                        View details
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </GlassCard>
      )}
    </div>
  );
}
