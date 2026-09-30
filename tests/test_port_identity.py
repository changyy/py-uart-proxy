"""S31: tell ports apart — list them usefully, and follow one that moved."""

from __future__ import annotations

import json
import os
import time

import pytest
from uart_helper import PortIdentity, UARTPortNotFoundError

from uart_proxy import cli
from uart_proxy.cli import attach_move_report, port_choices
from uart_proxy.core import daemon as daemon_mod
from uart_proxy.core.daemon import DAEMON_SUPPORTED, DaemonInfo
from uart_proxy.core.events import EventKind
from uart_proxy.core.port_identity import (
    clean_description,
    describe,
    find_moved,
    sort_ports,
)
from uart_proxy.core.session import UartSession
from uart_proxy.io import uart_source as mod
from uart_proxy.io.uart_source import UartSource

from conftest import FakeSource


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon_mod.HOME_ENV, str(tmp_path / "home"))


def _usb(name, *, serial="CKBHb13BN11", vid=0x067B, pid=0x23A3,
         desc="USB-Serial Controller", location="1-1"):
    return PortIdentity(device=f"/dev/cu.{name}", vid=vid, pid=pid,
                        serial_number=serial, description=desc, location=location)


def _virtual(name):
    return PortIdentity(device=f"/dev/cu.{name}", description="n/a")


PL_110 = _usb("PL2303G-USBtoUART110")
PL_120 = _usb("PL2303G-USBtoUART120")
BT = _virtual("Bluetooth-Incoming-Port")
DEBUG = _virtual("debug-console")


# ── listing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, shown", [
    ("n/a", ""), ("N/A", ""), ("", ""), (None, ""), ("  n/a ", ""),
    ("USB-Serial Controller", "USB-Serial Controller"),
])
def test_the_no_description_placeholder_is_not_a_description(raw, shown):
    assert clean_description(raw) == shown


def test_a_port_is_described_by_what_is_known_only():
    assert describe(PL_110) == '067b:23a3  "USB-Serial Controller"  serial=CKBHb13BN11'
    assert describe(BT) == ""


def test_usb_adapters_come_first_but_nothing_is_hidden():
    """A built-in UART (ttyS0, a Pi's ttyAMA0) has no VID either — it stays."""
    ordered = sort_ports([DEBUG, PL_110, BT])
    assert ordered[0] is PL_110
    assert {p.tty_device for p in ordered[1:]} == {BT.tty_device, DEBUG.tty_device}


def test_ports_lists_the_adapter_first_and_never_prints_na(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_scan_ports", lambda: [DEBUG, BT, PL_110])
    assert cli.main(["ports"]) == 0
    out = capsys.readouterr().out
    rows = [line.strip() for line in out.splitlines() if line.startswith("  ")]
    assert rows[0].startswith("/dev/tty.PL2303G-USBtoUART110  067b:23a3")
    assert len(rows) == 3
    assert "n/a" not in out


def test_ports_json_is_sorted_and_has_no_placeholder(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_scan_ports", lambda: [BT, PL_110])
    cli.main(["ports", "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    assert [d["device"] for d in data] == [PL_110.tty_device, BT.tty_device]
    assert data[1]["description"] == ""


def test_the_picker_lists_the_same_way():
    choices = port_choices(lambda: [BT, PL_110])
    assert choices[0].path == PL_110.tty_device
    assert "n/a" not in choices[1].label


# ── finding an adapter that moved ───────────────────────────────────────────


def test_a_serial_number_finds_it_under_its_new_name():
    assert find_moved(PL_110, [BT, PL_120]) == (PL_120.tty_device, "")


def test_a_different_serial_is_a_different_adapter():
    other = _usb("PL2303G-USBtoUART120", serial="ZZZ")
    path, why = find_moved(PL_110, [other])
    assert path is None and why == "not plugged in"


def test_without_a_serial_one_adapter_of_the_model_is_enough():
    old = _usb("usbserial-110", serial="")
    new = _usb("usbserial-120", serial="")
    assert find_moved(old, [BT, new]) == (new.tty_device, "")


def test_two_identical_serial_less_adapters_are_never_guessed_between():
    old = _usb("usbserial-110", serial="", location="")
    a = _usb("usbserial-120", serial="", location="")
    b = _usb("usbserial-130", serial="", location="")
    path, why = find_moved(old, [a, b])
    assert path is None and "2 identical adapters" in why


def test_the_same_usb_socket_tells_identical_adapters_apart():
    old = _usb("usbserial-110", serial="", location="1-1")
    here = _usb("usbserial-120", serial="", location="1-1")
    there = _usb("usbserial-130", serial="", location="1-2")
    assert find_moved(old, [there, here]) == (here.tty_device, "")


def test_a_port_with_no_usb_identity_cannot_be_followed():
    path, why = find_moved(BT, [BT])
    assert path is None and "no USB identity" in why


# ── UartSource follows it ───────────────────────────────────────────────────


class FakeDevice:
    """Stands in for UARTDevice: only the paths in `present` open."""

    present: set = set()
    opened: list = []

    def __init__(self, identity, config) -> None:
        self.path = identity.device

    def open(self) -> None:
        if self.path not in FakeDevice.present:
            raise UARTPortNotFoundError(f"Port not found: {self.path}")
        FakeDevice.opened.append(self.path)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_device(monkeypatch):
    FakeDevice.present = set()
    FakeDevice.opened = []
    monkeypatch.setattr(mod, "UARTDevice", FakeDevice)
    return FakeDevice


def test_a_replugged_adapter_is_followed_to_its_new_path(fake_device):
    ports = [PL_110]
    source = UartSource(PL_110.tty_device, scan=lambda: ports, exclusive=False)
    fake_device.present = {PL_110.tty_device}
    source.open()
    assert source.identity == PL_110, "who it is, learnt when it first opened"
    source.close()

    moves = []
    source.on_moved = lambda old, new: moves.append((old, new))
    ports[:] = [BT, PL_120]                    # unplugged, back as …120
    fake_device.present = {PL_120.tty_device}
    source.open()
    assert source.device_path == PL_120.tty_device
    assert moves == [(PL_110.tty_device, PL_120.tty_device)]
    assert fake_device.opened[-1] == PL_120.tty_device
    assert "PL2303G-USBtoUART120" in source.description()


def test_while_it_is_unplugged_the_open_just_fails(fake_device):
    ports = [PL_110]
    source = UartSource(PL_110.tty_device, scan=lambda: ports, exclusive=False)
    fake_device.present = {PL_110.tty_device}
    source.open()
    ports[:] = [BT]
    fake_device.present = set()
    with pytest.raises(UARTPortNotFoundError):
        source.open()
    assert source.device_path == PL_110.tty_device


def test_a_busy_port_is_not_a_moved_port(fake_device, monkeypatch):
    import errno

    import serial

    source = UartSource(PL_110.tty_device, scan=lambda: [PL_120], exclusive=False)
    source.identity = PL_110

    def busy():
        try:
            raise serial.SerialException(errno.EBUSY, "Resource busy")
        except serial.SerialException as exc:
            from uart_helper import UARTError

            raise UARTError("Failed to open") from exc

    source._dev.open = busy
    with pytest.raises(Exception):
        source.open()
    assert source.busy and source.device_path == PL_110.tty_device


def test_a_port_first_seen_without_usb_identity_is_never_followed(fake_device):
    source = UartSource(BT.tty_device, scan=lambda: [BT, PL_120], exclusive=False)
    fake_device.present = {BT.tty_device}
    source.open()
    assert source.identity is None
    fake_device.present = {PL_120.tty_device}
    with pytest.raises(UARTPortNotFoundError):
        source.open()
    assert source.device_path == BT.tty_device


def test_a_failing_scan_never_breaks_opening(fake_device):
    def boom():
        raise OSError("enumeration failed")

    source = UartSource(PL_110.tty_device, scan=boom, exclusive=False)
    fake_device.present = {PL_110.tty_device}
    source.open()
    assert source.identity is None


def test_a_reconnecting_session_ends_up_on_the_new_path(fake_device):
    """End to end through the session's own retry loop."""
    ports = [PL_110]
    source = UartSource(PL_110.tty_device, scan=lambda: ports, exclusive=False)
    source.read = lambda max_bytes, timeout: (time.sleep(timeout), b"")[1]
    fake_device.present = {PL_110.tty_device}
    session = UartSession(source, reconnect_interval=0.02)
    events = []
    session.bus.subscribe(events.append)
    attach_move_report(session, source)
    session.start()
    try:
        deadline = time.monotonic() + 3
        while not session.is_connected and time.monotonic() < deadline:
            time.sleep(0.02)
        assert session.is_connected
        # Unplug: the next read fails; replug under a new name.
        ports[:] = [PL_120]
        fake_device.present = {PL_120.tty_device}

        def dropped(max_bytes, timeout):
            raise IOError("device went away")

        source.read = dropped
        deadline = time.monotonic() + 3
        while source.device_path != PL_120.tty_device and time.monotonic() < deadline:
            time.sleep(0.02)
        source.read = lambda max_bytes, timeout: (time.sleep(timeout), b"")[1]
        assert source.device_path == PL_120.tty_device
        notices = [e.text for e in events if e.kind is EventKind.NOTICE]
        assert any("device moved" in n and "USBtoUART120" in n for n in notices)
    finally:
        session.stop()


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="POSIX registry")
def test_the_registry_follows_the_move_too():
    DaemonInfo(name="lab", pid=os.getpid(), port=PL_110.tty_device, baud=115200,
               listen_host="127.0.0.1", listen_port=9600, auth="x",
               started_at=time.time()).write()
    DaemonInfo(name="someone-else", pid=os.getppid(), port="/dev/tty.other",
               baud=115200, listen_host="127.0.0.1", listen_port=9601, auth="y",
               started_at=time.time()).write()
    session = UartSession(FakeSource())
    source = UartSource(PL_110.tty_device, scan=lambda: [], exclusive=False)
    source.identity = PL_110
    attach_move_report(session, source)
    source.on_moved(PL_110.tty_device, PL_120.tty_device)
    by_name = {d.name: d for d in daemon_mod.list_daemons()}
    assert by_name["lab"].port == PL_120.tty_device
    assert by_name["someone-else"].port == "/dev/tty.other", "only our own entry"
