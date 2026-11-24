import type { ReactNode } from "react";
import { Icon } from "./Icon";

export interface GlassCardProps {
  title?: string;
  icon?: string;
  action?: ReactNode;
  footer?: ReactNode;
  variant?: "default" | "hero";
  live?: boolean;
  className?: string;
  children: ReactNode;
}

export function GlassCard({
  title,
  icon,
  action,
  footer,
  variant = "default",
  live = false,
  className = "",
  children,
}: GlassCardProps) {
  const glowClass = variant === "hero" && live ? "ns-live-glow shadow-glow-accent" : "";
  return (
    <section
      className={`ns-glass rounded-lg p-6 transition-colors duration-base ease-out ${glowClass} ${className}`}
    >
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between gap-4">
          <div className="flex items-center gap-2 min-w-0">
            {icon && <Icon name={icon} size={20} className="text-accent-primary shrink-0" />}
            {title && (
              <h2 className="font-display text-lg font-semibold text-text-primary truncate">
                {title}
              </h2>
            )}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </header>
      )}
      <div>{children}</div>
      {footer && <footer className="mt-5 border-t border-border-subtle pt-4">{footer}</footer>}
    </section>
  );
}
