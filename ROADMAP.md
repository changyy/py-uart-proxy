# uart-proxy — Roadmap & Action Items

Status legend: ✅ done · 🟡 in progress · ⬜ todo · 💡 idea

## v0.1 — Core (current)

The seven original requirements, implemented end to end.

- ✅ **R1** Enumerate UART ports; pick one for read & write (`ports`, `connect`).
- ✅ **R2** Dual time axis (wall-clock + elapsed); three log files
  (`output.log`, `output-timestamp.log`, `output-fulltimestamp.log`).
- ✅ **R3** Simple ASCII display for BBS / telnet (`--encoding`, `--eol`, text/hex view).
- ✅ **R4** Socket proxy with auth code + roles (`--serve --auth CODE[:role]`).
- ✅ **R5** Command-line driven (`uart-proxy` with subcommands).
- ✅ **R6** Local UART **or** remote socket source (`connect` / `remote`).
- ✅ **R7** Plugin architecture for line-by-line pattern watching (`--grep`, `Plugin` API).
- ✅ **Mouse follow-tail**: wheel-up pauses auto-scroll to read history, wheel
  back to bottom (or `End`) resumes; status shows follow/paused (SPEC S10).
- ✅ Tests: engine unit tests, end-to-end proxy over a real socket, and TUI
  tests via Textual's headless harness (29 tests).
- 🟡 Manual hardware validation on macOS + Windows 11.

## v0.2 — Robustness & UX

- ✅ **Session retention**: auto-prune `~/.uart-proxy/sessions/` by age
  (30 days) and total size (500 MB, delete oldest); configurable via CLI or
  `~/.uart-proxy/config.toml`; `uart-proxy sessions [--prune]` (SPEC S11).
- 💡 Per-file rotation *within* a single very long session (split `output.log`
  at N MB) — currently retention is per-session-folder only; mid-session size
  is not capped while a run is active.

- ✅ **Reconnect / hot-plug**: `connect` waits for an absent device and
  auto-reattaches on drop/return (`--no-reconnect`, `--reconnect-interval`;
  SPEC S12). Still polling-based on the same path; matching a re-enumerated
  path (e.g. usbserial-110→120) via `SerialMonitor` is a future refinement.
- ✅ **Selection & clipboard** in the TUI (drag-select + Cmd/Ctrl+C; SPEC S13).
- ⬜ **Port-busy hint**: when opening a port fails because another process holds
  it (UART is exclusive-open), detect this and suggest attaching to an existing
  proxy via `uart-proxy remote` instead.
- ⬜ **Telnet IAC handling**: minimal negotiation so real telnet/BBS sessions
  render cleanly (currently raw passthrough).
- ⬜ **TUI port picker**: when `--port` is omitted, show a selectable list.
- ⬜ **Scrollback search / filter** in the TUI (find, highlight, freeze).
- ⬜ **Session header line** in logs (start time, port, baud, the absolute &
  relative window banner).
- ⬜ **TX echo over proxy**: optionally forward operator TX to all clients so
  remote viewers see what was typed.
- ⬜ **Config profiles**: reuse `uart_helper` TOML profiles for `connect`
  (baud/parity/device defaults by name).

## v0.3 — Security & packaging

- ⬜ **TLS for the proxy** (or an SSH-tunnel doc) — auth code alone is plaintext
  today; fine on a trusted LAN, not the open internet.
- ⬜ **Per-role command allow-list** (e.g. a role that may only send specific
  commands).
- ⬜ **Rate limit / connection cap** on the proxy.
- 🟡 **PyPI / pipx** as the primary channel — `uart-helper` is now a real
  dependency so `pipx install uart-proxy` will work once published.
- ✅ **Standalone repo** — the PC app now lives in its own
  [`changyy/py-uart-proxy`](https://github.com/changyy/py-uart-proxy) repo for
  PyPI; the dev-only `3rd-library` bootstrap fallback has been dropped (the
  serial engine is imported from the PyPI `uart-helper` package).
- ⬜ **PyInstaller builds** for macOS and Windows.
- ⬜ **macOS notarized .dmg** (Developer ID + notarytool + staple; not MAS —
  sandbox vs serial).
- ⬜ **Windows signed .exe installer** (Inno/NSIS); MS Store only if demand
  (MSIX sandbox restricts COM access).
- ⬜ **PySide6 GUI** for non-terminal users, reusing the engine (Flutter stays
  mobile-only).

## v1.0 — Ecosystem

- ⬜ **Plugin discovery via entry points** (pip-installable plugins).
- ⬜ **Richer plugin hooks**: `on_data` (raw), `on_match` with capture groups,
  timers, and the ability to register UI panels.
- 🟡 **Mobile (Flutter) client** (tracked separately) — a thin read-mostly
  consumer of the JSON-lines proxy protocol: connect, authenticate, watch the
  live log. Protocol client + screens implemented and tested; platform
  scaffolding still pending. It talks to this app's proxy over the wire contract
  in [PROTOCOL.md](./PROTOCOL.md), so it needs no changes here.
- ⬜ **Replay mode**: load a recorded `output*.log` and scrub the timeline.

## Integration: attach to a `uart_helper`-owned port

- ✅ **Protocol spec** ([PROTOCOL.md](./PROTOCOL.md)) — formalised so any broker
  can interoperate with uart-proxy's existing client.
- ✅ **Reference broker** ([examples/uart_helper_broker.py](./examples/uart_helper_broker.py))
  — stdlib + uart_helper, **loopback TCP** (portable to Windows & macOS; not a
  Unix socket file, which CPython can't do on Windows). uart-proxy's unmodified
  `remote` client attaches; proven by `tests/test_broker_interop.py`.
- ⬜ **Upstream it** into `uart_helper` as an optional `uart_helper.broker`
  (the maintainers' call), ideally sharing one protocol module with uart-proxy
  to avoid drift.

## Proposed upstream enhancements to `uart_helper`

Not changing the library here — collecting suggestions for its maintainers:

- 💡 **Streaming read helper**: a `read_available()` / iterator that returns
  whatever is in the buffer without a fixed size, so callers don't juggle
  `in_waiting` + `read(1, timeout)`. Would simplify `UartSource.read`.
- 💡 **Blocking-read cancellation**: a way to interrupt a pending `read` so
  shutdown doesn't wait out the timeout.
- 💡 **Expose a raw line iterator** with hot-plug-aware reconnection, to back
  the v0.2 auto-reconnect feature.

## Open questions

- ⬜ Default proxy bind: keep `0.0.0.0` or default to `127.0.0.1` and require
  opt-in for LAN exposure? (Security vs convenience.)
- ⬜ Should remote clients see and replay the wall-clock timestamps from the
  *server*, or re-stamp locally on arrival? (Currently re-stamped locally.)
