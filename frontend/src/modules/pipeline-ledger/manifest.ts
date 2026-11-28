import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "pipeline-ledger",
  title: "Pipeline Ledger",
  icon: "BarChart3",
  order: 55,
  route: "/pipeline-ledger",
  section: "tools",
  page: () => import("./Page"),
  commands: [
    {
      id: "open-pipeline-ledger",
      title: "Open Pipeline Ledger & Scores",
      run: async () => {
        window.history.pushState({}, "", "/pipeline-ledger");
        window.dispatchEvent(new PopStateEvent("popstate"));
      },
    },
  ],
};
