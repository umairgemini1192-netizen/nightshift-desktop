import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Icon } from "./Icon";

export interface SelectOption<T extends string = string> {
  value: T;
  label: string;
  description?: string;
  disabled?: boolean;
}

export interface SelectProps<T extends string = string> {
  id?: string;
  label: string;
  value: T | "";
  options: readonly SelectOption<T>[];
  onChange: (value: T) => void;
  placeholder?: string;
  helperText?: string;
  error?: string;
  disabled?: boolean;
  loading?: boolean;
  emptyText?: string;
  className?: string;
}

export function Select<T extends string = string>({
  id: providedId,
  label,
  value,
  options,
  onChange,
  placeholder = "Choose an option",
  helperText,
  error,
  disabled = false,
  loading = false,
  emptyText = "No options available",
  className = "",
}: SelectProps<T>) {
  const autoId = useId();
  const id = providedId ?? autoId;
  const labelId = `${id}-label`;
  const listboxId = `${id}-listbox`;
  const helperId = `${id}-helper`;
  const errorId = `${id}-error`;

  const [open, setOpen] = useState(false);
  const selectedIndex = options.findIndex((o) => o.value === value);
  const [highlightedIndex, setHighlightedIndex] = useState<number>(
    selectedIndex >= 0 ? selectedIndex : 0,
  );
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const isDisabled = disabled || loading;
  const selectedOption = options.find((o) => o.value === value);

  const describedBy =
    [error ? errorId : null, helperText ? helperId : null].filter(Boolean).join(" ") || undefined;

  useEffect(() => {
    if (!open) return;
    const handleOutside = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleOutside);
    return () => document.removeEventListener("mousedown", handleOutside);
  }, [open]);

  const findNextEnabled = (start: number, step: 1 | -1): number => {
    if (options.length === 0) return -1;
    let idx = start;
    for (let i = 0; i < options.length; i += 1) {
      idx = (idx + step + options.length) % options.length;
      if (!options[idx]?.disabled) return idx;
    }
    return -1;
  };

  const commitIndex = (idx: number) => {
    const opt = options[idx];
    if (!opt || opt.disabled) return;
    onChange(opt.value);
    setOpen(false);
    triggerRef.current?.focus();
  };

  const openMenu = (initialIdx?: number) => {
    if (isDisabled) return;
    const baseIdx =
      initialIdx !== undefined
        ? initialIdx
        : selectedIndex >= 0
          ? selectedIndex
          : findNextEnabled(-1, 1);
    setHighlightedIndex(baseIdx);
    setOpen(true);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (isDisabled) return;

    if (!open) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openMenu();
      }
      return;
    }

    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      return;
    }

    if (event.key === "ArrowDown") {
      event.preventDefault();
      const next = findNextEnabled(highlightedIndex, 1);
      if (next >= 0) setHighlightedIndex(next);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      const prev = findNextEnabled(highlightedIndex, -1);
      if (prev >= 0) setHighlightedIndex(prev);
    } else if (event.key === "Home") {
      event.preventDefault();
      const first = findNextEnabled(-1, 1);
      if (first >= 0) setHighlightedIndex(first);
    } else if (event.key === "End") {
      event.preventDefault();
      const last = findNextEnabled(options.length, -1);
      if (last >= 0) setHighlightedIndex(last);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      commitIndex(highlightedIndex);
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  };

  const activeDescendant =
    open && highlightedIndex >= 0 && options[highlightedIndex]
      ? `${listboxId}-opt-${highlightedIndex}`
      : undefined;

  return (
    <div ref={containerRef} className={`relative flex flex-col gap-1 ${className}`}>
      <label id={labelId} htmlFor={id} className="text-sm font-medium text-text-primary">
        {label}
      </label>

      <button
        ref={triggerRef}
        id={id}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-labelledby={labelId}
        aria-activedescendant={activeDescendant}
        aria-invalid={error ? true : undefined}
        aria-busy={loading || undefined}
        aria-describedby={describedBy}
        disabled={isDisabled}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={handleKeyDown}
        className={`flex h-btn-md w-full items-center justify-between gap-2 rounded-md border bg-surface-raised px-3 text-left text-sm transition-colors duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${
          error
            ? "border-status-danger"
            : open
              ? "border-border-glow"
              : "border-border-subtle hover:border-border-strong"
        }`}
      >
        <span className={selectedOption ? "truncate text-text-primary" : "truncate text-text-muted"}>
          {selectedOption ? selectedOption.label : placeholder}
        </span>
        {loading ? (
          <Icon name="Loader2" size={16} className="shrink-0 text-text-muted motion-safe:animate-spin" />
        ) : (
          <Icon
            name={open ? "ChevronUp" : "ChevronDown"}
            size={16}
            className="shrink-0 text-text-muted"
          />
        )}
      </button>

      {open && (
        <ul
          id={listboxId}
          role="listbox"
          aria-labelledby={labelId}
          className="ns-glass-strong absolute top-full right-0 left-0 z-30 mt-1 overflow-y-auto rounded-md p-1"
          style={{ maxHeight: "15rem" }}
        >
          {options.length === 0 ? (
            <li className="px-3 py-2 text-sm text-text-muted">{emptyText}</li>
          ) : (
            options.map((opt, idx) => {
              const isSelected = opt.value === value;
              const isHighlighted = idx === highlightedIndex;
              return (
                <li
                  key={opt.value}
                  id={`${listboxId}-opt-${idx}`}
                  role="option"
                  aria-selected={isSelected}
                  aria-disabled={opt.disabled || undefined}
                  onMouseEnter={() => !opt.disabled && setHighlightedIndex(idx)}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    commitIndex(idx);
                  }}
                  className={`flex cursor-pointer items-center justify-between gap-2 rounded-sm px-3 py-2 text-sm transition-colors duration-instant ${
                    opt.disabled
                      ? "cursor-not-allowed text-text-muted opacity-50"
                      : isHighlighted
                        ? "bg-accent-primary-soft text-accent-primary"
                        : "text-text-primary hover:bg-surface-raised"
                  }`}
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-medium">{opt.label}</span>
                    {opt.description && (
                      <span className="block truncate text-xs text-text-secondary">
                        {opt.description}
                      </span>
                    )}
                  </span>
                  {isSelected && <Icon name="Check" size={16} className="shrink-0 text-accent-primary" />}
                </li>
              );
            })
          )}
        </ul>
      )}

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
