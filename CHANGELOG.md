# Changelog

## [1.20261006.1075000] — 2026-10-06

Ports held elsewhere, and virtual ports, behave as they should.

### Fixed
- **A COM port held elsewhere was not "busy" on a Windows in another
  language.** "Access is denied" is translated there; it is now recognised by
  its codes (`winerror` 5, pyserial's `PermissionError(13, …, None, 5)`), so
  the busy hint (S21) names the holder and how to join it.
- **A virtual serial port stayed busy after it was closed.** Closing a port
  now gives up its exclusive claim (S15) first: the kernel clears it only at
  the tty's last close, so a pty kept open by its other program (socat,
  QEMU's `-serial pty`) refused every later open — ours included.

## [1.20261004.1221145] — 2026-10-04

When the device says X, do Y — as data, within limits, and with the person
holding the key.

### Added
- **Triggers** (SPEC S46): rules that watch a session — a text or regular
  expression in a line, a byte sequence, a silence, the device dropping or
  coming back — and, within limits (once, cooldown, after n within a window,
  at most 30 a minute), make an event, mark the line, notify, or send fixed
  text or bytes. Events are kept, published on the bus and written beside
  the recording (`<base>-events.jsonl`). `uart-proxy connect --rules FILE`;
  a rule that sends acts only once approved (`--approve-rules`).
  Safe by construction: no action runs a program or reaches the network,
  nothing from a match is ever sent, regular expressions that nest
  repetition are refused, and a rule cannot set itself off with its echo.
- **Triggers through the proxy and MCP** (SPEC S47): a client may add a few
  watches (`watch_add`, the owner's `max_watches`, default 3 —
  `--max-watches`) that report `trigger` events, and a `full` client may
  propose a rule that acts, which the owner decides (`TriggerPolicy(propose=…)`;
  the CLI accepts none). MCP tools `watch_add`, `watch_list`,
  `watch_remove`, `read_events`, `wait_for_event`, and `propose_rule` with
  `--allow-send`; events also arrive as notifications.


### Fixed
- **A stop during start-up left the logs open.** SIGTERM was turned into a
  clean shutdown only once the session was fully set up; one arriving while
  it registered or bound its proxy escaped every `finally` — the logs were
  neither closed nor reported. The trap is now set before anything is built,
  and everything built after it is inside the one `try` that cleans up.
- **A stop could hang the shutdown.** SIGTERM was raised as an exception
  wherever the main thread was — possibly inside a lock's `with`, leaving
  the lock held for the shutdown to wait on (a rare CI hang). It is now
  recorded: headless sessions (and background ones) see it in their wait
  loop and end without an exception; one during set-up ends the run when
  set-up is over; the TUI still takes it as Ctrl-C once it runs. Tests that
  stop a subprocess dump its threads' stacks if it does not exit.
## [1.20261004.1093657] — 2026-10-04

An agent can tell whether the device is really there, and what would help.

### Added
- **Device health** (SPEC S43): the session keeps its device's state, since
  when, the error, how often it reconnected and when it last spoke
  (`UartSession.device_health()`); `auth_ok` carries it, so a client attaching
  during a drop knows at once, and `status` messages carry `since`,
  `reconnects` and `error`. `SessionClient` pings and notices a silent link,
  and `health()` gives one verdict — ok, degraded or down — with advice.
- **Health through MCP** (SPEC S44): `session_status` reports each link and
  the advice; results from an unwell session say so first; `wait_for` says
  whether anyone was there to answer; **`wait_for_device`** waits for the
  device (or the share) to come back; a change of verdict is sent as an MCP
  `notifications/message`.
- `ProxyServer.clients()` adds each client's `last_seen` (SPEC S45).
- `.githooks/pre-push` runs the tests under Python 3.10–3.13, as CI does,
  before a push (`scripts/test-pythons.sh`): `git config core.hooksPath .githooks`.

### Fixed
- **Output right after attaching could be lost.** The proxy added a client to
  its live fan-out only after sending its replay, so what the device said in
  between never reached it. It is now live from the moment it is let in, under
  the fan-out's lock; live output waits behind the replay in its own queue.

## [1.20261003.1000725] — 2026-10-03

Scripts and AI agents can now drive a session somebody shares — and the person
sharing it still sees everything.

The first release on PyPI since 1.20260929.1214712: it also carries everything
listed under 1.20260930.1204523 and 1.20260930.1205413 below, and two fixes
made after them — a stopped proxy frees its port at once on Linux, and a test
race against the fake telnet server.

### Added
- **`uart-proxy mcp`** (SPEC S42): a Model Context Protocol server on stdio for
  AI tools. `list_sessions`, `session_status`, `tail`, `read_new` and
  `wait_for` read a served session; `send_text` / `send_hex` type into it, only
  with `--allow-send` and only with a full-access code. It is a client of the
  session, never the port's owner, results are bounded, and it is standard
  library only.
- **`uart-proxy tail` / `expect` / `send`** and **`uart_proxy.client.SessionClient`**
  (SPEC S41): a served session from a shell, a test or Python — stamped lines,
  a cursor that misses nothing, waiting for a pattern (prompts without a
  newline included), sending text or hex.
- **Who sent it** (SPEC S40): TX events carry `meta["origin"]` when a proxy
  client wrote them; clients may name themselves (`auth.client`, an optional
  protocol field); `ProxyServer.clients()` lists the connections.
- **The session registry on Windows** (SPEC S39): a foreground `connect
  --serve` — or an application serving a session (`register_served`) —
  registers on every OS, with an `owner` and a `title`. Liveness on Windows
  asks the OS for the process instead of `os.kill(pid, 0)`, which there would
  have sent Ctrl-C.

## [1.20260930.1205413] — 2026-09-30

Telnet joins the network ports, and a session can be watched again as it
happened — a full-screen program included.

### Added
- **`--port telnet://HOST[:PORT]`** (SPEC S36): a BBS (`telnet://ptt.cc`), a
  router's CLI, a console server in plain telnet mode. Option negotiation is
  answered rather than shown — echo and SGA accepted, terminal type and window
  size (NAWS) offered, everything else refused, a change only ever answered
  once so the two sides cannot loop — IAC and CR NUL are framed both ways, and
  the window size follows the terminal view (or `--term-size`). Character mode
  by default.
- **`uart-proxy replay [PATH]`** (SPEC S37): plays a recorded session back at
  its own pace through the terminal emulator — pause, seek, speed, and the
  wall-clock time of the moment on screen — so a full-screen program reads as
  it looked. `--no-tui` plays it into your terminal instead, like
  `scriptreplay`; `--max-idle` cuts long silences short.
- **`output-timing.log`**, beside `output.log`: when each run of its bytes
  arrived, as `<epoch> <elapsed> <bytes>` — absolute UTC time, so the file
  stands on its own and runs appended to one folder keep their order and pace,
  and session elapsed, to match the timestamped logs. It is what `replay` plays
  from, and it rotates into parts with the other files.
- **`replay --at TIME`**, and `g` in the player: start at, or jump to,
  `+00:47:15` (elapsed), `03:12:30` (time of day) or `2026-09-30 03:12:30` —
  grep the timestamped log for the moment, then watch it.
- **A proxy client's window size reaches the device** (SPEC S38): `remote` and
  `attach` send theirs (`{"type": "resize"}`, an optional protocol message), so
  an `ssh://` or `telnet://` session served elsewhere is drawn for the window
  you are looking at. Full-access clients only; the latest wins.

## [1.20260930.1204523] — 2026-09-30

The device can be anywhere now: a console server, ser2net or QEMU over TCP or
RFC 2217, or anything reachable with ssh — a BBS included, drawn at the size
of the window it is shown in.

### Added
- **`--port ssh://[USER@]HOST[:PORT]`** (SPEC S35): anything reachable with
  `ssh -tt` as the device — SSH console servers, a UART on another machine
  (`--ssh-command "picocom …"`), a BBS (`ssh://bbsu@ptt.cc`). Runs the system's
  OpenSSH client in a pty, so keys, known_hosts, the agent and `~/.ssh/config`
  apply and prompts are answered on screen. Starts in character mode; passes the
  terminal view's size to the far end and follows resizes (`--term-size auto`,
  the default; with `--no-tui`, the real terminal's size, followed on
  SIGWINCH), or holds `--term-size 80x24`; `--ssh-option` passes `-o`. POSIX
  only.

### Changed
- **Character mode hides the input box**, which takes no input there; its rows
  go to the device's screen — and, over `ssh://`, to the far end's window.
- **Network ports as the device** (SPEC S34): `--port socket://HOST:PORT` (raw
  TCP: ser2net raw, `qemu -serial tcp::…`, Wi-Fi bridges) and
  `--port rfc2217://HOST:PORT` (Telnet + RFC 2217: console servers, ser2net's
  telnet mode — baud, framing and DTR/RTS are applied to the far port), for
  `connect` and `start`. Waits for a server that isn't up and reconnects after a
  drop; says on connecting that there is no exclusive claim to take. Mirrors
  and sessions are named after the URL.
- **`examples/check_hardware.py`** — the checks CI cannot run, against a real
  adapter you name (or pick): the exclusive claim really refuses a second open
  with `EBUSY`, on the node and its cu/tty twin; the busy hint, `start`'s
  refusal and `status --show-auth` with a real session holding the port;
  `--profile` matching by the adapter's own VID/PID; `--loopback` data;
  `--replug` following it to a new name. It refuses to run while anything holds
  the port, asks before opening it, and writes nothing unless `--loopback`.

### Fixed
- **What a network device said on connect could vanish.** pyserial's `open()`
  ends by flushing input, which for a socket discards anything already
  received — a console server's banner or `login:` prompt was lost whenever it
  arrived before that flush (found as a test that failed only under load).
  Opening a network port no longer flushes.
- **Every headless session — and so every background `start` — exited on the
  first dropped device.** Headless mode stopped on any `error` status, and
  `error` is what a drop reports on its way to reconnecting; only a session that
  was waiting from the start survived. It now ends on `disconnected` alone,
  which a session always publishes when it really ends.
- **A failed read looked like silence.** `uart_helper` reports a read error in
  its result instead of raising, and `UartSource` returned the (empty) data —
  so an adapter that failed mid-read could look merely quiet, and the session
  would not reconnect. A read error now raises; only a timeout means "no data".

## [1.20260930.1202015] — 2026-09-30

A replugged adapter is followed to its new name, a session that never ends can
be capped, and a remote port can be opened as a local PTY.

### Added
- **A replugged adapter is followed to its new name** (SPEC S31):
  `usbserial-110` coming back as `usbserial-120` is recognised by VID/PID and
  serial number (or, without one, its USB socket) and reconnected, with a
  notice; the session registry follows too. Never a guess between identical
  adapters; ports without USB identity keep path-only reconnect.
- **Log parts** (SPEC S32): `--log-rotate-mb N` splits the three log files
  together into `output.001.log`, … at a line end, each part with its own
  banner; `--log-keep-parts K` deletes the oldest. Off by default.
- **PTY mirrors for `remote` and `attach`** (SPEC S33): `--proxy-dir` turns a
  remote stream into local PTYs (`<host>-<port>-0`, `<session>-attach-0`).
  Writes through a read-only code are refused, and said so.
- **SSH tunnel guide** in the README: loopback-only serving, `-L`, `-R` for a
  lab behind NAT, `-J`, and `autossh`.

### Changed
- `ports`, and the port picker, list USB adapters first, and no longer print
  pyserial's `"n/a"` placeholder as a description (`ports --json` gives `""`).
  Nothing is hidden: a built-in UART has no USB identity either.

## [1.20260930.1193756] — 2026-09-30

The proxy became safe to leave on a network, and the TUI learned to find
things — a port, a line, a code that scrolled away.

### Changed
- **BREAKING: `--serve` without `--auth` no longer uses `123456`** (SPEC S22).
  It generates a random code for the run — full access, printed at startup —
  because `--serve` listens on every interface and a well-known code there is
  an open door. Pass `--auth 123456` to keep the old behaviour explicitly; any
  code you give is used as given.
- `--serve` on every interface now says so at startup, with the flag that keeps
  it on this machine (`--listen 127.0.0.1`).

### Added
- **Say who has the port when it is busy** (SPEC S21). Opening a port that
  another process holds used to report `[Errno 16] Resource busy` once a second
  and nothing else. Now the first busy attempt of a streak raises one NOTICE
  naming the holder and the way in:
  - a **background session of ours** — the likeliest case since `start` —
    is named from its state file, with `uart-proxy attach <name>`, its mirror
    directory if it has one, and `uart-proxy stop <name>`;
  - anything else is named from `lsof` (`screen (pid 777)`) where `lsof` exists
    and can see it, with the ways to share the port on purpose (`--proxy-dir`,
    `--serve` + `remote`);
  - `/dev/cu.X` and `/dev/tty.X` count as one port, since holding either makes
    the other `EBUSY`. On Windows, "Access is denied" on a COM port is read as
    busy — a COM port has no permission bits to deny.
- **`start` refuses a port a background session already holds**, naming it and
  pointing at `attach` / `stop`. A second daemon there could only wait forever
  — or, if the first had opted out with `--no-exclusive`, split the stream.
- **Guessing the proxy code is rate-limited** (SPEC S22): 10 failed attempts
  from one address within a minute and that address is refused for 10 minutes,
  even with the right code, with a notice in the session. Per address and
  expiring on its own, so a stranger cannot use it to lock everyone out, and a
  right code clears earlier typos.

- **The auth code can be found again** after the console scrolls (SPEC S23):
  - `Ctrl+] i` in the TUI shows proxy address, codes and roles, the `attach`
    name, mirrors and log folder — as a toast only, never in the log or sent to
    proxy clients; the status bar shows `serve <addr>` without the code.
  - `connect --serve` is now registered like a background session, marked
    `(foreground)`: `status --show-auth` prints its codes, `attach` joins it
    from another terminal, and it is unregistered on exit. `status` hides codes
    unless asked.
  - `attach` reaches a session bound to `0.0.0.0` via `127.0.0.1`.

- **Fixed proxy codes in `~/.uart-proxy/config.toml`** (SPEC S24):
  `[proxy] auth = ["code", "code:readonly"]`, used by `connect --serve` and
  `start` when `--auth` is absent. Ignored with a note — falling back to a
  generated code — if the file is readable by others, the value is malformed,
  or Python is 3.10 (no `tomllib`).
- **A refused proxy client stops instead of retrying** (SPEC S22). `remote` and
  `attach` used to reconnect with a wrong code about once a second — and with
  the new rate limit, ban their own address within seconds. The realistic
  trigger: restart a `connect --serve`, get a fresh generated code, and every
  client still holding the old one reconnects with it. An unreachable server is
  still retried.

- **Choose the port from a list** (SPEC S27): `connect` without `--port`, in a
  terminal. Without one it lists the ports and exits rather than wait.
- **`--profile NAME|FILE.toml`** (SPEC S30) for `connect` and `start`: a
  `uart_helper` profile's `[defaults]` fill in the serial settings no flag
  gives, and its `[[rules]]` find the port when `--port` is left out.
- **Search the log** (SPEC S28): `Ctrl+] /` shows only the lines that match,
  highlighted and live, until an empty search; `Ctrl+W` copies what is shown.
- **Log banners** (SPEC S25): the timestamped logs open with version, port,
  settings and the wall-clock time of elapsed 0, and close with the window in
  both axes. `output.log` is untouched.
- **`--echo-tx`** (SPEC S29): proxy clients see each line typed into the
  device, by anyone but themselves. Off by default — passwords are lines too.
- **`--max-clients N`** (SPEC S26, default 16): connections beyond it are told
  the server is full and keep retrying.
- **Mirrors in the status bar**: `mirrors N`, and `dropped …` in red once a
  reader falls behind.

### Fixed
- **Typed lines never completed with the default `--eol cr`**, so they were
  missing as `>` lines from the log view and from `--log-tx` recordings: the
  line assembler only ended a line on `\n`. What is typed now ends a line on
  `\r`, `\n` or `\r\n`; device output keeps treating a bare `\r` as a repaint.
- **`connect --no-tui --no-reconnect` never exited when the port would not
  open.** The session gave up after one attempt but said nothing, and headless
  mode waits for `disconnected`. Giving up is now announced, once.
- **Waiting for a device printed a `waiting` line every second** — 3,600 an
  hour in the log view and on stderr. It is now said once, and again only when
  the reason changes (absent → busy, say).
- **`start --listen-port 0` reported `proxy 127.0.0.1:0`.** The banner is
  printed by the launching process, which read the port it had asked for; it
  now reads back the one the daemon actually bound.

## [1.20260929.1214712] — 2026-09-29

Character mode became a terminal, not just a keyboard.

### Added
- **A real terminal view for character mode** (SPEC S20) — device output is now
  rendered by a terminal emulator (`pyte`) while character mode is active, and
  the view switches with the mode.
  - This fixes what made character mode look broken: **one typed character per
    row**. The session force-flushes a partial RX line after 0.2s of silence so a
    prompt without a newline (`login: `) shows up at all — and at typing speed
    every echoed keystroke cleared that timer on its own, so `ls` arrived as two
    rows. A log can only append a finished line; a shell talks to a screen.
  - Tab completion, `^C`, backspace, `\r` repaints, ANSI colour, `clear`, `vi`
    and `htop` all behave, because the cursor can now move.
  - The division is clean: the **screen** is the device now, the **log** is the
    timestamped history, and `<prefix> c` switches. Both are fed at all times — a
    view that only started tracking when you looked at it would open blank — at a
    measured 1.19 MB/s, about 1% of a core at 115200 baud.
  - `<prefix> k` clears the **screen** and keeps the log, which is what `clear`
    means at a prompt. Notes are still written to the log and additionally
    raised as a toast while it is hidden, one per block rather than one per line.
  - The status bar reports the emulated size (`screen 135×34`). A serial line
    cannot carry a window size — RS-232 has no `SIGWINCH`, and `screen` over
    serial has the same limitation — so that is the number to set on the device
    with `stty rows 34 cols 135`.
  - TX is deliberately **not** echoed into the screen: the far end echoes what
    you type, so drawing it locally too would double every character and would
    show what a password prompt is deliberately hiding.
  - [`examples/check_char_mode.py`](./examples/check_char_mode.py) checks all of
    this against a **real interactive `bash`** on a pty with no hardware — Tab
    really completes, in place; `^C` abandons the line; ↑ recalls from the
    shell's own history — so it works as a smoke test as well as a demonstration.
- `pyte>=0.8` is now a core dependency, for the same reason `textual` is: without
  it character mode cannot render a shell, and talking to a shell is what
  character mode is for.

### Fixed
- **The footer offered `Ctrl+Q` “Quit” in character mode, where it does not
  quit.** `Ctrl+Q` is XON and belongs to the device, so `on_key` already sent it
  down the wire — but the binding stayed in the footer, so pressing it did
  nothing visible and left you believing you had quit. The session then carried
  on running with the port still claimed under `TIOCEXCL`, which looks exactly
  like a leak the next time you connect. `quit` and `follow_bottom` now stand
  down with the other bindings in character mode, and the status bar carries the
  quit hint (`char (Ctrl+] c · Ctrl+] q quit)`) since it is then the only thing
  on screen that says how to leave.
- **A hidden screen could come back with its opening rows missing.** `pyte` clips
  from the *top* when it shrinks — correct for a live terminal — and a hidden
  widget reports 0×0, so the screen was being crushed to a single cell and then
  restored at the real size with the first rows gone. The hidden screen is now
  kept at the size of the region it will be drawn in, and degenerate sizes are
  ignored rather than applied.
- **Colour from the device could raise instead of render.** `pyte` and Rich
  disagree about colour names — ANSI 33 is `brown` to one and `yellow` to the
  other, which rejects `brown` outright; the bright variants lack Rich's
  underscore; and `BG_AIXTERM[105]` is misspelled `bfightmagenta` in pyte 0.8.2.
  The mapping is now exhaustive over pyte's own tables, with a test that fails if
  pyte ever adds a name Rich does not know.

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
