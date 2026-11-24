import { EmptyState, PageHeader } from "../../design";
import { useAppStore } from "../../app/store";

export default function PlanBuilderPage() {
  const pushToast = useAppStore((s) => s.pushToast);

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        title="Plan Builder"
        description="Order phases, check dependencies, and choose which AI handles each phase."
      />

      <EmptyState
        icon="ListChecks"
        title="No phases loaded yet"
        description="Create a phased plan or import a legacy queue.json file to configure prompts and timeouts."
        actionLabel="Create first phase"
        onAction={() =>
          pushToast({
            title: "Plan editor coming in the next phase",
            description: "The module shell and bridge contract are ready.",
            tone: "info",
          })
        }
      />
    </div>
  );
}
