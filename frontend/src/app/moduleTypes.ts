import type { ComponentType } from "react";
import type { AppSnapshot, Bridge, ModuleBadgeState } from "../bridge/types";

export type ModuleSection = "core" | "build" | "tools" | "settings";

export interface ModuleCommand {
  id: string;
  title: string;
  run: (bridge: Bridge) => void | Promise<unknown>;
}

export interface ModuleManifest {
  id: string;
  title: string;
  icon: string;
  order: number;
  route: string;
  section: ModuleSection;
  page: () => Promise<{ default: ComponentType }>;
  statusBadge?: (snapshot: AppSnapshot) => ModuleBadgeState | null;
  settings?: () => Promise<{ default: ComponentType }>;
  commands?: ModuleCommand[];
}
