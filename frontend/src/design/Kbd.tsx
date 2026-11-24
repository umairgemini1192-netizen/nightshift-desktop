import type { ReactNode } from "react";

interface KbdProps {
  children: ReactNode;
}

export function Kbd({ children }: KbdProps) {
  return (
    <kbd className="inline-flex items-center rounded-sm border border-border-subtle bg-surface-raised px-2 py-1 font-mono text-xs text-text-muted ns-tabular">
      {children}
    </kbd>
  );
}
