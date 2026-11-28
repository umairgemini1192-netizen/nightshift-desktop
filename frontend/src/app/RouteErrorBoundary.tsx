import { Component, type ErrorInfo, type ReactNode } from "react";
import { EmptyState, GlassCard } from "../design";

interface RouteErrorBoundaryProps {
  routeKey: string;
  routeTitle: string;
  onError?: (error: Error, info: ErrorInfo) => void;
  children: ReactNode;
}

interface RouteErrorBoundaryState {
  error: Error | null;
}

export class RouteErrorBoundary extends Component<
  RouteErrorBoundaryProps,
  RouteErrorBoundaryState
> {
  state: RouteErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.props.onError?.(error, info);
  }

  componentDidUpdate(prevProps: RouteErrorBoundaryProps): void {
    if (prevProps.routeKey !== this.props.routeKey && this.state.error !== null) {
      this.setState({ error: null });
    }
  }

  private handleReload = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <GlassCard
          title={`${this.props.routeTitle} encountered an error`}
          icon="AlertTriangle"
          variant="raised"
        >
          <EmptyState
            icon="AlertTriangle"
            title={`Unable to render ${this.props.routeTitle}`}
            description={this.state.error.message || "An unexpected error occurred on this page."}
            actionLabel="Reload"
            onAction={this.handleReload}
          />
        </GlassCard>
      );
    }
    return this.props.children;
  }
}
