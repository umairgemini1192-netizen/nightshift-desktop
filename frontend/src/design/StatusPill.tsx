import type { AppRunState } from "../bridge/types";
import { Icon } from "./Icon";

interface StatusPillProps {
  state: AppRunState;
  label?: string;
}

const STATE_CONFIG: Record<
  AppRunState,
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
    toneClass: "bg-accent-primary-soft text-accent-primary border-border-glow",
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

export function StatusPill({ state, label }: StatusPillProps) {
  const cfg = STATE_CONFIG[state];
  return (
    <span
      role="status"
      className={`inline-flex items-center gap-2 rounded-pill border px-3 py-1 text-xs font-medium ns-tabular ${cfg.toneClass}`}
    >
      <Icon name={cfg.icon} size={16} className={cfg.pulse ? "motion-safe:animate-pulse" : undefined} />
      <span>{label ?? cfg.text}</span>
    </span>
  );
}
