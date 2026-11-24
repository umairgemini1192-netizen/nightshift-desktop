import { Icon } from "./Icon";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: string;
}

interface SegmentedControlProps<T extends string> {
  label: string;
  value: T;
  options: readonly SegmentedOption<T>[];
  onChange: (value: T) => void;
}

export function SegmentedControl<T extends string>({
  label,
  value,
  options,
  onChange,
}: SegmentedControlProps<T>) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex rounded-md border border-border-subtle bg-surface-raised p-1 gap-1"
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(opt.value)}
            className={`inline-flex items-center gap-2 rounded-sm px-3 py-2 text-sm font-medium transition-colors duration-fast ease-out ${
              active
                ? "bg-accent-primary-soft text-accent-primary"
                : "text-text-secondary hover:text-text-primary"
            }`}
          >
            {opt.icon && <Icon name={opt.icon} size={16} />}
            <span>{opt.label}</span>
          </button>
        );
      })}
    </div>
  );
}
