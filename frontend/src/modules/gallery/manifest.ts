import type { ModuleManifest } from "../../app/moduleTypes";

export const manifest: ModuleManifest | null = import.meta.env.DEV
  ? {
      id: "gallery",
      title: "Component Gallery",
      icon: "Layers",
      order: 90,
      route: "/gallery",
      section: "tools",
      page: () => import("./Page"),
    }
  : null;
