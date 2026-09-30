"""
Play a recorded session back (SPEC S37): ``uart-proxy replay [PATH]``.

Two players over one :class:`~uart_proxy.core.recording.Recording`:

* :func:`play_to_stream` — ``--no-tui``: the bytes go to your own terminal at
  their recorded pace, as ``scriptreplay`` does, and your terminal draws them.
* :class:`ReplayApp` — the TUI: the bytes go through the same terminal emulator
  as character mode (S20), with pause, seek and speed, and the wall-clock time
  of the moment on screen.

Long silences are shortened to ``max_idle`` seconds in both — an hour of a quiet
console should not take an hour to get through.
"""

from __future__ import annotations

import sys
import time
from typing import BinaryIO, Callable, Optional

from ..core.recording import Recording
from ..core.timestamp import format_elapsed

DEFAULT_MAX_IDLE = 2.0
SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
SEEK_STEP = 5.0


def play_to_stream(recording: Recording, out: BinaryIO, *, speed: float = 1.0,
                   max_idle: float = DEFAULT_MAX_IDLE, start_at: float = 0.0,
                   sleep: Callable[[float], None] = time.sleep) -> None:
    """Write the recording to ``out`` at its own pace (``--no-tui``).

    From ``start_at``, everything before it is written at once — the screen as
    it stood at that moment — and the rest at its pace.
    """
    start = 0
    previous: Optional[float] = None
    if start_at > 0:
        start = recording.offset_at(start_at)
        out.write(recording.data[:start])
        out.flush()
        previous = recording.origin + start_at
    for stamp, end in zip(recording.times or [0.0], recording.ends or [len(recording.data)]):
        if end <= start:
            continue
        if previous is not None and speed > 0:
            gap = stamp - previous
            if max_idle > 0:
                gap = min(gap, max_idle)
            if gap > 0:
                sleep(gap / speed)
        out.write(recording.data[start:end])
        out.flush()
        start, previous = end, stamp


try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, Input, Static

    from .tui import TerminalView

    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover - textual is a core dependency
    _TEXTUAL_AVAILABLE = False


if _TEXTUAL_AVAILABLE:

    class FixedTerminalView(TerminalView):
        """A screen that keeps the recorded size (``--term-size``) whatever
        the window does — a BBS drawn for 80×24 only reads right at 80×24."""

        def on_resize(self, event) -> None:
            # Textual also runs the base class's handler unless told not to,
            # and that one would resize the screen to the widget.
            event.prevent_default()
            self.repaint(force=True)

    class ReplayApp(App):
        # The keys drive playback; the (hidden) go-to box must not take them
        # just by being the first thing that can be focused.
        AUTO_FOCUS = None
        CSS = """
        Screen { layout: vertical; }
        #status { height: 1; background: $boost; padding: 0 1; }
        #term { height: 1fr; }
        #goto { dock: bottom; display: none; }
        """
        BINDINGS = [
            Binding("space", "toggle", "Play/pause"),
            Binding("left", "seek(-1)", "−5s"),
            Binding("right", "seek(1)", "+5s"),
            Binding("minus", "speed(-1)", "Slower"),
            Binding("plus,equals_sign", "speed(1)", "Faster"),
            Binding("home", "jump(0)", "Start"),
            Binding("end", "jump(1)", "End"),
            Binding("g", "goto", "Go to time"),
            Binding("q,escape", "quit", "Quit"),
        ]

        def __init__(self, recording: Recording, *, speed: float = 1.0,
                     max_idle: float = DEFAULT_MAX_IDLE,
                     size: Optional[tuple[int, int]] = None,
                     start_at: float = 0.0,
                     clock: Callable[[], float] = time.monotonic) -> None:
            super().__init__()
            self.recording = recording
            self.speed = speed
            self.max_idle = max_idle
            self.fixed_size = size
            self.position = 0.0          # seconds into playback
            self.playing = True
            self._fed = 0                # bytes of the recording on screen
            self._clock = clock
            self._last_tick: Optional[float] = None
            self._term: Optional[TerminalView] = None
            self._status: Optional[Static] = None
            self._start_at = start_at

        def compose(self) -> ComposeResult:
            self._status = Static("", id="status")
            yield self._status
            view = FixedTerminalView if self.fixed_size else TerminalView
            self._term = view(id="term")
            yield self._term
            yield Input(placeholder="go to: +00:12:40 (elapsed), 03:12:40, or "
                                    "2026-09-30 03:12:40 — Enter, or Esc",
                        id="goto")
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"replay · {self.recording.raw_path}"
            if self.fixed_size is not None:
                self._term.emulator.resize(*self.fixed_size)
            self.set_focus(None)
            if self._start_at:
                self.seek(self._start_at)
            self.set_interval(1 / 30, self.tick)
            self._refresh()

        # ── the clock ───────────────────────────────────────────────────────

        def tick(self) -> None:
            now = self._clock()
            elapsed = 0.0 if self._last_tick is None else now - self._last_tick
            self._last_tick = now
            if self.playing:
                self.advance(elapsed * self.speed)
            self._refresh()

        def advance(self, seconds: float) -> None:
            """Move playback on by ``seconds``, cutting long silences short:
            once ``max_idle`` of a longer gap has been sat through, jump to
            where the device speaks again."""
            target = self.position + seconds
            upcoming = self.recording.next_time_after(self.position)
            if self.max_idle > 0 and upcoming is not None:
                quiet_since = self.recording.previous_time_at(self.position)
                if (upcoming - quiet_since > self.max_idle
                        and target - quiet_since >= self.max_idle):
                    target = max(target, upcoming)
            self.position = min(target, self.recording.duration)
            self._feed_to(self.recording.offset_at(self.position))
            if self.position >= self.recording.duration:
                self.playing = False

        def _feed_to(self, offset: int) -> None:
            if offset > self._fed:
                self._term.feed(self.recording.data[self._fed:offset])
                self._fed = offset
                self._term.repaint()

        def seek(self, position: float) -> None:
            """Jump anywhere: backwards means replaying from the start."""
            position = max(0.0, min(position, self.recording.duration))
            offset = self.recording.offset_at(position)
            if offset < self._fed:
                self._term.emulator.reset()
                self._fed = 0
            self.position = position
            self._feed_to(offset)
            self._term.repaint(force=True)

        # ── keys ────────────────────────────────────────────────────────────

        def action_toggle(self) -> None:
            if not self.playing and self.position >= self.recording.duration:
                self.seek(0.0)                       # at the end: play again
            self.playing = not self.playing
            self._refresh()

        def action_seek(self, direction: int) -> None:
            self.seek(self.position + direction * SEEK_STEP)
            self._refresh()

        def action_speed(self, direction: int) -> None:
            index = min(range(len(SPEEDS)), key=lambda i: abs(SPEEDS[i] - self.speed))
            index = max(0, min(len(SPEEDS) - 1, index + direction))
            self.speed = SPEEDS[index]
            self._refresh()

        def action_jump(self, where: int) -> None:
            self.seek(0.0 if where == 0 else self.recording.duration)
            self._refresh()

        def action_goto(self) -> None:
            box = self.query_one("#goto", Input)
            box.value = ""
            box.display = True
            box.focus()

        def on_input_submitted(self, message) -> None:
            from ..core.recording import parse_at

            box = message.input
            text = box.value.strip()
            box.display = False
            self.set_focus(None)
            if not text:
                return
            try:
                self.seek(parse_at(text, self.recording))
            except ValueError as exc:
                self.notify(str(exc), severity="warning", timeout=5)
            self._refresh()

        def check_action(self, action: str, parameters) -> bool:
            # While typing a time, Esc closes the box (on_key) — it must not
            # quit, and space or the arrows must not drive playback.
            if action != "goto" and self._goto_open():
                return False
            return True

        def _goto_open(self) -> bool:
            try:
                return self.query_one("#goto", Input).display
            except Exception:  # noqa: BLE001 - before compose
                return False

        def on_key(self, event) -> None:
            box = self.query_one("#goto", Input)
            if event.key == "escape" and box.display:
                event.stop()
                event.prevent_default()
                box.display = False
                self.set_focus(None)

        # ── status ──────────────────────────────────────────────────────────

        def status_text(self) -> str:
            mark = "▶" if self.playing else "⏸"
            text = (f"{mark} {format_elapsed(self.position)} / "
                    f"{format_elapsed(self.recording.duration)} · ×{self.speed:g}")
            wall = self.recording.wall_at(self.position)
            if wall is not None:
                text += f" · {wall:%Y-%m-%d %H:%M:%S}"
            if not self.recording.has_timing:
                text += " · no timing file: shown all at once"
            emu = self._term.emulator if self._term is not None else None
            if emu is not None:
                text += f" · {emu.columns}×{emu.lines}"
            return text

        def _refresh(self) -> None:
            if self._status is not None:
                self._status.update(self.status_text())


def run_replay(recording: Recording, *, speed: float = 1.0,
               max_idle: float = DEFAULT_MAX_IDLE,
               size: Optional[tuple[int, int]] = None,
               start_at: float = 0.0) -> None:
    if not _TEXTUAL_AVAILABLE:  # pragma: no cover
        raise RuntimeError("textual is not installed; use --no-tui")
    ReplayApp(recording, speed=speed, max_idle=max_idle, size=size,
              start_at=start_at).run()


def stdout_bytes() -> BinaryIO:
    return sys.stdout.buffer
