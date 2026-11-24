import { create } from "zustand";
import { createBridge, SCENARIO_FIXTURES, type AppSnapshot, type Bridge } from "../bridge";
import type { ResolvedTheme, ThemeMode } from "../design/tokens";

export interface ToastItem {
  id: string;
  title: string;
  description?: string;
  tone: "info" | "success" | "warning" | "danger";
}

const THEME_STORAGE_KEY = "nightshift.theme";

export function resolveTheme(mode: ThemeMode): ResolvedTheme {
  if (mode === "dark" || mode === "light") return mode;
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  return "dark";
}

export function applyThemeToDocument(resolved: ResolvedTheme): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = resolved;
  }
}

function readInitialThemeMode(): ThemeMode {
  if (typeof window === "undefined") return "system";
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "system" || stored === "dark" || stored === "light") {
      return stored;
    }
  } catch {
    // Ignore storage access errors in restricted environments.
  }
  return "system";
}

interface AppState {
  bridge: Bridge;
  snapshot: AppSnapshot;
  themeMode: ThemeMode;
  resolvedTheme: ResolvedTheme;
  sidebarCollapsed: boolean;
  sidebarManualOverride: boolean;
  commandPaletteOpen: boolean;
  toasts: ToastItem[];
  setThemeMode: (mode: ThemeMode) => void;
  syncSystemTheme: () => void;
  toggleSidebar: () => void;
  setSidebarCollapsedAuto: (collapsed: boolean) => void;
  openCommandPalette: () => void;
  closeCommandPalette: () => void;
  toggleCommandPalette: () => void;
  pushToast: (toast: Omit<ToastItem, "id">) => void;
  dismissToast: (id: string) => void;
  refreshSnapshot: () => Promise<void>;
  selectProject: (projectId: string) => Promise<void>;
  reinitializeBridge: (search?: string) => Promise<void>;
}

const initialThemeMode = readInitialThemeMode();
const initialResolved = resolveTheme(initialThemeMode);
applyThemeToDocument(initialResolved);

export const useAppStore = create<AppState>((set, get) => ({
  bridge: createBridge(typeof window !== "undefined" ? window.location.search : ""),
  snapshot: SCENARIO_FIXTURES.idle,
  themeMode: initialThemeMode,
  resolvedTheme: initialResolved,
  sidebarCollapsed: typeof window !== "undefined" ? window.innerWidth < 1100 : false,
  sidebarManualOverride: false,
  commandPaletteOpen: false,
  toasts: [],

  setThemeMode: (mode) => {
    const resolved = resolveTheme(mode);
    applyThemeToDocument(resolved);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, mode);
    } catch {
      // Ignore storage write errors.
    }
    set({ themeMode: mode, resolvedTheme: resolved });
  },

  syncSystemTheme: () => {
    const { themeMode } = get();
    if (themeMode !== "system") return;
    const resolved = resolveTheme("system");
    applyThemeToDocument(resolved);
    set({ resolvedTheme: resolved });
  },

  toggleSidebar: () => {
    set((state) => ({
      sidebarCollapsed: !state.sidebarCollapsed,
      sidebarManualOverride: true,
    }));
  },

  setSidebarCollapsedAuto: (collapsed) => {
    if (get().sidebarManualOverride) return;
    set({ sidebarCollapsed: collapsed });
  },

  openCommandPalette: () => set({ commandPaletteOpen: true }),
  closeCommandPalette: () => set({ commandPaletteOpen: false }),
  toggleCommandPalette: () => set((s) => ({ commandPaletteOpen: !s.commandPaletteOpen })),

  pushToast: (toast) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    set((state) => ({ toasts: [...state.toasts, { ...toast, id }] }));
    window.setTimeout(() => {
      get().dismissToast(id);
    }, 5000);
  },

  dismissToast: (id) => {
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
  },

  refreshSnapshot: async () => {
    const snapshot = await get().bridge.call<AppSnapshot>("app.snapshot");
    set({ snapshot });
  },

  selectProject: async (projectId) => {
    const snapshot = await get().bridge.call<AppSnapshot>("app.selectProject", { projectId });
    set({ snapshot });
  },

  reinitializeBridge: async (search) => {
    const bridge = createBridge(search ?? window.location.search);
    set({ bridge });
    const snapshot = await bridge.call<AppSnapshot>("app.snapshot");
    set({ snapshot });
  },
}));
