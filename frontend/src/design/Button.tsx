import type { ButtonHTMLAttributes, ReactNode } from "react";
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
    "bg-accent-primary text-text-on-accent border-transparent hover:shadow-glow-accent active:scale-95",
  secondary:
    "ns-glass text-text-primary hover:border-border-strong active:scale-95",
  ghost:
    "bg-transparent text-text-secondary border-transparent hover:bg-accent-primary-soft hover:text-text-primary active:scale-95",
  danger:
    "bg-status-danger text-text-on-accent border-transparent hover:shadow-glow-danger active:scale-95",
};

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: "h-btn-sm px-3 text-sm gap-2",
  md: "h-btn-md px-4 text-sm gap-2",
  lg: "h-btn-lg px-5 text-md gap-3",
};

export function Button({
  variant = "primary",
  size = "md",
  icon,
  loading = false,
  disabled,
  className = "",
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  const isDisabled = disabled || loading;
  return (
    <button
      type={type}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={`inline-flex items-center justify-center rounded-md border font-medium transition-all duration-fast ease-out focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      {loading ? (
        <Icon name="Loader2" size={16} className="animate-spin" />
      ) : icon ? (
        <Icon name={icon} size={16} />
      ) : null}
      {children}
    </button>
  );
}
