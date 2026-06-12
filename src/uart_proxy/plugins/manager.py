"""
Plugin discovery, loading, and dispatch.

The manager:

* registers built-in plugins by name (e.g. ``grep``),
* loads user plugins from arbitrary ``.py`` files or directories,
* instantiates each ``Plugin`` subclass with a :class:`PluginContext`,
* subscribes to the bus and dispatches LINE events to every plugin's
  ``on_line`` hook.

Plugin failures are isolated: one misbehaving plugin is logged and skipped,
never crashing the session.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from ..core.events import Event, EventKind
from .base import Plugin, PluginContext
from .builtin.grep import GrepPlugin

if TYPE_CHECKING:  # avoid a circular import; only needed for type hints
    from ..core.session import UartSession

logger = logging.getLogger(__name__)

# Built-in plugins addressable by name on the CLI.
BUILTIN_PLUGINS: dict[str, type[Plugin]] = {
    "grep": GrepPlugin,
}


class PluginManager:
    def __init__(self, session: "UartSession") -> None:
        self.session = session
        self._plugins: list[Plugin] = []
        self._unsubscribe = None

    # ── registration ─────────────────────────────────────────────────────────

    def add_plugin(self, plugin_cls: type[Plugin], config: Optional[dict[str, Any]] = None) -> Plugin:
        ctx = PluginContext(
            notice=self.session.publish_notice,
            send_text=self.session.send_text,
            send_bytes=self.session.write,
            config=config,
            writable=self.session.source.writable,
        )
        plugin = plugin_cls(ctx)
        self._plugins.append(plugin)
        logger.info("Loaded plugin %r", plugin.name)
        return plugin

    def add_builtin(self, name: str, config: Optional[dict[str, Any]] = None) -> Plugin:
        if name not in BUILTIN_PLUGINS:
            raise KeyError(f"unknown built-in plugin {name!r}")
        return self.add_plugin(BUILTIN_PLUGINS[name], config)

    def load_file(self, path: str, config: Optional[dict[str, Any]] = None) -> list[Plugin]:
        """Import a ``.py`` file and instantiate every Plugin subclass in it."""
        path = os.path.abspath(path)
        module_name = "uart_proxy_userplugin_" + os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load plugin file: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        loaded: list[Plugin] = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Plugin) and obj is not Plugin and obj.__module__ == module_name:
                loaded.append(self.add_plugin(obj, config))
        if not loaded:
            logger.warning("No Plugin subclass found in %s", path)
        return loaded

    def load_dir(self, directory: str, config: Optional[dict[str, Any]] = None) -> list[Plugin]:
        """Load every ``*.py`` file in a directory (non-recursive)."""
        loaded: list[Plugin] = []
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".py") and not entry.startswith("_"):
                try:
                    loaded.extend(self.load_file(os.path.join(directory, entry), config))
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to load plugin %s", entry)
        return loaded

    # ── lifecycle / dispatch ───────────────────────────────────────────────────

    def start(self) -> None:
        for plugin in self._plugins:
            try:
                plugin.on_start()
            except Exception:  # noqa: BLE001
                logger.exception("Plugin %r on_start failed", plugin.name)
        self._unsubscribe = self.session.bus.subscribe(self._on_event)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for plugin in self._plugins:
            try:
                plugin.on_stop()
            except Exception:  # noqa: BLE001
                logger.exception("Plugin %r on_stop failed", plugin.name)

    def _on_event(self, event: Event) -> None:
        if event.kind != EventKind.LINE:
            return
        direction = event.direction.value
        for plugin in self._plugins:
            try:
                plugin.on_line(direction, event.text, event.stamp)
            except Exception:  # noqa: BLE001
                logger.exception("Plugin %r on_line failed", plugin.name)

    @property
    def plugins(self) -> list[Plugin]:
        return list(self._plugins)
