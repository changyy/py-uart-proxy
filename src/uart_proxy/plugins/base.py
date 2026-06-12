"""
Plugin base class and the context handed to plugins.

A plugin watches the line-by-line stream and reacts. The canonical example is
grep: when a line matches a pattern, do something (highlight it, count it, or
even send a command back to the device).

Write a plugin by subclassing :class:`Plugin` in a ``.py`` file placed in a
plugin directory. The manager discovers every ``Plugin`` subclass and
instantiates it with a :class:`PluginContext`. Hooks are optional — override
only what you need.

Minimal example::

    from uart_proxy.plugins import Plugin

    class HelloOnReady(Plugin):
        name = "hello-on-ready"

        def on_line(self, direction, line, stamp):
            if direction == "rx" and "READY" in line:
                self.ctx.notice("device is ready!")
                self.ctx.send_text("START")   # write back to the device
"""

from __future__ import annotations

from typing import Any, Callable, Optional


class PluginContext:
    """What a plugin is allowed to do with the session."""

    def __init__(
        self,
        *,
        notice: Callable[[str, Optional[dict]], None],
        send_text: Callable[[str], int],
        send_bytes: Callable[[bytes], int],
        config: Optional[dict[str, Any]] = None,
        writable: bool = True,
    ) -> None:
        self._notice = notice
        self._send_text = send_text
        self._send_bytes = send_bytes
        self.config: dict[str, Any] = config or {}
        self.writable = writable

    def notice(self, text: str, meta: Optional[dict] = None) -> None:
        """Surface a message into the stream (shown in UI, logged, broadcast)."""
        self._notice(text, meta)

    def send_text(self, text: str) -> int:
        """Send a text command back to the device (uses the session EOL)."""
        return self._send_text(text)

    def send_bytes(self, data: bytes) -> int:
        """Send raw bytes back to the device."""
        return self._send_bytes(data)


class Plugin:
    """Base class for all plugins. Override the hooks you care about."""

    #: Unique, human-readable plugin name (override in subclasses).
    name: str = "plugin"

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    def on_start(self) -> None:
        """Called once when the session starts."""

    def on_line(self, direction: str, line: str, stamp) -> None:
        """
        Called for every assembled line.

        Args:
            direction: ``"rx"`` (from device) or ``"tx"`` (sent by operator).
            line: the decoded line text (no trailing newline).
            stamp: a :class:`~uart_proxy.core.timestamp.Stamp` (wall + elapsed).
        """

    def on_stop(self) -> None:
        """Called once when the session stops."""
