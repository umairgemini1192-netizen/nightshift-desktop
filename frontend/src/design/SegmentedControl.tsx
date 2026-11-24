import { useId, useRef, type KeyboardEvent } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Icon } from "./Icon";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: string;
  disabled?: boolean;
}

export interface SegmentedControlProps<T extends string> {
  label: string;
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
}

export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled = false,
  className = "",
}: SegmentedControlProps<T>) {
  const groupId = useId();
  const prefersReduced = useReducedMotion();
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const enabledIndices = options
    .map((opt, idx) => (!disabled && !opt.disabled ? idx : -1))
    .filter((idx) => idx !== -1);

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    if (disabled || enabledIndices.length === 0) return;

    let nextIndex: number | null = null;
    const pos = enabledIndices.indexOf(currentIndex);

    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      nextIndex = enabledIndices[(pos + 1) % enabledIndices.length];
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      nextIndex = enabledIndices[(pos - 1 + enabledIndices.length) % enabledIndices.length];
    } else if (event.key === "Home") {
      event.preventDefault();
      nextIndex = enabledIndices[0];
    } else if (event.key === "End") {
      event.preventDefault();
      nextIndex = enabledIndices[enabledIndices.length - 1];
    }

    if (nextIndex !== null) {
      const nextOption = options[nextIndex];
      if (nextOption) {
        onChange(nextOption.value);
        buttonRefs.current[nextIndex]?.focus();
      }
    }
  };

  return (
    <div
      role="radiogroup"
      aria-label={label}
      aria-disabled={disabled || undefined}
      className={`inline-flex gap-1 rounded-md border border-border-subtle bg-surface-raised p-1 ${
        disabled ? "opacity-50" : ""
      } ${className}`}
    >
      {options.map((opt, idx) => {
        const active = opt.value === value;
        const optDisabled = disabled || Boolean(opt.disabled);
        return (
          <button
            key={opt.value}
            ref={(el) => {
              buttonRefs.current[idx] = el;
            }}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={optDisabled}
            tabIndex={active ? 0 : -1}
            onClick={() => !optDisabled && onChange(opt.value)}
            onKeyDown={(e) => handleKeyDown(e, idx)}
            className={`relative inline-flex items-center gap-2 rounded-sm px-3 py-2 text-sm font-medium transition-colors duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${
              active ? "text-accent-primary" : "text-text-secondary hover:text-text-primary"
            }`}
          >
            {active && (
              <motion.span
                layoutId={`segmented-${groupId}`}
                transition={prefersReduced ? { duration: 0 } : { duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                className="absolute inset-0 rounded-sm bg-accent-primary-soft"
                aria-hidden="true"
              />
            )}
            <span className="relative z-10 inline-flex items-center gap-2">
              {opt.icon && <Icon name={opt.icon} size={16} />}
              <span>{opt.label}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
