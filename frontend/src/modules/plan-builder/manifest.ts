import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "plan-builder",
  title: "Plan Builder",
  icon: "ListChecks",
  order: 20,
  route: "/plan-builder",
  section: "core",
  page: () => import("./Page"),
  statusBadge: (snapshot) => snapshot.moduleBadges["plan-builder"] ?? null,
  commands: [
    {
      id: "validate-plan",
      title: "Validate current plan",
      run: (bridge) => bridge.call("plan.validate"),
    },
  ],
};
