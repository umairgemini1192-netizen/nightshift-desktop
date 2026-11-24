import { forwardRef, useId, type TextareaHTMLAttributes } from "react";
import { Icon } from "./Icon";

export interface TextAreaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label: string;
  helperText?: string;
  error?: string;
  loading?: boolean;
  showCount?: boolean;
  containerClassName?: string;
}

export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea(
  {
    id: providedId,
    label,
    helperText,
    error,
    loading = false,
    showCount = false,
    disabled,
    maxLength,
    value,
    rows = 4,
    containerClassName = "",
    className = "",
    ...rest
  },
  ref,
) {
  const autoId = useId();
  const id = providedId ?? autoId;
  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;
  const isDisabled = Boolean(disabled || loading);

  const describedBy =
    [error ? errorId : null, helperText ? helperId : null].filter(Boolean).join(" ") || undefined;

  const length = typeof value === "string" ? value.length : 0;

  return (
    <div className={`flex flex-col gap-1 ${containerClassName}`}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-sm font-medium text-text-primary">
          {label}
        </label>
        {showCount && (
          <span className="font-mono text-xs text-text-muted ns-tabular">
            {maxLength ? `${length}/${maxLength}` : `${length}`}
          </span>
        )}
      </div>

      <div className="relative">
        <textarea
          ref={ref}
          id={id}
          rows={rows}
          value={value}
          maxLength={maxLength}
          disabled={isDisabled}
          aria-invalid={error ? true : undefined}
          aria-busy={loading || undefined}
          aria-describedby={describedBy}
          className={`w-full rounded-md border bg-surface-raised px-3 py-2 text-sm text-text-primary placeholder:text-text-muted transition-colors duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${
            error
              ? "border-status-danger"
              : "border-border-subtle hover:border-border-strong focus:border-border-glow"
          } ${className}`}
          {...rest}
        />
        {loading && (
          <Icon
            name="Loader2"
            size={16}
            className="pointer-events-none absolute top-3 right-3 text-text-muted motion-safe:animate-spin"
          />
        )}
      </div>

      {helperText && !error && (
        <p id={helperId} className="text-xs text-text-secondary">
          {helperText}
        </p>
      )}

      {error && (
        <p id={errorId} role="alert" className="flex items-center gap-1 text-xs text-status-danger">
          <Icon name="AlertCircle" size={16} className="shrink-0" />
          <span>{error}</span>
        </p>
      )}
    </div>
  );
});
