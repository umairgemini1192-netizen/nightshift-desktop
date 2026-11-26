"""Dynamic discovery and RPC dispatch registry for backend modules."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from typing import Any

import nightshift.modules as modules_pkg
from nightshift.modules.base import ModuleBase


class ModuleRegistry:
    """Discovers ``ModuleBase`` subclasses under ``nightshift.modules``."""

    def __init__(self) -> None:
        self._modules: dict[str, ModuleBase] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(self, module: ModuleBase) -> None:
        if not module.id:
            raise ValueError("Module must define a non-empty id")
        self._modules[module.id] = module
        for action, handler in module.api().items():
            self._handlers[f"{module.id}.{action}"] = handler

    def discover(self) -> list[str]:
        """Import sibling modules in ``nightshift.modules`` and register any ``Module`` class."""
        for info in pkgutil.iter_modules(modules_pkg.__path__):
            if info.name in {"base", "registry"} or info.name.startswith("_"):
                continue
            mod = importlib.import_module(f"nightshift.modules.{info.name}")
            cls = getattr(mod, "Module", None)
            if isinstance(cls, type) and issubclass(cls, ModuleBase) and cls is not ModuleBase:
                self.register(cls())
        return self.list_ids()

    def list_ids(self) -> list[str]:
        return sorted(self._modules.keys())

    def resolve(self, method: str) -> Callable[[dict[str, Any]], Any] | None:
        return self._handlers.get(method)

    def attach(
        self,
        *,
        store: Any = None,
        events: Any = None,
        creds: Any = None,
        engine: Any = None,
    ) -> None:
        """Attach core services to all registered modules and subscribe them to events."""
        for module in self._modules.values():
            module.configure(store=store, events=events, creds=creds, engine=engine)
            if events is not None and type(module).on_event is not ModuleBase.on_event:
                events.subscribe("*", module.on_event)

    def stop_all(self) -> None:
        """Stop all registered modules cleanly."""
        for module in self._modules.values():
            try:
                module.stop()
            except Exception:
                pass
