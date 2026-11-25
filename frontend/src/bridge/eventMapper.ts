export interface NativeEventPayload {
  kind: string;
  plain_text?: string;
  level?: "info" | "warn" | "error";
  phase_id?: string;
  payload?: Record<string, unknown>;
  ts?: string;
}

export interface ToastNotification {
  title: string;
  description: string;
  tone: "info" | "success" | "warning" | "danger";
}

/**
 * Filter determining whether an engine event triggers a desktop toast notification.
 *
 * Per BLUEPRINT 5e and prompt mandate:
 * Windows toast notifications ONLY for:
 * 1. run finished
 * 2. phase parked
 * 3. circuit breaker tripped
 * 4. power or network loss exceeding 10 minutes (> 600 seconds)
 *
 * Never for routine progress (phase starts, step ticks, CI passing, runner invocations).
 */
export function isNotifiableEvent(event: NativeEventPayload): boolean {
  if (!event || typeof event.kind !== "string") {
    return false;
  }

  const kind = event.kind.toLowerCase();

  if (kind === "run.finished" || kind === "run.completed") {
    return true;
  }

  if (kind === "phase.parked") {
    return true;
  }

  if (kind === "circuit_breaker.tripped" || kind === "circuit-breaker.tripped") {
    return true;
  }

  if (
    kind === "network.lost" ||
    kind === "network_lost" ||
    kind === "power_network_lost" ||
    kind === "power.lost"
  ) {
    const rawSeconds =
      event.payload?.duration_seconds ??
      event.payload?.seconds ??
      (typeof event.payload?.minutes === "number" ? event.payload.minutes * 60 : undefined);

    if (typeof rawSeconds === "number") {
      return rawSeconds >= 600;
    }
    // If explicitly marked as extended loss in payload
    if (event.payload?.extended === true || event.payload?.over_10_min === true) {
      return true;
    }
    return false;
  }

  return false;
}

/**
 * Map an allowed engine event to a user-facing toast notification in plain English.
 */
export function mapEventToNotification(event: NativeEventPayload): ToastNotification | null {
  if (!isNotifiableEvent(event)) {
    return null;
  }

  const kind = event.kind.toLowerCase();

  if (kind === "run.finished" || kind === "run.completed") {
    return {
      title: "Overnight run finished",
      description: event.plain_text || "All planned phases completed cleanly.",
      tone: "success",
    };
  }

  if (kind === "phase.parked") {
    return {
      title: "Phase parked",
      description:
        event.plain_text ||
        "A phase was parked after checks failed twice. Independent phases continue.",
      tone: "danger",
    };
  }

  if (kind === "circuit_breaker.tripped" || kind === "circuit-breaker.tripped") {
    return {
      title: "Circuit breaker tripped",
      description:
        event.plain_text ||
        "Three consecutive phases were parked. Halting new phase starts.",
      tone: "warning",
    };
  }

  if (
    kind === "network.lost" ||
    kind === "network_lost" ||
    kind === "power_network_lost" ||
    kind === "power.lost"
  ) {
    return {
      title: "Connection pause",
      description:
        event.plain_text ||
        "Power or network lost for more than 10 minutes. Run paused until connection returns.",
      tone: "danger",
    };
  }

  return null;
}

/**
 * Format any engine event to a clear, plain-English sentence without raw technical errors.
 */
export function formatEventPlainText(event: NativeEventPayload): string {
  if (event.plain_text && event.plain_text.trim().length > 0) {
    return event.plain_text.trim();
  }

  const kind = event.kind ? event.kind.toLowerCase() : "unknown";
  const p = event.payload || {};

  switch (kind) {
    case "run.started":
      return "Night-Shift overnight run started.";
    case "run.finished":
    case "run.completed":
      return "Overnight run finished cleanly.";
    case "phase.started":
      return p.title ? `Started ${p.title}.` : "Phase started.";
    case "phase.merged":
      return p.title ? `${p.title} merged cleanly to main.` : "Phase merged to main.";
    case "phase.parked":
      return p.reason ? `Phase parked: ${p.reason}.` : "Phase parked for review.";
    case "runner.started":
      return p.runner ? `${p.runner} started working.` : "AI runner started working.";
    case "handoff.started":
    case "handoff":
      return `${p.from_engine || "Previous AI"} hit limit; ${p.to_engine || "Next AI"} continuing from checkpoint.`;
    case "switchback":
    case "switchback.armed":
      return `${p.engine || "Preferred AI"} returned from cooldown and resumed control.`;
    case "ci.failed":
      return "Continuous integration checks failed; starting automatic fix round.";
    case "ci.fixed":
      return "Continuous integration fixer pushed a new commit to resolve test failures.";
    case "ci.passed":
      return "Continuous integration checks passed.";
    case "circuit_breaker.tripped":
      return "Circuit breaker tripped after 3 consecutive parked phases.";
    case "network.lost":
    case "power_network_lost":
      return "Network connection lost for over 10 minutes. Pausing run until connection returns.";
    default:
      return kind.replace(/[._]/g, " ");
  }
}
