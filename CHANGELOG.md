# Changelog

## [1.20260730.1220937] — 2026-07-30

Share one physical UART with several local tools — and stop anyone taking it by
accident.

### Added
- **Local PTY mirrors** (SPEC S14) — `connect --proxy-dir DIR [--proxy-count N]`
  exposes N full-duplex PTYs, symlinked into `DIR`, so `screen`, `minicom`, a
  pyserial script or an AI agent can attach while uart-proxy keeps the real port.
  Each mirror reads *and* writes, so the default `--proxy-count 2` is 2 readers
  and 2 writers. Device RX is broadcast to every mirror; concurrent writers are
  merged **line-atomically** so two commands can never interleave mid-line
  (`--tx-merge raw` for byte passthrough). `--proxy PATH` (repeatable) puts a
  mirror at an exact path. POSIX only — Windows has no `pty`; use `--serve`.
  - A client that stops reading has its backlog dropped past 1 MiB rather than
    stalling the serial pump or the other mirrors.
  - Symlinks are removed on exit including `SIGTERM`; a stale link left by a
    `kill -9` is replaced at startup, while a *non*-symlink in the way is
    refused, never deleted.
  - Mirror TX goes through `session.write`, so it appears in the TUI and the logs
    as ordinary TX.
- **Exclusive claim on the physical port** (SPEC S15) — `connect` now issues
  `TIOCEXCL`, so a second program opening the same device fails with `EBUSY`
  instead of silently splitting the byte stream with us. `--no-exclusive` opts
  out; `UartSource.is_exclusive` reports what was obtained.

### Fixed
- **The README overstated OS-level exclusivity.** A serial port is *not*
  exclusive by default on macOS/Linux — two processes can both open one and split
  the stream, with nothing reporting it (`screen` neither sets `TIOCEXCL` nor
  takes a lock file, and pyserial's `exclusive=True` is only an advisory `flock`
  that `screen` ignores). Windows COM ports *are* exclusive. The docs now say so,
  and the new claim makes the guarantee real on POSIX too.

## [1.20260612.1215230] — 2026-06-12

Initial public release. A cross-platform UART log reader / controller
(PuTTY/Minicom-style) for macOS and Windows 11, built on the PyPI
[`uart-helper`](https://pypi.org/project/uart-helper/) serial engine.

### Features
- **Port discovery & connect** — `uart-proxy ports`, `connect --port … --baud …`
  for local read & write; auto-reconnect / wait-for-device with hot-plug
  recovery (`--no-reconnect`, `--reconnect-interval`).
- **Dual time axis** — every line carries absolute wall-clock and relative
  elapsed time, derived from one monotonic reference so timestamps never jump.
- **File recording** — three streams per session: `output.log` (raw RX),
  `output-timestamp.log` (elapsed), `output-fulltimestamp.log` (wall + elapsed),
  under `~/.uart-proxy/sessions/<timestamp>/` with age/size retention.
- **Socket proxy** — re-share a port over TCP with a JSON-lines protocol, an
  auth code, and `full` / `readonly` roles (`--serve --auth CODE[:role]`);
  attach from elsewhere with `remote`.
- **Textual TUI** — live log with follow-tail, timestamp/hex toggles, native
  clipboard copy, and a select mode; `--no-tui` for a headless stream.
- **Plugins** — line-by-line pattern watching via `--grep` or a `Plugin` API
  with a `--plugin-dir`.
- **Integration broker** — a reference loopback-TCP broker
  ([`examples/uart_helper_broker.py`](./examples/uart_helper_broker.py)) lets an
  app that already owns the port (via `uart-helper`) tee its stream to an
  unmodified `uart-proxy remote` client. See [PROTOCOL.md](./PROTOCOL.md).

---

**Versioning scheme:** `1.YYYYmmdd.1HHmmss` — major `1`, minor is the build
*date* (`YYYYmmdd`), patch is `1` + the build *time* (`HHmmss`). Example: a build
made on 2026-06-12 at 21:52:30 is `1.20260612.1215230`. This gives a strictly
increasing, human-readable, timestamped version on every release. Planned work
lives in [ROADMAP.md](./ROADMAP.md).
