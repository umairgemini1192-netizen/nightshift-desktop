import { useCallback, useEffect, useState } from "react";
import { Badge, Button, GlassCard, TextField, Toggle } from "../../design";
import type { PhoneStatusSettings, PhoneStatusTestResult } from "../../bridge/types";
import { useAppStore } from "../../app/store";

function validateBridgeUrl(raw: string): string | undefined {
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  const lower = trimmed.toLowerCase();
  if (
    lower.startsWith("https://") ||
    lower.startsWith("http://127.0.0.1") ||
    lower.startsWith("http://localhost")
  ) {
    return undefined;
  }
  return "Use the https:// Web app URL from Apps Script.";
}

export function PhoneStatusSettingsCard() {
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);

  const [enabled, setEnabled] = useState(false);
  const [url, setUrl] = useState("");
  const [secretInput, setSecretInput] = useState("");
  const [hasSecret, setHasSecret] = useState(false);
  const [lastResult, setLastResult] = useState("not sent yet");
  const [saving, setSaving] = useState(false);
  const [sendingTest, setSendingTest] = useState(false);

  const urlError = validateBridgeUrl(url);

  const loadSettings = useCallback(async () => {
    try {
      const res = await bridge.call<PhoneStatusSettings>("phone_status.getSettings");
      if (res) {
        setEnabled(Boolean(res.enabled));
        setUrl(res.url ?? "");
        setHasSecret(Boolean(res.hasSecret));
        setLastResult(res.lastResult || "not sent yet");
      }
    } catch (err) {
      pushToast({
        title: "Could not load phone bridge settings",
        description: String(err),
        tone: "danger",
      });
    }
  }, [bridge, pushToast]);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const persistSettings = async (
    nextEnabled: boolean,
    options?: { clearSecret?: boolean },
  ) => {
    if (validateBridgeUrl(url)) {
      pushToast({
        title: "Check the Apps Script URL",
        description: "Paste the https:// Web app URL from Deploy -> Web app.",
        tone: "danger",
      });
      return;
    }
    setSaving(true);
    try {
      const res = await bridge.call<PhoneStatusSettings>("phone_status.saveSettings", {
        enabled: nextEnabled,
        url: url.trim(),
        secret: options?.clearSecret ? undefined : secretInput.trim() || undefined,
        clearSecret: options?.clearSecret || undefined,
      });
      setEnabled(Boolean(res.enabled));
      setUrl(res.url ?? "");
      setHasSecret(Boolean(res.hasSecret));
      setSecretInput("");
      setLastResult(res.lastResult || "not sent yet");
      pushToast({
        title: options?.clearSecret ? "Bridge secret cleared" : "Phone status bridge saved",
        description: options?.clearSecret
          ? "Enter a new shared secret before sending phone updates."
          : nextEnabled
            ? "Redacted summary updates will post to your Apps Script web app."
            : "Phone status bridge is off.",
        tone: "success",
      });
    } catch (err) {
      pushToast({
        title: "Could not save phone bridge settings",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = (nextEnabled: boolean) => {
    setEnabled(nextEnabled);
    void persistSettings(nextEnabled);
  };

  const handleSendTest = async () => {
    if (validateBridgeUrl(url)) {
      setLastResult("failed: invalid URL");
      return;
    }
    setSendingTest(true);
    try {
      if (url.trim() || secretInput.trim()) {
        const saved = await bridge.call<PhoneStatusSettings>("phone_status.saveSettings", {
          enabled,
          url: url.trim(),
          secret: secretInput.trim() || undefined,
        });
        setUrl(saved.url ?? url.trim());
        setHasSecret(Boolean(saved.hasSecret));
        setSecretInput("");
      }
      const res = await bridge.call<PhoneStatusTestResult>("phone_status.sendTest");
      setLastResult(res.lastResult || (res.ok ? "ok" : "failed"));
      pushToast({
        title: res.ok ? "Phone test update sent" : "Phone test update failed",
        description: res.ok
          ? "Check NIGHTSHIFT_LIVE_STATUS in Google Docs on your phone."
          : res.lastResult,
        tone: res.ok ? "success" : "danger",
      });
    } catch (err) {
      const msg = `failed: ${String(err)}`;
      setLastResult(msg);
      pushToast({
        title: "Phone test update failed",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setSendingTest(false);
    }
  };

  const resultTone: "success" | "danger" | "neutral" = lastResult.startsWith("ok")
    ? "success"
    : lastResult.startsWith("failed")
      ? "danger"
      : "neutral";

  return (
    <div id="phone-status-bridge-card">
      <GlassCard
        title="Phone Status Bridge"
        icon="Smartphone"
        action={
          <Badge tone={enabled ? "info" : "neutral"}>
            {enabled ? "Enabled" : "Off"}
          </Badge>
        }
      >
        <p className="text-xs text-text-secondary">
          Check overnight runs from your phone in one Google Doc (NIGHTSHIFT_LIVE_STATUS). Only the redacted summary deck is sent; log lines, file paths, prompts, and diffs never leave your PC.
        </p>

        <div className="mt-4 flex flex-col gap-4">
          <Toggle
            id="phone-status-enabled"
            label="Enable phone status bridge"
            helperText="Off by default. Posts at most every 60 seconds, immediately on start/stop or phase finish, and every 10 minutes while running."
            checked={enabled}
            onChange={handleToggle}
          />

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <TextField
              id="phone-status-url"
              label="Apps Script web app URL"
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://script.google.com/macros/s/.../exec"
              error={urlError}
              helperText="From Deploy -> Web app in tools/phone-bridge/README.md"
            />

            <TextField
              id="phone-status-secret"
              label="Shared secret"
              type="password"
              value={secretInput}
              onChange={(e) => setSecretInput(e.target.value)}
              placeholder={
                hasSecret
                  ? "Saved with Windows DPAPI (enter new secret to replace)"
                  : "Enter shared secret (stored with Windows DPAPI)"
              }
              helperText={
                hasSecret
                  ? "Secret is stored encrypted and never shown again."
                  : "Matches NIGHTSHIFT_BRIDGE_SECRET in Apps Script."
              }
            />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border-subtle pt-3">
            <div className="flex items-center gap-2 text-xs">
              <span className="text-text-secondary">Last phone update:</span>
              <Badge tone={resultTone}>{lastResult}</Badge>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              {hasSecret && (
                <Button
                  size="sm"
                  variant="ghost"
                  icon="Trash2"
                  disabled={saving || sendingTest}
                  onClick={() => void persistSettings(enabled, { clearSecret: true })}
                >
                  Clear secret
                </Button>
              )}
              <Button
                size="sm"
                variant="secondary"
                icon="Save"
                disabled={Boolean(urlError)}
                loading={saving}
                onClick={() => void persistSettings(enabled)}
              >
                Save bridge
              </Button>
              <Button
                size="sm"
                variant="primary"
                icon="Send"
                disabled={Boolean(urlError)}
                loading={sendingTest}
                onClick={() => void handleSendTest()}
              >
                Send test update
              </Button>
            </div>
          </div>
        </div>
      </GlassCard>
    </div>
  );
}

export default PhoneStatusSettingsCard;
