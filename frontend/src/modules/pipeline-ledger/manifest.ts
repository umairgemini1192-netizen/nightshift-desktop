import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "pipeline-ledger",
  title: "Pipeline & Ledger",
  icon: "BarChart3",
  order: 55,
  route: "/pipeline-ledger",
  section: "tools",
  page: () => import("./Page"),
  commands: [
    {
      id: "refresh-pipeline-ledger",
      title: "Refresh Pipeline Ledger & Scores",
      run: (bridge) => bridge.call("pipeline.getState"),
    },
  ],
};
