import { EmptyState, PageHeader } from "../../design";
import { useAppStore } from "../../app/store";

export default function EnginesQuotaPage() {
  const pushToast = useAppStore((s) => s.pushToast);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="AI Engines & Quota"
        description="Manage engine tiers, login health, cooldowns, and estimated budget usage."
      />

      <EmptyState
        icon="Cpu"
        title="Engine catalogue ready to configure"
        description="Check CLI login status and set estimated quota limits for Economy, Standard, and Gold engines."
        actionLabel="Check logins"
        onAction={() =>
          pushToast({
            title: "Login check queued",
            description: "CLI login health checks will report Ready, Cooling down, or Logged out.",
            tone: "info",
          })
        }
      />
    </div>
  );
}
