import { motion, useReducedMotion } from "framer-motion";
import { Badge } from "./Badge";
import { Icon } from "./Icon";

export type EngineAvailability = "ready" | "cooling" | "no-login" | "unavailable";

export interface EngineCardProps {
  name: string;
  model: string;
  effort: string;
  state: EngineAvailability;
  coolingUntil?: string;
  quotaUsedPct: number;
  live?: boolean;
  disabled?: boolean;
  onAction?: () => void;
  actionLabel?: string;
  className?: string;
}

const STATE_META: Record<
  EngineAvailability,
  { icon: string; tone: "success" | "warning" | "danger" | "neutral"; label: (until?: string) => string }
> = {
  ready: {
    icon: "CheckCircle2",
    tone: "success",
    label: () => "Ready",
  },
  cooling: {
    icon: "Clock",
    tone: "warning",
    label: (until) => (until ? `Cooling until ${until}` : "Cooling down"),
  },
  "no-login": {
    icon: "AlertTriangle",
    tone: "danger",
    label: () => "No login",
  },
  unavailable: {
    icon: "AlertCircle",
    tone: "neutral",
    label: () => "Unavailable",
  },
};

export function EngineCard({
  name,
  model,
  effort,
  state,
  coolingUntil,
  quotaUsedPct,
  live = false,
  disabled = false,
  onAction,
  actionLabel,
  className = "",
}: EngineCardProps) {
  const prefersReduced = useReducedMotion();
  const clampedQuota = Math.min(Math.max(Math.round(quotaUsedPct), 0), 100);
  const meta = STATE_META[state];

  const barTone =
    clampedQuota >= 90
      ? "bg-status-danger"
      : clampedQuota >= 70
        ? "bg-status-warning"
        : "bg-accent-primary";

  return (
    <article
      aria-disabled={disabled || undefined}
      className={`ns-glass flex flex-col gap-4 rounded-lg p-5 transition-colors duration-base ease-out ${
        live ? "ns-live-glow shadow-glow-accent" : ""
      } ${disabled ? "opacity-50" : ""} ${className}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Icon name="Cpu" size={20} className="shrink-0 text-accent-primary" />
            <h3 className="truncate font-display text-md font-semibold text-text-primary">{name}</h3>
          </div>
          <p className="mt-1 truncate font-mono text-xs text-text-secondary ns-tabular">{model}</p>
        </div>
        <Badge tone="neutral">{effort} effort</Badge>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <Badge tone={meta.tone}>
          <Icon name={meta.icon} size={16} />
          <span>{meta.label(coolingUntil)}</span>
        </Badge>
        {onAction && actionLabel && (
          <button
            type="button"
            disabled={disabled}
            onClick={onAction}
            className="rounded-sm border border-border-subtle bg-surface-raised px-2 py-1 text-xs font-medium text-text-primary transition-colors duration-fast hover:border-border-strong focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed"
          >
            {actionLabel}
          </button>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="text-text-secondary">Quota used (estimate)</span>
          <span className="font-mono text-text-primary ns-tabular">{clampedQuota}%</span>
        </div>
        <div
          role="progressbar"
          aria-label={`${name} estimated quota used`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={clampedQuota}
          aria-valuetext={`${clampedQuota}% estimate`}
          className="h-2 w-full overflow-hidden rounded-pill border border-border-subtle bg-surface-raised"
        >
          <motion.div
            initial={prefersReduced ? { width: `${clampedQuota}%` } : { width: "0%" }}
            animate={{ width: `${clampedQuota}%` }}
            transition={
              prefersReduced
                ? { duration: 0 }
                : { duration: 0.42, ease: [0.16, 1, 0.3, 1] }
            }
            className={`h-full rounded-pill ${barTone}`}
          />
        </div>
      </div>
    </article>
  );
}
