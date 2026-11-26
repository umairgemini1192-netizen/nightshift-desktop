import type {
  ModuleManifest,
  ModuleSection,
  RoutedModuleManifest,
  SettingsModuleManifest,
} from "./moduleTypes";

interface ManifestExport {
  manifest: ModuleManifest | null;
}

const rawModules = import.meta.glob<ManifestExport>("../modules/*/manifest.ts", {
  eager: true,
});

export function loadAllModules(): ModuleManifest[] {
  return Object.values(rawModules)
    .map((entry) => entry.manifest)
    .filter((m): m is ModuleManifest => Boolean(m && m.id))
    .sort((a, b) => a.order - b.order);
}

export function loadModules(): RoutedModuleManifest[] {
  return loadAllModules().filter(
    (m): m is RoutedModuleManifest => Boolean(m.route && m.page),
  );
}

export function loadSettingsModules(): SettingsModuleManifest[] {
  return loadAllModules().filter(
    (m): m is SettingsModuleManifest => Boolean(m.settings),
  );
}

export const ALL_MODULES: readonly ModuleManifest[] = loadAllModules();
export const MODULES: readonly RoutedModuleManifest[] = loadModules();
export const SETTINGS_MODULES: readonly SettingsModuleManifest[] = loadSettingsModules();

export function getModulesBySection(
  modules: readonly RoutedModuleManifest[],
  section: ModuleSection,
): RoutedModuleManifest[] {
  return modules.filter((m) => m.section === section);
}
