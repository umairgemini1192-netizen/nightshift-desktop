import { motion, useReducedMotion } from "framer-motion";

export interface BudgetMeterProps {
  label: string;
  valueText: string;
  percent: number;
  ariaLabel?: string;
  className?: string;
}

export function BudgetMeter({
  label,
  valueText,
  percent,
  ariaLabel,
  className = "",
}: BudgetMeterProps) {
  const prefersReduced = useReducedMotion();
  const clampedPct = Math.min(Math.max(Math.round(Number.isFinite(percent) ? percent : 0), 0), 100);

  const barTone =
    clampedPct >= 90
      ? "bg-status-danger"
      : clampedPct >= 70
        ? "bg-status-warning"
        : "bg-accent-primary";

  return (
    <div
      className={`rounded-lg border border-border-subtle bg-surface-raised p-4 flex flex-col gap-2 ${className}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="font-medium text-text-primary">{label}</span>
        <span className="font-mono text-text-secondary ns-tabular">{valueText}</span>
      </div>
      <div
        role="progressbar"
        aria-label={ariaLabel ?? label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={clampedPct}
        aria-valuetext={valueText}
        className="h-2 w-full rounded-pill bg-bg-base overflow-hidden border border-border-subtle"
      >
        <motion.div
          initial={prefersReduced ? { width: `${clampedPct}%` } : { width: "0%" }}
          animate={{ width: `${clampedPct}%` }}
          transition={
            prefersReduced
              ? { duration: 0 }
              : { duration: 0.42, ease: [0.16, 1, 0.3, 1] }
          }
          className={`h-full rounded-pill ${barTone}`}
        />
      </div>
    </div>
  );
}
