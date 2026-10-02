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
  tests via Textual's headless harness (29 tests at v0.1; 577 today).
- 🟡 Manual hardware validation on macOS + Windows 11.

## v0.2 — Robustness & UX

- ✅ **Session retention**: auto-prune `~/.uart-proxy/sessions/` by age
  (30 days) and total size (500 MB, delete oldest); configurable via CLI or
  `~/.uart-proxy/config.toml`; `uart-proxy sessions [--prune]` (SPEC S11).
- ✅ **Rotation within a session** (SPEC S32): `--log-rotate-mb` splits the
  three files together into numbered parts at a line end; `--log-keep-parts`
  deletes the oldest, which is what caps a session that never finishes.

- ✅ **Reconnect / hot-plug**: `connect` waits for an absent device and
  auto-reattaches on drop/return (`--no-reconnect`, `--reconnect-interval`;
  SPEC S12). Still polling-based; an adapter re-enumerated under a new path
  (usbserial-110→120) is followed by VID/PID/serial (SPEC S31).
- ✅ **Selection & clipboard** in the TUI (drag-select + Cmd/Ctrl+C; SPEC S13).
- ✅ **Exclusive claim on the port** (SPEC S15): `connect` now takes `TIOCEXCL`
  so no other program can open the same wire behind our back (pyserial's
  `exclusive=True` is only an advisory `flock`, which `screen` ignores).
  `--no-exclusive` opts out. Kernel enforcement is real-hardware-only —
  the pty driver ignores `TIOCEXCL`, so CI can only assert we ask for it.
- ✅ **Local PTY mirrors** (SPEC S14): `connect --proxy-dir DIR [--proxy-count N]`
  exposes N full-duplex PTYs (default 2 = 2 readers *and* 2 writers), symlinked
  into `DIR`, so `screen` / `minicom` / pyserial / an agent can share the one
  port while uart-proxy keeps it. RX broadcast; TX **raw by default** so `^C`,
  tab completion and arrow keys behave, with `--tx-merge line` as the opt-in that
  keeps concurrent commands atomic instead. POSIX only.
  - ✅ `remote` and `attach` take the mirror flags too (SPEC S33): a remote
    port as a local PTY.
- ✅ **Background sessions** (SPEC S17): `start` detaches (double fork + setsid),
  `status` lists what's running with time-since-last-output, `stop` shuts down in
  order. One `0600` JSON state file per session under `~/.uart-proxy/daemons/` is
  the whole registry; `UART_PROXY_HOME` relocates it. A daemon always serves the
  proxy on loopback with a generated auth code.
- ✅ **`attach` + replay** (SPEC S18): `uart-proxy attach [name]` joins a running
  background session and shows the recent history first, with the stamps from
  when each line actually arrived, then the live tail. History is a bounded ring
  of *events* (`ReplayBuffer`) — bytes would lose the timestamps, which is most of
  the value. Protocol additions are all optional fields (`auth.replay`,
  `auth_ok.replay_available`, `auth_ok.elapsed`, `replay` / `replay_end`), so
  older clients are unaffected. `remote --replay-lines N` works the same way.
  - This settled the open question below: a client now **adopts the server's**
    elapsed origin, because with replay, re-stamping locally makes the elapsed
    column jump backwards where the history ends.
- ✅ **Character input + the `Ctrl-]` command prefix** (SPEC S19): `--input char`
  or `<prefix> c` sends every keystroke as typed, so `^C`, `^D`, Tab completion
  and arrow-key history finally reach the device (none of them did before —
  `Ctrl+C` was Textual's quit and `Ctrl+D` was eaten by the input widget).
  `Ctrl+]` is telnet's escape, chosen for the same reason `screen`'s `Ctrl-A` and
  tmux's `Ctrl-B` are wrong here — both are readline keys a serial console needs.
  `<prefix> d` detaches, `q` quits, `<prefix> <prefix>` sends the literal byte,
  `--prefix` reconfigures. Verified by spike that Textual delivers `ctrl+c` to
  `on_key` and that the app survives it; character mode also stands the app's own
  priority bindings down (`check_action`) so `Ctrl+W` reaches the shell.
  - ✅ Mirror count and dropped bytes in the TUI status bar (`mirrors 2`,
    `dropped 2.0 KB` in red once a reader falls behind).
- ✅ **The terminal view** (SPEC S20): character mode now renders device output
  through a real terminal emulator (`pyte`), so Tab completion, backspace, `\r`
  repaints, ANSI colour, `clear`, `vi` and `htop` all behave. S19 had fixed only
  the input half; output was still a log of finished lines, and the session's
  0.2s idle flush turned every echoed keystroke into a row of its own — `ls`
  arrived as two lines. The screen is the device *now*, the log is the
  timestamped history, `<prefix> c` switches, and both are fed at all times.
  Verified against a real interactive `bash` on a pty by
  [`examples/check_char_mode.py`](./examples/check_char_mode.py).
  - 💡 Offer the terminal view in line mode too (a split, or a toggle
    independent of the input mode) — the emulator already tracks either way.
  - ✅ Tell the far end the window size where the transport can carry it:
    `ssh://` and `telnet://` do (SPEC S35, S36), and a proxy client passes its
    own on (SPEC S38).
- ✅ **`ssh://` ports** (SPEC S35): the system's OpenSSH client in a pty — keys,
  known_hosts and ssh config apply — with the window size passed on, for
  SSH console servers, a UART on another machine, or a BBS.
- ✅ **Port-busy hint** (SPEC S21): when opening the port fails because another
  process holds it, `connect` raises one NOTICE per busy streak naming the holder
  — a background session of ours from its state file (`attach` / its mirrors /
  `stop`), anything else from `lsof` — instead of a bare `Resource busy`.
  `start` refuses a second background session on a port one already holds.
- ✅ **Network ports** (SPEC S34): `--port socket://HOST:PORT` (raw TCP) and
  `rfc2217://HOST:PORT` (Telnet + COM-port control, settings applied remotely)
  make a console server, ser2net or QEMU port the device.
- ✅ **Telnet** (SPEC S36): `telnet://HOST[:PORT]` answers option negotiation
  (ECHO, SGA, BINARY accepted; NAWS and TTYPE offered; the rest refused, never
  re-confirmed), frames IAC and CR NUL, and sends NAWS on a resize.
- ✅ **TUI port picker** (SPEC S27): `connect` without `--port` lists the ports
  in a terminal, and lists-and-exits where nobody can answer.
- ✅ **Scrollback search / filter** (SPEC S28): `Ctrl+] /` shows only matching
  lines, highlighted, live; `Ctrl+W` copies what is shown.
- ✅ **Session header line** in logs (SPEC S25): a `#` banner and closing window
  in the timestamped files; the raw log stays pure.
- ✅ **TX echo over proxy** (SPEC S29): `--echo-tx` forwards typed lines to the
  other clients. Found on the way: with the default `--eol cr` no typed line
  ever completed, so TX lines were missing from the log view and `--log-tx`.
- ✅ **Config profiles** (SPEC S30): `--profile NAME|FILE` reuses `uart_helper`
  profiles — settings where no flag is given, and rules to find the port.

## v0.3 — Security & packaging

- 🟡 **TLS for the proxy** — still plaintext on the wire. The SSH-tunnel guide
  is written (README §5: `-L`, `-R` for a lab behind NAT, `-J`, autossh); TLS
  itself remains.
- ⬜ **Per-role command allow-list** (e.g. a role that may only send specific
  commands).
- ✅ **Auth rate limit** (SPEC S22): 10 failed attempts from one address within
  a minute refuse that address for 10 minutes — per address and time-limited,
  so it cannot be used to lock everyone out.
- ✅ **Find the auth code again** (SPEC S23): `Ctrl+] i` in the TUI, and
  `connect --serve` registered so `status --show-auth` / `attach` work on it.
- ✅ **Connection cap** (SPEC S26): `--max-clients` (16); the next client is
  told to retry, and a refused client (wrong code) stops instead of retrying.
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

- ✅ **Scripts and AI agents** (SPEC S39–S42): the registry on Windows too,
  who-sent-it on TX, `SessionClient` + `tail` / `expect` / `send`, and
  `uart-proxy mcp` — an MCP server that reads a shared session and, with
  `--allow-send` and a full-access code, types into it.
  - ⬜ Ask before each send: a hook the session's owner answers (an app shows
    the bytes, Allow / Deny).
  - 💡 Byte-level tools (hex frames with timing) for binary protocols.
- ⬜ **Plugin discovery via entry points** (pip-installable plugins).
- ⬜ **Richer plugin hooks**: `on_data` (raw), `on_match` with capture groups,
  timers, and the ability to register UI panels.
- 🟡 **Mobile (Flutter) client** (tracked separately) — a thin read-mostly
  consumer of the JSON-lines proxy protocol: connect, authenticate, watch the
  live log. Protocol client + screens implemented and tested; platform
  scaffolding still pending. It talks to this app's proxy over the wire contract
  in [PROTOCOL.md](./PROTOCOL.md), so it needs no changes here.
- ✅ **Replay mode** (SPEC S37): `uart-proxy replay` plays `output.log` back at
  its recorded pace (from `output-timing.log`) through the terminal emulator,
  with pause, seek, speed and the wall-clock time; `--at` / `g` go to a moment
  (elapsed, time of day or date-time); `--no-tui` plays into the terminal like
  `scriptreplay`. Timing rows carry epoch time, so appended runs replay right.
  - 💡 Seeking backwards re-feeds from the start (~1 MB/s): fine for hours of a
    serial console, slow for a very chatty one — keyframes would fix it.

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
- 💡 **A way to reach the open port's fd, or claim it exclusively.** For SPEC S15
  we need `TIOCEXCL` on the fd, and `UARTDevice` keeps its `serial.Serial`
  private with no `fileno()`, so `UartSource._fileno()` reaches for `_serial`
  defensively. Either a public `fileno()` or an `exclusive` flag on `UARTConfig`
  would remove the private-attribute dependency. Note pyserial's own
  `exclusive=True` is just an advisory `flock` and does **not** stop `screen`,
  so the flag would need to issue `TIOCEXCL` to be useful.

## Open questions

- ✅ Default proxy bind: keep `0.0.0.0` or default to `127.0.0.1` and require
  opt-in for LAN exposure? Settled by SPEC S22: keep `0.0.0.0` (serving is for
  other machines) and make the *code* strong instead — no `--auth` now means a
  random one, not `123456` — plus a per-address rate limit, and a startup line
  saying it is reachable from the network.
- ✅ Should remote clients see and replay the wall-clock timestamps from the
  *server*, or re-stamp locally on arrival? Settled by `attach` + replay (SPEC
  S18): replayed history keeps the server's stamps, and the client **adopts the
  server's elapsed origin** (`auth_ok.elapsed`) for live lines, since a local
  origin makes the elapsed column jump backwards where the history ends.
