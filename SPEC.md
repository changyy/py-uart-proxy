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
  enters a `waiting` state and retries every `reconnect_interval` seconds,
  emitting a STATUS event; it attaches as soon as the device appears.
- If the source drops mid-session (read error), it emits `reconnecting`, closes,
  and re-attaches when the device returns.
- `auto_reconnect=False` disables retries (one attempt, then give up).
- `write()` while not connected raises (it cannot reach the device).
- Default baud is 115200 (CLI `--baud` optional); the effective baud is shown in
  the status bar via the source description.

**Acceptance**
- A source whose first N `open()`s fail eventually connects and streams data.
- A connected session that hits a read error re-connects and resumes streaming.
- With `auto_reconnect=False`, a failing open never connects.
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
  macOS or driver update: `uart-proxy connect --port /dev/cu.usbserial-110`, then
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
- `<prefix> d` sets the detached state when detachable, and when not, leaves the
  app running and says there is nothing to detach from.
