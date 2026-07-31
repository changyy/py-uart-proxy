"""S14: local PTY mirrors — share one seized UART with several local tools."""

from __future__ import annotations

import os
import selectors
import threading
import time
from contextlib import contextmanager

import pytest

from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.pty_proxy import (
    PTY_SUPPORTED,
    PtyProxyGroup,
    build_links,
    device_stem,
)
from uart_proxy.core.session import UartSession
from uart_proxy.core.timestamp import TimestampTracker

pytestmark = pytest.mark.skipif(not PTY_SUPPORTED, reason="pty is POSIX-only")

if PTY_SUPPORTED:
    import tty


# ── helpers ────────────────────────────────────────────────────────────────


def _rx(data: bytes) -> Event:
    return Event(
        EventKind.DATA, Direction.RX, TimestampTracker().stamp(),
        data=data, text=data.decode(errors="replace"),
    )


@contextmanager
def client(link: str):
    """Attach to a mirror the way ``screen`` would: just open the symlink."""
    fd = os.open(link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    tty.setraw(fd)
    try:
        yield fd
    finally:
        os.close(fd)


def read_bytes(fd: int, count: int, timeout: float = 2.0) -> bytes:
    """Read until ``count`` bytes arrive or ``timeout`` expires."""
    got = bytearray()
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while len(got) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not sel.select(remaining):
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
    return bytes(got)


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@contextmanager
def group(tmp_path, count=2, **kwargs):
    """A started group over ``count`` mirrors, always stopped afterwards."""
    writes: list[bytes] = kwargs.pop("writes", [])
    on_tx = kwargs.pop("on_tx", None)
    if on_tx is None:
        def on_tx(data: bytes) -> int:
            writes.append(data)
            return len(data)

    links = build_links(str(tmp_path), "usbserial-110", count)
    grp = PtyProxyGroup(links, on_tx, **kwargs)
    grp.start()
    try:
        yield grp, writes
    finally:
        grp.stop()


# ── naming (pure) ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "device, stem",
    [
        ("/dev/cu.usbserial-110", "usbserial-110"),
        ("/dev/tty.usbserial-110", "usbserial-110"),
        ("/dev/ttyUSB0", "ttyUSB0"),
        ("COM3", "COM3"),
    ],
)
def test_mirror_names_come_from_the_hardware(device, stem):
    assert device_stem(device) == stem


def test_build_links_numbers_from_zero_and_appends_explicit(tmp_path):
    links = build_links(str(tmp_path), "usbserial-110", 2, extra=["/tmp/mine"])
    assert links == [
        os.path.join(tmp_path, "usbserial-110-0"),
        os.path.join(tmp_path, "usbserial-110-1"),
        "/tmp/mine",
    ]


# ── mirror lifecycle ────────────────────────────────────────────────────────


def test_start_creates_one_symlink_per_mirror_pointing_at_a_pty(tmp_path):
    with group(tmp_path, count=2) as (grp, _):
        for link in grp.links:
            assert os.path.islink(link)
            assert os.readlink(link).startswith("/dev/")
            # A client can open it like any serial device.
            with client(link):
                pass
        assert len(grp.stats()) == 2


def test_stop_removes_the_symlinks(tmp_path):
    with group(tmp_path, count=2) as (grp, _):
        links = grp.links
        assert all(os.path.islink(link) for link in links)
    assert not any(os.path.exists(link) for link in links)


def test_a_stale_symlink_from_a_killed_run_is_replaced(tmp_path):
    stale = tmp_path / "usbserial-110-0"
    stale.symlink_to("/dev/ttys999")  # dangling, as SIGKILL would leave it
    with group(tmp_path, count=1) as (grp, _):
        assert os.readlink(grp.links[0]) != "/dev/ttys999"
        with client(grp.links[0]):
            pass


def test_a_real_file_in_the_way_is_refused_not_deleted(tmp_path):
    victim = tmp_path / "usbserial-110-0"
    victim.write_text("precious")
    grp = PtyProxyGroup([str(victim)], lambda data: len(data))
    with pytest.raises(FileExistsError):
        grp.start()
    assert victim.read_text() == "precious"  # untouched
    grp.stop()


def test_unsupported_tx_merge_is_rejected():
    with pytest.raises(ValueError):
        PtyProxyGroup(["/tmp/x"], lambda data: len(data), tx_merge="magic")


# ── RX: broadcast ───────────────────────────────────────────────────────────


def test_device_rx_is_broadcast_to_every_mirror(tmp_path):
    with group(tmp_path, count=2) as (grp, _):
        with client(grp.links[0]) as a, client(grp.links[1]) as b:
            grp.handle(_rx(b"boot ok\r\n"))
            assert read_bytes(a, 9) == b"boot ok\r\n"
            assert read_bytes(b, 9) == b"boot ok\r\n"


def test_only_device_rx_is_mirrored(tmp_path):
    """TX and LINE events must not reach the mirrors — else our own writes
    would echo back and a mirror could not tell them from the device."""
    with group(tmp_path, count=1) as (grp, _):
        with client(grp.links[0]) as fd:
            stamp = TimestampTracker().stamp()
            grp.handle(Event(EventKind.DATA, Direction.TX, stamp, data=b"typed"))
            grp.handle(Event(EventKind.LINE, Direction.RX, stamp, data=b"assembled"))
            grp.handle(_rx(b"real"))
            assert read_bytes(fd, 4) == b"real"


def _read_without_flushing(link: str, timeout: float = 0.8) -> bytes:
    """Attach the way `cat` would — no termios call, so nothing is flushed.

    `screen` sets raw mode with TCSAFLUSH and pyserial calls tcflush on open, so
    both would hide a stale backlog by accident. This is the reader that doesn't.
    """
    fd = os.open(link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        return read_bytes(fd, 1 << 20, timeout=timeout)
    finally:
        os.close(fd)


def test_a_mirror_is_a_live_view_not_a_backlog(tmp_path):
    """Output that arrived while nobody was attached must not be handed to the
    next tool that opens the mirror: a program would read the past as the
    present, which is worse than not seeing it. History is the log's job."""
    with group(tmp_path, count=1, max_lag=0.3) as (grp, _):
        for _ in range(200):
            grp.handle(_rx(b"x" * 200 + b"\r\n"))
        assert wait_for(lambda: grp.stats()[0].stale > 0, timeout=3.0), \
            "a backlog nobody drained should have been discarded"
        assert grp.stats()[0].pending == 0
        assert _read_without_flushing(grp.links[0]) == b"", \
            "the kernel's own pty buffer must be cleared too"

        # Still a working mirror: what arrives from now on gets through.
        grp.handle(_rx(b"live now\r\n"))
        assert b"live now" in _read_without_flushing(grp.links[0])


def test_a_slow_but_real_reader_never_loses_data(tmp_path):
    """The rule is "no progress for a while", not "the oldest byte is old" — a
    reader that is merely slow is still a reader."""
    with group(tmp_path, count=1, max_lag=0.3) as (grp, _):
        with client(grp.links[0]) as fd:
            received = 0
            for i in range(6):  # 1.8s total, six times the lag window
                grp.handle(_rx(f"chunk {i}\r\n".encode()))
                time.sleep(0.3)
                received += len(read_bytes(fd, 1 << 20, timeout=0.05))
            assert grp.stats()[0].stale == 0, "punished a client that was reading"
            assert received > 0


def test_staleness_can_be_switched_off(tmp_path):
    with group(tmp_path, count=1, max_lag=0) as (grp, _):
        grp.handle(_rx(b"kept\r\n"))
        time.sleep(0.4)
        assert grp.stats()[0].stale == 0
        assert b"kept" in _read_without_flushing(grp.links[0])


def test_a_client_that_stops_reading_drops_instead_of_growing(tmp_path):
    """A stalled reader must not be able to consume memory without bound, nor
    stall the other mirrors."""
    with group(tmp_path, count=2, queue_max=512) as (grp, _):
        with client(grp.links[1]) as live:
            # Nobody opens mirror 0; its PTY buffer fills, then we start dropping.
            for _ in range(50):
                grp.handle(_rx(b"x" * 1024))
            assert wait_for(lambda: grp.stats()[0].dropped > 0)
            # The attentive client still gets served.
            grp.handle(_rx(b"still here"))
            assert b"still here" in read_bytes(live, 10, timeout=3.0)


# ── TX: merge back onto the wire ────────────────────────────────────────────


def test_what_a_client_types_reaches_the_device(tmp_path):
    with group(tmp_path, count=1) as (grp, writes):
        with client(grp.links[0]) as fd:
            os.write(fd, b"reboot\n")
            assert wait_for(lambda: writes)
            assert b"".join(writes) == b"reboot\n"


def test_line_merge_never_splices_two_writers_commands(tmp_path):
    """Two people typing at once: each command crosses the wire whole."""
    with group(tmp_path, count=2, tx_merge="line") as (grp, writes):
        with client(grp.links[0]) as a, client(grp.links[1]) as b:
            os.write(a, b"reb")
            os.write(b, b"who")
            time.sleep(0.05)
            assert not writes, "a partial line must not be forwarded"
            os.write(a, b"oot\n")
            os.write(b, b"ami\n")
            assert wait_for(lambda: len(writes) >= 2)
            assert sorted(writes) == [b"reboot\n", b"whoami\n"]


def test_line_merge_flushes_a_runaway_line(tmp_path):
    """A client streaming binary with no terminator still gets through."""
    with group(tmp_path, count=1, tx_merge="line", line_max=64) as (grp, writes):
        with client(grp.links[0]) as fd:
            os.write(fd, b"z" * 100)
            assert wait_for(lambda: writes)
            assert b"z" in b"".join(writes)


def test_raw_merge_forwards_without_waiting_for_a_line(tmp_path):
    with group(tmp_path, count=1, tx_merge="raw") as (grp, writes):
        with client(grp.links[0]) as fd:
            os.write(fd, b"no newline here")
            assert wait_for(lambda: writes)
            assert b"".join(writes) == b"no newline here"


def test_raw_is_the_default(tmp_path):
    """A mirror is a serial port; a serial port passes bytes through. Holding
    them is the special case you opt into, not the other way round."""
    with group(tmp_path, count=1) as (grp, writes):
        assert grp.tx_merge == "raw"
        with client(grp.links[0]) as fd:
            os.write(fd, b"a")
            assert wait_for(lambda: writes)
            assert b"".join(writes) == b"a"


# ── signals must not be held, even in line-merge mode ───────────────────────


@pytest.mark.parametrize(
    "byte, name",
    [(b"\x03", "^C INTR"), (b"\x04", "^D EOF"),
     (b"\x1a", "^Z SUSP"), (b"\x1c", "^\\ QUIT")],
)
def test_a_signal_is_never_held_waiting_for_a_line(tmp_path, byte, name):
    """A ^C that arrives whenever the sender next presses Enter interrupts the
    wrong thing — it isn't a slow ^C, it's a wrong one."""
    with group(tmp_path, count=1, tx_merge="line") as (grp, writes):
        with client(grp.links[0]) as fd:
            os.write(fd, byte)
            assert wait_for(lambda: writes), f"{name} was swallowed by line-merge"
            assert b"".join(writes) == byte


def test_a_signal_takes_the_half_typed_line_with_it(tmp_path):
    """Order is preserved and nothing the client gave us is dropped: the device's
    own line discipline is what discards the abandoned command."""
    with group(tmp_path, count=1, tx_merge="line") as (grp, writes):
        with client(grp.links[0]) as fd:
            os.write(fd, b"reboo")
            time.sleep(0.05)
            assert not writes, "a partial line alone must still be held"
            os.write(fd, b"\x03")
            assert wait_for(lambda: writes)
            assert b"".join(writes) == b"reboo\x03"


def test_text_control_bytes_are_still_part_of_the_line(tmp_path):
    """Tab and ESC are content, not signals — ESC especially, since flushing it
    alone would split the escape sequence its following bytes belong to."""
    with group(tmp_path, count=1, tx_merge="line") as (grp, writes):
        with client(grp.links[0]) as fd:
            os.write(fd, b"ls\t\x1b[A")
            time.sleep(0.15)
            assert not writes, "line-merge should still be holding this"
            os.write(fd, b"\r")
            assert wait_for(lambda: writes)
            assert b"".join(writes) == b"ls\t\x1b[A\r"


def test_tx_to_a_disconnected_device_is_reported_and_survives(tmp_path):
    """``session.write`` raises while the device is away; the loop must keep
    running so the mirrors work again once it returns."""
    notices: list[str] = []
    alive = threading.Event()

    def on_tx(data: bytes) -> int:
        if not alive.is_set():
            raise RuntimeError("not connected (waiting for the device)")
        return len(data)

    links = build_links(str(tmp_path), "usbserial-110", 1)
    grp = PtyProxyGroup(links, on_tx, on_notice=notices.append)
    grp.start()
    try:
        with client(grp.links[0]) as fd:
            os.write(fd, b"early\n")
            assert wait_for(lambda: notices)
            assert "dropped" in notices[0]

            alive.set()  # device comes back
            os.write(fd, b"later\n")
            assert wait_for(lambda: grp.stats()[0].tx_bytes == len(b"later\n"))
    finally:
        grp.stop()


# ── wired to a real session ─────────────────────────────────────────────────


def test_end_to_end_through_a_session(tmp_path, fake_source):
    """The documented wiring: RX off the bus, TX in via ``session.write``."""
    session = UartSession(fake_source, auto_reconnect=False)
    links = build_links(str(tmp_path), "usbserial-110", 2)
    grp = PtyProxyGroup(links, session.write)
    session.bus.subscribe(grp.handle)
    grp.start()
    session.start()
    try:
        assert wait_for(lambda: session.is_connected)
        with client(grp.links[0]) as agent, client(grp.links[1]) as human:
            fake_source.feed(b"login: ")
            assert read_bytes(agent, 7) == b"login: "
            assert read_bytes(human, 7) == b"login: "

            os.write(human, b"root\n")
            assert wait_for(lambda: b"root\n" in fake_source.writes)
    finally:
        grp.stop()
        session.stop()
