import { EmptyState, GlassCard, PageHeader, SegmentedControl } from "../../design";
import type { ThemeMode } from "../../design/tokens";
import { useAppStore } from "../../app/store";

const THEME_OPTIONS = [
  { value: "system", label: "System", icon: "Monitor" },
  { value: "dark", label: "Dark", icon: "Moon" },
  { value: "light", label: "Light", icon: "Sun" },
] as const;

export default function SettingsPage() {
  const themeMode = useAppStore((s) => s.themeMode);
  const resolvedTheme = useAppStore((s) => s.resolvedTheme);
  const setThemeMode = useAppStore((s) => s.setThemeMode);
  const pushToast = useAppStore((s) => s.pushToast);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Settings"
        description="Configure appearance, fallback priority order, guard limits, and connected projects."
      />

      <GlassCard title="Appearance" icon="Sparkles">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-text-primary">Theme</p>
            <p className="mt-1 text-xs text-text-secondary">
              Follows your Windows setting by default. Currently showing {resolvedTheme} theme.
            </p>
          </div>
          <SegmentedControl<ThemeMode>
            label="Theme preference"
            value={themeMode}
            options={THEME_OPTIONS}
            onChange={(mode) => setThemeMode(mode)}
          />
        </div>
      </GlassCard>

      <EmptyState
        icon="Settings"
        title="No custom projects configured"
        description="Add a local repository, build rules, and release artifact pattern to run Night-Shift unattended."
        actionLabel="Add project"
        onAction={() =>
          pushToast({
            title: "Project setup ready",
            description: "Project configuration forms will be added in the next phase.",
            tone: "info",
          })
        }
      />
    </div>
  );
}
