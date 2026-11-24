import type { ModuleManifest, ModuleSection } from "./moduleTypes";

interface ManifestExport {
  manifest: ModuleManifest | null;
}

const rawModules = import.meta.glob<ManifestExport>("../modules/*/manifest.ts", {
  eager: true,
});

export function loadModules(): ModuleManifest[] {
  return Object.values(rawModules)
    .map((entry) => entry.manifest)
    .filter((m): m is ModuleManifest => Boolean(m && m.id && m.route))
    .sort((a, b) => a.order - b.order);
}

export const MODULES: readonly ModuleManifest[] = loadModules();

export function getModulesBySection(
  modules: readonly ModuleManifest[],
  section: ModuleSection,
): ModuleManifest[] {
  return modules.filter((m) => m.section === section);
}
