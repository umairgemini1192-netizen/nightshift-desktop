import type { AppRunState } from "../bridge/types";
import { Icon } from "./Icon";

export type StatusPillState = AppRunState;

export interface StatusPillProps {
  state: StatusPillState;
  label?: string;
  className?: string;
}

const STATE_CONFIG: Record<
  StatusPillState,
  { text: string; icon: string; toneClass: string; pulse?: boolean }
> = {
  idle: {
    text: "Idle",
    icon: "PauseCircle",
    toneClass: "bg-surface-raised text-text-secondary border-border-subtle",
  },
  running: {
    text: "Running",
    icon: "Activity",
    toneClass: "bg-accent-primary-soft text-accent-primary border-border-glow ns-live-glow",
    pulse: true,
  },
  waiting: {
    text: "Waiting",
    icon: "Clock",
    toneClass: "bg-surface-raised text-status-info border-border-subtle",
  },
  cooling: {
    text: "Cooling down",
    icon: "Clock",
    toneClass: "bg-surface-raised text-status-warning border-border-subtle",
  },
  failed: {
    text: "Needs you",
    icon: "AlertTriangle",
    toneClass: "bg-surface-raised text-status-danger border-border-strong",
  },
  done: {
    text: "All good",
    icon: "CheckCircle2",
    toneClass: "bg-surface-raised text-status-success border-border-subtle",
  },
};

export function StatusPill({ state, label, className = "" }: StatusPillProps) {
  const cfg = STATE_CONFIG[state];
  return (
    <span
      role="status"
      data-status={state}
      className={`inline-flex items-center gap-2 rounded-pill border px-3 py-1 text-xs font-medium ns-tabular ${cfg.toneClass} ${className}`}
    >
      {cfg.pulse && (
        <span
          aria-hidden="true"
          className="h-2 w-2 shrink-0 rounded-pill bg-accent-primary motion-safe:animate-pulse"
        />
      )}
      <Icon
        name={cfg.icon}
        size={16}
        className={cfg.pulse ? "shrink-0 motion-safe:animate-pulse" : "shrink-0"}
      />
      <span>{label ?? cfg.text}</span>
    </span>
  );
}
