"""
The Textual TUI — a PuTTY/Minicom-style interactive terminal.

Layout (top to bottom):

    Header
    Status bar      source · role · live elapsed clock · RX/TX byte counts
    RichLog         the timestamped log, line by line          (line mode)
    TerminalView    the device's screen, emulated               (character mode)
    Input           type here, Enter sends (with the configured line ending)
    Footer          key bindings

Input has two modes, because a serial console needs both:

* **line** (default) — type a command, press Enter. Comfortable, with editing.
* **character** — every keystroke goes straight to the device, so ``^C``
  interrupts, Tab completes and ↑ reaches the shell's history. This is what a
  serial terminal normally does.

Switching mode switches the *view* with it, because the two halves cannot be
separated: a shell echoes into a screen, not a log, and a log can only append.
Character mode therefore shows a real terminal emulation of what the device has
drawn; line mode shows the timestamped history. Both are fed from the same bus,
so ``<prefix> c`` never loses anything — see :mod:`uart_proxy.ui.terminal`.

Character mode can only work if *every* key reaches the app, so it moves focus off
the Input widget (a focused Input swallows printable keys) and ``check_action``
stands the app's own ``Ctrl+…`` bindings down. That leaves one key reserved: the
**command prefix**, ``Ctrl+]`` by default (see :mod:`uart_proxy.ui.keymap` for why
that one). ``<prefix> ?`` lists the commands; ``<prefix> <prefix>`` sends the
literal byte.

Direct bindings (``Ctrl+T`` timestamps, ``Ctrl+Y`` hex, ``Ctrl+K`` clear,
``Ctrl+W`` copy, ``Ctrl+E`` select, ``Ctrl+Q`` quit) still work in line mode,
where they cannot conflict with anything.

The session runs its read loop on a background thread; bus events are marshalled
onto the Textual event loop with ``call_from_thread``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections import deque
from typing import Callable, Optional

from .. import __version__
from ..core.events import Direction, Event, EventKind
from ..core.session import UartSession
from ..core.retention import format_bytes
from ..core.timestamp import format_elapsed
from .keymap import DEFAULT_PREFIX, key_to_bytes, prefix_label
from .terminal import PYTE_AVAILABLE, TerminalEmulator

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

    class TerminalView(Static):
        """The device's screen, drawn by a real terminal emulator.

        A thin wrapper: :class:`~uart_proxy.ui.terminal.TerminalEmulator` does the
        emulation, this gives it a size and puts it on screen. Repainting rides
        the same timer that drains the event queue and is skipped entirely unless
        something moved, so a quiet device costs nothing.
        """

        def __init__(self, **kwargs) -> None:
            super().__init__("", **kwargs)
            self.emulator = TerminalEmulator()

        def on_resize(self, event) -> None:
            # The emulated screen is always exactly the size of the widget. The
            # device is not told (RS-232 has no SIGWINCH), so it keeps drawing to
            # whatever size it assumed; the status bar reports ours so the size
            # to set over there with `stty` is never a guess.
            #
            # A hidden widget reports 0x0, and the screen keeps tracking while it
            # is hidden — squashing it to a single cell would throw away exactly
            # the output that <prefix> c exists to reveal. No real terminal is
            # that small, so a degenerate size means "not on screen", not "resize".
            if event.size.width < 2 or event.size.height < 2:
                return
            self.emulator.resize(event.size.width, event.size.height)
            self.repaint(force=True)

        def feed(self, data: bytes) -> None:
            self.emulator.feed(data)

        def repaint(self, *, force: bool = False) -> None:
            if force or self.emulator.dirty:
                self.update(self.emulator.render())

        @property
        def geometry_label(self) -> str:
            return f"{self.emulator.columns}×{self.emulator.lines}"

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
        #term { height: 1fr; }
        #cmd { dock: bottom; }
        #find { dock: bottom; display: none; }
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
            history: Optional[list] = None,
            prefix: str = DEFAULT_PREFIX,
            input_mode: str = "line",
            detachable: bool = False,
            info: Optional[list[str]] = None,
            serve_hint: Optional[str] = None,
            mirror_stats: Optional[Callable[[], list]] = None,
        ) -> None:
            super().__init__()
            self.session = session
            self._title = title
            self._log_hint = log_hint
            # `<prefix> i`: how to reach this session, auth codes included
            # (SPEC S23). Never written to the log — see _show_info.
            self._info = list(info or [])
            self._serve_hint = serve_hint
            # PTY mirrors (S14): how many, and whether a reader is losing data.
            self._mirror_stats = mirror_stats
            # The one reserved key. Everything else can then be given to the
            # device in character mode (see _handle_char_key).
            self._prefix = prefix
            self._prefix_label = prefix_label(prefix)
            self._awaiting_command = False
            self._char_mode = input_mode == "char"
            # Only a background session can be left running behind us.
            self._detachable = detachable
            self._detached = False
            # Replayed history to show above the live tail, if we attached to a
            # session that had already been running.
            self._history = history or []
            self._ts_index = _TS_MODES.index(ts_mode) if ts_mode in _TS_MODES else 1
            self._hex = False
            self._select_mode = False
            self._log: Optional[FollowLog] = None
            # None when pyte is missing: character mode still forwards keys, it
            # just cannot draw a screen, and says so rather than silently
            # falling back to a view that is wrong for it.
            self._term: Optional["TerminalView"] = None
            self._status: Optional[_StatusBar] = None
            self._unsubscribe = None
            # Events from any thread land here; the UI drains them on a timer.
            self._pending: "deque[Event]" = deque()
            # Plain-text mirror of what's shown, for clean clipboard copy
            # (no border, no padding, no markup).
            self._copy_lines: "deque[str]" = deque(maxlen=5000)
            # The same lines as rendered, in lockstep, so the log can be rebuilt
            # when a search filter (SPEC S28) is set or cleared.
            self._log_markup: "deque[str]" = deque(maxlen=5000)
            self._filter: Optional[str] = None

        # ── composition ─────────────────────────────────────────────────────

        def compose(self) -> "ComposeResult":
            yield Header(show_clock=True)
            self._status = _StatusBar(id="status")
            yield self._status
            with Vertical():
                yield FollowLog(id="log", highlight=False, markup=True, wrap=True, auto_scroll=True)
                if PYTE_AVAILABLE:
                    yield TerminalView(id="term")
            placeholder = (
                "Type and press Enter to send…"
                if self.session.source.writable
                else "read-only — input disabled"
            )
            inp = Input(placeholder=placeholder, id="cmd")
            inp.disabled = not self.session.source.writable
            yield inp
            yield Input(placeholder="search the log — Enter to filter, empty "
                                    "Enter to clear, Esc to cancel", id="find")
            yield Footer()

        def on_mount(self) -> None:
            self.title = self._title
            self.sub_title = f"v{__version__}"
            self._log = self.query_one("#log", FollowLog)
            if PYTE_AVAILABLE:
                self._term = self.query_one("#term", TerminalView)
            # History first, so it sits above the live tail in the right order.
            self._write_history()
            # Subscribe BEFORE start() so the "connected" status is captured.
            self._unsubscribe = self.session.bus.subscribe(self._enqueue_event)
            # Drain the cross-thread queue and refresh the status on timers.
            self.set_interval(0.05, self._drain_events)
            self.set_interval(0.25, self._refresh_status)
            # Make sure typing goes where the current mode needs it: the input
            # box in line mode, the log in character mode (a focused Input eats
            # printable keys before on_key ever sees them).
            self._show_view()
            # A transport that can carry a window size (ssh, SPEC S35) is told
            # the terminal view's, now and whenever it changes.
            tell = getattr(self.session.source, "set_window_size", None)
            if self._term is not None and tell is not None:
                self._term.emulator.on_resize = tell
            # Layout is not settled yet, so the first honest size arrives here.
            self.call_after_refresh(self._sync_terminal_size)
            if self._char_mode or not self.session.source.writable:
                self.query_one("#cmd", Input).disabled = True
                self._defocus_input()
            else:
                self.query_one("#cmd", Input).focus()
            # In character mode the box takes no input: give its row to the
            # device's screen (and so to a far end that is told the size).
            self.query_one("#cmd", Input).display = not self._char_mode
            self._note(f"{self._prefix_label} is the command prefix "
                       f"({self._prefix_label} ? for the list)")
            try:
                self.session.start()
            except Exception as exc:  # noqa: BLE001
                self._log.write(f"[red]Failed to open source: {exc}[/red]")
            self._refresh_status()

        def _write_history(self) -> None:
            """Show replayed lines dimmed, between two dividers.

            A serial console is often quiet, so without this an attach looks
            identical to a broken one. The stamps are the server's, from when
            each line actually arrived — anything else would be history claiming
            to have happened at the moment you attached.
            """
            if self._log is None or not self._history:
                return
            from ..core.replay import describe

            header = f"── replayed {describe(self._history)} ──"
            self._append(f"[dim]{self._escape(header)}[/dim]", header)
            for entry in self._history:
                prefix = self._history_prefix(entry)
                plain = f"{prefix}< {entry.text}"
                self._append(f"[dim]{self._escape(plain)}[/dim]", plain)
            self._append("[dim]── live ──[/dim]", "── live ──")

        def _history_prefix(self, entry) -> str:
            mode = _TS_MODES[self._ts_index]
            if mode == "full":
                return f"{entry.wall} | {format_elapsed(entry.elapsed)} "
            if mode == "relative":
                return f"{format_elapsed(entry.elapsed)} "
            return ""

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
            if self._term is not None and self._term.display:
                self._term.repaint()

        def _render_event(self, event: Event) -> None:
            # Fed whether or not the terminal is the view on screen: <prefix> c
            # has to reveal the device's screen as it is *now*, and a view that
            # only started tracking when you looked at it would open blank.
            # Measured at 1.19 MB/s, i.e. ~1% of a core at 115200 baud.
            if (self._term is not None and event.kind == EventKind.DATA
                    and event.direction == Direction.RX):
                self._term.feed(event.data)
            if self._log is None:
                return
            if event.kind == EventKind.LINE:
                self._append(*self._format_line(event))
            elif event.kind == EventKind.NOTICE:
                self._append(f"[yellow]* {self._escape(event.text)}[/yellow]",
                             f"* {event.text}")
            elif event.kind == EventKind.STATUS:
                meta = f" {event.meta}" if event.meta else ""
                self._append(
                    f"[cyan]# {self._escape(event.text)}{self._escape(meta)}[/cyan]",
                    f"# {event.text}{meta}",
                )

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

        # ── views ────────────────────────────────────────────

        def _terminal_showing(self) -> bool:
            """Whether the emulated screen is the view currently covering the log."""
            return self._term is not None and self._term.display

        def _show_view(self) -> None:
            """Put the view that belongs to the current input mode on screen.

            Exactly one is visible; the hidden one keeps receiving everything, so
            switching is instant and never shows a gap where the other view was
            not looking.
            """
            terminal = self._char_mode and self._term is not None
            self._sync_terminal_size()
            if self._term is not None:
                self._term.display = terminal
                if terminal:
                    self._term.repaint(force=True)
            if self._log is not None:
                self._log.display = not terminal

        def _sync_terminal_size(self) -> None:
            """Keep the hidden screen the size of the region it will be drawn in.

            A hidden widget has no size of its own, so the screen would keep
            whatever size it was built with and only meet reality when it is
            first shown — by which point it has output on it, and pyte drops
            lines from the *top* when it shrinks. The first ``<prefix> c`` would
            then reveal a screen with its opening rows missing.

            The log occupies the same slot, so its size is the answer. Cheap
            enough to call on the status timer: a resize to the size it already
            is returns immediately.
            """
            if self._term is None or self._term.display:
                return  # on screen: its own on_resize is authoritative
            if self._log is None:
                return
            size = self._log.size
            if size.width >= 2 and size.height >= 2:
                self._term.emulator.resize(size.width, size.height)

        def _defocus_input(self) -> None:
            """Take focus off the Input, which would otherwise swallow keys.

            Focus goes to whichever view is showing. The terminal is a Static and
            not focusable, so there it is cleared entirely — with no focused
            widget the App itself receives the keys, which is what character mode
            needs.
            """
            if self._log is not None and self._log.display:
                self._log.focus()
            else:
                self.set_focus(None)

        # ── status bar ─────────────────────────────────────────────────────────

        def _refresh_status(self) -> None:
            # Also the moment to notice the window changed while the screen was
            # hidden; see _sync_terminal_size for why that cannot wait.
            self._sync_terminal_size()
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
            if self._terminal_showing():
                # Follow/paused says nothing about a screen. The size does, and
                # it is the one the device was never told — so it is also the
                # answer to "what should I set over there with stty?".
                follow = f"[cyan]screen {self._term.geometry_label}[/cyan]"
            elif self._log is not None and self._log.following:
                follow = "[green]follow[/green]"
            else:
                follow = "[yellow]paused ▲[/yellow]"
            rec = f" · rec→{self._escape(self._log_hint)}" if self._log_hint else " · rec off"
            if self._serve_hint:
                # Where, never the code: a status bar ends up in screenshots.
                rec += (f" · serve {self._escape(self._serve_hint)}"
                        f" ({self._prefix_label} i)")
            rec += self._mirror_label()
            rec += self._filter_label()
            if self._awaiting_command:
                mode = f"[black on yellow] {self._prefix_label} … [/]"
            elif self._char_mode:
                # The quit hint earns its place here: Ctrl+Q is XON in this
                # mode and the footer no longer offers it, so this is the only
                # thing on screen that says how to leave.
                mode = (f"[cyan]char[/cyan] ({self._prefix_label} c"
                        f" · {self._prefix_label} q quit)")
            else:
                mode = f"line ({self._prefix_label} c)"
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
                f" · {mode}"
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
            # In character mode what is on screen is the device's screen, so
            # clear *that* — the log behind it is the history and throwing it
            # away is not what `clear` means at a shell prompt.
            if self._terminal_showing():
                self._term.emulator.reset()
                self._term.repaint(force=True)
                self.notify("Screen cleared (the log behind it is kept).", timeout=3)
                return
            # Clear both the visible log AND the copy buffer, so the range that
            # Ctrl+W copies is reset too (clear → accumulate → Ctrl+W copies
            # just the new range).
            if self._log is not None:
                self._log.clear()
            self._copy_lines.clear()
            self._log_markup.clear()
            self.notify("Cleared (display + copy range).", timeout=2)

        def action_follow_bottom(self) -> None:
            if self._log is not None:
                self._log.jump_to_bottom()
            self._refresh_status()

        def action_copy_all(self) -> None:
            """Copy the whole in-memory log to the clipboard as clean text
            (no border, no padding, no markup)."""
            # What is shown: with a filter set, the matching lines only.
            lines = [line for line in self._copy_lines if self._matches(line)]
            text = "\n".join(lines)
            if not text:
                self.notify("Nothing to copy yet.", timeout=2)
                return
            via = self._copy_text(text)
            which = f" matching {self._filter!r}" if self._filter else ""
            self.notify(
                f"Copied {len(lines)} lines{which} to the clipboard ({via}).",
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

        # ── keys ────────────────────────────────────────────────────────────

        #: Bindings that must get out of the way in character mode, because the
        #: keys belong to the device then. Reachable via the prefix either way.
        #:
        #: ``quit`` and ``follow_bottom`` are here for the footer rather than for
        #: the keys: ``on_key`` already consumes them in character mode, so
        #: ``Ctrl+Q`` goes to the device as XON and ``End`` as its escape
        #: sequence. Leaving them in the footer advertised a key that silently
        #: did nothing — you press ``Ctrl+Q``, nothing happens, and the session
        #: is left running with the port still claimed.
        _MODAL_ACTIONS = frozenset({
            "toggle_select", "cycle_ts", "toggle_hex", "clear_log", "copy_all",
            "quit", "follow_bottom",
        })

        def check_action(self, action: str, parameters) -> bool:
            """Disable the direct Ctrl+… bindings while in character mode.

            They are `priority=True`, so without this they would be swallowed by
            the app before `on_key` ever ran — and stealing `Ctrl+W` from a shell
            is exactly the problem character mode exists to fix. Returning False
            lets the key fall through to the device; the same commands stay
            available as `<prefix> w` and friends.
            """
            if self._char_mode and action in self._MODAL_ACTIONS:
                return False
            return True

        def on_key(self, event) -> None:
            """The command prefix, and character mode.

            Character mode can only work if *every* key reaches here, which is
            why it moves focus off the Input widget — a focused Input swallows
            printable keys (they belong in the box) and they never bubble up.
            """
            if self._awaiting_command:
                event.stop()
                event.prevent_default()
                self._awaiting_command = False
                self._run_command(event)
                self._restore_focus()
                return

            if event.key == self._prefix:
                event.stop()
                event.prevent_default()
                self._awaiting_command = True
                # In line mode the Input holds focus and would swallow the
                # command letter — printable keys go in the box and never bubble
                # up to here. Park focus on the log for the one keystroke that
                # follows, then give it back.
                self._defocus_input()
                self._note(f"{self._prefix_label} — d detach · q quit · c char/line "
                           f"· t time · y hex · k clear · w copy · ? help")
                self._refresh_status()
                return

            if event.key == "escape" and self.query_one("#find", Input).display:
                event.stop()
                self._close_find()
                return

            if self._char_mode:
                event.stop()
                event.prevent_default()
                self._send_key(event)

        def _restore_focus(self) -> None:
            """Put focus back where the current mode wants it."""
            find = self.query_one("#find", Input)
            if find.display:
                find.focus()
                return
            if self._char_mode or self._detached or not self.session.source.writable:
                return
            inp = self.query_one("#cmd", Input)
            if not inp.disabled:
                inp.focus()

        def _run_command(self, event) -> None:
            """Handle the key pressed *after* the prefix."""
            key = event.key
            if key == self._prefix:          # escape the escape
                self._send_key(event, literal=True)
                return
            command = {
                "d": self._detach, "q": self._quit,
                "c": self._toggle_input_mode, "t": self.action_cycle_ts,
                "y": self.action_toggle_hex, "k": self.action_clear_log,
                "w": self.action_copy_all, "e": self.action_toggle_select,
                "i": self._show_info, "slash": self._open_find, "/": self._open_find,
                "question_mark": self._show_help, "?": self._show_help,
            }.get(key)
            if command is None:
                self._note(f"{self._prefix_label} {key}: unknown — "
                           f"{self._prefix_label} ? for the list")
                return
            command()

        def _send_key(self, event, *, literal: bool = False) -> None:
            """Put one keystroke on the wire, if it has bytes to send."""
            if not self.session.source.writable:
                return
            data = key_to_bytes(
                event.key, event.character,
                eol=self.session.default_eol or b"\r",
                encoding=self.session.encoding,
            )
            if not data:
                return
            try:
                self.session.write(data)
            except Exception as exc:  # noqa: BLE001
                self._note(f"send failed: {exc}")

        def _toggle_input_mode(self) -> None:
            self._char_mode = not self._char_mode
            inp = self.query_one("#cmd", Input)
            self._show_view()
            # No input in character mode, so its row goes to the screen.
            inp.display = not self._char_mode
            if self._char_mode:
                # Focus has to leave the Input for printable keys to reach on_key.
                inp.disabled = True
                self._defocus_input()
                if self._term is not None:
                    self._note("character mode — every key goes straight to the "
                               "device, including ^C and ^Q, and the view is the "
                               f"device's screen. {self._prefix_label} c for the "
                               f"log again, {self._prefix_label} q to quit")
                else:
                    self._note("character mode — keys go straight to the device, "
                               "but pyte is not installed, so output stays in the "
                               "log view and a shell's redraws will look wrong. "
                               "Install it with: pip install pyte")
            else:
                inp.disabled = not self.session.source.writable
                inp.placeholder = "Type and press Enter to send…"
                if not inp.disabled:
                    inp.focus()
                self._note("line mode — type a command and press Enter; "
                           "the view is the timestamped log")
            self._refresh_status()

        def _quit(self) -> None:
            # exit(), not action_quit(): the latter is a coroutine in Textual and
            # calling it from a sync handler would quietly do nothing.
            self.exit()

        def _detach(self) -> None:
            """Leave the UI but let the session carry on, if there is one to leave."""
            if not self._detachable:
                self._note("nothing to detach from — this session runs in this "
                           "process. Start one with 'uart-proxy start' to be able "
                           "to leave it running.")
                return
            self._detached = True
            self.exit()

        def _show_help(self) -> None:
            p = self._prefix_label
            # One block, so character mode raises one toast rather than eight.
            self._note_lines((
                f"{p} is the command prefix. Everything else goes to the device.",
                f"  {p} d   detach (leave a background session running)",
                f"  {p} q   quit",
                f"  {p} c   character / line input, and the view that goes with it",
                f"  {p} t   timestamps    {p} y   hex    {p} k   clear",
                f"  {p} w   copy log      {p} e   select mode",
                f"  {p} i   session info — proxy address and auth code",
                f"  {p} /   search: show only the log lines that match",
                f"  {p} {p.split('+')[-1]}   send a literal {p}",
            ), timeout=12)

        # ── search (SPEC S28) ────────────────────────────────────────────────

        def _append(self, markup: str, plain: str) -> None:
            """Record one log line, and show it unless a filter hides it."""
            self._log_markup.append(markup)
            self._copy_lines.append(plain)
            if self._log is None:
                return
            if self._filter is None:
                self._log.write(markup)
            elif self._matches(plain):
                self._log.write(self._highlighted(plain))

        def _matches(self, plain: str) -> bool:
            """Smart case: case-insensitive unless the pattern has a capital."""
            if self._filter is None:
                return True
            if self._filter != self._filter.lower():
                return self._filter in plain
            return self._filter in plain.lower()

        def _highlighted(self, plain: str):
            from rich.text import Text

            text = Text(plain)
            text.highlight_words([self._filter], style="black on yellow",
                                 case_sensitive=self._filter != self._filter.lower())
            return text

        def _set_filter(self, pattern: str) -> None:
            self._filter = pattern or None
            self._rebuild_log()
            if self._filter is None:
                self._note("filter cleared — showing the whole log")
            else:
                count = sum(1 for line in self._copy_lines if self._matches(line))
                self.notify(f"{count} line(s) match {self._filter!r} — "
                            f"{self._prefix_label} / and an empty Enter to clear",
                            timeout=4)
            self._refresh_status()

        def _rebuild_log(self) -> None:
            if self._log is None:
                return
            self._log.clear()
            for markup, plain in zip(self._log_markup, self._copy_lines):
                if self._filter is None:
                    self._log.write(markup)
                elif self._matches(plain):
                    self._log.write(self._highlighted(plain))
            self._log.jump_to_bottom()

        def _open_find(self) -> None:
            if self._char_mode:
                # Every key belongs to the device here, and the log is behind
                # the device's screen: there is nothing to type a search into.
                self._note(f"search works on the log — switch to line mode "
                           f"first ({self._prefix_label} c)")
                return
            find = self.query_one("#find", Input)
            # Empty, so an empty Enter really does clear: the current pattern
            # goes in the placeholder instead of in the way.
            find.value = ""
            find.placeholder = (
                f"filtering on {self._filter!r} — new pattern and Enter, empty "
                f"Enter to clear, Esc to keep it" if self._filter else
                "search the log — Enter to filter, Esc to cancel")
            find.display = True
            find.focus()

        def _close_find(self) -> None:
            find = self.query_one("#find", Input)
            find.display = False
            self._restore_focus()

        def _filter_label(self) -> str:
            if self._filter is None:
                return ""
            count = sum(1 for line in self._copy_lines if self._matches(line))
            return (f" · [black on yellow] filter {self._escape(repr(self._filter))}"
                    f" ({count}) [/]")

        def _mirror_label(self) -> str:
            """`` · mirrors 2``, and what a lagging reader has lost, if any.

            Only ``dropped`` is worth alarm: it is a reader that is attached but
            not keeping up. ``stale`` is output nobody was attached to read,
            which is the normal state of an unused mirror.
            """
            if self._mirror_stats is None:
                return ""
            try:
                stats = self._mirror_stats()
            except Exception:  # noqa: BLE001 - status must never break the UI
                return ""
            label = f" · mirrors {len(stats)}"
            dropped = sum(m.dropped for m in stats)
            if dropped:
                label += f" [red]dropped {format_bytes(dropped)}[/red]"
            return label

        def _show_info(self) -> None:
            """Raise the session's connection details as a toast, and only that.

            Unlike every other note this is not written to the log: the log is
            what ``Ctrl+W`` copies and people paste into tickets, and the lines
            carry the proxy's auth code.
            """
            lines = self._info or ["nothing to show — not serving (--serve)"]
            self.notify("\n".join(lines), title="session", timeout=20)

        def _note(self, text: str) -> None:
            self._note_lines([text], timeout=4)

        def _note_lines(self, lines, *, timeout: float = 8) -> None:
            """Write related notes to the log, and surface them if it is hidden.

            Notes always go to the log — that is the record, and what
            ``Ctrl+W`` copies. But in character mode the log is behind the
            terminal view, where a note would never be seen, so the set is also
            raised as a single toast: one for the group, not one per line.
            """
            lines = list(lines)
            for line in lines:
                if self._log is not None:
                    self._append(f"[yellow]* {self._escape(line)}[/yellow]",
                                 f"* {line}")
            if self._terminal_showing():
                self.notify("\n".join(lines), timeout=timeout)

        def on_input_submitted(self, message: "Input.Submitted") -> None:
            if message.input.id == "find":
                self._set_filter(message.value)
                message.input.value = ""
                self._close_find()
                return
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
    history: Optional[list] = None,
    prefix: str = DEFAULT_PREFIX,
    input_mode: str = "line",
    detachable: bool = False,
    info: Optional[list[str]] = None,
    serve_hint: Optional[str] = None,
    mirror_stats: Optional[Callable[[], list]] = None,
) -> None:
    """Launch the Textual TUI. Raises RuntimeError if textual isn't installed."""
    if not _TEXTUAL_AVAILABLE:
        raise RuntimeError(
            "textual is not installed. Install it with:  pip install textual\n"
            "or run with --no-tui for the headless stream view."
        )
    UartProxyApp(session, title=title, ts_mode=ts_mode, log_hint=log_hint,
                 history=history, prefix=prefix, input_mode=input_mode,
                 detachable=detachable, info=info, serve_hint=serve_hint,
                 mirror_stats=mirror_stats).run()
