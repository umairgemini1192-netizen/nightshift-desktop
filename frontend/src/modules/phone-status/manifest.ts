import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "phone_status",
  title: "Phone Status Bridge",
  icon: "Smartphone",
  order: 80,
  section: "tools",
  settings: () => import("./SettingsCard"),
};
