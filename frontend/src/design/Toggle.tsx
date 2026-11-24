import { useId, type KeyboardEvent } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Icon } from "./Icon";

export interface ToggleProps {
  id?: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  helperText?: string;
  error?: string;
  disabled?: boolean;
  loading?: boolean;
  className?: string;
}

export function Toggle({
  id: providedId,
  label,
  checked,
  onChange,
  helperText,
  error,
  disabled = false,
  loading = false,
  className = "",
}: ToggleProps) {
  const autoId = useId();
  const id = providedId ?? autoId;
  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;
  const prefersReduced = useReducedMotion();
  const isDisabled = disabled || loading;

  const describedBy =
    [error ? errorId : null, helperText ? helperId : null].filter(Boolean).join(" ") || undefined;

  const toggle = () => {
    if (!isDisabled) {
      onChange(!checked);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      toggle();
    }
  };

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <label
            htmlFor={id}
            className={`block text-sm font-medium ${
              isDisabled ? "cursor-not-allowed text-text-muted" : "cursor-pointer text-text-primary"
            }`}
          >
            {label}
          </label>
          {helperText && (
            <p id={helperId} className="mt-1 text-xs text-text-secondary">
              {helperText}
            </p>
          )}
        </div>

        <button
          id={id}
          type="button"
          role="switch"
          aria-checked={checked}
          aria-busy={loading || undefined}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy}
          disabled={isDisabled}
          onClick={toggle}
          onKeyDown={handleKeyDown}
          className={`relative inline-flex h-6 w-12 shrink-0 items-center rounded-pill border p-1 transition-colors duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${
            checked
              ? "border-border-glow bg-accent-primary-soft justify-end"
              : "border-border-strong bg-surface-raised justify-start"
          } ${error ? "border-status-danger" : ""}`}
        >
          <motion.span
            layout={!prefersReduced}
            transition={prefersReduced ? { duration: 0 } : { duration: 0.16, ease: [0.16, 1, 0.3, 1] }}
            className={`flex h-4 w-4 items-center justify-center rounded-pill ${
              checked ? "bg-accent-primary text-text-on-accent" : "bg-text-muted text-bg-canvas"
            }`}
          >
            {loading && <Icon name="Loader2" size={16} className="motion-safe:animate-spin" />}
          </motion.span>
        </button>
      </div>

      {error && (
        <p id={errorId} role="alert" className="flex items-center gap-1 text-xs text-status-danger">
          <Icon name="AlertCircle" size={16} className="shrink-0" />
          <span>{error}</span>
        </p>
      )}
    </div>
  );
}
