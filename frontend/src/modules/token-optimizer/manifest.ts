import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest = {
  id: "token_optimizer",
  title: "Token Optimizer",
  icon: "Zap",
  order: 75,
  section: "tools",
  settings: () => import("./SettingsCard"),
};
