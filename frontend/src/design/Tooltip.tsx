import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";

export interface TooltipProps {
  content: ReactNode;
  placement?: "top" | "bottom";
  children: ReactElement;
  className?: string;
}

export function Tooltip({
  content,
  placement = "top",
  children,
  className = "",
}: TooltipProps) {
  const id = useId();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  const trigger = isValidElement<Record<string, unknown>>(children)
    ? cloneElement(children, {
        "aria-describedby": open ? id : undefined,
        onMouseEnter: (e: unknown) => {
          setOpen(true);
          const orig = children.props.onMouseEnter as ((ev: unknown) => void) | undefined;
          orig?.(e);
        },
        onMouseLeave: (e: unknown) => {
          setOpen(false);
          const orig = children.props.onMouseLeave as ((ev: unknown) => void) | undefined;
          orig?.(e);
        },
        onFocus: (e: unknown) => {
          setOpen(true);
          const orig = children.props.onFocus as ((ev: unknown) => void) | undefined;
          orig?.(e);
        },
        onBlur: (e: unknown) => {
          setOpen(false);
          const orig = children.props.onBlur as ((ev: unknown) => void) | undefined;
          orig?.(e);
        },
        onKeyDown: (e: KeyboardEvent) => {
          if (e.key === "Escape") {
            setOpen(false);
          }
          const orig = children.props.onKeyDown as ((ev: KeyboardEvent) => void) | undefined;
          orig?.(e);
        },
      })
    : children;

  return (
    <span className={`relative inline-flex ${className}`}>
      {trigger}
      {open && (
        <span
          id={id}
          role="tooltip"
          className={`ns-glass-strong pointer-events-none absolute left-1/2 z-40 -translate-x-1/2 whitespace-nowrap rounded-sm px-3 py-1 text-xs text-text-primary ${
            placement === "top" ? "bottom-full mb-2" : "top-full mt-2"
          }`}
        >
          {content}
        </span>
      )}
    </span>
  );
}
