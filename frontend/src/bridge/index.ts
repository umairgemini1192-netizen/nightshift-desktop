import { parseScenarioParam } from "./fixtures";
import { MockBridge } from "./mockBridge";
import { PyWebviewBridge } from "./pywebviewBridge";
import type { Bridge } from "./types";

export * from "./types";
export * from "./fixtures";
export { MockBridge } from "./mockBridge";
export { PyWebviewBridge } from "./pywebviewBridge";

export function createBridge(search: string = window.location.search): Bridge {
  const params = new URLSearchParams(search);
  const hasScenarioOverride = params.has("scenario");
  if (!hasScenarioOverride && typeof window !== "undefined" && window.pywebview?.api) {
    return new PyWebviewBridge();
  }
  return new MockBridge(parseScenarioParam(search));
}
