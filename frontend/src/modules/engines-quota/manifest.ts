import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "engines-quota",
  title: "AI Engines & Quota",
  icon: "Cpu",
  order: 40,
  route: "/engines",
  section: "core",
  page: () => import("./Page"),
  statusBadge: (snapshot) => snapshot.moduleBadges["engines-quota"] ?? null,
  commands: [
    {
      id: "check-logins",
      title: "Check AI tool logins",
      run: (bridge) => bridge.call("engines.checkLogins"),
    },
  ],
};
