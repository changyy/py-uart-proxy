# uart-proxy — Specification (SDD)

This is the behavioural contract the implementation must satisfy. Each section
has **acceptance criteria** that map to tests in `tests/`. Development is
spec-driven (write/adjust this spec first) and test-driven (encode the criteria
as tests before/with the code). See [README.arch.md](./README.arch.md) for the
design and [ROADMAP.md](./ROADMAP.md) for status.

Requirement IDs (`R1`–`R7`) match the original feature list.

---

## S1. Versioning

- The version follows `1.YYYYmmdd.1HHmmss`.
- It is defined exactly once (`src/uart_proxy/_version.py`) and consumed by both
  the runtime (`uart_proxy.__version__`) and the build backend (pyproject
  dynamic version).
- It is shown in the UI and via `uart-proxy --version`.

**Acceptance**
- `uart_proxy.__version__` matches `importlib.metadata.version("uart-proxy")`.
- The string matches the regex `^1\.\d{8}\.1\d{6}$`.

## S2. Time axes (R2)

- `format_elapsed(seconds)` returns `HH:MM:SS.ffff` (4 decimals, zero-padded).
- A `Stamp` exposes both `wall` (local datetime) and `elapsed` (seconds).
- Wall-clock event time is derived from start + monotonic delta (never goes
  backwards if the system clock changes).

**Acceptance**
- `format_elapsed(10) == "00:00:10.0000"`, `format_elapsed(3661.5) == "01:01:01.5000"`.
- Two stamps taken in order satisfy `s2.elapsed >= s1.elapsed`.

## S3. Line assembly

- Bytes are split into lines on `\n`; a trailing `\r` is stripped.
- Partial data (no newline) is buffered and only emitted on `flush()`.

**Acceptance**
- `feed(b"a\r\nb")` yields `[b"a"]` and leaves `b"b"` pending; `flush()` → `b"b"`.

**TX** (what is typed) uses `cr_ends_line`: a bare `\r`, a `\n`, or a `\r\n` —
even split across two writes — each end exactly one line. Enter on a serial
console is a bare `\r` (`--eol cr`, the default, and every keystroke in
character mode); ending TX lines only on `\n` meant no typed line ever
completed. **RX** keeps `\n` only: a device's bare `\r` is a repaint.

## S4. Recording (R2)

- With recording on, exactly these files are produced from RX traffic:
  - `<base>.log` — raw RX bytes.
  - `<base>-timestamp.log` — `[HH:MM:SS.ffff] line`.
  - `<base>-fulltimestamp.log` — `[YYYY-mm-dd HH:MM:SS | HH:MM:SS.ffff] line`.
- TX lines appear in the timestamped files only when `include_tx` is set, with a
  `>>` marker.
- **Default location:** when `--output-dir` is not given, logs go to a
  per-session folder `~/.uart-proxy/sessions/<YYYYmmdd-HHMMSS>/` so successive
  runs never overwrite each other. `--output-dir` overrides it.

**Acceptance**
- After feeding `b"hello\n"` as RX, the three files exist and contain the raw
  bytes / the elapsed-prefixed line / the wall+elapsed-prefixed line.
- `_resolve_output_dir` returns the given dir when set, else a path under
  `~/.uart-proxy/sessions/`.

## S11. Session retention (auto-cleanup)

- The default session store is pruned along two axes:
  - **age**: delete session folders older than `max_age_days` (default 30);
  - **total size**: if still over `max_total_bytes` (default 500 MB), delete the
    **oldest** folders until under the cap.
- `0` on either axis disables it. The active session is never deleted.
- Precedence for the limits: CLI flag > `~/.uart-proxy/config.toml [retention]`
  > built-in default.
- Pruning runs automatically at session start (default store only) and on
  `uart-proxy sessions --prune`.

**Acceptance**
- A 40-day-old session is removed when `max_age_days=30`; a 5-day-old one stays.
- With three equal sessions and a cap below their sum, the **oldest** are
  removed first until under the cap.
- A path passed via `protect` is never deleted.
- Both axes `0` ⇒ nothing is deleted.

## S12. Auto-reconnect / wait-for-device

- `start()` does not block: it returns immediately and a background manager
  opens the source.
- If the source can't be opened (device absent / no permission), the session
  enters a `waiting` state and retries every `reconnect_interval` seconds; it
  attaches as soon as the device appears. The `waiting` STATUS is emitted on
  the first failed attempt and again only when the error **changes** (absent →
  busy, say) — a wait can last hours, and one line per retry would bury
  everything else. A successful open ends the wait, so the next one is
  reported afresh.
- If the source drops mid-session (read error), it emits `reconnecting`, closes,
  and re-attaches when the device returns.
- `auto_reconnect=False` disables retries (one attempt, then give up). Giving
  up is announced with `disconnected` (`reason: gave up`), exactly once even if
  `stop()` follows — headless mode ends on it, and without it waited forever.
- `write()` while not connected raises (it cannot reach the device).
- A UI ends with the session on `disconnected`, never on `error`: `error` is a
  drop on its way to `reconnecting`. Headless mode once stopped on either, so
  every background session exited on its first unplug.
- Default baud is 115200 (CLI `--baud` optional); the effective baud is shown in
  the status bar via the source description.

**Acceptance**
- A source whose first N `open()`s fail eventually connects and streams data.
- A connected session that hits a read error re-connects and resumes streaming.
- With `auto_reconnect=False`, a failing open never connects, and one
  `disconnected` is emitted; `connect --no-tui --no-reconnect` on an absent
  device exits.
- Ten failed opens with the same error produce one `waiting`; errors
  absent, absent, busy, busy, absent produce three; a drop and a new wait
  produce a second.
- `write()` on a disconnected session raises `RuntimeError`.

## S13. Copying log text (TUI)

Two paths, because terminal-native selection copies *screen cells* (which would
include a box border and padding):

- **`Ctrl+W` — copy whole log (clean).** Copies the in-memory log to the
  clipboard as plain text (no border, no padding, no markup) via
  `app.copy_to_clipboard` (OSC-52). The app keeps a plain-text mirror of every
  rendered line for this.
- **`Ctrl+E` — Select Mode (range).** Freezes the view (auto-follow off) and
  hands the mouse back to the terminal so its native drag-select + copy work;
  toggling again restores mouse capture and following.
- The log widget has **no border**, so terminal selection doesn't pick up frame
  characters. Both toggles are **priority** bindings (work while the input is
  focused).
- **`Ctrl+K`** clears the display **and** the copy buffer, so it resets the
  range `Ctrl+W` copies (clear → accumulate → copy just the new range).

**Acceptance**
- After RX lines arrive, `Ctrl+W` puts them on the clipboard with no `│` and no
  multi-space padding runs.
- `Ctrl+E` sets select mode and freezes `auto_scroll`; pressing it again clears
  select mode and restores `auto_scroll`.
- After `Ctrl+K`, the copy buffer is empty; newly arriving lines form a fresh
  copy range.

## S5. Session pipeline (R1, R6)

- A `UartSession` drives any `DataSource`. On RX it publishes a `DATA(RX)` event
  and one `LINE(RX)` event per completed line.
- `write()` / `send_text()` publish the mirror `DATA(TX)` / `LINE(TX)` events and
  return the number of bytes written.
- `send_text` appends the configured EOL.
- A partial RX line is flushed after a short idle period.

**Acceptance**
- Feeding `b"one\ntwo\n"` produces two `LINE(RX)` events with text `one`, `two`.
- `send_text("AT")` with `eol=crlf` writes `b"AT\r\n"` to the source.

## S6. Socket proxy (R4, R6)

- The wire protocol is one JSON object per line, UTF-8, `\n`-terminated.
- A client must authenticate first: `{"type":"auth","code":...}`.
- An unknown code gets `{"type":"auth_fail"}` and is disconnected.
- A valid code gets `{"type":"auth_ok","role":...}` where role ∈ {full, readonly}.
- After auth the server forwards `rx` / `notice` / `status` messages.
- A `full` client's `tx` is written to the session; a `readonly` client's `tx`
  is rejected (never reaches the device).
- `parse_auth_spec("CODE")` → `(CODE, full)`; `"CODE:readonly"` → `(CODE, readonly)`.

**Acceptance**
- A `SocketSource` authenticating with a valid full code connects and receives
  device RX bytes reconstructed from `rx` messages.
- A `readonly` `SocketSource.write(...)` raises and the device receives nothing.
- A bad code raises on connect.

## S7. Plugins (R7)

- A plugin is a `Plugin` subclass; `on_line(direction, line, stamp)` is called
  for every assembled line.
- The built-in `grep` plugin emits a notice for each RX line matching any
  configured pattern and keeps per-pattern counts.
- Plugin exceptions are isolated and never stop the session.
- User plugins load from a `.py` file or a directory.

**Acceptance**
- Grep configured with `["ERROR"]` emits exactly one notice for an `ERROR` line
  and none for a clean line.
- A plugin that raises in `on_line` does not prevent other subscribers from
  receiving the event.

## S8. CLI (R5)

- `ports` lists serial ports (text and `--json`).
- `connect --port …` opens a local UART; `remote --host … --auth …` attaches to
  a proxy.
- `--no-tui` streams headlessly; otherwise the Textual TUI launches.
- `--serve` exposes the session via the proxy with `--auth CODE[:role]` entries.

**Acceptance**
- `ports --json` emits valid JSON with a `data` array.
- The argument parser accepts the documented flags for each subcommand.

## S9. ASCII / BBS display (R3)

- `--encoding` controls text decoding (e.g. `latin-1` for BBS/8-bit).
- `--eol` controls the line ending appended to sent text
  (`crlf`/`lf`/`cr`/`none`).
- A hex view is available in the TUI.

**Acceptance**
- With `encoding="latin-1"`, bytes `0x80..0xFF` decode to single characters
  without error.

## S10. Mouse / scrollback follow-tail (TUI)

- The TUI log responds to the mouse wheel for scrolling.
- By default the log **follows the tail** (auto-scrolls as new lines arrive).
- When the user scrolls **up** with the wheel, auto-follow **pauses** so they
  can read history without being yanked back to the bottom.
- When the user scrolls back to the **bottom**, auto-follow **resumes**.
- A key (`End`) jumps to the bottom and resumes following.
- The status bar shows the current mode (`follow` vs `paused`).

**Acceptance**
- A fresh log has `auto_scroll` (follow) enabled.
- Simulating a mouse-scroll-up disables follow; `jump_to_bottom()` (or
  reaching the bottom) re-enables it.

## S14. Local PTY mirrors (sharing one port)

uart-proxy holds the physical port (S15), so nothing else on the machine can
reach the device. Mirrors are the way back in: N **full-duplex PTYs**, each
symlinked into a directory, that any serial-capable tool can open.

- Enabled by `--proxy-dir [DIR]` (default `/tmp/uart-proxy`) and/or repeated
  `--proxy PATH`. Off unless asked for. `--proxy-count N` sets how many
  auto-named mirrors (default **2**); `--proxy` alone creates only the paths
  given.
- Each mirror is **read *and* write**, so `--proxy-count 2` is 2 readers *and*
  2 writers. Links are named `<stem>-<i>` where `<stem>` is the device basename
  with a `cu.`/`tty.` prefix stripped (`/dev/cu.usbserial-110` → `usbserial-110-0`).
- **RX is broadcast**: every mirror receives the whole device output stream.
- **RX only** — a mirror never sees another mirror's TX, nor assembled LINE
  events. (Real consoles echo, so a human still sees an agent's command; staying
  transparent is what lets an unmodified `screen` attach.)
- **TX is merged** onto the one wire, `--tx-merge raw` **by default**: every byte
  crosses the instant it arrives. That is what a serial port is and what an
  attached tool assumes — `^C` interrupts *now*, tab completion completes, arrow
  keys reach the shell's history, a single-key `y/n` prompt answers, escape
  sequences stay whole.
- `--tx-merge line` is the opt-in trade: each mirror's bytes are held until `\n`
  or `\r` and the whole line is forwarded in one write, so concurrent writers
  cannot splice one command into another; a line that never terminates is flushed
  at 4096 B. It costs every interactive behaviour above, so it is for several
  *unattended* writers sharing a wire, where a mangled command is worse than a
  laggy one.
- Even in `line` mode, **`SIGNAL_BYTES` are never held**: `^C` (0x03), `^D`
  (0x04), `^Z` (0x1A) and `^\` (0x1C) are asynchronous signals, not content, and
  one delivered late is not slow but *wrong* — it interrupts whatever happens to
  be running by then. A signal flushes the half-typed line ahead of it in the
  same write, so order is kept and nothing the client sent is discarded (the
  device's own line discipline cancels the abandoned command). Tab and `ESC`
  stay content — flushing `ESC` alone would split the escape sequence that its
  following bytes belong to.
- Mirror TX goes through `session.write`, so it appears in the TUI and the logs
  as ordinary TX, and is refused (reported, not fatal) while disconnected.
- A mirror whose client stops reading is **dropped from, not blocked on**: past
  1 MiB of backlog its bytes are discarded and counted, so one stalled reader
  can neither stall the device read thread nor starve the other mirrors.
- **A mirror is a live view, never a backlog.** A backlog that sees no progress
  for `--proxy-max-lag` seconds (default 5; `0` disables) is discarded, and the
  kernel's own pty queue is flushed with it. Otherwise output that arrived while
  nobody was attached is handed to whichever tool opens the mirror next, and a
  *program* reads minutes-old output as the current state — worse than not
  seeing it. History belongs to the recorder's logs and to `attach`, which can
  present it with its original timestamps; a raw byte pipe cannot.
  - The rule is "no **progress** for that long", not "the oldest byte is that
    old": a reader that is merely slow but *is* draining must never lose bytes,
    and only progress distinguishes it from nobody being there.
  - The flush must be the **slave's input** queue (`TCIFLUSH`) — that is where
    bytes written to the master wait. `TCOFLUSH` on the master does nothing
    (measured), and `TCIOFLUSH` would also discard TX a client just wrote.
  - Common tools hide this by accident — `screen` sets raw mode with
    `TCSAFLUSH`, pyserial calls `tcflush` on open — so the guarantee has to be
    ours, not theirs.
- Startup replaces a **stale symlink** from a killed run; a path that exists and
  is *not* a symlink is refused, never deleted. `SIGTERM` as well as `Ctrl-C`
  removes the symlinks.
- POSIX only (no `pty` on Windows); `--proxy-dir` there is an error pointing at
  `--serve`.
- **Not** provided: per-writer request/response routing. A UART is one unframed
  byte stream, so which reply belongs to which writer is not answerable at this
  layer — `line` merge keeps commands intact, correlation is the caller's job.

**Acceptance**
- Starting a group creates one symlink per mirror, each pointing at a `/dev`
  PTY a client can open; stopping it removes them.
- A `DATA(RX)` event published on the bus is received by every attached client;
  a `DATA(TX)` or `LINE(RX)` event is received by none.
- Bytes written by a client arrive at `on_tx`; with `line` merge, two clients
  writing `reb`/`who` then `oot\n`/`ami\n` produce exactly `reboot\n` and
  `whoami\n`, and nothing is forwarded while both lines are partial.
- The default merge mode is `raw`, and a lone `a` is forwarded under it.
- With `raw` merge, `no newline here` is forwarded without a terminator.
- With `line` merge, each of `^C` / `^D` / `^Z` / `^\` sent alone is forwarded
  immediately; sent after `reboo` it produces exactly one write of `reboo\x03`;
  and `ls\t\x1b[A` is still held until a terminator arrives.
- A client that never reads records a non-zero `dropped` count while another
  client continues to receive.
- With a short `max_lag`, output queued while nobody is attached records a
  non-zero `stale` count, leaves `pending` at 0, and a reader that flushes
  nothing on open receives **no** bytes — while output arriving afterwards still
  gets through. A client that reads slowly but steadily for several times the
  window records `stale == 0`. `max_lag=0` keeps the backlog.
- `on_tx` raising (disconnected device) emits a notice and the group keeps
  serving; a later write succeeds.
- A dangling symlink at a mirror path is replaced; a regular file there raises
  `FileExistsError` and is left intact.
- End to end, against the real CLI and needing no hardware:
  `python examples/check_pty_mirrors.py` exits 0 (a PTY pair stands in for the
  adapter; it also covers the startup banner, the `-0`/`-1` naming, and SIGTERM
  cleanup, which the unit tests reach only from the inside).

## S15. Exclusive claim on the physical port

- `connect` claims the port **exclusively** by default, via `TIOCEXCL` on the
  open fd: every later `open()` of that device path fails with `EBUSY`, so a
  second program cannot start splitting the byte stream with us. Sharing is done
  through mirrors (S14) or the socket proxy (S6), never by two opens of one wire.
- `--no-exclusive` opts out.
- Best-effort by design: Windows COM ports are already exclusive-open, and a
  failure to claim (odd platform, non-tty, a future `uart_helper` that hides its
  port object) is logged and the session continues. `UartSource.is_exclusive`
  reports what was actually obtained.
- **The outcome is announced.** Because the claim is best-effort, and because it
  happens on the session's connection thread *after* the CLI has printed its
  banner, `connect` emits a NOTICE once connected saying which of the three
  things happened — claimed / could not claim / opted out with
  `--no-exclusive`. Silence would let a failed claim pass for a protected wire.
  It re-reports only when the answer changes, so a flapping device doesn't fill
  the log with one line.
- This is deliberately **not** pyserial's `exclusive=True`, which takes an
  advisory `flock` — that only stops programs which also `flock`.

**Measured POSIX behaviour** (macOS 15, `open(2)` on a serial node, `O_NONBLOCK`),
which is what the claim is for. Note `UartSource` opens the **`tty.*`** node —
`PortIdentity.tty_device` rewrites a `cu.*` argument — so the first row is ours:

| holder | 2nd open, same node | open of the paired node |
|--------|---------------------|-------------------------|
| `tty.X`, no claim | **succeeds** — streams silently split | `cu.X` → `EBUSY` |
| `tty.X` + `TIOCEXCL` | `EBUSY` | `cu.X` → `EBUSY` |
| `cu.X`, no claim | **succeeds** | `tty.X` → `EBUSY` |
| `cu.X` + `TIOCEXCL` | `EBUSY` | — |

So the dialin/callout interlock already covers the *cross*-node case for free;
`TIOCEXCL` is what closes the **same-node** hole (a second `uart-proxy`, a
`pyserial` script, `cat /dev/tty.X`). `screen` and `minicom` claim the line
themselves and were never the threat; unclaimed readers are.

**Acceptance**
- `seize_exclusive` returns True for a tty fd and False (no raise) for a pipe.
- `UartSource.open()` claims the port's own fd by default; `exclusive=False`
  claims nothing; `close()` clears `is_exclusive`.
- An unreachable port object leaves `open()` working and `is_exclusive` False.
- On `connected`, exactly one NOTICE names the outcome: a claim says
  `claimed <path> (TIOCEXCL)`; a failed claim says `COULD NOT claim`;
  `--no-exclusive` says so instead of reporting a failure. Nothing is said on
  `waiting` / `reconnecting` / `error`. Five `connected` events in a row produce
  one notice; a changed outcome produces a second.
- **Not coverable in CI** — the pty driver ignores `TIOCEXCL` (the ioctl
  succeeds, a second open still works), so kernel enforcement needs a real tty.
  Verified by hand on macOS 15 against a PL2303 adapter and a spare
  `Bluetooth-Incoming-Port` node, producing the table above. To re-check after a
  macOS or driver update, `python examples/check_hardware.py DEV` does all of
  this (and S21, S23, S30, S31) against any adapter; by hand: `uart-proxy connect --port /dev/cu.usbserial-110`, then
  `python3 -c "import serial; serial.Serial('/dev/cu.usbserial-110')"` must raise
  `Resource busy` (errno 16) while `screen /tmp/uart-proxy/usbserial-110-0`
  attaches.

## S16. Shutdown & termination signals

A session holds things that outlive the process if it dies unceremoniously —
mirror symlinks on disk, proxy client sockets, open log files.

- **`SIGINT` (Ctrl-C)** — ordered shutdown: mirrors → proxy → plugins → session →
  recorder, then the written log files are listed.
- **`SIGTERM` (`kill`)** — the same ordered shutdown. It is turned into
  `KeyboardInterrupt` so the `finally` path runs, **in every mode**: not only when
  `--proxy-dir` made the leak visible, because otherwise an ordered shutdown
  would depend on which flags were passed, and each newly added resource would
  have to remember to opt in.
- **`SIGKILL` (`kill -9`)** — uncatchable; nothing runs, by definition. What that
  costs, measured:
  - Log files keep everything, because the recorder flushes on every write.
  - The serial port is released by the kernel, and the `TIOCEXCL` claim dies with
    the fd — a killed run never leaves the device locked.
  - The proxy's listening socket and every PTY fd are released by the kernel.
  - **Mirror symlinks leak** as dangling links. The next start replaces them
    (S14), but until then a tool that opens one gets `ENOENT` — and since
    `/dev/ttysNNN` names are recycled, a stale link could later resolve to an
    unrelated live PTY. Cleaning at startup is what bounds that.
- Recorder note: `Recorder.paths` is derived from the open file handles, so it
  must be read **before** `close()`. Reading it after yields `[]`.

**Acceptance**
- `_trap_sigterm()` installs a handler that raises `KeyboardInterrupt`, and its
  returned callable restores the previous handler.
- `connect` sent `SIGTERM` exits with a non-negative status (it unwound rather
  than being killed) and reports the log files it wrote — with **and** without
  `--serve`.
- `close_recorder()` returns the three written paths; `Recorder.paths` is empty
  after `close()`.
- A stale mirror symlink from a killed run is replaced on the next start (S14).

## S17. Background sessions (`start` / `status` / `stop`)

A serial session is long-lived, so it must be able to outlive the terminal that
launched it. That requires the engine to be a **process of its own** with any UI
as a client — the same split tmux makes between its server and `tmux attach`; an
in-process engine cannot be detached from, because an open port and its threads
cannot be handed to another process mid-flight.

- **`uart-proxy start --port …`** detaches (double `fork` + `setsid`) and returns.
  `connect` is unchanged and stays foreground/in-process: a leftover daemon
  invisibly holding the port (S15) is a bad default to impose on the simple case,
  so detaching is explicit.
- A daemon **always serves the proxy**, on `127.0.0.1` unless `--listen` says
  otherwise, with a **generated auth code** — a background session nothing can
  reach is useless, and one listening on every interface is not what "in the
  background" means.
- **Identity** is a name, defaulting to the device stem (`usbserial-110`), the
  same rule the mirrors use. `--name` overrides it and **also names the mirrors**,
  so `--name router` gives `router-0`, `router-1` — one handle for the session.
  Starting a second session under a live name is refused, pointing at `stop`.
- **State** is one JSON file per daemon at `~/.uart-proxy/daemons/<name>.json`
  (`0600`, since it holds the auth code; directory `0700`). The files *are* the
  registry — no index to fall out of step, and a crashed daemon leaves exactly
  one stale file. `UART_PROXY_HOME` relocates the root.
- Unknown keys in a state file are ignored, so a file written by another version
  never makes a running session unlistable; an unparseable file is skipped.
- **Liveness** is the recorded pid answering signal 0. `status` and `stop` prune
  dead entries first, which is what clears up after `kill -9` (S16).
- **`start` fails when the daemon fails.** The child reports readiness over a
  pipe before the parent exits, so a fatal startup error (e.g. `--proxy` pointing
  at a real file) is exit 1 with the reason and no state file — not a success
  with a corpse behind it. Readiness means *serving*, so an **absent device is
  not** a failure: waiting for it is S12's documented behaviour.
- **`stop`** sends `SIGTERM`, which is an ordered shutdown in every mode (S16);
  `--force` escalates to `SIGKILL` and strands the symlinks. `--all` stops
  everything.
- The daemon's stdout is discarded (the traffic belongs in the recorder's logs,
  not a second copy); notices and status go to **stderr**, captured in
  `<log_dir>/daemon.log` — the startup banner, the exclusivity report (S15) and
  any warnings.

**Acceptance**
- A state file round-trips; it is `0600` in a `0700` directory and contains the
  auth code; a file with unknown keys still loads; an unparseable one is skipped.
- Generated auth codes are unique across 50 draws and at least 16 chars.
- A live pid reads as running, a reaped one does not; `prune_dead()` removes only
  the dead and reports what it removed.
- With one session running, no name is needed; with two, resolving without a name
  fails and names both; an unknown name lists what exists; with none, the error
  suggests `start`.
- End to end: `start` against a PTY exits 0, the session is a different process,
  it records with nobody attached, its mirrors are named after the session,
  `status --json` reports it, and `stop` ends it and clears the symlinks.
- A second `start` under a live name exits 1 saying "already running".
- `start` with `--proxy` pointing at a regular file exits 1, leaves no state
  file, and does not touch the file.
- `start` on an absent device exits 0 and the session is alive.
- `status` with nothing running exits 0 and suggests `start`; `stop` with nothing
  running exits 1 and says so.

## S18. Attach, and replaying what you missed

A background session keeps running with nobody watching, so attaching to a
live-only stream is close to useless: a serial console is often quiet, and a
blank screen cannot be told apart from a broken connection.

- **`uart-proxy attach [name]`** connects to a running session (S17) as a client,
  reading host, port and auth code from its state file. The daemon keeps the port
  (S15), so attaching is *always* by protocol — which is also why several clients
  can attach at once, each with its own view.
- It connects **eagerly**, before the first frame: the history has to be in hand
  to draw, and an unreachable session should be an error you see immediately
  rather than a view stuck in "waiting". `SocketSource.open()` is therefore
  idempotent, since the session's own manager calls it again.
- The client does **not** record: the daemon is already writing this session's
  logs, and a second copy would only duplicate them.
- **History is kept as events, not bytes** (`ReplayBuffer`, a bus sink alongside
  the recorder, so it fills from session start rather than from whenever a client
  connects). An event carries the `Stamp` from when the line actually arrived; a
  byte buffer would lose it and every replayed line would appear to have happened
  the moment you attached — history that lies about when it happened is worse
  than no history.
- Only `LINE(RX)` is kept: not TX (the operator's own past keystrokes), not
  `DATA` (it would duplicate the lines), not notices or status (a stale
  `connected` banner would mislead). Bounded by `--replay-lines` (default 2000,
  `0` disables) — nobody wants two hours of boot log poured into a fresh view,
  and the complete record is on disk.
- Replay is **display-only**: it never enters the client's session. Those lines
  were already assembled, stamped, recorded and passed to plugins on the server,
  so re-injecting them would re-stamp them, duplicate them into the client's log,
  and fire every grep rule again on output from an hour ago.
- The client **adopts the server's elapsed origin** (`auth_ok.elapsed` →
  `TimestampTracker.rebase`), so replayed and live output share one axis and a
  line's elapsed means the same as in the server's own logs. This settles the
  question the roadmap left open — with replay, re-stamping locally is simply
  wrong.
- A UI must show replayed lines distinctly; uart-proxy dims them between
  `── replayed <n> lines · <from> → <to> (<span>) ──` and `── live ──`.
- Wire format in [PROTOCOL.md](./PROTOCOL.md): `auth` gains `replay: N`,
  `auth_ok` gains `replay_available` and `elapsed`, and history arrives as
  `replay` messages followed by `replay_end`. All additive — a client or server
  that doesn't know them behaves exactly as before.

**Acceptance**
- The buffer keeps assembled RX lines with their original stamps, and keeps
  nothing else (TX / DATA / notice / status); it is bounded, keeps the newest,
  honours a smaller request, and `0` disables it.
- A `ReplayEntry` survives a round trip through its wire form.
- `rebase()` adopts another session's clock, time still moves forwards after it,
  and a negative value is ignored.
- Over a real socket: a client that asks receives the history **before** any live
  traffic, with the server's own stamps, then the live stream; a client that does
  not ask receives none (but is still told what was available); a smaller request
  is honoured; a server with no history sends an empty block rather than hanging;
  the client learns the server's elapsed; and calling `open()` twice neither
  reconnects nor loses the replay.
- End to end: with a daemon running and a line arriving while nobody is attached,
  `attach --no-tui` prints a `── replayed` block containing that line, then
  `── live ──`, then output that arrived afterwards — in that order.
- `attach` with no session running exits 1 saying so.

## S19. Character input, and the command prefix

Line input is comfortable for typing commands but cannot express what a shell
needs *now*: `^C` to interrupt, `^D` for EOF, Tab to complete, ↑ for history.
Before this, none of those reached the device at all — `Ctrl+C` was Textual's quit
and `Ctrl+D` was eaten by the input widget's emacs keys.

- **Character mode** (`--input char`, or `<prefix> c`) sends every keystroke as
  it is pressed. Line mode stays the default.
- It requires two things that are easy to get wrong, both verified by test:
  - **Focus must leave the Input widget**, because a focused Input swallows
    printable keys and they never bubble to `on_key`.
  - The app's own `priority=True` bindings must **stand down** (`check_action`),
    or `Ctrl+W` would still be "copy log" instead of the shell's kill-word —
    stealing keys from the device is the very thing this fixes.
  - **The footer must stand down with them.** `Ctrl+Q` is XON and `End` is an
    escape sequence, so both belong to the device here; a footer still offering
    "Quit" is worse than useless, because pressing it does nothing visible and
    leaves you believing you quit while the session runs on **holding the port
    exclusively**. In character mode the quit key is `<prefix> q`, and the status
    bar says so — it is then the only thing on screen that does.
- **One key is reserved**, the command prefix, `Ctrl+]` by default. Not `screen`'s
  `Ctrl+A` or tmux's `Ctrl+B`: on a serial console the far end is usually a shell,
  where those are line-start and back-one-character. `Ctrl+]` is telnet's escape,
  chosen there for the same reason, and it means nothing to flow control either
  (unlike `Ctrl+Q` = XON). `--prefix` reconfigures it and rejects anything
  unusable rather than silently picking something else.
- `<prefix> <prefix>` sends the **literal** byte, so the choice of prefix is never
  a dead end.
- Commands: `d` detach · `q` quit · `c` switch mode · `t` timestamps · `y` hex ·
  `k` clear · `w` copy · `e` select · `?` help. Anything else says so rather than
  being sent to the device.
- **`<prefix> d` detaches** — leaves the UI while the session carries on. Only
  meaningful when there *is* something to leave: a foreground `connect` runs the
  engine in this process, so there it explains that instead of pretending.
- The direct `Ctrl+T/Y/K/W/E` bindings keep working **in line mode**, where they
  cannot conflict, so existing habits are unaffected.
- Key-to-byte mapping (`ui/keymap.py`): Enter sends the configured EOL,
  **Backspace sends DEL (0x7F)** — what Unix consoles and readline expect; BS
  shows as `^H` on many devices, which is why PuTTY and minicom default the same
  way — arrows and navigation keys send their ANSI sequences whole, `ctrl+<letter>`
  sends the control byte, and a key with nothing sensible to send sends nothing.

**Acceptance**
- Every documented key maps to the byte a terminal would send; Enter follows
  `--eol`; Backspace is DEL by default and BS on request; a key with no mapping
  yields `None`; non-ASCII uses the session encoding.
- `--prefix` accepts `ctrl+]`, `ctrl-]`, `^]`, `CTRL+]` and Textual's own name,
  and raises on `]`, `a`, `alt+x`, `ctrl+f13`, empty.
- Through the Textual harness: a printable key reaches the device in character
  mode; **`Ctrl+C` reaches the device and does not quit the app**; `Ctrl+D` and
  arrows reach it; line mode forwards nothing until Enter, then the line plus EOL.
- The prefix alone is not sent and sets the awaiting state; a command after it is
  not sent either; an unknown command reports itself; `<prefix> <prefix>` sends
  `0x1D`; `<prefix> c` switches modes both ways; a custom prefix takes effect.
- In character mode `Ctrl+W/T/Y/K/E` arrive at the device as
  `\x17\x14\x19\x0b\x05`; in line mode `Ctrl+T` still cycles timestamps and
  sends nothing.
- `Ctrl+Q` reaches the device as `\x11` in character mode without quitting, and
  `End` as `\x1b[F`; the footer offers neither there, and offers `Ctrl+Q` in
  line mode.
- `<prefix> d` sets the detached state when detachable, and when not, leaves the
  app running and says there is nothing to detach from.

## S20. The terminal view (character mode's other half)

S19 fixed *input*: every keystroke reaches the device. It left *output* as it
was — a log of finished lines — and that turned out to be only half a terminal.

The two models are incompatible by construction. A log appends: a row, once
written, is final. A shell talks to a **screen**: it echoes a character where the
cursor is, takes it back with `BS`, repaints the row from column 0 with `CR`,
addresses a region with an ANSI sequence. None of that can be expressed by
appending a row.

The symptom was concrete and made the feature look broken. The session
force-flushes a partial RX line after `_IDLE_FLUSH` (0.2 s) so a prompt without a
newline (`login: `) appears at all. At human typing speed *every echoed keystroke*
cleared that timer on its own, so each character was flushed as its own "line":
typing `ls` produced two rows. Switching to character mode made the device
reachable and the output unreadable.

So character mode renders through a real terminal emulator (`pyte`), and the
view switches with the mode:

```
RX bytes ─┬─> LineAssembler ──> LINE events ──> log view, recorder,
          │                                     plugins, proxy clients
          └─> pyte.Screen ────────────────────> terminal view
```

- **The terminal view is the device's screen now** — colour, cursor, in-place
  redraws, `clear`, `vi`, `htop`. It has no scrollback, because it is a screen.
- **The log view is the history** — every line that ever arrived, timestamped.
  `<prefix> c` switches between them at any time.
- **Both are fed at all times**, including the hidden one. A view that only began
  tracking when you looked at it would open blank, which is exactly the moment
  you need it. Measured cost of feeding `pyte`: 1.19 MB/s, i.e. ~1% of a core at
  115200 baud and ~9% at 921600 — bounded, and worth it for a view that is
  correct the instant it appears.
- **Redrawing is skipped unless something moved**, cursor included: moving the
  cursor need not dirty a line, and a cursor drawn in the wrong place is very
  visible.
- `<prefix> k` **clears the screen, not the history** — that is what `clear`
  means at a prompt, and the log behind it is the record.
- Notes (`<prefix> ?`, mode changes) are written to the log as always, *and*
  raised as a toast while the log is hidden behind the screen — one toast per
  block, not one per line.

Three things it deliberately does not do:

- **TX is not echoed into the screen.** The far end echoes what you type, which
  is why your keystrokes appear at all. Drawing them locally as well would double
  every character, and would show input that a device with echo off — a password
  prompt — is deliberately hiding.
- **The device is never told the window size.** RS-232 has no `SIGWINCH` and no
  in-band way to send one, so the far end keeps whatever size it assumed and a
  full-screen program paints to *that*. `screen` over serial has the same
  limitation. The status bar therefore reports the emulated size (`screen
  135×34`) so the value to set on the device (`stty rows 34 cols 135`) is never a
  guess.
- **No scrollback in the terminal view.** The log already holds all of it, with
  timestamps, one keystroke away.

Two details that are easy to get wrong, both pinned by test:

- **`pyte`'s colour names are not Rich's.** ANSI 33 is `brown` to pyte and
  `yellow` to Rich, which rejects `brown` outright; the bright variants lack
  Rich's underscore; and `BG_AIXTERM[105]` is misspelled `bfightmagenta` in pyte
  0.8.2. An unmapped name is an exception raised because a device printed in
  colour, so the mapping is exhaustive over pyte's own tables.
- **Shrinking a `pyte` screen clips from the top.** Right for a live terminal,
  wrong for one squashed while hidden — a hidden widget reports 0×0, so the
  screen would be crushed to a cell and come back with its opening rows gone. The
  hidden screen is kept at the size of the region it will be drawn in (the log
  occupies the same slot), and degenerate sizes are ignored rather than applied.

**Acceptance**
- Keystrokes echoed one at a time, slower than the idle flush, land side by side
  on one row; the second row stays empty.
- `BS` edits in place; `CR` repaints the row; `ESC [ 2J` + `ESC [ H` repaints the
  screen; a double-width character does not shift the rest of its row.
- Colour reaches the rendered text; every name in pyte's four colour tables
  translates to one Rich accepts.
- Nothing is redrawn when nothing moved; moving the cursor alone still counts.
- Malformed escape sequences and invalid bytes never raise.
- Through the Textual harness: the view follows the mode both ways; the screen
  already holds what arrived while the log was showing; the log still holds what
  arrived while the screen was showing; `<prefix> k` clears the screen and keeps
  the log; the emulated size matches the widget; `<prefix> ?` is raised as one
  toast when the log is hidden.
- Against a **real interactive `bash`** on a pty
  ([`examples/check_char_mode.py`](./examples/check_char_mode.py)): Tab completes
  a filename in place, the typed command occupies one row rather than one per
  character, `^C` abandons the line, colour survives, and ↑ recalls from the
  shell's own history.
- Character mode hides the (disabled) input box, and the screen takes its rows;
  line mode shows it again.

## S21. When the port is busy, say who has it

A serial port is exclusive-open, and ours doubly so (S15). Since background
sessions (S17) the likeliest holder of a busy port is **our own daemon**, started
and forgotten — and all the session reported was `waiting` with
`[Errno 16] Resource busy`, once per `--reconnect-interval`. True, and no help:
the fix is almost always one command away.

- **Recognising busy.** `UartSource.open()` sets `busy` when the open failed
  because another process holds the port. `uart_helper` re-raises pyserial's
  error as a `UARTError`, so `is_busy_error` walks the cause chain for
  `errno == EBUSY` (or its text). On Windows a COM port held elsewhere fails
  with "Access is denied", which `uart_helper` files as a permission error; a COM
  port has no permission bits, so there it counts as busy. An absent device or a
  real permission problem is not busy.
- **Naming the holder**, best-effort, in this order:
  1. a **registered background session** — its pid among `lsof`'s holders, or,
     when `lsof` is missing or cannot see the pid, a live state file whose
     `port` is this device. The hint gives `uart-proxy attach <name>`, its
     `proxy_dir` if it has mirrors, and `uart-proxy stop <name>`;
  2. any other process `lsof` reports (`screen (pid 777)`), with the ways to
     share on purpose: `--proxy-dir`, or `--serve` and `uart-proxy remote`;
  3. nobody known — the same alternatives, without a name.
- **`cu.X` and `tty.X` are one port** for all of this: holding either makes the
  other `EBUSY` (S15's table), so both nodes are asked about and matched.
- **Once per streak.** The NOTICE is raised on the first busy `waiting` after
  start or after a successful open, not on every retry — `lsof` walks every open
  file on the machine, and the answer does not change each second. `lsof` is
  given 2 s; a late hint is worth less than none.
- **`start` refuses a held port** before detaching, naming the session that has
  it. A second daemon there could only wait forever, or — if the first had
  taken `--no-exclusive` — split the stream with it.

**Acceptance**
- `EBUSY` is recognised wrapped in `UARTError`, and bare; `ENOENT` and a POSIX
  permission error are not; "Access is denied" is, on Windows.
- `paired_nodes` / `same_device` treat `/dev/cu.X` and `/dev/tty.X` as one port
  and leave other paths alone.
- A holder whose pid is a registered session gets `attach` / mirrors / `stop`;
  with no `lsof` result the registry still answers; a session on *another* port
  is never blamed; an unknown holder still gets the alternatives.
- `find_holders` names a real process holding a pty open, never ourselves, and
  returns `[]` without `lsof` or for a missing node.
- Five busy `waiting` events produce one NOTICE and one lookup; `connected` ends
  the streak; waiting for an absent device raises none.
- `start` on a port a live state file holds exits 1 naming it.
- **By hand**, since the pty driver ignores `TIOCEXCL`: on macOS 15 with
  `/dev/tty.Bluetooth-Incoming-Port` held under `TIOCEXCL` by a pyserial script,
  `connect` named `Python (pid …)`; with it held by `start --name bt`, a
  second `start` was refused and `connect` pointed at `uart-proxy attach bt` and
  the mirror directory.

## S22. No guessable default code; guessing is rate-limited

`--serve` without `--auth` used to install a fixed `123456` with full access,
and `--serve` binds every interface by default — a well-known password on the
LAN. Binding loopback by default was considered and rejected: the point of
`--serve` is to let *other* machines in, so that default would be switched off
every time it was used. The code is what has to be strong.

- **No `--auth` → a random code**, `secrets.token_hex(8)` (the same generator
  `start` uses), fresh each run, full access, printed at startup. Any `--auth`
  given is used exactly as given, and nothing is generated.
- **Listening on every interface is said out loud** at startup, with the flag
  that keeps it local.
- **Failed attempts are counted per address**: `MAX_AUTH_FAILURES` (10) within
  `FAIL_WINDOW` (60 s) and that address is refused for `BAN_SECONDS` (600 s).
  A refused address gets `auth_fail` with the time left, straight after
  connecting, even with a right code. The session gets one NOTICE when an
  address is refused.
  - Per address and for a fixed time, not a lockout of the server until
    restart: a global lockout is one any stranger on the LAN can trigger, at
    will, against the people the proxy is for — and a daemon locked that way
    needs someone at the machine to restart it.
  - A right code clears that address's earlier failures, so a typo or two never
    accumulates towards a refusal.
  - A malformed hello or a non-`auth` first message counts as a failure; a
    connection that closes without sending anything does not.
  - Each connection already gets one attempt (S6), so the limit is on
    connections, and a 16-hex-digit code is out of reach of guessing anyway;
    the limit is what keeps a short *chosen* code (`--auth 123456`) safe too.
- **A refused client stops instead of retrying.** `open()` raising
  `SourceRefused` (the socket client's `AuthRefused` — a wrong code or a ban)
  ends the session: one `error` STATUS with `refused: true`, then
  `disconnected` with `reason: refused`. A retry would repeat the same wrong
  code every `reconnect_interval` and ban itself within seconds — which is
  exactly what happened when a `connect --serve` restarted with a fresh
  generated code and its clients reconnected with the old one. A server that is
  merely unreachable is still retried.
- Clients behind one NAT share an address, so one of them guessing wrong
  refuses the rest for the ban. Accepted: the alternative identities an
  unauthenticated client can offer are all ones it chooses itself.

**Acceptance**
- `--serve` with no `--auth` yields one full-access code, never `123456`,
  different on each run, at least 16 characters, printed; `--auth` codes are
  used as given with nothing generated.
- The limiter refuses an address on its Nth failure within the window, only
  that address, for the ban and no longer; failures spaced beyond the window
  never add up; a success clears earlier failures.
- Through a real server: N wrong codes, then the right one is refused with
  `try again in <N>s`, and one NOTICE names the address; alternating wrong and
  right codes never refuses; malformed hellos count.

## S23. Looking the auth code up again

Since S22 a `--serve` without `--auth` prints a generated code once, at startup
— and a terminal full of device output scrolls it away in seconds. Two places to
find it again:

- **`<prefix> i` in the TUI** raises the session's details as one toast: proxy
  address, every code with its role, the `attach` name if registered, mirrors,
  the log folder. Without a proxy it says so and how to get one.
  - A **toast only**. Not written to the log view (what `Ctrl+W` copies and
    people paste into tickets), and never published as a NOTICE — the proxy
    forwards notices to every client, read-only ones included, which would hand
    them the full-access code. The recorder never writes notices either way.
  - The status bar shows `serve <addr> (<prefix> i)` — where to look, never the
    code, since a status bar ends up in screenshots.
- **The session registry** (S17) now also holds a foreground `connect --serve`
  (`foreground: true`), in the same 0600 state file, under the device stem
  (`-2`, `-3`… if a live session already has that name). It is removed on exit
  and pruned like any other if the process dies. So, from any terminal:
  - `status` lists it, marked `(foreground)`; `status --show-auth` (or
    `--json --show-auth`) adds each code and role. Codes stay hidden otherwise,
    with a line saying how to show them.
  - `attach` joins it with the recorded code. A wildcard bind (`0.0.0.0`,
    `::`) is reached on loopback (`127.0.0.1`, `::1`).
  - `start` refuses its port, and S21's busy hint names it as a session "in
    another terminal" rather than a background one.
- State files gain `codes` (code → role; `auth` remains the one `attach` uses,
  full access when there is one) and `foreground`. Older files load: `codes`
  falls back to `{auth: full}`.
- POSIX only, as the registry is: its liveness probe `os.kill(pid, 0)` would be
  a Ctrl-C on Windows. A registry that cannot be written is a note on stderr,
  never a failure to serve.

**Acceptance**
- `attach`'s code is the full-access one when there is one, else any.
- `codes` / `foreground` round-trip; a file without them still shows its code.
- Names are unique among live sessions; a dead session does not hold one.
- `register_foreground` writes a 0600 file with the bound port, every code and
  `foreground: true`; skips cleanly off POSIX; a write failure is a note.
- The info lines name address, each code and role, `attach`, mirrors and logs.
- `status` hides codes and says how to show them; `--show-auth` lists each with
  its role; JSON carries `auth` only with `--show-auth`, and `foreground` always.
- Through the Textual harness, in line and character mode: `<prefix> i` raises
  exactly one toast with the code, and the code is in neither the log, the bus,
  nor the status bar, which does name the serve address; with no proxy it
  explains; `<prefix> ?` lists `i`.
- A real `connect --serve --listen-port 0`: its generated code shows up in
  `status --json --show-auth` from another process, a client joins with the
  registry's details via loopback, and an ordered exit leaves no state file.
  A 0.0.0.0 bind warns about the network; `--listen 127.0.0.1 --auth given`
  neither warns nor generates.

## S24. Fixed proxy codes in config.toml

A generated code (S22) changes every run, so anyone connecting from elsewhere
needs the new one after each restart; `--auth CODE` avoids that but puts the
code where `ps` shows it to every local user and the shell history keeps it.

```toml
# ~/.uart-proxy/config.toml   (chmod 600)
[proxy]
auth = ["fixedcode", "look:readonly"]   # or one string
```

- Precedence, as for retention: `--auth` > `[proxy] auth` > a generated code.
  `connect --serve` and `start` resolve it the same way, and `start` records
  every code (S23), `attach` taking the full-access one.
- Using config codes is said at startup (`Using N auth code(s) from …`).
- **Fails safe to a generated code**, with a note on stderr, whenever the
  setting cannot be trusted or read: the file is readable by group or others
  (POSIX — a code in a world-readable file is not a secret); the value is not a
  string or a list of non-empty strings; or Python is 3.10, which has no
  `tomllib` — said rather than silently skipped.

**Acceptance**
- Config codes are used with their roles when `--auth` is absent; a single
  string is one code; `--auth` wins and the config is not mentioned.
- A mode-644 file, a malformed value, or a missing `tomllib` each give a note
  and a generated code; no file, or no `[proxy]` section, gives a generated code
  and no note.
- A real `start` with config codes lists all of them in
  `status --json --show-auth` and records the full-access one for `attach`.
- The test suite never reads the developer's own config.toml.

## S25. Log banners

- The two timestamped files open with `#` lines — version, the source's
  description (port, baud, framing), encoding, EOL, and the wall-clock time of
  elapsed 0 with its UTC offset — and close with `# ended · <abs window> ·
  <rel window>`. A `#` line can never be taken for a device line, which always
  starts `[stamp]`.
- The raw `output.log` never gets one: it is the device's bytes, byte for byte.
- A session that adopted a remote timeline (S18) states the adopted origin.

**Acceptance**: marks reach only the timestamped files; a device line starting
`#` still gets its stamp; the header names version, source, encoding, EOL and a
start with offset; the footer has both windows; a real `connect` recording opens
and closes with them while `output.log` is exactly what was sent.

## S26. Connection cap

- `max_clients` (`--max-clients`, default 16, `0` = none) bounds connections
  being served, **authenticated or not** — otherwise idle sockets that never
  send `auth` could hold every slot for their 10 s grace each.
- Over the cap: `auth_fail` with `retry: true` and `server full (N
  connections)`, sent from the accept thread, then close. `retry: true` makes
  the client raise a retryable error rather than S22's `AuthRefused`: full is
  temporary. Being turned away never counts as an auth failure.

**Acceptance**: the N+1th client is turned away with a retryable error; a slot
frees when a client leaves; idle unauthenticated sockets count; turned-away
attempts never ban; `0` admits 20; a waiting client gets in once a slot frees.

## S27. Choosing the port

- `connect` without `--port`: in a terminal (stdin and stdout both ttys, no
  `--no-tui`) a picker lists the ports — path, VID:PID, description, and
  `held by '<session>'` for one a session of ours holds; `r` rescans, Esc
  cancels (`No port chosen.`, exit 1). Elsewhere the ports are listed on stderr
  and the command fails: a script must never wait for a keypress.
- `start` never asks — detached, nobody could answer.

**Acceptance**: a given port is used without asking; a terminal asks; backing
out says so; no terminal or `--no-tui` lists and fails; no ports says so;
`start` without a port or profile fails without asking; in the picker Enter,
↓+Enter, Esc, `r` after a plug-in, and an empty list behave.

## S28. Searching the log

- `<prefix> /` opens a search box (line mode; in character mode the log is
  behind the device's screen and every key is the device's, so it says so).
  Enter filters the log to matching lines, the match highlighted; lines that
  arrive later are filtered live. An empty Enter restores the full log. The box
  opens empty — the current pattern is in its placeholder — so that an empty
  Enter really is one keystroke. Esc leaves the filter as it was.
- **Smart case**: case-insensitive unless the pattern has a capital. Matching is
  on the plain text, so `[red]` in device output is literal.
- The status bar shows `filter '<p>' (<n>)`. `Ctrl+W` copies what is shown.
  `<prefix> k` clears the lines and keeps the filter.
- Rendered lines are kept alongside the plain copy buffer (both bounded, 5000),
  so setting or clearing a filter rebuilds the view without losing anything.

**Acceptance** (Textual harness): only matches shown; live filtering; an empty
search restores lines that arrived while filtered; smart case; the status count;
copy takes the shown lines; Esc keeps the filter and returns focus to the input;
a reopened box is empty with the pattern named; nothing typed reaches the
device; markup-like text matches literally; character mode explains instead;
clear keeps the filter; `<prefix> ?` lists it.

## S29. Echoing typed lines to proxy clients

- `--echo-tx` (off by default) sends each completed TX line to clients as
  `{"type": "tx_echo", "seq", "wall", "elapsed", "text"}` — lines, not
  keystrokes, so character-mode typing arrives as the command. Off by default
  because a line typed at a password prompt is a line.
- Not sent to the client whose `tx` produced it: the TX event fires
  synchronously inside that client's write, on its thread, which is how the
  server knows (a thread-local). That client already shows its own line.
- A client shows it as a TX line marked `remote` (`publish_remote_tx`) and never
  writes it: it has already reached the device. Clients that don't know
  `tx_echo` ignore it.

**Acceptance**: off by default; on, a server-side line reaches clients; the
typing client is not echoed and shows its line once; keystrokes echo as one
line; read-only viewers get it; nothing is re-sent; an unaware client keeps
streaming; the flag reaches the server; a typed line is a TX line for `\r`,
`\r\n` and `\n`.

## S30. Device profiles

- `--profile NAME|FILE.toml` (`connect`, `start`) loads a `uart_helper`
  profile — by name from `./uart-helper.d/` then `~/.config/uart-helper/`, or
  by path.
- `[defaults]` fill baud / bytesize / parity / stopbits and flow control
  (`xonxoff`, `rtscts`, `dsrdtr`) where **no flag** is given. The flags
  therefore default to None: `--baud 115200` with a 9600 profile means 115200.
  Settled once (`start` before detaching; the `connect` it runs does not
  reload).
- `[[rules]]` find the port when `--port` is absent: one match is used (and
  said); several go to the picker restricted to the matches, or — with nobody to
  ask, and always for `start` — are listed and refused; none is an error.
- An unknown or unparsable profile is an error naming it, never a traceback.

**Acceptance**: built-in defaults without a profile; a profile by name or path
sets the rest, flow control included; a flag beats it even at the default
value; unknown and broken profiles are errors; settling is idempotent; one
match is taken, none is said, several are offered (only those) or listed; an
explicit port wins; a rules-free profile leaves the port to you; `start` with a
non-matching profile fails before detaching, and with several refuses rather
than asks.

**Test safety**: in-process tests never see real ports or detach a daemon
(`conftest.py` replaces the scan with `[]` and `daemonize` with a failure,
unless a test is marked `real_ports` / `real_daemonize`). A test that scanned,
found a developer's plugged-in adapter matching its profile, and detached a
daemon onto it is why.

## S31. Following an adapter that re-enumerated

- A USB adapter replugged may return under another path (`usbserial-110` →
  `usbserial-120`); path-only reconnect (S12) would wait for the old node
  forever. `UartSource` learns the port's **identity** from a scan at its first
  successful open, and when a later open fails for any reason but busy (S21),
  scans for it:
  1. VID + PID + serial number, when the adapter has one;
  2. otherwise VID + PID + description, narrowed to the same USB location
     (physical socket) when that separates candidates.
  Exactly one match → switch to it (new `UARTDevice`, same config), call
  `on_moved(old, new)`, and open there. None → the open fails as before.
  Several → never a guess: connecting to the wrong device is worse than
  waiting.
- A port first seen without USB identity (`/dev/ttyS0`, macOS's
  `Bluetooth-Incoming-Port`) is never followed.
- `connect` announces the move as a NOTICE naming the adapter, and rewrites the
  `port` of any registry entry this process owns (S17/S23), which `status`, the
  busy hint and `start`'s refusal all use.
- **Listing:** `ports` and the picker sort USB adapters first and drop
  pyserial's `n/a` placeholder; nothing is filtered, since built-in UARTs have
  no VID either.

**Acceptance**: the placeholder is not a description; description shows only
what is known; adapters sort first and nothing is hidden, in `ports`, `--json`
and the picker. A serial finds the adapter under a new name; a different serial
does not; without a serial one of the model suffices, two are never guessed
between, and a matching USB socket separates them; no USB identity, no
following. `UartSource` learns identity on first open, follows a replug, fails
plainly while unplugged, never treats busy as moved, never follows a non-USB
port, and survives a failing scan; a reconnecting session ends up on the new
path with a notice; only this process's registry entry is rewritten.

## S32. Log parts

- `Recorder(rotate_bytes=N)` (`--log-rotate-mb`): once the raw file or the
  larger timestamped file passes N bytes, all three are closed together at the
  **end of the next line**, renamed `<base>.NNN.log`, `<base>-timestamp.NNN.log`,
  `<base>-fulltimestamp.NNN.log`, and reopened empty. A stream with no newlines
  rotates anyway past 2N; with no text files, at N.
- The old part ends `# continues in part K+1`; the new one repeats the banner
  (S25) and says which file came before. The raw files never get a mark.
- Numbering continues past parts already in the folder (`--log-append`), never
  overwriting one.
- `keep_parts=K` (`--log-keep-parts`) deletes the oldest beyond K — the only
  thing that bounds a session which never ends, since retention (S11) prunes
  finished session folders. "Logs written" lists every part.

**Acceptance**: off by default; the limit splits all three together; raw is
byte-exact and lines whole and ordered across parts; each part reads on its
own; raw never gets a banner; `keep_parts` removes the oldest; a line longer
than the limit is not split; binary and raw-only recordings rotate; appending
never overwrites an earlier run's parts; `close_recorder` lists every part; a
real `connect --log-rotate-mb 0.01 --log-keep-parts 2` produces part 3 and has
deleted part 1.

## S33. PTY mirrors for clients

- `remote` and `attach` take `--proxy-dir`, `--proxy-count`, `--proxy`,
  `--tx-merge` and `--mirror-name`: the S14 mirrors, fed from the stream we are
  a client of, so a remote port can be opened locally as a PTY.
- Default names: `<host>-<port>-N` for `remote`; `<session>-attach-N` for
  `attach`, so they never collide with the daemon's own `<session>-N` in the
  same directory.
- Writes go through the client session to the server. With a read-only code
  they are refused, and the refusal is a NOTICE (headless mode prints it), not
  silence.

**Acceptance**: both commands parse the flags; off unless asked; a stem names
the links; a real `remote --proxy-dir` against an in-process server carries RX
to the mirror and mirror input to the far device, and removes its link on exit;
with a read-only code the device receives nothing and the refusal is printed.

## S34. Network ports

- `--port` may be a URL (`connect`, `start`): `socket://HOST:PORT` or
  `rfc2217://HOST:PORT`, opened with pyserial's `serial_for_url` by a
  `UrlSource`. Anything else with `://` is refused before anything starts,
  naming the two that work; so is a URL without a host or port.
- `socket://` is raw TCP: the serial settings are the server's, ours are not
  sent, and the description says so. `rfc2217://` sends baud, framing and flow
  control to the far port with Telnet COM-port control; Telnet's IAC byte in
  the data is escaped both ways.
- **Pitfalls pyserial sets**, pinned by test: its RFC 2217 client raises for any
  `write_timeout`, so only raw TCP is given one; and setting `timeout` on an
  open port reconfigures it — for RFC 2217 a round trip — so it is set only
  when it changes, not on every read; and its `open()` ends by flushing input,
  which for a socket discards what the server has already sent — a banner or
  `login:` prompt sent on connect was lost whenever it beat the flush — so
  `reset_input_buffer` is a no-op for the duration of `open()`.
- A server that is not up is S12's absent device (`waiting`); a dropped
  connection raises on read, so the session reconnects.
- Local-device reporting does not apply and is not attached: no `TIOCEXCL`
  (S15) — instead one NOTICE on connecting says there is no claim to take and
  the server decides who may connect — no busy hint (S21), no following a
  replug (S31).
- Names: `device_stem` turns a URL into `scheme-host-port` for mirrors and the
  session registry.
- Not yet: plain `telnet://` needs option negotiation (the Telnet IAC item).

**Acceptance**: URLs are told from device paths; the two schemes pass and
others (and host- or port-less URLs) are refused with what works; stems are
file-name safe; descriptions say whose settings apply. Against an in-process
TCP device: both directions; a server not up yet is waited for; a dropped
connection reconnects and is greeted again. Against pyserial's own RFC 2217
`PortManager` over `loop://`: our baud and parity land on the far port; data
including `0xFF` round-trips; reading does not reconfigure each time. The CLI
refuses `telnet://` up front; a real `connect --port socket://…` records the
server's output, names its mirror after the URL, says there is no exclusive
claim, and puts the URL in the log banner.

## S35. ssh:// ports

- `--port ssh://[USER@]HOST[:PORT]` runs the **system's OpenSSH client** in a
  pty (`ssh -tt`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`, `-p` only
  when given, `--ssh-option` → `-o`, `--ssh-command` after `--`) and makes the
  pty's master the device. Not an SSH implementation: keys, `known_hosts`, the
  agent, FIDO keys, `~/.ssh/config` and `ProxyJump` behave as they do for `ssh`.
- The child is started through a small shim that makes the pty its
  **controlling terminal** (`TIOCSCTTY`) and sets its window size before ssh
  starts, then execs ssh. ssh reads host-key answers and passwords from
  `/dev/tty`, which needs one; a shim because `preexec_fn` is unsafe once the
  parent has threads. `TERM` defaults to `xterm-256color`.
- **Window size** (`--term-size auto`, the default): the terminal view's
  emulator reports real size changes and the TUI passes them to a source that
  can take one (`set_window_size` → `TIOCSWINSZ` → SIGWINCH → ssh forwards it).
  The view is the window minus header, status bar and footer; in character mode
  the input box is hidden and its rows go to the screen too. With `--no-tui` the
  size is the real terminal's (`os.get_terminal_size`), set before ssh starts
  and followed on SIGWINCH, the previous handler restored after; with no
  terminal (a daemon) nothing is sent. `--term-size COLSxROWS` fixes it (a BBS
  draws for 80×24) and every resize is ignored.
- `connect` defaults to character mode for `ssh://` (`--input` overrides).
- ssh exiting reads as a dropped device (`EIO`/EOF on the master, or the child
  gone), so the session runs it again after `reconnect_interval`. `close()`
  hangs up the pty, then SIGTERMs (and if need be SIGKILLs) ssh's process
  group; nothing is left behind.
- UTF-8 only; other encodings are left to the far end.

**Acceptance**: URLs parse (user, `%3A`-escaped user, port, none); `ssh://`
passes the port check without a port; the command line has `-tt`, keepalives,
`-p`, extra options and the command after `--`, and leaves user and port to ssh
config when absent; sizes parse and bad ones are refused; the stem names the
host. Against a fake `ssh` first on PATH: it has a controlling terminal; typing
reaches it; the initial size is there before it looks; `set_window_size`
reaches it as a resize, but not with a fixed size; its exit raises `ssh exited
(status 255)`; a session runs it again; `close()` leaves no process. The CLI
builds an `SshSource`, applies `--term-size` and `--ssh-option`, defaults to
character mode but respects `--input`, and refuses a bad size. Through the
Textual harness the transport is told the terminal view's size and told again
after a resize. A real `connect --port ssh://…` records the fake's output with
the fixed size, names OpenSSH in its notice and the size in the log banner. By
hand, the real `/usr/bin/ssh` against a closed local port: its error is shown
and it is retried.

## S36. telnet:// ports

- `--port telnet://HOST[:PORT]` (port 23 by default): a TCP connection with
  `TelnetProtocol` — framing and negotiation with no I/O, tested byte by byte —
  between it and the session.
- **Negotiation** (RFC 854/855; RFC 1143's rule of answering only a *change*,
  so two sides can never loop): the server may `WILL` ECHO, SGA and BINARY (we
  answer `DO`); we `WILL` NAWS, TTYPE, SGA and BINARY when asked (`DO`); the
  rest is refused with `DONT`/`WONT`; a withdrawal is confirmed only for what
  was on. `SB TTYPE SEND` gets `XTERM-256COLOR`. `DO NAWS` gets our size at
  once, and every later resize sends it again (unless `--term-size` fixes it).
  Other commands (NOP, GA, …) are dropped. Commands split across reads are
  understood.
- **Framing**: `IAC IAC` is a literal 0xFF both ways; outside binary mode a bare
  CR is sent as `CR NUL` and `CR NUL` received is a CR.
- Each connection negotiates afresh, keeping the last size. A dropped
  connection reconnects (S12). Character mode by default, like `ssh://`.

**Acceptance**: every rule above as a byte-level test of `TelnetProtocol`;
against an in-process server that negotiates like a BBS, the device log holds
only `login: `, and the server receives DO ECHO, DO SGA, WILL NAWS with the
size, and the terminal type; typing arrives framed; a resize is sent as NAWS,
but not with a fixed size; a drop reconnects and negotiates again; the CLI
builds a `TelnetSource` in character mode; a real `connect --port telnet://…`
records no negotiation bytes.

## S37. Replay

- The recorder writes `<base>-timing.log` beside the raw log: one row
  `<epoch> <elapsed> <bytes>` per RX chunk. The epoch (UTC seconds, from the
  monotonic-anchored wall clock of S2, so it never goes backwards within a run)
  makes the file stand on its own and keeps runs appended to one folder in
  order — elapsed alone restarts at 0 for each, and a second run would be
  squashed into an instant. Elapsed matches the timestamped files. It rotates
  with the other files (S32). No banner: it is data. The first two-column
  format (`<elapsed> <bytes>`) still loads, its wall clock from the banner.
- `uart-proxy replay [PATH]` — a session folder, a raw `output*.log` (a part
  finds its own `output-timing.NNN.log`), or by default the newest session in
  the store. `Recording` maps playback position to bytes; the banner (S25)
  gives the wall-clock time of each moment. A timing file cut short has the
  rest of the bytes appended at its end; one claiming more than exists is
  trimmed; without one, the recording plays all at once, with a note.
- The TUI player feeds bytes through the terminal emulator (S20) as their time
  comes: Space pauses (and at the end, plays again), ←/→ seek 5 s — backwards
  resets the screen and re-feeds from the start — `+`/`-` step through speeds
  ×0.25…×64, Home/End jump. `--term-size` holds a size whatever the window
  does. The status bar shows position / duration, speed, wall-clock time and
  screen size.
- `--no-tui` writes the bytes to stdout at their pace, for the terminal to draw
  (as `scriptreplay`).
- Silences longer than `--max-idle` (2 s; 0 keeps them) are cut to that.
- **Going to a moment**: `--at TIME`, and `g` in the player (a box; Enter goes,
  Esc closes it without quitting, and while it is open no key drives playback).
  `+HH:MM:SS` / `+SECONDS` is session elapsed — the first run's, when runs were
  appended — interpolated within a chunk's gap; `HH:MM:SS` is that time of day
  on the recording's first day that has it, or the next for one that crosses
  midnight; `YYYY-mm-dd HH:MM:SS` is exact. A time of day needs a wall clock
  (three-column timing, or a banner). Everything before the moment is drawn at
  once; `--at` outside the recording is an error.

**Acceptance**: loading gives the bytes, times and duration; positions map to
bytes; the wall clock comes from the banner; without timing everything is at
position 0; inconsistent timing is made consistent; a part finds its timing;
resolving takes a file, a folder or the newest session, and fails clearly. The
stream player writes every byte with the recorded gaps, scaled by speed and
capped by `max_idle`. The TUI player (driven by a fake clock) plays in time and
stops at the end; cuts silences; pause holds; End/Home seek both ways into a
clean screen; speed steps; Space at the end restarts; the status names position,
wall time and speed; a fixed size survives a resize; a full-screen redraw
replays as drawn-over text. The CLI reports an empty store and a missing timing
file. End to end: a real recording, replayed with `--no-tui`, produces the
recorded bytes exactly. The recorder writes epoch, elapsed and bytes; a
three-column file needs no banner; appended runs keep order and their own gaps;
the two-column format still plays. `--at` reads elapsed, time of day, a moment
and a time past midnight, rejects nonsense, and refuses a time of day without a
wall clock; the stream player and the TUI start at a moment; `g` jumps, Esc
closes without quitting and space is typed rather than obeyed, and a bad time is
said.

## S38. A proxy client's window size

- Client → server `{"type": "resize", "cols", "rows"}` (optional; older servers
  ignore unknown types). `SocketSource.set_window_size` sends it — after
  authenticating if set before, again after every reconnect — so the TUI's and
  headless mode's size-following (S35) reaches a device served elsewhere.
  Read-only clients do not send it.
- The server applies it to a source that can take a size (`ssh://`,
  `telnet://`), from `full` clients only — the size shapes what the device draws
  for everyone — ignoring nonsense (non-integers, outside 2…1000). The latest
  wins. For any other device it is ignored.
- Sends on the client socket are serialized: tx and resize can come from
  different threads, and two `sendall`s at once could interleave on the wire.

**Acceptance**: a full client's size reaches the device; one set before
connecting is sent on connect and again after a reconnect; a read-only client
neither sends it nor, if it does, has it applied; nonsense is ignored; a device
without sizes keeps the client; and the whole chain — client → proxy →
`telnet://` → the far telnet server — delivers NAWS with the client's size.

## S39. The session registry on every OS

The registry (S17, S23) is how anything on this machine finds a session that
serves the proxy: `status`, `attach`, and now the session client and the MCP
server (S41, S42). It was POSIX-only because its liveness probe,
`os.kill(pid, 0)`, is not a probe on Windows — signal 0 there is
`CTRL_C_EVENT`. With a probe that is one, the registry works everywhere;
detaching (`start`) stays POSIX-only.

- **Liveness** asks the OS without touching the process: POSIX
  `os.kill(pid, 0)`; Windows `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`
  then `GetExitCodeProcess` = `STILL_ACTIVE` (access denied means it exists).
  `os.kill` is never called on Windows.
- **A foreground `connect --serve` registers on every OS** (S23), as does any
  program serving a session: `register_served(server, …)` writes the entry
  for a running `ProxyServer` — name, pid, bound address, every code with its
  role — and returns it; `DaemonInfo.remove()` takes it away. Writing never
  fails the caller: an unwritable registry is reported, and serving goes on.
- Entries gain two optional fields: **`owner`** (who serves it: `uart-proxy`,
  or an embedding application such as `uartist`) and **`title`** (what to
  call it, e.g. the tab's port). Older files load, as `uart-proxy` / no title.
- The state file stays `0600` on POSIX. On Windows the file lives in the
  user's profile, whose permissions already keep other users out; `chmod`
  there is best-effort.

**Acceptance**
- With the platform made Windows, `is_alive` uses the Windows probe and never
  `os.kill` (a recorded fake); a live pid is alive, a gone one is not, access
  denied is alive.
- `register_served` writes owner, title, codes and the bound port; `status
  --json` lists it from another process; `remove()` deletes it; a write
  failure returns `None` with a note and does not raise.
- A file without `owner` / `title` loads as `uart-proxy` / `""`.

## S40. Who sent it

A shared session has several writers: the person at the keyboard, and any
proxy client with the `full` role. What each one sent should be told apart —
in the TUI, in an embedding app, and in the recording.

- `UartSession.write(data, *, origin=None)`: the `DATA(TX)` and `LINE(TX)`
  events it publishes carry `meta["origin"]` when one is given. The proxy
  writes with `{"via": "proxy", "role": …, "client": …, "address": …}`;
  local typing has no origin.
- **`auth.client`** (optional, ≤ 64 characters, client → server): a name for
  the client, e.g. `uart-proxy mcp (claude-ai)`. Kept for the connection,
  shown in `clients()` and in the origin. Older clients do not send it.
- `ProxyServer.clients()` lists the authenticated connections: address, role,
  client name, when it connected — what an app shows as "1 connection".

**Acceptance**
- A proxy client's `tx` produces `DATA(TX)` / `LINE(TX)` events whose origin
  names `proxy`, its role, its client name and address; a local `write`
  produces none.
- `clients()` lists a connected client with its name and role, and drops it
  when it disconnects; a name longer than 64 characters is cut to 64.

## S41. The session client, and `tail` / `expect` / `send`

A script, a test, or an AI agent wants three things from a running session:
what it said lately, to wait until it says something, and to send a line.
`attach` is a person's view and stays one; this is the programmatic one.

- **`uart_proxy.client.SessionClient`** connects to a served session by
  registry name (`SessionClient.from_registry(name)`), or by host, port and
  code. It speaks the proxy protocol (S6, S18, S40): asks for replay, adopts
  the server's elapsed, names itself (`client`).
- It keeps the session's **lines**, each `{n, wall, elapsed, text, replayed}`,
  in a bounded ring (oldest dropped, counted). Lines are assembled from `rx`
  bytes as the session assembles them (S3); each carries the stamp of the
  chunk that completed it. `text` is cleaned for reading: terminal escape
  sequences and control characters other than tab removed. The line still
  being written (a prompt with no newline yet) is the **partial line**.
- `read(cursor)` → the lines after `cursor`, the new cursor, and how many were
  dropped in between: a reader that keeps its cursor misses nothing it is
  told about. `tail(n)` → the last `n`.
- `expect(pattern, timeout, *, since=None)` waits for a line — or the partial
  line, so `login:` and `#` prompts count — matching a regular expression,
  among lines after `since` (default: from the moment of the call). Returns
  the matching line and the lines before it, or `None` on timeout.
- `send_text(text, eol="cr")`, `send_hex(hex)`; refused locally with a clear
  error for a `readonly` connection (the server would refuse it too, S6).
- **CLI**, for scripts and shells, by registry name or
  `--host/--port/--auth`:
  - `uart-proxy tail [NAME] [-n 50]` prints the last lines, stamped.
  - `uart-proxy expect [NAME] PATTERN [--timeout 10]` exits 0 with the
    matching line printed, 1 on timeout.
  - `uart-proxy send [NAME] TEXT [--hex] [--eol cr] [--expect PATTERN
    --timeout 10]` sends, and with `--expect` waits for the reply; exits 1 if
    it does not come.

**Acceptance** (against a real `ProxyServer` on a fake source)
- Replayed lines arrive first, marked `replayed`, with the server's stamps;
  live lines follow with increasing `n`.
- `read` with a cursor returns only newer lines; after the ring overflowed
  it reports the dropped count.
- `expect` matches a complete line, matches a partial `login: ` prompt,
  ignores lines from before the call, and returns `None` after its timeout.
- Text is cleaned (`\x1b[32mok\x1b[0m` → `ok`), and invalid UTF-8 is replaced,
  never raises.
- A readonly client's `send_text` raises and the device receives nothing.
- `uart-proxy send … --expect` against a session that echoes exits 0; `expect`
  for text that never comes exits 1 after its timeout.

## S42. An MCP server for AI tools

`uart-proxy mcp` lets an AI tool — Claude Desktop, Claude Code, any Model
Context Protocol client — read a session someone shares, and, when allowed,
type into it. It is a **client of served sessions** (S41), never the owner of
a port: whatever holds the port (a `connect --serve`, a `start`ed session, an
application embedding the proxy) stays where a person can watch it.

- **Transport**: MCP over stdio — JSON-RPC 2.0, one message per line on
  stdin / stdout. Nothing but protocol goes to stdout; logs go to stderr.
  Implemented with the standard library only.
- **Methods**: `initialize` (answers the client's protocol version when it is
  one we know, else our newest; capabilities `tools`), `notifications/
  initialized`, `ping`, `tools/list`, `tools/call`. Unknown methods get
  JSON-RPC error `-32601`; bad arguments `-32602`; a tool that fails answers
  with `isError: true` and a message, so the agent can read why.
- **Tools** (each takes `session`, optional when exactly one is running):
  - `list_sessions` — name, title, owner, port, and whether this server can
    send to it.
  - `session_status` — device state, access, lines seen, last activity.
  - `read_new` — the lines since this server last read that session (it
    keeps the cursor), with a note when some were dropped.
  - `tail` — the last `lines` (default 50).
  - `wait_for` — S41's `expect`: `pattern`, `timeout` (default 10 s, at most
    120), over what this server has not handed back yet — output since the
    last `read_new` or the last match — so a reply that came before the call
    still counts. A match moves that mark past it.
  - `send_text` / `send_hex` — **only with `--allow-send`**, and only to a
    session reached with a `full` code; each may take `wait_for` and
    `timeout` to send and wait for the reply in one call.
- **Access is the session's to give**: the server connects with the
  session's read-only code when it has one (always, without `--allow-send`),
  else its full code — and then never writes without `--allow-send`. It names
  itself `uart-proxy mcp (<client name>)` (S40), so the session's owner sees
  who is attached.
- **Results are bounded**: at most 200 lines and 20 000 characters each, cut
  from the oldest with a note; lines are S41's cleaned text with their local
  and elapsed stamps.

**Acceptance** (driving `uart-proxy mcp` as a subprocess over pipes)
- `initialize` answers a known version with it and an unknown one with ours;
  `tools/list` lists the read tools, and the send tools only with
  `--allow-send`.
- Against a served fake session: `tail` returns its lines with stamps;
  `read_new` twice returns only what is new the second time; `wait_for`
  returns the line and times out cleanly.
- `send_text` without `--allow-send` is not a tool (`-32602`); with it, to a
  read-only session, is an error result and the device receives nothing;
  with a full code it reaches the device and `wait_for` returns the echo.
- Bad JSON and unknown methods get JSON-RPC errors, and the server keeps
  serving; stdout carries only JSON-RPC lines.

## S43. Device health, on the wire and in the client

An agent working through a shared session (S42) must know whether the device
is really there — and must not assume it is because nobody said otherwise. A
client used to learn the device's state only when it *changed*; one that
attached during a drop believed it connected.

- The session keeps its **device state** — the last status it published
  (`connecting`, `connected`, `waiting`, `reconnecting`, `error`,
  `disconnected`) — with when it began (`since`, local wall time), the error
  that came with it, how many times it has **reconnected** (connected again
  after a first connection), and when the device last said anything
  (`last_output`). `UartSession.device_health()` returns them.
- **On the wire** (optional fields, PROTOCOL.md): `auth_ok` carries `device`
  — that snapshot — so a client knows the state the moment it attaches; every
  `status` message carries `since` and `reconnects`.
- **The session client** (S41) keeps the snapshot current from `auth_ok`,
  `status` and `rx`, and watches its own link: it pings every few seconds and
  counts the link lost when nothing at all has come back for three intervals
  (a half-open socket), as well as on a close.
- `uart_proxy.health.assess(…)` turns the two into one verdict:
  - **down** — the session is no longer shared or reachable; or the device is
    `waiting` (absent), `error` / `reconnecting` (dropped) or `disconnected`;
  - **degraded** — `connecting`, or connected again less than 60 s ago (output
    from the gap may be missing);
  - **ok** — connected and settled.
  Each comes with **advice** a person can act on: re-plug the adapter, check
  the cable, share the tab again; and, when the device has been silent for a
  minute, that this is normal for an idle console but that a device which
  should be talking may need a reset.

**Acceptance**
- A client attaching while the device is absent sees `waiting` and its error
  at once; one attaching while connected sees `connected` with `since`.
- A drop and a return: the client sees `error`/`reconnecting`, then
  `connected` with `reconnects` 1; for 60 s that is `degraded`, then `ok`.
- A server that stops answering (no pong, no traffic) is a lost link within
  three heartbeats; the verdict is `down` with advice to share again.
- `assess` covers each state above with its level and advice, and names the
  silence only past a minute.

## S44. Health through MCP

- `session_status` returns the verdict and each link: `share` (the MCP
  server's connection to the session) and `device` (S43), with `advice`.
- Every tool that reads or sends adds the verdict to its result when it is
  not `ok`, and a `wait_for` that times out says whether the device was
  there and how long it had been silent — so an agent can tell "no answer"
  from "nobody to answer".
- **`wait_for_device`** (`timeout`, default 60 s, at most 600): waits until the
  session is shared and its device connected — joining again if the session
  is shared anew — and returns the verdict; the tool to call after asking a
  person to re-plug the adapter.
- **Notifications**: the server offers the `logging` capability; while a
  client is attached to a session, a change of its verdict is sent as
  `notifications/message` (`warning` for down, `notice` for degraded, `info`
  for ok) with the session, the verdict and the advice. `logging/setLevel`
  is accepted.

**Acceptance** (over pipes, against a served fake device)
- With the device absent, `session_status` is `down` with re-plug advice; a
  `tail` result carries the verdict.
- `wait_for_device` returns `ok` once the device comes back, and times out
  cleanly with the verdict when it does not.
- Dropping the device sends a `warning` notification naming the session;
  its return sends `notice` then, a minute on, `info`.
- Stopping the share: the next call reports `down` with advice to share the
  tab again.

## S45. When each proxy client was last heard

- `ProxyServer.clients()` adds `last_seen` (epoch seconds): the last message
  from that client, pings included — so an owner can show "claude-ai · 3 s
  ago", and tell a working agent from one that stopped.

**Acceptance**: a client's `last_seen` moves on with its pings and stays put
when it is silent.

## S46. Triggers: when the device says X, do Y

A session can watch its own output and act on it — the grep plugin (S7)
generalised, with limits, and without running anyone's code. Rules are data,
never code; what they may do is ranked by risk.

- **A rule**: `{id, name, owner, enabled, when, limit, context, actions}`.
  - `owner`: `{"kind": "person"}`, or `{"kind": "ai", "client": <name>,
    "connection": <id>}` for one made through the proxy (S47).
  - `when` — one of:
    - `text` (a substring) or `regex` (Python `re`), `case` sensitive or
      not (default not), on `rx` lines (default) or `tx` lines. A prompt
      without a newline (`login: `, `# `) is a line once S3's idle flush
      emits it, so it needs nothing more;
    - `hex`: a byte sequence in the `rx` stream, across chunks;
    - `silence`: no `rx` for `seconds` while the device is connected (fires
      once per silence);
    - `state`: the device `disconnected`, `connected` or `reconnected` (S43).
  - `limit`: `once`; `cooldown` seconds (default 1); `max_per_minute`
    (default 30); `after` — fire on the n-th match within `window` seconds.
  - `context`: how many lines before the match the event carries (0–20,
    default 3).
  - `actions`, each of a **level**; the rule's level is its highest action's:
    - level 0 — `event` (always: every firing is an event), `mark` (the
      match is marked for viewers), `notify` (asks the embedding app or the
      TUI to tell the person);
    - level 1 — `send`: literal `text` with `eol`, or `hex`. Nothing from the
      match is put into what is sent (no captures, no templates): what a
      device prints never becomes what it is told.
    - There is no level 2 here — no action runs a program or reaches the
      network.
- **Safe by construction**:
  - Patterns are checked when a rule is added: a regex must compile, be at
    most 512 characters, and not nest repetition (`(a+)+`, `(.*)*`), so a
    long line cannot stall the session; matching looks at the first 4096
    characters of a line.
  - A rule that hits `max_per_minute` is disabled, with the reason, and a
    notice says so.
  - A `send` rule does not fire on an `rx` line equal to what it sent in the
    last 2 s (the device's echo), nor on its own `tx`.
  - A rule's `send` goes through `UartSession.write` with `origin = {"via":
    "rule", "rule": id, "owner": …}` (S40) — recorded and shown like any
    other writer — and is skipped, with a notice, when the session cannot be
    written.
  - Every level-1 rule carries `approved`: the SHA-256 of its `when`,
    `limit` and `actions` at the time a person approved it. A level-1 rule
    whose content no longer matches its `approved` hash does not act.
    Rules loaded from a file (import) arrive disabled and unapproved.
- **An event**: `{seq, rule, name, owner, wall, elapsed, line, groups,
  context, actions: [{kind, ok, detail}]}`, published on the session's bus
  as `TRIGGER`, written beside the recording as `<base>-events.jsonl` (one
  JSON object per line), and kept in a ring of the last 500
  (`events(since=seq)`). A rule with `notify` also publishes a notice
  `⚡ <name>: <line>`, which the TUI shows.
- **API**: `session.triggers` — `add(rule) -> id` (raises `ValueError` naming
  what is wrong), `remove(id)`, `enable(id, on)`, `list()`, `events(since)`;
  rule files are JSON (`load(path)` / `dump(path)`).
- **CLI**: `uart-proxy connect … --rules FILE` loads rules (level-1 rules
  only with `--approve-rules`, which approves them as loaded; otherwise they
  load off, and it says so).

**Acceptance**
- A `text` rule fires once for a matching `rx` line and not for a clean one;
  `regex` groups reach the event; a `login: ` prompt with no newline fires it
  once flushed; `hex` matches a sequence split across two chunks;
  `silence` fires once after its seconds and again only after new output;
  `state` fires on a drop and on the return.
- `once`, `cooldown`, `after n within window` behave as named; a rule past
  `max_per_minute` is disabled with its reason.
- Nested repetition, an over-long or invalid regex, and `{1}` in a `send` are
  refused when added; a 1 MB line is matched without stalling.
- A `send` rule writes with a `rule` origin; it does not fire on the device's
  echo of what it sent; on a read-only session it is skipped with a notice;
  edited after approval, it does not act.
- Events are numbered, carry their context lines, reach `events(since)`, and
  are in `<base>-events.jsonl`.
- `--rules FILE` loads rules; one that sends stays off without
  `--approve-rules`, and is approved with it.

## S47. Triggers through the proxy, and for AI tools

A proxy client — an AI tool's MCP server above all — may ask the session to
watch for something and tell it when it happens. Whether it may, and how
much, is the session owner's to decide, never the client's.

- **Protocol** (client → server, after `auth`):
  - `{"type": "watch_add", "name", "when", "limit", "context"}` — a level-0
    rule (no `actions` field is accepted: a watch only makes events) →
    `{"type": "watch_ok", "id"}` or `{"type": "watch_fail", "reason"}`.
  - `{"type": "watch_remove", "id"}`, `{"type": "watch_list"}` → the
    client's own watches.
  - `{"type": "rule_propose", "rule"}` — a level-1 rule, `full` role only →
    `{"type": "proposal", "id", "status"}` with `status` `refused`
    (not allowed now), then, if it was put to the owner, `accepted` or
    `declined`.
  - server → client: `{"type": "trigger", …event}` for every event of the
    session (a client can read every line anyway).
- **The owner's policy** (`ProxyServer(…, triggers=TriggerPolicy(...))`):
  - `max_watches` per connection (default 3; 0 refuses all); a client's
    watches are removed when it disconnects;
  - `propose`: a callable the owner supplies — `None` (the default) refuses
    every proposal; an app passes one that asks the person (UARTist: D41).
    An accepted proposal becomes a level-1 rule owned by `ai`, approved as
    the person saw it.
  - `uart-proxy connect --serve` / `start`: `--max-watches N` (default 3);
    proposals are always refused (no one to ask).
- **MCP** (S42): tools `watch_add` (`pattern` with `regex`, or `hex`;
  `name`, `context`), `watch_list`, `watch_remove`, `read_events` (since this
  server last read, at most 200), `wait_for_event` (`timeout`, optionally one
  `watch`; as `wait_for`, an event not yet handed back counts even if it came
  first); `propose_rule` (`pattern` / `hex`, `send_text` + `eol` or
  `send_hex`, `name`, `timeout` up to 150 s) only with `--allow-send`,
  answering what the owner decided. The watch tools are read tools: listed
  without `--allow-send`. Events also arrive as `notifications/message`
  (`notice`), with the session, the rule's name, `seq` and the line.

**Acceptance** (a real `ProxyServer` on a fake source; MCP over pipes)
- A read-only client adds a watch and receives `trigger` when the device
  prints its pattern; its fourth watch is refused with the default policy;
  disconnecting removes its watches.
- A `watch_add` with `actions` is refused; a `rule_propose` from a read-only
  client, or with no `propose` callable, is `refused`; with a callable that
  accepts, the rule is added, owned by `ai`, and its `send` reaches the
  device with a `rule` origin.
- Over MCP: `watch_add`, then `wait_for_event` returns the event when the
  device prints the pattern; `read_events` returns it once; `propose_rule`
  is not listed without `--allow-send`.
