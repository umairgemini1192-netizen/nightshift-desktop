import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "plan-studio",
  title: "Plan Studio",
  icon: "Sparkles",
  order: 15,
  route: "/plan-studio",
  section: "core",
  page: () => import("./Page"),
  statusBadge: (snapshot) => snapshot.moduleBadges["plan-studio"] ?? null,
  commands: [
    {
      id: "new-council-plan",
      title: "Draft plan with AI Planning Council",
      run: (bridge) => bridge.call("council.run"),
    },
  ],
};
