import { motion, useReducedMotion } from "framer-motion";

export type ProgressSegmentStatus = "done" | "running" | "failed" | "blocked" | "pending";

export interface ProgressSegment {
  id: string;
  label: string;
  status: ProgressSegmentStatus;
  progress?: number;
}

export interface ProgressBarProps {
  label: string;
  segments: readonly ProgressSegment[];
  showSummary?: boolean;
  className?: string;
}

const SEGMENT_TONE: Record<ProgressSegmentStatus, string> = {
  done: "bg-status-success",
  running: "bg-accent-primary",
  failed: "bg-status-danger",
  blocked: "bg-status-warning",
  pending: "bg-transparent",
};

export function ProgressBar({
  label,
  segments,
  showSummary = true,
  className = "",
}: ProgressBarProps) {
  const prefersReduced = useReducedMotion();
  const total = segments.length;
  const doneCount = segments.filter((s) => s.status === "done").length;
  const pct = total > 0 ? Math.round((doneCount / total) * 100) : 0;

  return (
    <div className={`flex flex-col gap-2 ${className}`}>
      {showSummary && (
        <div className="flex items-center justify-between gap-4 text-xs">
          <span className="font-medium text-text-secondary">{label}</span>
          <span className="font-mono text-text-muted ns-tabular">
            {doneCount}/{total} phases ({pct}%)
          </span>
        </div>
      )}

      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={doneCount}
        aria-valuetext={`${doneCount} of ${total} phases complete`}
        className="flex w-full gap-1"
      >
        {total === 0 ? (
          <div className="h-2 w-full rounded-pill bg-surface-raised border border-border-subtle" />
        ) : (
          segments.map((seg) => {
            const fillPct =
              seg.status === "done"
                ? 100
                : seg.status === "running"
                  ? Math.min(Math.max(seg.progress ?? 50, 10), 100)
                  : seg.status === "failed" || seg.status === "blocked"
                    ? 100
                    : 0;

            return (
              <div
                key={seg.id}
                title={`${seg.label}: ${seg.status}`}
                className="relative h-2 flex-1 overflow-hidden rounded-pill border border-border-subtle bg-surface-raised"
              >
                <motion.div
                  initial={prefersReduced ? { width: `${fillPct}%` } : { width: "0%" }}
                  animate={{ width: `${fillPct}%` }}
                  transition={
                    prefersReduced
                      ? { duration: 0 }
                      : { duration: 0.42, ease: [0.16, 1, 0.3, 1] }
                  }
                  className={`h-full rounded-pill ${SEGMENT_TONE[seg.status]} ${
                    seg.status === "running" ? "motion-safe:animate-pulse" : ""
                  }`}
                />
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
