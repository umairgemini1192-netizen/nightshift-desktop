"""Optional Windows system tray controller backed by pystray.

Provides three plain-English actions:
- Open: restores and focuses the Night-Shift window
- Stop after current phase: arms or cancels the graceful stop at phase boundary
- Quit: shuts down the tray, engine, and window cleanly

Gracefully becomes a no-op if ``pystray`` or ``Pillow`` is unavailable.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_tray_image(icon_path: Path | None = None) -> Any:
    """Load the Night-Shift tray icon as a PIL Image, generating a fallback if needed."""
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]

    candidates: list[Path] = []
    if icon_path is not None:
        candidates.append(icon_path)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "packaging" / "nightshift.ico")
    candidates.append(Path(__file__).resolve().parents[1] / "packaging" / "nightshift.ico")

    for path in candidates:
        if path.exists():
            try:
                return Image.open(path)
            except Exception:
                logger.debug("Could not open icon file %s, using generated fallback", path)

    # Pure-Pillow fallback matching Night-Shift dark theme tokens
    img = Image.new("RGBA", (64, 64), (8, 13, 26, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), outline=(56, 189, 248, 255), width=4)
    draw.ellipse((20, 20, 44, 44), outline=(167, 139, 250, 220), width=3)
    draw.ellipse((28, 28, 36, 36), fill=(241, 245, 249, 255))
    return img


class TrayController:
    """Manages the Night-Shift system tray icon and menu."""

    def __init__(
        self,
        *,
        on_open: Callable[[], None],
        on_stop_after_phase: Callable[[], None],
        on_quit: Callable[[], None],
        is_stop_armed: Callable[[], bool] | None = None,
        icon_path: Path | None = None,
        pystray_module: Any | None = None,
        image_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._on_open = on_open
        self._on_stop_after_phase = on_stop_after_phase
        self._on_quit = on_quit
        self._is_stop_armed = is_stop_armed or (lambda: False)
        self._icon_path = icon_path
        self._pystray = pystray_module
        self._image_factory = image_factory
        self._icon: Any | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def _resolve_pystray(self) -> Any | None:
        if self._pystray is not None:
            return self._pystray
        try:
            import pystray  # type: ignore[import-not-found]

            return pystray
        except Exception:
            logger.info("pystray is not installed; skipping system tray icon.")
            return None

    def _build_menu(self, pystray_mod: Any) -> Any:
        item_cls = pystray_mod.MenuItem
        menu_cls = pystray_mod.Menu
        return menu_cls(
            item_cls("Open", self._handle_open, default=True),
            item_cls(
                "Stop after current phase",
                self._handle_stop_after_phase,
                checked=lambda _item: bool(self._is_stop_armed()),
            ),
            item_cls("Quit", self._handle_quit),
        )

    def _handle_open(self, _icon: Any = None, _item: Any = None) -> None:
        try:
            self._on_open()
        except Exception:
            logger.exception("Tray 'Open' action failed")

    def _handle_stop_after_phase(self, _icon: Any = None, _item: Any = None) -> None:
        try:
            self._on_stop_after_phase()
        except Exception:
            logger.exception("Tray 'Stop after current phase' action failed")

    def _handle_quit(self, _icon: Any = None, _item: Any = None) -> None:
        try:
            self.stop()
        finally:
            try:
                self._on_quit()
            except Exception:
                logger.exception("Tray 'Quit' action failed")

    def start(self) -> bool:
        """Start the tray icon in detached mode. Returns False if unavailable."""
        if self._running:
            return True
        pystray_mod = self._resolve_pystray()
        if pystray_mod is None:
            return False
        try:
            image = self._image_factory() if self._image_factory else _load_tray_image(self._icon_path)
            menu = self._build_menu(pystray_mod)
            icon = pystray_mod.Icon("NightShift", image, "Night-Shift Desktop", menu)
            icon.run_detached()
            self._icon = icon
            self._running = True
            return True
        except Exception:
            logger.exception("Could not start system tray icon; continuing without tray.")
            self._icon = None
            self._running = False
            return False

    def stop(self) -> None:
        """Stop and remove the system tray icon cleanly."""
        icon = self._icon
        self._icon = None
        self._running = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logger.debug("Ignoring tray stop error", exc_info=True)
