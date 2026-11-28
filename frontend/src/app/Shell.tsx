import { lazy, Suspense, useEffect, useMemo } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { motion, MotionConfig, useReducedMotion } from "framer-motion";
import { isNotifiableEvent, mapEventToNotification, type NativeEventPayload } from "../bridge";
import { GlassCard, Skeleton } from "../design";
import { AuroraCanvas } from "./AuroraCanvas";
import { CommandPalette } from "./CommandPalette";
import { MODULES } from "./registry";
import { Sidebar } from "./Sidebar";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { useAppStore } from "./store";
import { ToastHost } from "./ToastHost";
import { TopBar } from "./TopBar";

export function Shell() {
  const location = useLocation();
  const prefersReduced = useReducedMotion();
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);
  const refreshSnapshot = useAppStore((s) => s.refreshSnapshot);
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
    const unsub = bridge.subscribe("*", (payload: unknown) => {
      const evt = payload as NativeEventPayload;
      if (evt && isNotifiableEvent(evt)) {
        const toast = mapEventToNotification(evt);
        if (toast) {
          pushToast(toast);
        }
      }
      void refreshSnapshot();
    });
    return unsub;
  }, [bridge, pushToast, refreshSnapshot]);

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
                  <GlassCard>
                    <Skeleton lines={3} />
                  </GlassCard>
                }
              >
                <motion.div
                  key={location.pathname}
                  initial={prefersReduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
                  animate={prefersReduced ? { opacity: 1 } : { opacity: 1, y: 0 }}
                  transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
                >
                  <Routes>
                    {lazyPages.map(({ manifest, Component }) => (
                      <Route
                        key={manifest.id}
                        path={manifest.route}
                        element={
                          <RouteErrorBoundary
                            key={manifest.id}
                            routeKey={location.pathname}
                            routeTitle={manifest.title}
                            onError={(error, info) => {
                              void bridge
                                .call("app.logError", {
                                  route: manifest.id,
                                  title: manifest.title,
                                  message: error.message,
                                  stack: error.stack ?? info.componentStack ?? "",
                                })
                                .catch(() => {});
                            }}
                          >
                            <Component />
                          </RouteErrorBoundary>
                        }
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
