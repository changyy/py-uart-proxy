"""
Pick a serial port from a list, for ``connect`` without ``--port`` (SPEC S27).

A small Textual app of its own, run before the session exists: it returns the
chosen device path, or None if you backed out. ``r`` rescans, because the usual
reason to look at the list is that you have just plugged something in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, Header, OptionList, Static
    from textual.widgets.option_list import Option

    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover - textual is a core dependency
    _TEXTUAL_AVAILABLE = False


@dataclass(frozen=True)
class PortChoice:
    path: str
    label: str          # everything shown after the path


def format_choice(choice: PortChoice) -> str:
    return f"{choice.path}  {choice.label}".rstrip()


if _TEXTUAL_AVAILABLE:

    class PortPickerApp(App):
        TITLE = "uart-proxy · choose a port"
        CSS = """
        #hint { padding: 0 1; color: $text-muted; }
        OptionList { height: 1fr; }
        """
        BINDINGS = [
            Binding("r", "rescan", "Rescan"),
            Binding("escape", "cancel", "Cancel"),
            Binding("q", "cancel", "Cancel", show=False),
        ]

        def __init__(self, scan: Callable[[], list[PortChoice]]) -> None:
            super().__init__()
            self._scan = scan
            self._choices: list[PortChoice] = []

        def compose(self) -> ComposeResult:
            yield Header()
            yield Static("", id="hint")
            yield OptionList(id="ports")
            yield Footer()

        def on_mount(self) -> None:
            self.action_rescan()

        def action_rescan(self) -> None:
            self._choices = list(self._scan())
            options = self.query_one("#ports", OptionList)
            options.clear_options()
            for choice in self._choices:
                options.add_option(Option(format_choice(choice)))
            hint = self.query_one("#hint", Static)
            if self._choices:
                hint.update("Enter to connect · r to rescan · Esc to cancel")
                options.highlighted = 0
                options.focus()
            else:
                hint.update("No serial ports found — plug one in and press r, "
                            "or Esc to cancel")

        def on_option_list_option_selected(self, event) -> None:
            index = event.option_index
            if 0 <= index < len(self._choices):
                self.exit(self._choices[index].path)

        def action_cancel(self) -> None:
            self.exit(None)


def pick_port(scan: Callable[[], list[PortChoice]]) -> Optional[str]:
    """Show the picker; the chosen device path, or None if cancelled."""
    if not _TEXTUAL_AVAILABLE:  # pragma: no cover
        raise RuntimeError("textual is not installed; pass --port instead")
    return PortPickerApp(scan).run()
