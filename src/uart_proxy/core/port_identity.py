"""
Telling serial ports apart: for listing them, and for finding one that moved.

A USB adapter unplugged and plugged back in can come back under another name
(``usbserial-110`` → ``usbserial-120``), and a path-only reconnect then waits
forever for a node that will never reappear. What survives the replug is the
adapter's **identity** — VID, PID and, on most adapters, a serial number — so
that is what :func:`find_moved` matches on (SPEC S31).

Ports with no USB identity (a built-in ``/dev/ttyS0``, macOS's
``Bluetooth-Incoming-Port``) cannot be followed that way; they keep path-only
reconnect. They are still real ports — a Raspberry Pi's ``ttyAMA0`` has no VID
either — so listings sort them last rather than hide them.
"""

from __future__ import annotations

from typing import Iterable, Optional

#: What pyserial fills in when it has no description: not worth printing.
_NO_DESCRIPTION = {"", "n/a"}


def clean_description(description: Optional[str]) -> str:
    """The port's description, or "" for pyserial's ``n/a`` placeholder."""
    text = (description or "").strip()
    return "" if text.lower() in _NO_DESCRIPTION else text


def has_usb_identity(ident) -> bool:
    return getattr(ident, "vid", None) is not None


def sort_ports(idents: Iterable) -> list:
    """USB adapters first — they are what people plug in to use — then the rest."""
    return sorted(idents, key=lambda i: (not has_usb_identity(i), i.tty_device))


def describe(ident) -> str:
    """``067b:23a3  "USB-Serial Controller"  serial=…`` — only what is known."""
    parts = []
    if has_usb_identity(ident):
        parts.append(ident.vid_pid_str)
    description = clean_description(getattr(ident, "description", ""))
    if description:
        parts.append(f'"{description}"')
    if getattr(ident, "serial_number", None):
        parts.append(f"serial={ident.serial_number}")
    return "  ".join(parts)


def find_moved(original, candidates: Iterable) -> tuple[Optional[str], str]:
    """Where ``original`` is now, if it re-enumerated under another path.

    Returns ``(path, "")`` on exactly one match, else ``(None, why)``. Never a
    guess: with two identical adapters and nothing to tell them apart, following
    the wrong one would connect to another device, which is worse than waiting.

    Matching, strongest first:
    * VID + PID + serial number, when the adapter has one;
    * otherwise VID + PID + description, narrowed by USB location (the physical
      socket) when that is known — replugged into the same socket, it is the
      same adapter.
    """
    if not has_usb_identity(original):
        return None, "no USB identity to follow"
    same_model = [c for c in candidates
                  if c.vid == original.vid and c.pid == original.pid]
    serial = getattr(original, "serial_number", None)
    if serial:
        matches = [c for c in same_model if c.serial_number == serial]
    else:
        wanted = clean_description(getattr(original, "description", ""))
        matches = [c for c in same_model
                   if clean_description(getattr(c, "description", "")) == wanted]
        location = getattr(original, "location", None)
        if location and len(matches) > 1:
            here = [c for c in matches if getattr(c, "location", None) == location]
            if here:
                matches = here
    if len(matches) == 1:
        return matches[0].tty_device, ""
    if not matches:
        return None, "not plugged in"
    return None, (f"{len(matches)} identical adapters and no serial number to "
                  f"tell them apart")
