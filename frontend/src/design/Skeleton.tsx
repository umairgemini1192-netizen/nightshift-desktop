export interface SkeletonProps {
  variant?: "text" | "circle" | "rect";
  lines?: number;
  className?: string;
}

export function Skeleton({ variant = "text", lines = 1, className = "" }: SkeletonProps) {
  if (variant === "circle") {
    return (
      <div
        role="status"
        aria-label="Loading"
        aria-busy="true"
        className={`ns-skeleton h-12 w-12 shrink-0 rounded-pill bg-surface-raised ${className}`}
      />
    );
  }

  if (variant === "rect") {
    return (
      <div
        role="status"
        aria-label="Loading"
        aria-busy="true"
        className={`ns-skeleton h-16 w-full rounded-md bg-surface-raised ${className}`}
      />
    );
  }

  return (
    <div role="status" aria-label="Loading" aria-busy="true" className={`flex flex-col gap-2 ${className}`}>
      {Array.from({ length: Math.max(lines, 1) }, (_, idx) => (
        <div
          key={idx}
          className={`ns-skeleton h-4 rounded-sm bg-surface-raised ${
            idx === lines - 1 && lines > 1 ? "w-2/3" : "w-full"
          }`}
        />
      ))}
    </div>
  );
}
