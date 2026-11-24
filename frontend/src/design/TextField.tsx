import { forwardRef, useId, type InputHTMLAttributes } from "react";
import { Icon } from "./Icon";

export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  label: string;
  helperText?: string;
  error?: string;
  icon?: string;
  loading?: boolean;
  mono?: boolean;
  containerClassName?: string;
}

export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField(
  {
    id: providedId,
    label,
    helperText,
    error,
    icon,
    loading = false,
    mono = false,
    disabled,
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

  return (
    <div className={`flex flex-col gap-1 ${containerClassName}`}>
      <label htmlFor={id} className="text-sm font-medium text-text-primary">
        {label}
      </label>

      <div className="relative flex items-center">
        {icon && (
          <Icon
            name={icon}
            size={16}
            className="pointer-events-none absolute left-3 text-text-muted"
          />
        )}

        <input
          ref={ref}
          id={id}
          disabled={isDisabled}
          aria-invalid={error ? true : undefined}
          aria-busy={loading || undefined}
          aria-describedby={describedBy}
          className={`h-btn-md w-full rounded-md border bg-surface-raised text-sm text-text-primary placeholder:text-text-muted transition-colors duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${
            icon ? "pl-10" : "pl-3"
          } ${loading ? "pr-10" : "pr-3"} ${
            error
              ? "border-status-danger"
              : "border-border-subtle hover:border-border-strong focus:border-border-glow"
          } ${mono ? "font-mono ns-tabular" : ""} ${className}`}
          {...rest}
        />

        {loading && (
          <Icon
            name="Loader2"
            size={16}
            className="pointer-events-none absolute right-3 text-text-muted motion-safe:animate-spin"
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
