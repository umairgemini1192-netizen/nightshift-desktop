import type { ReactNode } from "react";
import { Icon } from "./Icon";

export interface GlassCardProps {
  title?: string;
  icon?: string;
  action?: ReactNode;
  footer?: ReactNode;
  variant?: "default" | "hero" | "raised";
  live?: boolean;
  loading?: boolean;
  error?: string;
  empty?: ReactNode;
  disabled?: boolean;
  className?: string;
  children?: ReactNode;
}

export function GlassCard({
  title,
  icon,
  action,
  footer,
  variant = "default",
  live = false,
  loading = false,
  error,
  empty,
  disabled = false,
  className = "",
  children,
}: GlassCardProps) {
  const surfaceClass = variant === "raised" ? "ns-glass-strong" : "ns-glass";
  const glowClass =
    variant === "hero" && live
      ? "ns-live-glow shadow-glow-accent"
      : live
        ? "ns-live-glow"
        : "";
  const errorClass = error ? "border-status-danger" : "";
  const disabledClass = disabled ? "opacity-60 pointer-events-none" : "";

  return (
    <section
      aria-busy={loading || undefined}
      aria-disabled={disabled || undefined}
      className={`${surfaceClass} rounded-lg p-6 transition-colors duration-base ease-out ${glowClass} ${errorClass} ${disabledClass} ${className}`}
    >
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between gap-4">
          <div className="flex min-w-0 items-center gap-2">
            {icon && <Icon name={icon} size={20} className="shrink-0 text-accent-primary" />}
            {title && (
              <h2 className="truncate font-display text-lg font-semibold text-text-primary">
                {title}
              </h2>
            )}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </header>
      )}

      {error ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-status-danger bg-surface-raised p-4 text-sm text-status-danger"
        >
          <Icon name="AlertTriangle" size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : loading ? (
        <div role="status" className="flex flex-col gap-3" aria-label="Loading content">
          <div className="ns-skeleton h-4 w-2/3 rounded-sm bg-surface-raised" />
          <div className="ns-skeleton h-4 w-full rounded-sm bg-surface-raised" />
          <div className="ns-skeleton h-4 w-1/2 rounded-sm bg-surface-raised" />
        </div>
      ) : empty ? (
        <div>{empty}</div>
      ) : (
        <div>{children}</div>
      )}

      {footer && <footer className="mt-5 border-t border-border-subtle pt-4">{footer}</footer>}
    </section>
  );
}
