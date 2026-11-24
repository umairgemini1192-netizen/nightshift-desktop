import { EmptyState, PageHeader } from "../../design";
import { useAppStore } from "../../app/store";

export default function RunsHistoryPage() {
  const pushToast = useAppStore((s) => s.pushToast);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Runs & History"
        description="Review past overnight runs, morning digests, pull requests, and release artifacts."
      />

      <EmptyState
        icon="History"
        title="No completed runs recorded"
        description="Completed and parked runs will appear here with their timeline, PR links, and plain-English digest."
        actionLabel="Refresh history"
        onAction={() =>
          pushToast({
            title: "History is up to date",
            description: "No previous runs found in the local SQLite store.",
            tone: "info",
          })
        }
      />
    </div>
  );
}
