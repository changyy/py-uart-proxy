"""
One verdict on a shared session's health, with advice a person can act on (SPEC S43).

An agent reading a device through a share depends on two links: its own to
the session (is it still shared?) and the session's to the device (is the
adapter there, is the device talking?). ``assess`` turns both into ``ok``,
``degraded`` or ``down`` — and says what would help, in words an agent can
pass on: re-plug the adapter, share the tab again, reset the board.
"""

from __future__ import annotations

import os
from typing import Optional

#: How long a device that came back counts as just reconnected (output from
#: the gap may be missing). UART_PROXY_HEALTH_SETTLE shortens it for tests.
SETTLE_SECONDS = float(os.environ.get("UART_PROXY_HEALTH_SETTLE", "60"))
#: How long a connected device may say nothing before the advice mentions it.
SILENCE_SECONDS = 60.0


def assess(*, shared: bool, device: Optional[dict], link_error: Optional[str] = None) -> dict:
    """``{level, summary, advice}`` for a session.

    ``device`` is a client's view of ``UartSession.device_health()`` with two
    ages added: ``since_age`` (seconds in this state) and ``silent_for``
    (seconds since the device last said anything; None if it never has).
    """
    if not shared or device is None:
        why = f" ({link_error})" if link_error else ""
        return {"level": "down",
                "summary": f"The session is no longer shared{why}.",
                "advice": "The app sharing it was closed, sharing was turned off, or the session "
                          "ended. Ask the person to share the device's tab again."}
    state = device.get("state") or "unknown"
    error = device.get("error")
    since = device.get("since") or "?"
    detail = f" ({error})" if error else ""
    if state == "waiting":
        return {"level": "down",
                "summary": f"The device is not there{detail}, since {since}.",
                "advice": "Ask the person to check the cable and re-plug the USB-serial adapter; "
                          "the session reconnects by itself when it is back."}
    if state in ("error", "reconnecting"):
        return {"level": "down",
                "summary": f"The device dropped{detail} at {since}; reconnecting.",
                "advice": "If it is not back within a few seconds, ask the person to re-plug the "
                          "USB-serial adapter."}
    if state == "disconnected":
        return {"level": "down",
                "summary": f"The session was disconnected at {since}.",
                "advice": "Ask the person to connect it again."}
    if state == "connecting":
        return {"level": "degraded", "summary": "Connecting to the device.", "advice": ""}
    if state != "connected":
        return {"level": "degraded", "summary": f"The device is {state}.", "advice": ""}
    since_age = device.get("since_age")
    if device.get("reconnects") and since_age is not None and since_age < SETTLE_SECONDS:
        return {"level": "degraded",
                "summary": f"The device reconnected {since_age:.0f} s ago.",
                "advice": "Output from while it was gone may be missing; check its state before "
                          "relying on earlier lines."}
    silent = device.get("silent_for")
    if silent is not None and silent >= SILENCE_SECONDS:
        return {"level": "ok",
                "summary": f"Connected; no output for {silent:.0f} s.",
                "advice": f"No output for {silent:.0f} s — normal for an idle console. If it "
                          "should be printing, it may be hung: ask the person to reset it."}
    return {"level": "ok", "summary": "Connected.", "advice": ""}
