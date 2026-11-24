import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "settings",
  title: "Settings",
  icon: "Settings",
  order: 90,
  route: "/settings",
  section: "settings",
  page: () => import("./Page"),
};
