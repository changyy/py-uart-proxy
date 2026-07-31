# Changelog

## [1.20260731.1204419] — 2026-07-31

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
  - [`examples/check_pty_mirrors.py`](./examples/check_pty_mirrors.py) checks all
    of the above against the **real CLI** with no hardware (a PTY pair stands in
    for the adapter), reporting each guarantee separately and exiting non-zero on
    failure, so it works as a smoke test as well as a demonstration.
- **Exclusive claim on the physical port** (SPEC S15) — `connect` now issues
  `TIOCEXCL`, so a second program opening the same device fails with `EBUSY`
  instead of silently splitting the byte stream with us. `--no-exclusive` opts
  out; `UartSource.is_exclusive` reports what was obtained.
  - The outcome is **announced** as a notice once connected — claimed / could not
    claim / opted out. The claim is best-effort and happens on the connection
    thread after the banner is printed, so staying quiet would let a failed claim
    pass for a protected wire. Re-reported only when the answer changes, so a
    flapping device doesn't repeat it.

### Added
- **Character input mode and a `Ctrl+]` command prefix** (SPEC S19) — `^C`, `^D`,
  Tab completion and arrow-key history now reach the device. None of them did
  before: `Ctrl+C` was Textual's quit and `Ctrl+D` was eaten by the input widget's
  emacs keys, so a runaway command on the target could not be interrupted at all.
  - `--input char`, or `Ctrl+] c`, sends every keystroke as typed. Line mode stays
    the default — it is nicer for typing commands.
  - Since almost every key then belongs to the device, exactly one is reserved.
    `Ctrl+]` is telnet's escape, picked there for this same problem; `screen`'s
    `Ctrl+A` and tmux's `Ctrl+B` are both readline keys a serial console needs
    (line-start, back-one-char), and `Ctrl+Q` is XON. `--prefix` reconfigures it,
    and `Ctrl+] Ctrl+]` sends the literal byte so the choice is never a dead end.
  - `Ctrl+] d` **detaches** — leaves a background session running. In a
    foreground `connect` there is nothing to leave, and it says so.
  - `Ctrl+] ?` lists the commands: `d` `q` `c` `t` `y` `k` `w` `e`.
  - Character mode stands the app's own `priority` bindings down, so `Ctrl+W`
    reaches the shell as kill-word instead of copying the log. The direct
    `Ctrl+T/Y/K/W/E` shortcuts still work in line mode, where they can't conflict.
  - Backspace sends DEL (0x7F), what Unix consoles and readline expect; arrows and
    navigation keys send their ANSI sequences whole.
- **`uart-proxy attach [name]`, with replay** (SPEC S18) — rejoin a background
  session and see what happened while nobody was watching:
  ```
  ── replayed 4 lines · 19:09:52 → 19:09:53 (1.1s) ──
  [2026-07-31 19:09:52 | 00:00:01.5229] [boot 0] initialising subsystem 0
  ── live ──
  [2026-07-31 19:09:55 | 00:00:04.9557] [live] this arrived AFTER attaching
  ```
  - History is kept as **events, not bytes** (`ReplayBuffer`, a bus sink beside
    the recorder so it fills from session start). An event carries the stamp from
    when the line arrived; a byte buffer loses it, and history that appears to
    have happened the moment you attached is worse than no history.
  - Only device output is kept — not your own TX, not notices, not a stale
    `connected` banner. Bounded by `--replay-lines` (default 2000, `0` disables).
  - Replay is **display-only**: the server already assembled, stamped, recorded
    and grepped those lines, so re-injecting them would duplicate all of it.
  - The client **adopts the server's elapsed origin**, so replayed and live output
    share one axis and elapsed means the same as in the daemon's own log files.
  - Host, port and auth come from the session's state file. Several clients can
    attach at once; the daemon keeps the port either way.
  - `remote --replay-lines N` gets the same from a remote server.
- **Protocol** (PROTOCOL.md): `auth` accepts `replay: N`, `auth_ok` reports
  `replay_available` and `elapsed`, and history arrives as `replay` messages
  followed by `replay_end` (always sent, even empty, so a client cannot hang).
  Every field is additive — clients and servers that don't know them are
  unaffected.
- **Background sessions** (SPEC S17) — a serial session should outlive the
  terminal that launched it:
  - `uart-proxy start --port …` detaches (double `fork` + `setsid`) and keeps the
    port, the recording, the mirrors and the proxy running with nobody watching.
  - `uart-proxy status` lists what is running, with uptime and **time since the
    last recorded byte** — usually the thing you actually want to know about a
    session you left alone.
  - `uart-proxy stop [name|--all] [--force]` shuts down in order (`SIGTERM`,
    which is now ordered in every mode — see the fix below).
  - One `0600` JSON file per session under `~/.uart-proxy/daemons/` *is* the
    registry: no index to fall out of step, and a crashed daemon leaves exactly
    one stale file, which the next command prunes. `UART_PROXY_HOME` relocates it.
  - A daemon always serves the proxy — one you cannot reach is useless — bound to
    `127.0.0.1` unless `--listen` says otherwise, with a generated auth code, so
    a client needs no shared secret from you.
  - `--name` names the session **and its mirrors** (`router-0`, `router-1`), so
    one word is the handle for the whole thing.
  - `start` fails if the daemon fails: the child reports readiness over a pipe
    before the parent exits, so a fatal startup error is exit 1 with the reason
    and no state file. An *absent device* is not a failure — waiting for it is
    S12's documented behaviour.
  - `connect` is unchanged: foreground, single process, no daemon.
  - `attach` is **not** in this release; `remote` gives a live view but cannot
    show what happened while you were away. See ROADMAP for the replay work that
    has to come first.

### Changed
- **A client attached over the socket now uses the *server's* elapsed clock**
  rather than starting its own at connect. This settles a question the roadmap had
  left open, and replay forced the answer: with history in the same view, two
  origins make the elapsed column jump backwards where the replay block ends.
  `uart-proxy remote` therefore no longer starts its elapsed axis at zero — it
  shows where the session it joined actually is, matching that server's logs.
- **PTY mirrors are `--tx-merge raw` by default now** (was `line`). A mirror
  stands in for a serial port, and a serial port does not buffer: holding bytes
  until a line ends breaks everything that depends on a keystroke arriving when
  it was typed — `^C`, tab completion, arrow-key history, single-key `y/n`
  prompts, intact escape sequences. Keeping concurrent commands atomic is the
  narrower need, so it became the opt-in.

### Fixed
- **`--listen-port 0` now works for a background session.** The state file
  recorded the *requested* port, so a kernel-assigned one left the daemon
  unreachable — nothing could look up where it had actually bound. `start` now
  rewrites the state file once the proxy is listening, and the banner reports the
  real port.
- **A client asking a history-less server for replay had to wait out its
  timeout.** `replay_end` is now always sent when a client asked, even with
  nothing to send; the timeout is only there to cope with *older* servers, and
  letting it fire against a current one put seconds of dead air into every attach
  to a session started with `--replay-lines 0`.
- **The TUI test suite was 12× slower than it needed to be** — `FakeSource.read`
  returned immediately instead of waiting out its timeout, which turned the
  session's read loop into a busy spin that starved the asyncio loop the Textual
  tests run on. 34.6s → 2.8s for `test_tui.py`; the test double now blocks like a
  real source.
- **A mirror could hand a newly attached tool a pile of stale output.** Device
  output that arrived while nobody was attached stayed queued (up to 1 MiB) and
  went to whichever tool opened the mirror next — so a *program* could read
  minutes-old output and take it for the current state, which is worse than not
  seeing it. A backlog that sees no progress for `--proxy-max-lag` seconds
  (default 5, `0` disables) is now discarded, and the kernel's pty queue is
  flushed with it. Measured: 32,578 B were waiting for a mirror nobody had ever
  opened; a `cat`-style reader received all of it, and now receives none.
  - The rule is "no **progress** for that long", not "the oldest byte is old", so
    a reader that is merely slow but is draining never loses bytes.
  - `screen` (raw mode with `TCSAFLUSH`) and pyserial (`tcflush` on open) hid
    this by accident, which is why the guarantee had to become ours.
  - It also silences the bogus "client not reading" warnings that were being
    logged about a client which had never existed.
- **`--tx-merge line` swallowed `^C` and `^D`.** They have no line terminator, so
  they sat in the buffer until the sender next pressed Enter — by which time an
  interrupt hits whatever is running *then*. A signal delivered late is not slow,
  it is wrong. `^C` (0x03), `^D` (0x04), `^Z` (0x1A) and `^\` (0x1C) now overtake
  the buffer, taking any half-typed line with them in the same write, so order is
  kept and nothing the client sent is dropped. Tab and `ESC` remain content —
  flushing `ESC` alone would split the escape sequence following it.
- **`kill` bypassed the entire shutdown path unless `--proxy-dir` was given**
  (SPEC S16). The `SIGTERM`→`KeyboardInterrupt` trap was installed only when PTY
  mirrors were active — the one case with a visible leak — so `uart-proxy connect
  --serve` sent a plain `kill` died on the spot: proxy clients got no ordered
  close, plugins never stopped, log files were never closed. It is now installed
  for every session, so an ordered shutdown no longer depends on which flags were
  passed. (`kill -9` remains uncatchable; S16 records exactly what it costs —
  only leaked mirror symlinks, which the next start clears.)
- **The "Logs written:" summary never printed.** `Recorder.paths` is derived from
  the open file handles and `close()` drops them, but the CLI read `paths` *after*
  closing, so the list was always empty — dead code since the first release. The
  paths are now captured first.
- **The closing `disconnected` status was invisible** in headless mode: the
  printer was unsubscribed before `session.stop()` published it.
- **The README overstated OS-level exclusivity.** A serial port is *not*
  exclusive by default on POSIX — a second `open()` of the same node succeeds and
  the two processes then split the stream, with nothing reporting it. Windows COM
  ports *are* exclusive. SPEC S15 now carries the behaviour measured on macOS 15,
  including the detail that matters for `--port`: `UartSource` opens the `tty.*`
  node (`PortIdentity.tty_device` rewrites `cu.*`), and with the claim taken
  **both** nodes of the pair return `EBUSY` to everyone else.
- Two claims in those docs were wrong and are corrected: **`screen` does claim
  the line** (measured — a second open while it holds an otherwise-shareable node
  gives `EBUSY`), and the `cu.*`/`tty.*` **dialin/callout interlock already
  refuses the cross-node case** without any claim. So `TIOCEXCL` is not what stops
  `screen`; what it closes is the *same-node* hole — a second `uart-proxy`, a
  `pyserial` script, `cat /dev/tty.X`.

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
