import { Badge, GlassCard, Icon } from "../../design";
import type { TokenSavingsTonight } from "../../bridge/types";

interface TokenSavingsCardProps {
  savings: TokenSavingsTonight;
}

export function TokenSavingsCard({ savings }: TokenSavingsCardProps) {
  const hasWarnings = savings.warnings.length > 0;

  return (
    <div id="token-savings-card">
      <GlassCard
        title="Token optimizer savings"
        icon="Zap"
        action={
          <Badge tone={savings.totalTokensSaved > 0 ? "success" : "neutral"}>
            {savings.estimateLabel}
          </Badge>
        }
      >
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <p className="text-xs uppercase tracking-wider text-text-muted">
                Saved tonight (estimate)
              </p>
              <p className="mt-1 font-display text-2xl font-semibold text-text-primary ns-tabular">
                ~{savings.totalTokensSaved.toLocaleString()} tokens
              </p>
            </div>
            <p className="max-w-prose text-xs text-text-secondary">
              Measured by comparing runs with each tool on and off on the same phase kind.
            </p>
          </div>

          {savings.perTool.length > 0 ? (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {savings.perTool.map((item) => {
                const tone = item.autoDisabled
                  ? "danger"
                  : item.abAction === "keep_enabled"
                    ? "success"
                    : "neutral";
                const statusLabel = item.autoDisabled
                  ? "Auto-disabled"
                  : item.abAction === "keep_enabled"
                    ? "Verified saving"
                    : item.enabled
                      ? "Measuring"
                      : "Off";

                return (
                  <div
                    key={item.tool}
                    className="flex flex-col gap-1.5 rounded-md border border-border-subtle bg-surface-raised p-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-text-primary">{item.tool}</span>
                      <Badge tone={tone}>{statusLabel}</Badge>
                    </div>
                    <p className="font-mono text-xs text-accent-primary ns-tabular">
                      {item.estimateLabel}
                    </p>
                    {item.abReason && (
                      <p className="text-xs text-text-muted leading-snug">{item.abReason}</p>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="text-xs text-text-muted">
              No optimizer measurements recorded yet tonight. Enable a tool in Settings to begin A/B measurement.
            </p>
          )}

          {hasWarnings && (
            <div className="flex flex-col gap-2 border-t border-border-subtle pt-3">
              {savings.warnings.map((warn, idx) => (
                <div
                  key={idx}
                  className="flex items-start gap-2 rounded-md border border-border-subtle bg-surface-raised px-3 py-2 text-xs text-text-secondary"
                >
                  <Icon name="AlertTriangle" size={16} className="mt-0.5 shrink-0 text-status-warning" />
                  <span>{warn}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </GlassCard>
    </div>
  );
}

export default TokenSavingsCard;
