import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "mission-control",
  title: "Mission Control",
  icon: "Radar",
  order: 10,
  route: "/",
  section: "core",
  page: () => import("./Page"),
  statusBadge: (snapshot) => snapshot.moduleBadges["mission-control"] ?? null,
  commands: [
    {
      id: "stop-after-phase",
      title: "Stop after current phase",
      run: (bridge) => bridge.call("engine.stopAfterPhase"),
    },
  ],
};
