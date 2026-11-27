"""Desktop application entry point.

Starts the SQLite store, event bus, module registry, background engine thread,
optional system tray icon, and the frameless pywebview window hosting the built
React frontend.

Supports:
- ``--self-test``: boots the engine and verifies startup without opening a window,
  prints ``OK``, and exits with code 0.
- ``--minimized``: launches hidden to the system tray (used by per-user autostart).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from nightshift.bridge import BridgeAPI, attach_event_pump
from nightshift.engine.state_machine import EngineStateMachine, SingleInstanceLock
from nightshift.events import EventBus
from nightshift.modules.registry import ModuleRegistry
from nightshift.quota.ledger import QuotaLedger
from nightshift.store import Store
from nightshift.tray import TrayController

logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """Return the per-user Night-Shift data directory under %LOCALAPPDATA%\\NightShift."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NightShift"
    if sys.platform == "win32":
        return Path.home() / "AppData" / "Local" / "NightShift"
    return Path.home() / ".nightshift"


def resolve_frontend_dist_index() -> Path:
    """Locate the built frontend index.html for both frozen (PyInstaller) and dev runs."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "frontend" / "dist" / "index.html"
        if bundled.exists():
            return bundled
    return Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"


def create_app_components(
    db_path: Path | None = None,
    engine: Any | None = None,
) -> tuple[Store, EventBus, ModuleRegistry, BridgeAPI, EngineStateMachine]:
    """Initialize and wire core backend services without starting a GUI window."""
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
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


def _write_cli_line(text: str) -> None:
    """Write a line to stdout even in a PyInstaller Windows GUI (console=False) binary."""
    if sys.stdout is not None:
        try:
            sys.stdout.write(f"{text}\n")
            sys.stdout.flush()
            return
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS
            with open("CONOUT$", "w", encoding="utf-8") as con:
                con.write(f"{text}\n")
                con.flush()
        except Exception:
            pass


def run_self_test() -> int:
    """Boot the engine without opening a window, verify core services, print OK, exit 0."""
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    store: Store | None = None
    registry: ModuleRegistry | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="selftest-", dir=data_dir) as tmp:
            db_path = Path(tmp) / "nightshift-selftest.db"
            store, _events, registry, bridge, engine = create_app_components(db_path=db_path)
            info = bridge.call("app.info")
            if not info.get("ok"):
                raise RuntimeError(f"Bridge app.info failed: {info}")
            status = bridge.call("engine.status")
            if not status.get("ok"):
                raise RuntimeError(f"Bridge engine.status failed: {status}")
            if engine is None:
                raise RuntimeError("Engine did not initialize")
            # When running from a frozen bundle, verify the bundled frontend is present.
            if getattr(sys, "frozen", False):
                dist_index = resolve_frontend_dist_index()
                if not dist_index.exists():
                    raise FileNotFoundError(f"Bundled frontend missing: {dist_index}")
            registry.stop_all()
            store.close()
            store = None
            registry = None
        _write_cli_line("OK")
        return 0
    except Exception as exc:
        logger.exception("Self-test failed")
        _write_cli_line(f"FAIL: {exc}")
        return 1
    finally:
        if registry is not None:
            registry.stop_all()
        if store is not None:
            store.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="NightShift", description="Night-Shift Desktop")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Boot the engine without opening a window, print OK, and exit 0.",
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Launch minimized to the system tray.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Launch the pywebview desktop window (EdgeChromium / WebView2 on Windows)."""
    args = parse_args(argv)
    if args.self_test:
        return run_self_test()

    import webview  # type: ignore[import-not-found]

    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = SingleInstanceLock("nightshift_app", lock_dir=data_dir / "locks")
    if not lock.acquire():
        logger.error("Another instance of Night-Shift is already running.")
        _write_cli_line("Another instance of Night-Shift is already running.")
        return 1

    store, events, registry, bridge, engine = create_app_components()
    _engine_thread = start_engine_thread(engine)
    dist_index = resolve_frontend_dist_index()
    url = dist_index.as_uri() if dist_index.exists() else "http://localhost:5173"

    tray: TrayController | None = None
    unsubscribe = None
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
            hidden=bool(args.minimized),
        )
        bridge.attach_window(window)

        def on_open() -> None:
            try:
                window.show()
                window.restore()
            except Exception:
                logger.debug("Window show/restore failed", exc_info=True)

        def on_stop_after_phase() -> None:
            if getattr(engine, "stop_requested", False):
                bridge.call("engine.undoStop")
            else:
                bridge.call("engine.stopAfterPhase")

        def on_quit() -> None:
            try:
                window.destroy()
            except Exception:
                logger.debug("Window destroy failed", exc_info=True)

        tray = TrayController(
            on_open=on_open,
            on_stop_after_phase=on_stop_after_phase,
            on_quit=on_quit,
            is_stop_armed=lambda: bool(getattr(engine, "stop_requested", False)),
        )
        tray.start()

        unsubscribe = attach_event_pump(events, window.evaluate_js)
        webview.start(debug=False)
    finally:
        if tray is not None:
            tray.stop()
        if unsubscribe is not None:
            unsubscribe()
        registry.stop_all()
        store.close()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
