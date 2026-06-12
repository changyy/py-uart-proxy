# Changelog

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
