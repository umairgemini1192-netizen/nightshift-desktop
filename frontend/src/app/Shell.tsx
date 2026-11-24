import { lazy, Suspense, useEffect, useMemo } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { motion, MotionConfig } from "framer-motion";
import { AuroraCanvas } from "./AuroraCanvas";
import { CommandPalette } from "./CommandPalette";
import { MODULES } from "./registry";
import { Sidebar } from "./Sidebar";
import { useAppStore } from "./store";
import { ToastHost } from "./ToastHost";
import { TopBar } from "./TopBar";

export function Shell() {
  const location = useLocation();
  const reinitializeBridge = useAppStore((s) => s.reinitializeBridge);
  const setSidebarCollapsedAuto = useAppStore((s) => s.setSidebarCollapsedAuto);
  const syncSystemTheme = useAppStore((s) => s.syncSystemTheme);

  const lazyPages = useMemo(
    () =>
      MODULES.map((mod) => ({
        manifest: mod,
        Component: lazy(mod.page),
      })),
    [],
  );

  useEffect(() => {
    void reinitializeBridge(location.search);
  }, [location.search, reinitializeBridge]);

  useEffect(() => {
    const onResize = () => {
      setSidebarCollapsedAuto(window.innerWidth < 1100);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [setSidebarCollapsedAuto]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => syncSystemTheme();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [syncSystemTheme]);

  const defaultRoute = MODULES[0]?.route ?? "/";

  return (
    <MotionConfig reducedMotion="user">
      <div className="relative flex h-screen w-full flex-col overflow-hidden text-text-primary">
        <AuroraCanvas />
        <TopBar />

        <div className="flex min-h-0 flex-1 overflow-hidden">
          <Sidebar />

          <main className="min-w-0 flex-1 overflow-y-auto px-6 py-6">
            <div className="mx-auto w-full max-w-content">
              <Suspense
                fallback={
                  <div className="ns-glass rounded-lg p-6 text-sm text-text-secondary">
                    Loading module...
                  </div>
                }
              >
                <motion.div
                  key={location.pathname}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.24 }}
                >
                  <Routes>
                    {lazyPages.map(({ manifest, Component }) => (
                      <Route
                        key={manifest.id}
                        path={manifest.route}
                        element={<Component />}
                      />
                    ))}
                    <Route path="*" element={<Navigate to={defaultRoute} replace />} />
                  </Routes>
                </motion.div>
              </Suspense>
            </div>
          </main>
        </div>

        <CommandPalette />
        <ToastHost />
      </div>
    </MotionConfig>
  );
}
