"""
The Textual TUI — a PuTTY/Minicom-style interactive terminal.

Layout (top to bottom):

    Header
    Status bar      source · role · live elapsed clock · RX/TX byte counts
    RichLog         scrolling, line-by-line view with optional timestamps
    Input           type here, Enter sends (with the configured line ending)
    Footer          key bindings

Key bindings:
    ctrl+t   cycle timestamp display (none → relative → full)
    ctrl+y   toggle hex view
    ctrl+k   clear the log
    ctrl+q   quit

The session runs its read loop on a background thread; bus events are marshalled
onto the Textual event loop with ``call_from_thread``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections import deque
from typing import Optional

from .. import __version__
from ..core.events import Direction, Event, EventKind
from ..core.session import UartSession
from ..core.timestamp import format_elapsed

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import Footer, Header, Input, RichLog, Static

    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without textual
    _TEXTUAL_AVAILABLE = False
    App = object  # type: ignore


_TS_MODES = ["none", "relative", "full"]


def _native_clipboard_cmd() -> Optional[list[str]]:
    """The OS-native 'copy to clipboard' command, or None if unavailable.

    Textual's copy_to_clipboard uses OSC-52, which macOS Terminal.app does not
    support (and iTerm2 gates behind a setting). Shelling out to the platform
    tool is reliable.
    """
    if sys.platform == "darwin" and shutil.which("pbcopy"):
        return ["pbcopy"]
    if sys.platform.startswith("win"):
        return ["clip"]
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


if _TEXTUAL_AVAILABLE:

    class _StatusBar(Static):
        pass

    class FollowLog(RichLog):
        """
        A RichLog that follows the tail but pauses when the user scrolls up.

        The base mouse-wheel scrolling lives in Widget._on_mouse_scroll_* (a
        private handler), so defining the public on_mouse_scroll_* here is
        additive — it does not disable normal wheel scrolling.
        """

        def on_mouse_scroll_up(self, event) -> None:
            # User is reading history -> stop yanking back to the bottom.
            self.auto_scroll = False

        def on_mouse_scroll_down(self, event) -> None:
            # If they have scrolled back to the bottom, resume following.
            self.call_after_refresh(self._resume_if_at_bottom)

        def _resume_if_at_bottom(self) -> None:
            if self.is_vertical_scroll_end:
                self.auto_scroll = True

        def jump_to_bottom(self) -> None:
            self.auto_scroll = True
            self.scroll_end(animate=False)

        @property
        def following(self) -> bool:
            return self.auto_scroll

    class UartProxyApp(App):
        CSS = """
        Screen { layout: vertical; }
        _StatusBar {
            height: 1;
            background: $boost;
            color: $text;
            padding: 0 1;
        }
        /* No border: a box border's │ characters get picked up by the
           terminal's own selection when copying. The status bar above already
           separates the log visually. */
        #log { height: 1fr; }
        #cmd { dock: bottom; }
        """

        # priority=True so these app controls win even while the Input box has
        # focus (otherwise the Input's emacs-style keys, e.g. Ctrl+E = end of
        # line, would swallow them).
        BINDINGS = [
            Binding("ctrl+e", "toggle_select", "Select", priority=True),
            Binding("ctrl+t", "cycle_ts", "Timestamp", priority=True),
            Binding("ctrl+y", "toggle_hex", "Hex", priority=True),
            Binding("ctrl+k", "clear_log", "Clear", priority=True),
            Binding("ctrl+w", "copy_all", "Copy log", priority=True),
            ("end", "follow_bottom", "Follow"),
            ("ctrl+q", "quit", "Quit"),
        ]

        def __init__(
            self,
            session: UartSession,
            *,
            title: str = "uart-proxy",
            ts_mode: str = "relative",
            log_hint: Optional[str] = None,
        ) -> None:
            super().__init__()
            self.session = session
            self._title = title
            self._log_hint = log_hint
            self._ts_index = _TS_MODES.index(ts_mode) if ts_mode in _TS_MODES else 1
            self._hex = False
            self._select_mode = False
            self._log: Optional[FollowLog] = None
            self._status: Optional[_StatusBar] = None
            self._unsubscribe = None
            # Events from any thread land here; the UI drains them on a timer.
            self._pending: "deque[Event]" = deque()
            # Plain-text mirror of what's shown, for clean clipboard copy
            # (no border, no padding, no markup).
            self._copy_lines: "deque[str]" = deque(maxlen=5000)

        # ── composition ─────────────────────────────────────────────────────

        def compose(self) -> "ComposeResult":
            yield Header(show_clock=True)
            self._status = _StatusBar(id="status")
            yield self._status
            with Vertical():
                yield FollowLog(id="log", highlight=False, markup=True, wrap=True, auto_scroll=True)
            placeholder = (
                "Type and press Enter to send…"
                if self.session.source.writable
                else "read-only — input disabled"
            )
            inp = Input(placeholder=placeholder, id="cmd")
            inp.disabled = not self.session.source.writable
            yield inp
            yield Footer()

        def on_mount(self) -> None:
            self.title = self._title
            self.sub_title = f"v{__version__}"
            self._log = self.query_one("#log", FollowLog)
            # Subscribe BEFORE start() so the "connected" status is captured.
            self._unsubscribe = self.session.bus.subscribe(self._enqueue_event)
            # Drain the cross-thread queue and refresh the status on timers.
            self.set_interval(0.05, self._drain_events)
            self.set_interval(0.25, self._refresh_status)
            # Make sure typing goes to the input box, not the scroll log.
            if self.session.source.writable:
                self.query_one("#cmd", Input).focus()
            else:
                self._log.focus()
            try:
                self.session.start()
            except Exception as exc:  # noqa: BLE001
                self._log.write(f"[red]Failed to open source: {exc}[/red]")
            self._refresh_status()

        def on_unmount(self) -> None:
            if self._unsubscribe is not None:
                self._unsubscribe()
            self.session.stop()

        # ── event rendering ──────────────────────────────────────────────────

        def _enqueue_event(self, event: Event) -> None:
            # Called from ANY thread (read loop, UI thread, plugin). Just queue;
            # the timer drains it on the UI thread. deque.append is atomic.
            self._pending.append(event)

        def _drain_events(self) -> None:
            while self._pending:
                try:
                    event = self._pending.popleft()
                except IndexError:
                    break
                self._render_event(event)

        def _render_event(self, event: Event) -> None:
            if self._log is None:
                return
            if event.kind == EventKind.LINE:
                markup, plain = self._format_line(event)
                self._log.write(markup)
                self._copy_lines.append(plain)
            elif event.kind == EventKind.NOTICE:
                self._log.write(f"[yellow]* {self._escape(event.text)}[/yellow]")
                self._copy_lines.append(f"* {event.text}")
            elif event.kind == EventKind.STATUS:
                meta = f" {event.meta}" if event.meta else ""
                self._log.write(
                    f"[cyan]# {self._escape(event.text)}{self._escape(meta)}[/cyan]"
                )
                self._copy_lines.append(f"# {event.text}{meta}")

        def _format_line(self, event: Event) -> tuple[str, str]:
            """Return (markup_for_display, plain_for_clipboard)."""
            prefix = self._prefix_plain(event)
            arrow = "<" if event.direction == Direction.RX else ">"
            body = event.data.hex(" ") if self._hex else event.text
            plain = f"{prefix}{arrow} {body}"
            arrow_m = "[green]<[/green]" if event.direction == Direction.RX else "[blue]>[/blue]"
            prefix_m = f"[dim]{self._escape(prefix)}[/dim]" if prefix else ""
            markup = f"{prefix_m}{arrow_m} {self._escape(body)}"
            return markup, plain

        def _prefix_plain(self, event: Event) -> str:
            mode = _TS_MODES[self._ts_index]
            if mode == "relative":
                return f"{event.stamp.elapsed_str()} "
            if mode == "full":
                return f"{event.stamp.wall_str()} | {event.stamp.elapsed_str()} "
            return ""

        @staticmethod
        def _escape(text: str) -> str:
            # RichLog markup is on; escape Rich's markup brackets.
            return text.replace("[", "\\[")

        # ── status bar ─────────────────────────────────────────────────────────

        def _refresh_status(self) -> None:
            if self._status is None:
                return
            if self._select_mode:
                # A loud banner so it's obvious scrolling is off and why.
                self._status.update(
                    "[black on yellow] SELECT MODE [/] drag to select · "
                    "copy with your terminal (⌘/Ctrl+C) · Ctrl+E to exit"
                )
                return
            stamp = self.session.tracker.stamp()
            role = ""
            if not self.session.source.writable:
                role = " · [red]READ-ONLY[/red]"
            if self._log is not None and self._log.following:
                follow = "[green]follow[/green]"
            else:
                follow = "[yellow]paused ▲[/yellow]"
            rec = f" · rec→{self._escape(self._log_hint)}" if self._log_hint else " · rec off"
            if self.session.is_connected:
                state = "[green]● live[/green]"
            elif self.session.is_running:
                state = "[yellow]○ waiting[/yellow]"
            else:
                state = "[red]○ stopped[/red]"
            self._status.update(
                f"{state} {self._escape(self.session.source.description())}{role}"
                f" · elapsed {format_elapsed(stamp.elapsed)}"
                f" · rx {self.session.rx_bytes}B tx {self.session.tx_bytes}B"
                f" · ts={_TS_MODES[self._ts_index]} hex={'on' if self._hex else 'off'}"
                f" · {follow}{rec}"
            )

        # ── actions ─────────────────────────────────────────────────────────────

        def action_cycle_ts(self) -> None:
            self._ts_index = (self._ts_index + 1) % len(_TS_MODES)
            self._refresh_status()

        def action_toggle_hex(self) -> None:
            self._hex = not self._hex
            self._refresh_status()

        def action_clear_log(self) -> None:
            # Clear both the visible log AND the copy buffer, so the range that
            # Ctrl+W copies is reset too (clear → accumulate → Ctrl+W copies
            # just the new range).
            if self._log is not None:
                self._log.clear()
            self._copy_lines.clear()
            self.notify("Cleared (display + copy range).", timeout=2)

        def action_follow_bottom(self) -> None:
            if self._log is not None:
                self._log.jump_to_bottom()
            self._refresh_status()

        def action_copy_all(self) -> None:
            """Copy the whole in-memory log to the clipboard as clean text
            (no border, no padding, no markup)."""
            text = "\n".join(self._copy_lines)
            if not text:
                self.notify("Nothing to copy yet.", timeout=2)
                return
            via = self._copy_text(text)
            self.notify(
                f"Copied {len(self._copy_lines)} lines to the clipboard ({via}).",
                timeout=3,
            )

        def _copy_text(self, text: str) -> str:
            """Put text on the clipboard. Returns which mechanism was used.

            Prefers the OS-native tool (reliable on macOS Terminal.app, which
            lacks OSC-52); always also emits Textual's OSC-52 copy as a fallback
            for terminals that support it / remote sessions.
            """
            self.copy_to_clipboard(text)  # OSC-52 + sets app.clipboard
            # Skip the external tool under the headless test driver.
            if type(self._driver).__name__ == "HeadlessDriver":
                return "osc52"
            cmd = _native_clipboard_cmd()
            if cmd:
                try:
                    subprocess.run(cmd, input=text.encode("utf-8"),
                                   timeout=2, check=False)
                    return cmd[0]
                except Exception:  # noqa: BLE001
                    pass
            return "osc52"

        def action_toggle_select(self) -> None:
            """Toggle 'select mode': hand the mouse back to the terminal so its
            native drag-select + copy work, and freeze the view so incoming
            data doesn't disturb the selection. Toggle again to resume."""
            self._select_mode = not self._select_mode
            if self._select_mode:
                if self._log is not None:
                    self._log.auto_scroll = False  # freeze while selecting
                self._set_mouse_capture(False)
                self.notify(
                    "SELECT MODE on — drag to select, copy with your terminal "
                    "(⌘/Ctrl+C). Press Ctrl+E to exit.",
                    timeout=6,
                )
            else:
                self._set_mouse_capture(True)
                if self._log is not None:
                    self._log.jump_to_bottom()  # back to the live tail
                self.notify("SELECT MODE off — mouse scrolling restored.", timeout=3)
            self._refresh_status()

        def _set_mouse_capture(self, enabled: bool) -> None:
            """Enable/disable Textual's mouse tracking via the active driver.

            Disabling it returns the terminal to normal mode so click-drag
            selects text natively. Guarded so the headless test driver (which
            lacks these methods) is a no-op.
            """
            driver = getattr(self, "_driver", None)
            if driver is None:
                return
            try:
                if enabled and hasattr(driver, "_enable_mouse_support"):
                    driver._enable_mouse_support()
                elif not enabled and hasattr(driver, "_disable_mouse_support"):
                    driver._disable_mouse_support()
            except Exception:  # noqa: BLE001 - never let a driver quirk crash the UI
                pass

        def on_input_submitted(self, message: "Input.Submitted") -> None:
            text = message.value
            message.input.value = ""
            if not self.session.source.writable:
                return
            try:
                self.session.send_text(text)
            except Exception as exc:  # noqa: BLE001
                if self._log is not None:
                    self._log.write(f"[red]send failed: {exc}[/red]")


def run_tui(
    session: UartSession,
    *,
    title: str = "uart-proxy",
    ts_mode: str = "relative",
    log_hint: Optional[str] = None,
) -> None:
    """Launch the Textual TUI. Raises RuntimeError if textual isn't installed."""
    if not _TEXTUAL_AVAILABLE:
        raise RuntimeError(
            "textual is not installed. Install it with:  pip install textual\n"
            "or run with --no-tui for the headless stream view."
        )
    UartProxyApp(session, title=title, ts_mode=ts_mode, log_hint=log_hint).run()
