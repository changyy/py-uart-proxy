"""
Example user plugin: drop this file in any directory and load it with

    uart-proxy connect --port ... --plugin-dir ./plugins

It demonstrates the two most common plugin actions:

1. React to pattern 1 ("READY") by sending a command back to the device.
2. React to pattern 2 ("ERROR"/"FAIL") by surfacing a highlighted notice.

This mirrors the "grep, but with side effects" use case: watch lines, and do
different things for different patterns.
"""

from __future__ import annotations

import re

from uart_proxy.plugins import Plugin


class ExampleAlertPlugin(Plugin):
    name = "example-alert"

    def on_start(self) -> None:
        self._ready = re.compile(r"READY", re.IGNORECASE)
        self._error = re.compile(r"ERROR|FAIL|panic", re.IGNORECASE)
        self._errors = 0
        self.ctx.notice("example-alert plugin loaded")

    def on_line(self, direction: str, line: str, stamp) -> None:
        if direction != "rx":
            return

        # Pattern 1: when the device reports READY, kick it with a command.
        if self._ready.search(line):
            self.ctx.notice(f"device READY at {stamp.elapsed_str()} → sending 'START'")
            if self.ctx.writable:
                self.ctx.send_text("START")

        # Pattern 2: count and highlight errors.
        elif self._error.search(line):
            self._errors += 1
            self.ctx.notice(f"ERROR #{self._errors}: {line}", {"count": self._errors})

    def on_stop(self) -> None:
        if self._errors:
            self.ctx.notice(f"example-alert saw {self._errors} error line(s)")
