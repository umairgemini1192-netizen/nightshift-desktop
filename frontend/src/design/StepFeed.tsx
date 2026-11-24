import { useState } from "react";
import { Icon } from "./Icon";

export type StepFeedStatus = "info" | "success" | "warning" | "danger" | "running";

export interface StepFeedItem {
  id: string;
  message: string;
  relativeTime: string;
  status?: StepFeedStatus;
  rawLog?: string;
}

export interface StepFeedProps {
  items: readonly StepFeedItem[];
  loading?: boolean;
  emptyText?: string;
  className?: string;
}

const STEP_META: Record<StepFeedStatus, { icon: string; toneClass: string }> = {
  info: { icon: "Info", toneClass: "text-status-info" },
  success: { icon: "CheckCircle2", toneClass: "text-status-success" },
  warning: { icon: "AlertTriangle", toneClass: "text-status-warning" },
  danger: { icon: "AlertCircle", toneClass: "text-status-danger" },
  running: { icon: "Activity", toneClass: "text-accent-primary motion-safe:animate-pulse" },
};

export function StepFeed({
  items,
  loading = false,
  emptyText = "No steps recorded yet.",
  className = "",
}: StepFeedProps) {
  const [expandedIds, setExpandedIds] = useState<Record<string, boolean>>({});

  const toggleDetails = (id: string) => {
    setExpandedIds((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  if (loading) {
    return (
      <div
        role="status"
        className={`flex flex-col gap-3 ${className}`}
        aria-busy="true"
        aria-label="Loading steps"
      >
        {[0, 1, 2].map((i) => (
          <div key={i} className="ns-skeleton h-10 w-full rounded-md bg-surface-raised" />
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return <p className="text-sm text-text-muted">{emptyText}</p>;
  }

  return (
    <ul className={`flex flex-col gap-2 ${className}`}>
      {items.map((item) => {
        const meta = STEP_META[item.status ?? "info"];
        const expanded = Boolean(expandedIds[item.id]);
        const logPanelId = `step-log-${item.id}`;

        return (
          <li
            key={item.id}
            className="rounded-md border border-border-subtle bg-surface-raised px-4 py-3"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <Icon name={meta.icon} size={16} className={`mt-1 shrink-0 ${meta.toneClass}`} />
                <p className="text-sm text-text-primary">{item.message}</p>
              </div>

              <div className="flex shrink-0 items-center gap-3">
                <span className="font-mono text-xs text-text-muted ns-tabular">
                  {item.relativeTime}
                </span>
                {item.rawLog && (
                  <button
                    type="button"
                    aria-expanded={expanded}
                    aria-controls={logPanelId}
                    onClick={() => toggleDetails(item.id)}
                    className="inline-flex items-center gap-1 rounded-sm border border-border-subtle bg-bg-base px-2 py-1 text-xs font-medium text-text-secondary transition-colors duration-fast hover:border-border-strong hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
                  >
                    <Icon name={expanded ? "ChevronUp" : "ChevronDown"} size={16} />
                    <span>Details</span>
                  </button>
                )}
              </div>
            </div>

            {item.rawLog && expanded && (
              <pre
                id={logPanelId}
                className="mt-3 overflow-x-auto rounded-sm border border-border-subtle bg-bg-canvas p-3 font-mono text-xs text-text-secondary ns-tabular"
              >
                {item.rawLog}
              </pre>
            )}
          </li>
        );
      })}
    </ul>
  );
}
