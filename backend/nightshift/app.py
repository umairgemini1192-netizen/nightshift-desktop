"""Desktop application entry point.

Starts the SQLite store, event bus, module registry, background engine thread,
and the frameless pywebview window hosting the built React frontend.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from nightshift.bridge import BridgeAPI, attach_event_pump
from nightshift.engine.state_machine import EngineStateMachine, SingleInstanceLock
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.quota.ledger import QuotaLedger
from nightshift.store import Store

logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """Return the per-user Night-Shift data directory under %LOCALAPPDATA%."""
    import os

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NightShift"
    return Path.home() / ".nightshift"


def create_app_components(
    db_path: Path | None = None,
    engine: Any | None = None,
) -> tuple[Store, EventBus, ModuleRegistry, BridgeAPI, EngineStateMachine]:
    """Initialize and wire core backend services without starting a GUI window."""
    data_dir = default_data_dir()
    resolved_db = db_path or (data_dir / "nightshift.db")
    store = Store(resolved_db)
    store.migrate()
    events = EventBus(store=store)
    registry = ModuleRegistry()
    registry.discover()
    resolved_engine = engine or EngineStateMachine(
        store=store,
        event_bus=events,
        quota_ledger=QuotaLedger(store),
    )
    bridge = BridgeAPI(store=store, events=events, registry=registry, engine=resolved_engine)
    return store, events, registry, bridge, resolved_engine


def start_engine_thread(engine: EngineStateMachine) -> threading.Thread:
    """Start the background engine loop worker thread."""
    def worker() -> None:
        logger.info("Engine background thread running")

    thread = threading.Thread(target=worker, daemon=True, name="NightShiftEngineThread")
    thread.start()
    return thread


def main() -> None:
    """Launch the pywebview desktop window (EdgeChromium / WebView2 on Windows)."""
    import webview  # type: ignore[import-not-found]

    data_dir = default_data_dir()
    lock = SingleInstanceLock("nightshift_app", lock_dir=data_dir / "locks")
    if not lock.acquire():
        logger.error("Another instance of Night-Shift is already running.")
        print("Another instance of Night-Shift is already running.")
        return

    store, events, registry, bridge, engine = create_app_components()
    engine_thread = start_engine_thread(engine)
    dist_index = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
    url = dist_index.as_uri() if dist_index.exists() else "http://localhost:5173"

    logger.info("Starting Night-Shift Desktop window pointing at %s", url)
    try:
        window = webview.create_window(
            title="Night-Shift",
            url=url,
            js_api=bridge,
            frameless=True,
            easy_drag=False,
            width=1440,
            height=900,
            min_size=(1024, 700),
            background_color="#080d1a",
        )
        unsubscribe = attach_event_pump(events, window.evaluate_js)
        webview.start(debug=False)
    finally:
        if "unsubscribe" in locals():
            unsubscribe()
        registry.stop_all()
        store.close()
        lock.release()


if __name__ == "__main__":
    main()
