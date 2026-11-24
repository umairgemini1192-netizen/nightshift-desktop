import { Button } from "./Button";
import { Icon } from "./Icon";

export interface EmptyStateProps {
  icon: string;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}

export function EmptyState({
  icon,
  title,
  description,
  actionLabel,
  onAction,
}: EmptyStateProps) {
  return (
    <div className="ns-glass flex flex-col items-center justify-center rounded-lg px-6 py-12 text-center">
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-pill bg-accent-primary-soft text-accent-primary">
        <Icon name={icon} size={24} />
      </div>
      <h2 className="font-display text-lg font-semibold text-text-primary">{title}</h2>
      <p className="mt-2 max-w-readable text-sm text-text-secondary">{description}</p>
      {actionLabel && onAction && (
        <div className="mt-6">
          <Button variant="primary" onClick={onAction}>
            {actionLabel}
          </Button>
        </div>
      )}
    </div>
  );
}
