"""Desktop application entry point.

Starts the SQLite store, event bus, module registry, background engine thread,
and the frameless pywebview window hosting the built React frontend.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nightshift.bridge import BridgeAPI
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.store import Store

logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """Return the per-user Night-Shift data directory under %LOCALAPPDATA%."""
    import os

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NightShift"
    return Path.home() / ".nightshift"


def create_app_components(db_path: Path | None = None) -> tuple[Store, EventBus, ModuleRegistry, BridgeAPI]:
    """Initialize and wire core backend services without starting a GUI window."""
    data_dir = default_data_dir()
    resolved_db = db_path or (data_dir / "nightshift.db")
    store = Store(resolved_db)
    store.migrate()
    events = EventBus(store=store)
    registry = ModuleRegistry()
    registry.discover()
    bridge = BridgeAPI(store=store, events=events, registry=registry)
    return store, events, registry, bridge


def main() -> None:
    """Launch the pywebview desktop window (EdgeChromium / WebView2 on Windows)."""
    import webview  # type: ignore[import-not-found]

    store, _events, _registry, bridge = create_app_components()
    dist_index = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
    url = dist_index.as_uri() if dist_index.exists() else "http://localhost:5173"

    logger.info("Starting Night-Shift Desktop window pointing at %s", url)
    try:
        webview.create_window(
            title="Night-Shift",
            url=url,
            js_api=bridge,
            frameless=True,
            easy_drag=False,
            width=1440,
            height=900,
            min_size=(1024, 700),
        )
        webview.start(debug=False)
    finally:
        store.close()


if __name__ == "__main__":
    main()
