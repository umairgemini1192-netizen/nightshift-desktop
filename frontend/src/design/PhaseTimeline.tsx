import { Icon } from "./Icon";

export type PhaseTimelineStatus = "done" | "current" | "next" | "blocked" | "failed";

export interface PhaseTimelineItem {
  id: string;
  title: string;
  status: PhaseTimelineStatus;
  detail?: string;
  blockedReason?: string;
  duration?: string;
}

export interface PhaseTimelineProps {
  items: readonly PhaseTimelineItem[];
  loading?: boolean;
  emptyText?: string;
  className?: string;
}

const STATUS_META: Record<
  PhaseTimelineStatus,
  { icon: string; badgeText: string; dotClass: string; textClass: string }
> = {
  done: {
    icon: "Check",
    badgeText: "Done",
    dotClass: "border-status-success bg-surface-raised text-status-success",
    textClass: "text-status-success",
  },
  current: {
    icon: "Activity",
    badgeText: "In progress",
    dotClass: "border-border-glow bg-accent-primary-soft text-accent-primary ns-live-glow",
    textClass: "text-accent-primary",
  },
  next: {
    icon: "Clock",
    badgeText: "Up next",
    dotClass: "border-border-subtle bg-surface-raised text-text-muted",
    textClass: "text-text-muted",
  },
  blocked: {
    icon: "Lock",
    badgeText: "Blocked",
    dotClass: "border-status-warning bg-surface-raised text-status-warning",
    textClass: "text-status-warning",
  },
  failed: {
    icon: "AlertTriangle",
    badgeText: "Needs you",
    dotClass: "border-status-danger bg-surface-raised text-status-danger",
    textClass: "text-status-danger",
  },
};

export function PhaseTimeline({
  items,
  loading = false,
  emptyText = "No phases scheduled yet.",
  className = "",
}: PhaseTimelineProps) {
  if (loading) {
    return (
      <div
        role="status"
        className={`flex flex-col gap-4 ${className}`}
        aria-busy="true"
        aria-label="Loading timeline"
      >
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex items-center gap-3">
            <div className="ns-skeleton h-8 w-8 shrink-0 rounded-pill bg-surface-raised" />
            <div className="flex flex-1 flex-col gap-2">
              <div className="ns-skeleton h-4 w-1/2 rounded-sm bg-surface-raised" />
              <div className="ns-skeleton h-3 w-2/3 rounded-sm bg-surface-raised" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (items.length === 0) {
    return <p className="text-sm text-text-muted">{emptyText}</p>;
  }

  return (
    <ol className={`flex flex-col ${className}`}>
      {items.map((item, idx) => {
        const meta = STATUS_META[item.status];
        const isLast = idx === items.length - 1;
        return (
          <li
            key={item.id}
            aria-current={item.status === "current" ? "step" : undefined}
            className="relative flex gap-4 pb-5 last:pb-0"
          >
            {!isLast && (
              <span
                aria-hidden="true"
                className="absolute top-8 bottom-0 left-4 w-0 border-l border-border-subtle"
              />
            )}

            <span
              className={`relative z-10 flex h-8 w-8 shrink-0 items-center justify-center rounded-pill border ${meta.dotClass}`}
            >
              <Icon
                name={meta.icon}
                size={16}
                className={item.status === "current" ? "motion-safe:animate-pulse" : undefined}
              />
            </span>

            <div className="min-w-0 flex-1 pt-1">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-medium text-text-primary">{item.title}</p>
                <div className="flex items-center gap-2">
                  {item.duration && (
                    <span className="font-mono text-xs text-text-muted ns-tabular">
                      {item.duration}
                    </span>
                  )}
                  <span className={`text-xs font-medium ${meta.textClass}`}>{meta.badgeText}</span>
                </div>
              </div>

              {item.detail && (
                <p className="mt-1 text-xs text-text-secondary">{item.detail}</p>
              )}

              {item.status === "blocked" && item.blockedReason && (
                <p className="mt-1 flex items-center gap-1 text-xs text-status-warning">
                  <Icon name="Lock" size={16} className="shrink-0" />
                  <span>{item.blockedReason}</span>
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
