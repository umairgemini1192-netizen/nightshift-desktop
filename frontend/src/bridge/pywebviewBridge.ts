import type { Bridge, BridgeEventCallback } from "./types";

interface PyWebviewApi {
  call: (method: string, args?: Record<string, unknown>) => Promise<{ ok: boolean; data?: unknown; error?: string }>;
}

declare global {
  interface Window {
    pywebview?: {
      api?: PyWebviewApi;
    };
  }
}

export class PyWebviewBridge implements Bridge {
  private listeners = new Map<string, Set<BridgeEventCallback>>();

  constructor() {
    window.addEventListener("nightshift:event", this.handleNativeEvent as EventListener);
  }

  private handleNativeEvent = (ev: CustomEvent<{ kind: string; payload: unknown }>): void => {
    const detail = ev.detail;
    if (!detail?.kind) return;
    const bucket = this.listeners.get(detail.kind);
    if (bucket) {
      for (const cb of bucket) cb(detail.payload);
    }
    const wildcard = this.listeners.get("*");
    if (wildcard) {
      for (const cb of wildcard) cb(detail);
    }
  };

  async call<T = unknown>(method: string, args?: Record<string, unknown>): Promise<T> {
    const api = window.pywebview?.api;
    if (!api) {
      throw new Error("pywebview bridge is not available");
    }
    const response = await api.call(method, args ?? {});
    if (!response.ok) {
      throw new Error(response.error ?? `Bridge call failed: ${method}`);
    }
    return response.data as T;
  }

  subscribe(event: string, cb: BridgeEventCallback): () => void {
    let bucket = this.listeners.get(event);
    if (!bucket) {
      bucket = new Set();
      this.listeners.set(event, bucket);
    }
    bucket.add(cb);
    return () => {
      bucket?.delete(cb);
    };
  }
}
