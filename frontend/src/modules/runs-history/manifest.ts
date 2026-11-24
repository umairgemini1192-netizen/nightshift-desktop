import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "runs-history",
  title: "Runs & History",
  icon: "History",
  order: 30,
  route: "/runs",
  section: "core",
  page: () => import("./Page"),
  statusBadge: (snapshot) => snapshot.moduleBadges["runs-history"] ?? null,
};
