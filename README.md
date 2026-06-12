# uart-proxy

[![PyPI](https://img.shields.io/pypi/v/uart-proxy.svg)](https://pypi.org/project/uart-proxy/)
[![PyPI Downloads](https://static.pepy.tech/badge/uart-proxy)](https://pepy.tech/projects/uart-proxy)
[![Python](https://img.shields.io/pypi/pyversions/uart-proxy.svg)](https://pypi.org/project/uart-proxy/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

A cross-platform **UART log reader / controller** for macOS and Windows 11 —
think PuTTY / Minicom, but a small tool you own and can extend.

It reads and writes a UART, shows the log on **two time axes at once** (absolute
wall-clock and relative elapsed time), records to files, can **re-share the port
over a network socket** (with an auth code + role), and supports a **plugin
system** for line-by-line pattern matching.

The serial engine is the [`uart-helper`](https://pypi.org/project/uart-helper/)
library (built on pyserial), installed from PyPI; this project layers the
viewer, recorder, proxy, plugins, and UI on top of it.

> See **[README.arch.md](./README.arch.md)** for the architecture diagrams and
> **[ROADMAP.md](./ROADMAP.md)** for the action items / status.

---

## Features

| # | Requirement | Status |
|---|-------------|--------|
| 1 | Detect all UART ports; pick one for read & write | ✅ `uart-proxy ports`, `connect` |
| 2 | Show local time **and** elapsed time; output `output.log`, `output-timestamp.log`, `output-fulltimestamp.log` | ✅ recorder + dual time axis |
| 3 | Simple ASCII display (BBS / telnet style) | ✅ `--encoding latin-1`, text view |
| 4 | Re-share the port via a socket proxy with auth | ✅ `--serve --auth CODE[:role]` |
| 5 | Command-line driven | ✅ `uart-proxy …` |
| 6 | Connect to a **local UART** or a **remote socket** | ✅ `connect` / `remote` |
| 7 | Plugin architecture for pattern watching (grep-style) | ✅ `--grep`, `--plugin-dir`, `Plugin` API |

---

## Install

Once published to PyPI:

```bash
pipx install uart-proxy
```

From source (development):

```bash
git clone https://github.com/changyy/py-uart-proxy.git
cd py-uart-proxy
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                                 # editable install + test deps
# uart-helper (the serial engine) and textual (the TUI) install automatically.
```

Verify:

```bash
uart-proxy --version
uart-proxy ports
```

---

## Usage

### 1. List ports

```bash
uart-proxy ports
uart-proxy ports --json
```

### 2. Open a local UART (read & write, with the TUI)

```bash
uart-proxy connect --port /dev/tty.usbserial-110 --baud 115200
uart-proxy connect --port COM3 --baud 115200          # Windows
```

In the TUI:

| Key / action | Effect |
|-----|--------|
| type + `Enter` | send a line to the device (the input box is focused on start) |
| `Ctrl+W` | **copy the whole log** to the clipboard as clean text |
| `Ctrl+E` | **toggle Select Mode** — drag-select a range with the terminal |
| **mouse wheel up** | scroll into history — auto-follow **pauses** |
| **mouse wheel down to bottom** | auto-follow **resumes** |
| `End` | jump to the bottom and resume following |
| `Ctrl+T` | cycle timestamp display: none → relative → full |
| `Ctrl+Y` | toggle hex view |
| `Ctrl+K` | clear the log **and** reset the `Ctrl+W` copy range |
| `Ctrl+Q` | quit |

The status bar shows the connection state (`● live` / `○ waiting`), the port and
baud (e.g. `… @ 115200 8N1`), the elapsed clock, byte counts, and whether the
view is following the tail (`follow` / `paused ▲`).

#### Copying log text

Two ways, depending on what you need:

**Whole log — `Ctrl+W` (recommended, always clean).** Copies the entire
in-memory log to the clipboard as plain text — no border, no padding, no colour
codes. It uses the OS-native clipboard (`pbcopy` on macOS, `clip` on Windows,
`xclip`/`wl-copy` on Linux), so it works even in macOS Terminal.app (which
doesn't support the OSC-52 escape that many TUIs rely on). Best when you want to
grab the log and paste it into a ticket/chat.

> The copy range is everything since the last clear. Press **`Ctrl+K`** to clear
> the display and reset that range, let the lines you care about accumulate,
> then **`Ctrl+W`** to copy just that range.

**A specific range — `Ctrl+E` (Select Mode).** While the app is live it captures
the mouse for scrolling, so the terminal's own click-drag selection is off.
Press `Ctrl+E` to:

- **freeze** the view (incoming data won't scroll it away), and
- **hand the mouse back to your terminal**, so you can **drag-select** a range
  and copy with your terminal's copy (⌘C / Ctrl+C / right-click).

Press `Ctrl+E` again to resume live scrolling. The log has **no border**, so the
selection won't pick up frame characters, and macOS terminals trim trailing
spaces on copy. (If you still see padding, use `Ctrl+W` for a guaranteed-clean
copy.)

### Auto-reconnect / wait for device

If the port isn't there yet (or you haven't plugged the adapter in), `connect`
no longer fails — it shows `○ waiting` and **attaches automatically as soon as
the device appears**. If the device is unplugged mid-session it shows
`reconnecting` and re-attaches when it returns. Disable with `--no-reconnect`;
tune the retry period with `--reconnect-interval SECONDS`.

### Baud rate

`--baud` defaults to **115200**, so it is optional. The effective baud (and
framing) is always visible in the status bar, e.g. `… @ 115200 8N1`.

### Line ending on Enter (`--eol`)

Pressing Enter appends a line ending, default **`cr`** (`\r`) — the convention
for Unix consoles (same as PuTTY/minicom/screen). Using `crlf` against such a
console sends two line-ends, which the device sees as **two** Enters (e.g. the
login prompt prints twice). Change it if your device needs something else:

```bash
uart-proxy connect --port … --eol cr     # default: \r  (Unix console, login prompts)
uart-proxy connect --port … --eol crlf   # \r\n (some modems / AT firmwares)
uart-proxy connect --port … --eol lf      # \n
uart-proxy connect --port … --eol none    # send exactly what you typed
```

### 3. Time axes & log files

Every line carries both axes. The display can show either:

```
relative:  00:00:10.0000  < device output here
full:      2026-06-12 08:40:20 | 00:00:10.0000  < device output here
```

Recording writes three files:

```
output.log                 raw RX bytes, exactly as received
output-timestamp.log       [00:00:10.0000] line          (elapsed only)
output-fulltimestamp.log   [2026-06-12 08:40:20 | 00:00:10.0000] line
```

**Where they go:** by default each run gets its own folder so nothing is ever
clobbered:

```
~/.uart-proxy/sessions/<YYYYmmdd-HHMMSS>/output*.log
```

The path is printed at startup and shown live in the TUI status bar
(`rec→…`). Override with `--output-dir DIR` (use `--output-dir .` for the
current directory), rename the files with `--log-base NAME`, disable with
`--no-log`, or append instead of overwrite with `--log-append`.

#### Retention (auto-cleanup of the session store)

The default store is pruned automatically on each run along two axes:

- **age** — sessions older than **30 days** are deleted;
- **total size** — if the store still exceeds **500 MB**, the **oldest**
  sessions are deleted (logrotate-style) until it fits.

Either can be changed per-run or made permanent. `0` disables an axis. The
in-progress session is never deleted.

```bash
# per-run override
uart-proxy connect --port … --max-age-days 14 --max-total-mb 1000

# inspect / prune manually
uart-proxy sessions                 # list sessions + current policy
uart-proxy sessions --prune         # apply the policy now
uart-proxy sessions --json
```

Permanent defaults live in `~/.uart-proxy/config.toml`:

```toml
[retention]
max_age_days = 30      # 0 = keep forever
max_total_mb = 500     # 0 = no size cap
```

Precedence: CLI flag > config file > built-in default.

### 4. BBS / telnet style ASCII

```bash
uart-proxy connect --port /dev/ttyUSB0 --encoding latin-1 --eol cr
```

### 5. Share the port over the network (socket proxy)

On the machine with the UART:

```bash
uart-proxy connect --port /dev/ttyUSB0 --serve \
    --auth 123456 \              # full access (read + write)
    --auth 000000:readonly       # read-only (e.g. for a mobile viewer)
```

From another machine:

```bash
uart-proxy remote --host 192.168.1.10 --port 9600 --auth 123456
```

#### Attaching to a `uart_helper`-owned port (integration apps)

If another app already owns the serial port via
[`uart-helper`](https://pypi.org/project/uart-helper/), uart-proxy can't open it
(UART is exclusive). Instead, have that app expose a **loopback-TCP broker** speaking
this same protocol, and attach with `remote` — **no uart-proxy changes needed**.

A drop-in, dependency-free broker (stdlib + uart_helper, portable to Windows &
macOS — loopback TCP, not a Unix socket file) lives at
[`examples/uart_helper_broker.py`](./examples/uart_helper_broker.py). It has two
modes:

**Embedded / tee mode** — your app keeps owning the UART (it reads & uses the
data) and just tees a copy to uart-proxy. This avoids two readers on one port:

```python
from uart_helper import UARTDevice, PortIdentity, UARTConfig
from uart_helper_broker import UartHelperBroker   # or uart_helper.broker

dev = UARTDevice(PortIdentity(device="COM3"), UARTConfig(baudrate=115200))
dev.open()
broker = UartHelperBroker(host="127.0.0.1", port=9600,
                          auth={"123456": "full", "000000": "readonly"},
                          on_tx=lambda b: dev.write(b),   # client → device
                          source="my-app COM3")
broker.start()
while running:
    data = dev.read(...).data
    if data:
        my_app_consume(data)        # your app uses the data
        broker.publish_rx(data)     # …and tees it to uart-proxy
```

**Owned mode** — a standalone bridge where the broker opens the port itself:

```bash
python examples/uart_helper_broker.py --port COM3 --baud 115200 \
    --auth 123456 --auth 000000:readonly
```

Either way, attach from anywhere with the **unmodified** client:

```bash
uart-proxy remote --host 127.0.0.1 --port 9600 --auth 123456
```

The wire protocol is specified in [PROTOCOL.md](./PROTOCOL.md).

A read-only client (`--auth 000000`) can watch the stream but cannot send.

> **Why the proxy matters — UART is exclusive-open.** A serial port can only be
> held by **one** process at a time (the OS gives it exclusive access; pyserial
> and `screen` both lock it). If `screen -U /dev/tty.usbserial-120 115200` is
> running, `uart-proxy` cannot open the same port, and vice-versa. There is no
> OS-level "multiple readers" for a raw UART — bytes are delivered once. To let
> several people watch (and optionally one control), make **uart-proxy the
> single owner** (`--serve`) and have everyone else attach via the proxy with a
> `readonly` (or `full`) auth code. That is the supported multi-viewer model.

### 6. Plugins (pattern watching)

Quick grep:

```bash
uart-proxy connect --port /dev/ttyUSB0 --grep ERROR --grep "panic.*" --grep-ignore-case
```

Load your own plugins:

```bash
uart-proxy connect --port /dev/ttyUSB0 --plugin-dir ./plugins
```

A plugin is a `Plugin` subclass — override `on_line` to react to patterns and
optionally write back to the device. See
[`plugins/example_alert_plugin.py`](./plugins/example_alert_plugin.py).

### Headless (no TUI)

Add `--no-tui` to stream to stdout instead — handy for a server box that only
needs to serve the proxy and write logs:

```bash
uart-proxy connect --port /dev/ttyUSB0 --serve --auth 123456 --no-tui
```

---

## Project layout

```
py-uart-proxy/
  pyproject.toml
  README.md            ← you are here
  README.arch.md       ← architecture diagrams
  ROADMAP.md           ← action items / status
  src/uart_proxy/
    core/    timestamp · events · bus · line_assembler · recorder · session
    io/      source (ABC) · uart_source · socket_source
    proxy/   protocol (JSON-lines + auth/roles) · server
    plugins/ base · manager · builtin/grep
    ui/      tui (Textual) · headless
    cli.py
  plugins/   example user plugin
  tests/
```

---

## Requirements

- Python 3.10+
- pyserial ≥ 3.5
- textual ≥ 0.60 (core dependency, powers the TUI; `--no-tui` runs without using it)
- [`uart-helper`](https://pypi.org/project/uart-helper/) ≥ 1.0 (from PyPI)

## License

MIT © Yuan-Yi Chang
