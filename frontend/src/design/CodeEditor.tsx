import { useId, type KeyboardEvent } from "react";
import { Icon } from "./Icon";

export interface CodeEditorProps {
  id?: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  language?: "json" | "prompt" | "text";
  helperText?: string;
  error?: string;
  disabled?: boolean;
  readOnly?: boolean;
  loading?: boolean;
  rows?: number;
  placeholder?: string;
  className?: string;
}

export function CodeEditor({
  id: providedId,
  label,
  value,
  onChange,
  language = "json",
  helperText,
  error,
  disabled = false,
  readOnly = false,
  loading = false,
  rows = 6,
  placeholder,
  className = "",
}: CodeEditorProps) {
  const autoId = useId();
  const id = providedId ?? autoId;
  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;
  const isDisabled = disabled || loading;

  const lines = value.split("\n");
  const lineCount = Math.max(lines.length, rows);

  const describedBy =
    [error ? errorId : null, helperText ? helperId : null].filter(Boolean).join(" ") || undefined;

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Tab" && !readOnly && !isDisabled) {
      event.preventDefault();
      const target = event.currentTarget;
      const start = target.selectionStart;
      const end = target.selectionEnd;
      const next = `${value.slice(0, start)}  ${value.slice(end)}`;
      onChange(next);
      window.requestAnimationFrame(() => {
        target.selectionStart = start + 2;
        target.selectionEnd = start + 2;
      });
    }
  };

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="inline-flex items-center gap-2 text-sm font-medium text-text-primary">
          <Icon name="Code2" size={16} className="text-accent-primary" />
          <span>{label}</span>
        </label>
        <span className="font-mono text-xs uppercase text-text-muted ns-tabular">
          {language} · {lines.length} {lines.length === 1 ? "line" : "lines"}
        </span>
      </div>

      <div
        className={`relative flex overflow-hidden rounded-md border bg-surface-raised transition-colors duration-fast ease-out ${
          error
            ? "border-status-danger"
            : "border-border-subtle focus-within:border-border-glow hover:border-border-strong"
        } ${isDisabled ? "opacity-50" : ""}`}
      >
        <div
          aria-hidden="true"
          className="select-none border-r border-border-subtle bg-bg-base px-3 py-3 text-right font-mono text-xs leading-6 text-text-muted ns-tabular"
        >
          {Array.from({ length: lineCount }, (_, idx) => (
            <div key={idx}>{idx + 1}</div>
          ))}
        </div>

        <textarea
          id={id}
          rows={lineCount}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={isDisabled}
          readOnly={readOnly}
          spellCheck={false}
          placeholder={placeholder}
          aria-invalid={error ? true : undefined}
          aria-busy={loading || undefined}
          aria-describedby={describedBy}
          className="w-full resize-y bg-transparent px-3 py-3 font-mono text-xs leading-6 text-text-primary placeholder:text-text-muted ns-tabular focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed"
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
}
