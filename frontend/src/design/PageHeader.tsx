import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  description: string;
  action?: ReactNode;
}

export function PageHeader({ title, description, action }: PageHeaderProps) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="font-display text-2xl font-semibold tracking-tight text-text-primary">
          {title}
        </h1>
        <p className="mt-1 max-w-readable text-sm text-text-secondary">{description}</p>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </header>
  );
}
