import { useId } from "react";
import { Icon } from "./Icon";

export interface CheckboxProps {
  id?: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  helperText?: string;
  error?: string;
  disabled?: boolean;
  className?: string;
}

export function Checkbox({
  id: providedId,
  label,
  checked,
  onChange,
  helperText,
  error,
  disabled = false,
  className = "",
}: CheckboxProps) {
  const autoId = useId();
  const id = providedId ?? autoId;
  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;

  const describedBy =
    [error ? errorId : null, helperText ? helperId : null].filter(Boolean).join(" ") || undefined;

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <label
        htmlFor={id}
        className={`inline-flex items-start gap-3 ${
          disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer"
        }`}
      >
        <span className="relative mt-1 flex h-5 w-5 shrink-0 items-center justify-center">
          <input
            id={id}
            type="checkbox"
            checked={checked}
            disabled={disabled}
            aria-invalid={error ? true : undefined}
            aria-describedby={describedBy}
            onChange={(e) => onChange(e.target.checked)}
            className="peer sr-only"
          />
          <span
            aria-hidden="true"
            className={`flex h-5 w-5 items-center justify-center rounded-sm border transition-colors duration-fast ease-out peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-focus-ring ${
              checked
                ? "border-accent-primary bg-accent-primary text-text-on-accent"
                : "border-border-strong bg-surface-raised text-transparent"
            } ${error ? "border-status-danger" : ""}`}
          >
            {checked && <Icon name="Check" size={16} />}
          </span>
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-text-primary">{label}</span>
          {helperText && (
            <span id={helperId} className="mt-1 block text-xs text-text-secondary">
              {helperText}
            </span>
          )}
        </span>
      </label>

      {error && (
        <p id={errorId} role="alert" className="flex items-center gap-1 text-xs text-status-danger">
          <Icon name="AlertCircle" size={16} className="shrink-0" />
          <span>{error}</span>
        </p>
      )}
    </div>
  );
}
