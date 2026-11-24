import type { ReactNode } from "react";

export type BadgeTone = "neutral" | "info" | "success" | "warning" | "danger";

interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}

const TONE_CLASSES: Record<BadgeTone, string> = {
  neutral: "bg-surface-raised text-text-secondary border-border-subtle",
  info: "bg-surface-raised text-accent-primary border-border-glow",
  success: "bg-surface-raised text-status-success border-border-subtle",
  warning: "bg-surface-raised text-status-warning border-border-subtle",
  danger: "bg-surface-raised text-status-danger border-border-subtle",
};

export function Badge({ tone = "neutral", children, className = "" }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-pill border px-2 py-1 text-xs font-medium leading-none ns-tabular ${TONE_CLASSES[tone]} ${className}`}
    >
      {children}
    </span>
  );
}
