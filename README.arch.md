# uart-proxy — Architecture

This document explains how the PC app is put together and why. For usage see
[README.md](./README.md); for planned work see [ROADMAP.md](./ROADMAP.md).

## Design principles

1. **Engine / interface separation.** A single data pipeline feeds every
   consumer (TUI, headless, recorder, plugins, proxy). The UI is just one more
   subscriber, so the CLI, the TUI and a remote client all share identical
   behaviour.
2. **One transport abstraction.** A local UART and a remote socket both look
   like a `DataSource` (open / read / write / close). The session does not know
   or care which it is driving.
3. **The proxy protocol is a first-class, language-agnostic contract**
   (JSON-lines + auth). It is the gateway for *every* non-local client —
   another PC today, a Flutter mobile app later.
4. **Don't fork the engine.** `uart_helper` / `usb_helper` are reused as-is.
   Enhancements are proposed upstream, not patched in here.

## Component map

```
                         ┌──────────────────────────────────────────┐
                         │                CLI (cli.py)               │
                         │   ports · connect · remote  +  wiring      │
                         └───────────────┬───────────────────────────┘
                                         │ builds & wires
                                         ▼
   ┌───────────────┐          ┌──────────────────────┐
   │  DataSource   │  bytes   │     UartSession       │   Event       ┌─────────────┐
   │  (transport)  │◄────────►│   (the data pump)     │──────────────►│  EventBus   │
   └───────┬───────┘  read/   │ timestamps + line     │   publish     │ (pub/sub)   │
           │          write   │ assembly + counters   │               └──────┬──────┘
   ┌───────┴────────┐         └──────────────────────┘                       │ fan-out
   │ UartSource     │  (uart_helper.UARTDevice → pyserial)                    │
   │ SocketSource   │  (remote uart-proxy, JSON-lines client)        ┌────────┼─────────┬──────────────┐
   └────────────────┘                                                ▼        ▼         ▼              ▼
                                                              ┌──────────┐ ┌────────┐ ┌──────────┐ ┌──────────┐
                                                              │ Recorder │ │ Plugin │ │  Proxy   │ │   UI     │
                                                              │ 3 files  │ │Manager │ │ Server   │ │ TUI /    │
                                                              └──────────┘ └────────┘ └────┬─────┘ │ headless │
                                                                                           │       └──────────┘
                                                                              JSON-lines over TCP (auth + role)
                                                                                           │
                                                                          ┌────────────────┴───────────────┐
                                                                          ▼                                ▼
                                                                  remote PC (uart-proxy remote)     future mobile (Flutter)
```

## The dual time axis

`TimestampTracker` captures one start instant (`datetime.now()` + a monotonic
reference). Every `Stamp` derives its wall-clock time from
`start_wall + monotonic_delta`, so log times never jump if the system clock is
adjusted mid-session.

```
Stamp ─┬─ wall     →  2026-06-12 08:40:20            (absolute)
       └─ elapsed  →  00:00:10.0000  (HH:MM:SS.ffff) (relative)

window views:
  absolute  2026-06-12 08:40:10 ~ 2026-06-12 08:40:20 (10s)
  relative  00:00:00.0000 ~ 00:00:10.0000
```

## Event flow (per received chunk)

```
source.read() ─► bytes
   │
   ├─► Event(DATA, RX)                  → live display · raw output.log · proxy rx
   └─► LineAssembler.feed() ─► lines
            └─► Event(LINE, RX)         → output-timestamp.log
                                          output-fulltimestamp.log
                                          plugins (on_line)
```

`write()` produces the mirror-image `DATA(TX)` / `LINE(TX)` events. A short idle
flush emits a buffered partial line so prompts without a trailing newline (e.g.
`login: `) still surface.

## Threading model

| Thread | Owner | Job |
|--------|-------|-----|
| read loop | `UartSession` | pull bytes, build & publish events |
| accept | `ProxyServer` | accept TCP clients |
| reader (per client) | `ProxyServer` | parse client→server messages (tx/ping) |
| writer (per client) | `ProxyServer` | drain that client's queue to its socket |
| main / asyncio | Textual TUI | render; bus callbacks hop in via `call_from_thread` |

Bus callbacks run in the publishing (read) thread and must be quick. The proxy
only *enqueues* bytes per client, so a slow remote client can never stall the
serial pump (its queue fills and the client is dropped).

## Proxy protocol (summary)

One JSON object per line, UTF-8, `\n`-terminated. Full spec in
[`proxy/protocol.py`](./src/uart_proxy/proxy/protocol.py).

```
client → server   {"type":"auth","code":"123456"}
server → client   {"type":"auth_ok","role":"full","source":"…"}   | {"type":"auth_fail","reason":"…"}
server → client   {"type":"rx","seq":N,"wall":"…","elapsed":F,"hex":"…","text":"…"}
server → client   {"type":"notice"|"status", …}
client → server   {"type":"tx","hex":"…"}            (full role only)
client → server   {"type":"tx","text":"…","eol":"crlf"}
client → server   {"type":"ping"}  → {"type":"pong"}
```

**Roles:** `full` (read + write) and `readonly` (read only — the natural
"limited" mode for a mobile viewer). Roles are bound to auth codes server-side.

## Deployment topologies

The protocol above is the contract. Because both ends speak it, the same
`uart-proxy remote` client attaches to **either** of these, identically
(`--host … --port … --auth …`). Transport is loopback/LAN **TCP** on both
(no Unix socket file — not portable to Windows). See
[PROTOCOL.md](./PROTOCOL.md).

```
Topology 1 — uart_helper-owned (integration app is the device owner)
  ┌─────────────────────────────────────────┐
  │ integration app  (imports uart_helper)   │  owns & reads the UART
  │   dev.read()/write()  ← app's own logic   │
  │   uart_helper.broker (embedded/tee)       │  publish_rx(data) ─┐  on_tx ◄─┐
  └───────────────────────────┬───────────────┘                   │          │
                              TCP ip:port (same protocol)          │          │
                               ▼                                   ▼          │
                       uart-proxy remote  ──────── reads stream ───┘  writes ─┘

Topology 2 — uart-proxy-owned (uart-proxy is the device owner)
  ┌─────────────────────────────┐
  │ uart-proxy connect --serve  │  owns & reads the UART
  └──────────────┬──────────────┘
               TCP ip:port (same protocol)
                 ▼
        uart-proxy remote   (on another machine)  ── reads & writes
```

| | Device owner / reader | Opens the ip:port | Connects in | Status |
|--|--|--|--|--|
| **Topology 1** | the integration app via `uart_helper` | `uart_helper.broker` (embedded/tee: `publish_rx` out, `on_tx` in) | `uart-proxy remote` | broker written & tested ([examples/uart_helper_broker.py](./examples/uart_helper_broker.py)); pending fold-in to `uart_helper` |
| **Topology 2** | `uart-proxy connect` | `uart-proxy --serve` | `uart-proxy remote` | shipping today |

Both support read+write, the `full`/`readonly` roles, an auth code, and either
`127.0.0.1` (local) or `0.0.0.0` (LAN). In Topology 1 the device has a single
reader (the app); the broker only *tees* a copy out and feeds remote input back
in via `on_tx`, so there is never a second reader on the port.

## Why these choices

- **Python** reuses the existing `uart_helper`/`usb_helper` investment and makes
  the proxy and plugin system the cheapest to build and extend.
- **Textual TUI** matches the PuTTY/Minicom mental model, is cross-platform, and
  needs no GUI packaging. `--no-tui` keeps a pure-CLI path for servers.
- **JSON-lines proxy** is trivial to implement from any language, which is what
  makes the future Flutter mobile client a thin consumer rather than a rewrite.
