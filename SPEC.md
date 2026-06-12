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
