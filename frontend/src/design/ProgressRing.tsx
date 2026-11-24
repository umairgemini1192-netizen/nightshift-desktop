import { motion, useReducedMotion } from "framer-motion";

export interface ProgressRingProps {
  value: number;
  max?: number;
  size?: number;
  strokeWidth?: number;
  label: string;
  caption?: string;
  tone?: "accent" | "success" | "warning" | "danger";
  loading?: boolean;
  className?: string;
}

const TONE_STROKE: Record<NonNullable<ProgressRingProps["tone"]>, string> = {
  accent: "var(--accent-primary)",
  success: "var(--status-success)",
  warning: "var(--status-warning)",
  danger: "var(--status-danger)",
};

export function ProgressRing({
  value,
  max = 100,
  size = 168,
  strokeWidth = 10,
  label,
  caption,
  tone = "accent",
  loading = false,
  className = "",
}: ProgressRingProps) {
  const prefersReduced = useReducedMotion();
  const safeMax = max > 0 ? max : 100;
  const clamped = Math.min(Math.max(value, 0), safeMax);
  const pct = Math.round((clamped / safeMax) * 100);

  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const targetOffset = circumference - (clamped / safeMax) * circumference;

  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={safeMax}
      aria-valuenow={Math.round(clamped)}
      aria-valuetext={`${pct}%`}
      aria-busy={loading || undefined}
      className={`relative inline-flex items-center justify-center ${className}`}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        className="-rotate-90"
        aria-hidden="true"
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--border-subtle)"
          strokeWidth={strokeWidth}
        />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={TONE_STROKE[tone]}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={circumference}
          initial={prefersReduced ? { strokeDashoffset: targetOffset } : { strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: loading ? circumference * 0.72 : targetOffset }}
          transition={
            prefersReduced
              ? { duration: 0 }
              : { duration: 0.42, ease: [0.16, 1, 0.3, 1] }
          }
        />
      </svg>

      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center px-4">
        <span className="font-mono text-2xl font-semibold text-text-primary ns-tabular">
          {loading ? "--" : `${pct}%`}
        </span>
        {caption && (
          <span className="mt-1 text-xs text-text-secondary">
            {caption}
          </span>
        )}
      </div>
    </div>
  );
}
