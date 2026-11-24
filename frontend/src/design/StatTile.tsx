import { Icon } from "./Icon";

export interface StatTileProps {
  label: string;
  value: string;
  delta?: string;
  deltaTone?: "positive" | "negative" | "neutral";
  sparkline?: readonly number[];
  loading?: boolean;
  className?: string;
}

const DELTA_STYLE: Record<
  NonNullable<StatTileProps["deltaTone"]>,
  { icon: string; cls: string }
> = {
  positive: { icon: "TrendingUp", cls: "text-status-success" },
  negative: { icon: "TrendingDown", cls: "text-status-danger" },
  neutral: { icon: "Minus", cls: "text-text-muted" },
};

function buildSparklinePoints(data: readonly number[], width: number, height: number): string {
  if (data.length === 0) return "";
  if (data.length === 1) return `0,${height / 2} ${width},${height / 2}`;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  return data
    .map((v, i) => {
      const x = (i / (data.length - 1)) * width;
      const y = height - ((v - min) / span) * (height - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export function StatTile({
  label,
  value,
  delta,
  deltaTone = "neutral",
  sparkline,
  loading = false,
  className = "",
}: StatTileProps) {
  const deltaMeta = DELTA_STYLE[deltaTone];
  const points = sparkline && sparkline.length > 0 ? buildSparklinePoints(sparkline, 96, 28) : "";

  return (
    <div
      aria-busy={loading || undefined}
      className={`ns-glass flex flex-col justify-between gap-3 rounded-lg p-5 ${className}`}
    >
      <span className="text-xs font-medium text-text-secondary">{label}</span>

      {loading ? (
        <div className="flex flex-col gap-2">
          <div className="ns-skeleton h-8 w-2/3 rounded-sm bg-surface-raised" />
          <div className="ns-skeleton h-4 w-1/2 rounded-sm bg-surface-raised" />
        </div>
      ) : (
        <div className="flex items-end justify-between gap-3">
          <div className="min-w-0">
            <p className="font-mono text-2xl font-semibold text-text-primary ns-tabular">{value}</p>
            {delta && (
              <p className={`mt-1 inline-flex items-center gap-1 font-mono text-xs ns-tabular ${deltaMeta.cls}`}>
                <Icon name={deltaMeta.icon} size={16} />
                <span>{delta}</span>
              </p>
            )}
          </div>

          {points && (
            <svg
              width={96}
              height={28}
              viewBox="0 0 96 28"
              aria-hidden="true"
              className="shrink-0 overflow-visible"
            >
              <polyline
                fill="none"
                stroke="var(--accent-primary)"
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
                points={points}
              />
            </svg>
          )}
        </div>
      )}
    </div>
  );
}
