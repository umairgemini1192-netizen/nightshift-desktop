import { AnimatePresence, motion } from "framer-motion";
import { Icon } from "../design";
import { useAppStore } from "./store";

const TONE_ICON: Record<string, { icon: string; cls: string }> = {
  info: { icon: "Activity", cls: "text-status-info" },
  success: { icon: "CheckCircle2", cls: "text-status-success" },
  warning: { icon: "AlertTriangle", cls: "text-status-warning" },
  danger: { icon: "AlertCircle", cls: "text-status-danger" },
};

export function ToastHost() {
  const toasts = useAppStore((s) => s.toasts);
  const dismiss = useAppStore((s) => s.dismissToast);

  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      className="pointer-events-none fixed right-6 bottom-6 z-40 flex w-full max-w-sm flex-col gap-3"
    >
      <AnimatePresence>
        {toasts.map((toast) => {
          const meta = TONE_ICON[toast.tone] ?? TONE_ICON.info;
          return (
            <motion.div
              key={toast.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              transition={{ duration: 0.24 }}
              className="ns-glass-strong pointer-events-auto flex items-start gap-3 rounded-lg p-4"
            >
              <Icon name={meta.icon} size={20} className={`mt-0.5 shrink-0 ${meta.cls}`} />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-text-primary">{toast.title}</p>
                {toast.description && (
                  <p className="mt-1 text-xs text-text-secondary">{toast.description}</p>
                )}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
                className="rounded-sm p-1 text-text-muted transition-colors duration-fast hover:text-text-primary"
              >
                <Icon name="X" size={16} />
              </button>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
