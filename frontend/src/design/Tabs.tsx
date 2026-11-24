import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Icon } from "./Icon";

export interface TabItem<T extends string = string> {
  id: T;
  label: string;
  icon?: string;
  disabled?: boolean;
  content: ReactNode;
}

export interface TabsProps<T extends string = string> {
  label: string;
  items: readonly TabItem<T>[];
  value: T;
  onChange: (id: T) => void;
  className?: string;
}

export function Tabs<T extends string = string>({
  label,
  items,
  value,
  onChange,
  className = "",
}: TabsProps<T>) {
  const baseId = useId();
  const prefersReduced = useReducedMotion();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const enabledIndices = items
    .map((item, idx) => (!item.disabled ? idx : -1))
    .filter((idx) => idx !== -1);

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>, currentIndex: number) => {
    if (enabledIndices.length === 0) return;
    const pos = enabledIndices.indexOf(currentIndex);
    let nextIndex: number | null = null;

    if (event.key === "ArrowRight") {
      event.preventDefault();
      nextIndex = enabledIndices[(pos + 1) % enabledIndices.length];
    } else if (event.key === "ArrowLeft") {
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
      const next = items[nextIndex];
      if (next) {
        onChange(next.id);
        tabRefs.current[nextIndex]?.focus();
      }
    }
  };

  const activeItem = items.find((i) => i.id === value) ?? items[0];

  return (
    <div className={`flex flex-col gap-4 ${className}`}>
      <div
        role="tablist"
        aria-label={label}
        className="flex flex-wrap gap-1 border-b border-border-subtle pb-2"
      >
        {items.map((item, idx) => {
          const selected = item.id === activeItem?.id;
          const tabId = `${baseId}-tab-${item.id}`;
          const panelId = `${baseId}-panel-${item.id}`;
          return (
            <button
              key={item.id}
              ref={(el) => {
                tabRefs.current[idx] = el;
              }}
              id={tabId}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={panelId}
              disabled={item.disabled}
              tabIndex={selected ? 0 : -1}
              onClick={() => !item.disabled && onChange(item.id)}
              onKeyDown={(e) => handleKeyDown(e, idx)}
              className={`relative inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${
                selected ? "text-accent-primary" : "text-text-secondary hover:text-text-primary"
              }`}
            >
              {selected && (
                <motion.span
                  layoutId={`tabs-${baseId}`}
                  transition={prefersReduced ? { duration: 0 } : { duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                  className="absolute inset-0 rounded-md bg-accent-primary-soft"
                  aria-hidden="true"
                />
              )}
              <span className="relative z-10 inline-flex items-center gap-2">
                {item.icon && <Icon name={item.icon} size={16} />}
                <span>{item.label}</span>
              </span>
            </button>
          );
        })}
      </div>

      {activeItem && (
        <div
          id={`${baseId}-panel-${activeItem.id}`}
          role="tabpanel"
          aria-labelledby={`${baseId}-tab-${activeItem.id}`}
          tabIndex={0}
          className="rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring"
        >
          {activeItem.content}
        </div>
      )}
    </div>
  );
}
