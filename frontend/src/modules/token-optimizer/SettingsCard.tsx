import { useCallback, useEffect, useState } from "react";
import { Badge, Button, GlassCard, Icon, Toggle } from "../../design";
import type { TokenOptimizerState, TokenOptimizerToolItem } from "../../bridge/types";
import { useAppStore } from "../../app/store";

function statusBadgeProps(
  tool: TokenOptimizerToolItem,
): { tone: "success" | "info" | "warning" | "danger" | "neutral"; label: string } {
  switch (tool.status) {
    case "enabled":
      return { tone: "success", label: "Enabled" };
    case "auto_disabled":
      return { tone: "danger", label: "Auto-disabled" };
    case "not_installed":
      return { tone: "neutral", label: "Not installed" };
    case "hash_mismatch":
      return { tone: "danger", label: "SHA-256 mismatch" };
    case "isolation_blocked":
      return { tone: "warning", label: "Isolation required" };
    case "catalogue_only":
      return { tone: "neutral", label: "Catalogue only" };
    default:
      return { tone: "neutral", label: "Off" };
  }
}

export function TokenOptimizerSettingsCard() {
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);

  const [state, setState] = useState<TokenOptimizerState | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [probing, setProbing] = useState(false);
  const [togglingTool, setTogglingTool] = useState<string | null>(null);

  const loadState = useCallback(async () => {
    try {
      const res = await bridge.call<TokenOptimizerState>("token_optimizer.getState");
      if (res && Array.isArray(res.tools)) {
        setState(res);
      }
    } catch (err) {
      pushToast({
        title: "Could not load token optimizer settings",
        description: String(err),
        tone: "danger",
      });
    }
  }, [bridge, pushToast]);

  useEffect(() => {
    void loadState();
  }, [loadState]);

  const handleToggle = async (tool: TokenOptimizerToolItem, nextEnabled: boolean) => {
    setTogglingTool(tool.name);
    try {
      const res = await bridge.call<TokenOptimizerState>("token_optimizer.setToolEnabled", {
        tool: tool.name,
        enabled: nextEnabled,
      });
      if (res && Array.isArray(res.tools)) {
        setState(res);
      }
      pushToast({
        title: `${tool.displayName} ${nextEnabled ? "enabled" : "turned off"}`,
        description: nextEnabled
          ? "Runs will be compared against baseline runs on the same phase kind."
          : "Night-Shift will run without this tool from the next phase.",
        tone: "info",
      });
    } catch (err) {
      pushToast({
        title: `Could not update ${tool.displayName}`,
        description: String(err),
        tone: "danger",
      });
    } finally {
      setTogglingTool(null);
    }
  };

  const handleInstall = async (tool: TokenOptimizerToolItem) => {
    setTogglingTool(tool.name);
    try {
      const res = await bridge.call<TokenOptimizerState>("token_optimizer.installTool", { tool: tool.name });
      if (res && Array.isArray(res.tools)) setState(res);
    } catch (err) {
      pushToast({ title: `Could not install ${tool.displayName}`, description: String(err), tone: "danger" });
    } finally {
      setTogglingTool(null);
    }
  };

  const handleVerifyHashes = async () => {
    setVerifying(true);
    try {
      const res = await bridge.call<TokenOptimizerState>("token_optimizer.verifyTools");
      if (res && Array.isArray(res.tools)) {
        setState(res);
      }
      const hasMismatch = res?.tools?.some((t) => t.status === "hash_mismatch");
      pushToast({
        title: hasMismatch ? "Checksum mismatch detected" : "Tool checksums verified",
        description: hasMismatch
          ? "One or more tools failed SHA-256 verification and were disabled automatically."
          : "All installed artifacts match tools.lock.json.",
        tone: hasMismatch ? "danger" : "success",
      });
    } catch (err) {
      pushToast({
        title: "Verification failed",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setVerifying(false);
    }
  };

  const handleRunProbe = async () => {
    setProbing(true);
    try {
      const res = await bridge.call<TokenOptimizerState>("token_optimizer.runIsolationProbe");
      if (res && Array.isArray(res.tools)) {
        setState(res);
      }
      const passed = Boolean(res?.probeResult?.passed);
      pushToast({
        title: passed ? "Isolation probe passed" : "Isolation probe blocked agy-fast",
        description:
          res?.probeResult?.reason ??
          (passed
            ? "Antigravity loaded agy-fast per run without editing global config."
            : "agy-fast stayed disabled to protect ~/.gemini/config."),
        tone: passed ? "success" : "warning",
      });
    } catch (err) {
      pushToast({
        title: "Isolation probe failed",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setProbing(false);
    }
  };

  return (
    <div id="token-optimizer-settings-card">
      <GlassCard
        title="Token Optimizer"
        icon="Zap"
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone="info">Telemetry off</Badge>
            <Button
              size="sm"
              variant="secondary"
              icon="ShieldCheck"
              loading={verifying}
              onClick={() => void handleVerifyHashes()}
            >
              Verify hashes
            </Button>
          </div>
        }
      >
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
            <p className="max-w-prose">
              Every tool is pinned in tools.lock.json, verified by SHA-256 in your local app data folder, and kept only when A/B runs prove net token savings without extra retries or failures.
            </p>
            {state && (
              <Badge tone={state.totalTokensSaved > 0 ? "success" : "neutral"}>
                {state.estimateLabel}
              </Badge>
            )}
          </div>

          {state?.warnings && state.warnings.length > 0 && (
            <div className="flex flex-col gap-2">
              {state.warnings.map((warn, idx) => (
                <div
                  key={idx}
                  className="flex items-start gap-2 rounded-md border border-border-subtle bg-surface-raised px-3 py-2 text-xs text-text-secondary"
                >
                  <Icon name="ShieldAlert" size={16} className="mt-0.5 shrink-0 text-status-warning" />
                  <span>{warn}</span>
                </div>
              ))}
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {(state?.tools ?? []).map((tool) => {
              const badge = statusBadgeProps(tool);
              const toggleDisabled =
                tool.catalogueOnly ||
                tool.status === "hash_mismatch" ||
                (tool.isolationGateRequired && !tool.isolationPassed) ||
                togglingTool === tool.name;

              return (
                <div
                  key={tool.name}
                  className="ns-glass flex flex-col justify-between gap-3 rounded-lg p-3.5"
                >
                  <div className="flex flex-col gap-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-semibold text-text-primary">
                          {tool.displayName}
                        </span>
                        <span className="font-mono text-xs text-text-muted">{tool.version}</span>
                      </div>
                      <Badge tone={badge.tone}>{badge.label}</Badge>
                    </div>

                    <p className="text-xs text-text-secondary">{tool.role}</p>

                    <div className="flex flex-wrap items-center gap-2 text-xs">
                      <Badge tone={tool.sha256Verified ? "success" : "neutral"}>
                        <Icon name={tool.sha256Verified ? "ShieldCheck" : "Lock"} size={16} />
                        <span>{tool.sha256Verified ? "SHA-256 verified" : "Not verified"}</span>
                      </Badge>
                      {tool.license && <Badge tone="neutral">{tool.license}</Badge>}
                      {tool.tokensSavedTonight > 0 && (
                        <span className="font-mono text-accent-primary ns-tabular">
                          {tool.estimateLabel}
                        </span>
                      )}
                    </div>

                    {tool.warning && (
                      <p className="text-xs text-status-warning leading-snug">{tool.warning}</p>
                    )}
                    {tool.abReason && !tool.warning && (
                      <p className="text-xs text-text-muted leading-snug">{tool.abReason}</p>
                    )}
                  </div>

                  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle pt-2.5">
                    <Toggle
                      id={`token-tool-${tool.name}`}
                      label={`Enable ${tool.displayName}`}
                      checked={tool.enabled}
                      disabled={toggleDisabled}
                      onChange={(checked) => void handleToggle(tool, checked)}
                    />
                    {tool.status === "not_installed" && (
                      <Button size="sm" variant="secondary" loading={togglingTool === tool.name} onClick={() => void handleInstall(tool)}>Install</Button>
                    )}
                    {tool.isolationGateRequired && (
                      <Button
                        size="sm"
                        variant="ghost"
                        icon="Terminal"
                        loading={probing}
                        onClick={() => void handleRunProbe()}
                      >
                        Run isolation probe
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {state?.toolsRoot && (
            <p className="font-mono text-xs text-text-muted">
              Local tools folder: {state.toolsRoot}
            </p>
          )}
        </div>
      </GlassCard>
    </div>
  );
}

export default TokenOptimizerSettingsCard;
