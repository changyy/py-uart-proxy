"""
The grep plugin — the canonical "watch for a pattern" example.

Configured with one or more regular expressions. Whenever an RX line matches,
it emits a notice (surfaced in the UI, written to logs, broadcast to remote
clients) and keeps a per-pattern hit count.

Config keys (passed via ``--plugin-config`` or the CLI ``--grep`` shortcut):

    {
        "patterns": ["ERROR", "panic", "WARN.*timeout"],
        "ignore_case": true,
        "directions": ["rx"]          # which directions to watch; default rx
    }
"""

from __future__ import annotations

import re

from ..base import Plugin


class GrepPlugin(Plugin):
    name = "grep"

    def on_start(self) -> None:
        cfg = self.ctx.config
        patterns = cfg.get("patterns", [])
        if isinstance(patterns, str):
            patterns = [patterns]
        flags = re.IGNORECASE if cfg.get("ignore_case", False) else 0
        self._matchers = [(p, re.compile(p, flags)) for p in patterns]
        self._directions = set(cfg.get("directions", ["rx"]))
        self._counts: dict[str, int] = {p: 0 for p, _ in self._matchers}
        if self._matchers:
            self.ctx.notice(
                "grep watching: " + ", ".join(p for p, _ in self._matchers)
            )

    def on_line(self, direction: str, line: str, stamp) -> None:
        if direction not in self._directions:
            return
        for pattern, regex in self._matchers:
            if regex.search(line):
                self._counts[pattern] += 1
                self.ctx.notice(
                    f"grep[{pattern}] #{self._counts[pattern]}: {line}",
                    {"pattern": pattern, "count": self._counts[pattern]},
                )

    def on_stop(self) -> None:
        if any(self._counts.values()):
            summary = ", ".join(f"{p}={c}" for p, c in self._counts.items())
            self.ctx.notice(f"grep summary: {summary}")
