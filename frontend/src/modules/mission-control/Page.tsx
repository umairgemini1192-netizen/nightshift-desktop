import { useNavigate } from "react-router-dom";
import { EmptyState, GlassCard, PageHeader, StatusPill } from "../../design";
import { useAppStore } from "../../app/store";

export default function MissionControlPage() {
  const snapshot = useAppStore((s) => s.snapshot);
  const pushToast = useAppStore((s) => s.pushToast);
  const navigate = useNavigate();

  const isLive = snapshot.runState === "running";

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Mission Control"
        description={snapshot.headline}
        action={<StatusPill state={snapshot.runState} />}
      />

      {snapshot.activePhaseTitle && (
        <GlassCard
          title={snapshot.activePhaseTitle}
          icon="Activity"
          variant="hero"
          live={isLive}
        >
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div>
              <p className="text-xs uppercase tracking-wider text-text-muted">Working AI</p>
              <p className="mt-1 text-md text-text-primary">
                {snapshot.activeEngineLabel ?? "Waiting for router assignment"}
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wider text-text-muted">Time remaining</p>
              <p className="mt-1 font-mono text-md text-text-primary ns-tabular">
                {snapshot.etaEstimate ?? "No active estimate"}
              </p>
            </div>
          </div>
        </GlassCard>
      )}

      <EmptyState
        icon="Radar"
        title={
          snapshot.scenario === "empty"
            ? "Connect a project to start overnight runs"
            : "Mission Control deck is ready"
        }
        description={
          snapshot.scenario === "failure"
            ? "One phase is parked for review while independent phases keep moving."
            : "Live progress, handoffs, login health, and plain-English morning digests will appear here."
        }
        actionLabel={snapshot.scenario === "empty" ? "Open Settings" : "Open Plan Builder"}
        onAction={() => {
          if (snapshot.scenario === "empty") {
            navigate("/settings");
          } else {
            pushToast({
              title: "Opened Plan Builder",
              description: "Review phases before starting an overnight run.",
              tone: "info",
            });
            navigate("/plan-builder");
          }
        }}
      />
    </div>
  );
}
