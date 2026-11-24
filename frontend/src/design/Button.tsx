import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Icon } from "./Icon";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: string;
  loading?: boolean;
  children?: ReactNode;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-accent-primary text-text-on-accent border-transparent hover:shadow-glow-accent motion-safe:active:scale-95",
  secondary:
    "ns-glass text-text-primary hover:border-border-strong motion-safe:active:scale-95",
  ghost:
    "bg-transparent text-text-secondary border-transparent hover:bg-accent-primary-soft hover:text-text-primary motion-safe:active:scale-95",
  danger:
    "bg-status-danger text-text-on-accent border-transparent hover:shadow-glow-danger motion-safe:active:scale-95",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "h-btn-sm px-3 text-sm",
  md: "h-btn-md px-4 text-sm",
  lg: "h-btn-lg px-5 text-md",
};

const INNER_GAP: Record<ButtonSize, string> = {
  sm: "gap-2",
  md: "gap-2",
  lg: "gap-3",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "primary",
    size = "md",
    icon,
    loading = false,
    disabled,
    className = "",
    children,
    type = "button",
    ...rest
  },
  ref,
) {
  const isDisabled = Boolean(disabled || loading);
  return (
    <button
      ref={ref}
      type={type}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={`relative inline-flex items-center justify-center rounded-md border font-medium transition-all duration-fast ease-out focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus-ring disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      <span
        data-testid="button-content"
        aria-hidden={loading || undefined}
        className={`inline-flex items-center justify-center ${INNER_GAP[size]} ${
          loading ? "invisible" : ""
        }`}
      >
        {icon && <Icon name={icon} size={16} />}
        {children}
      </span>
      {loading && (
        <span
          data-testid="button-spinner"
          className="absolute inset-0 flex items-center justify-center"
        >
          <Icon name="Loader2" size={16} className="motion-safe:animate-spin" />
          <span className="sr-only">Loading</span>
        </span>
      )}
    </button>
  );
});
