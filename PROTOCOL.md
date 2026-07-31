# uart-proxy wire protocol

The protocol uart-proxy speaks over the network, and the contract any broker
(including a future `uart_helper.broker`) must implement so uart-proxy can
attach **unchanged**. It is the single source of truth; keep
[`proxy/protocol.py`](./src/uart_proxy/proxy/protocol.py) and
[`examples/uart_helper_broker.py`](./examples/uart_helper_broker.py) in sync
with this document.

## Transport

- **Loopback/LAN TCP.** One TCP connection per client. Bind to `127.0.0.1` for
  local-only, `0.0.0.0` for LAN.
- **Why not a Unix socket file:** it is not portable — CPython does not expose
  `AF_UNIX` on Windows. TCP on `127.0.0.1` behaves identically on Windows and
  macOS and is the standard mechanism here.

## Framing

- One **JSON object per line**, UTF-8, terminated by `\n`.
- Lines that don't parse as a JSON object are ignored.

## Handshake (required, first line)

Client's first line MUST be an auth request:

```json
{"type": "auth", "code": "123456"}
{"type": "auth", "code": "123456", "replay": 2000}
```

Server replies with exactly one of:

```json
{"type": "auth_ok", "role": "full", "source": "/dev/tty.usbserial @ 115200", "replay_available": 1832, "elapsed": 9482.11}
{"type": "auth_fail", "reason": "invalid code"}
```

On `auth_fail` the server closes the connection.

- `replay` (optional, client) — ask for up to N lines of recent history before the
  live stream starts. Omit it, or send `0`, for live only.
- `replay_available` (optional, server) — how many lines the server *could* have
  offered. Informational.
- `elapsed` (optional, server) — where the server's session is on its own clock,
  in seconds. A client should **adopt this as its own origin** so that replayed
  and live output share one elapsed axis, and so a line's elapsed value means the
  same thing as in the server's log files. A client that ignores it measures from
  its own connect instead, and its elapsed column will jump backwards where the
  replay block ends.

All three fields are additive: a client or server that doesn't know them behaves
exactly as before.

## Replay (optional, immediately after `auth_ok`)

If the client asked for `replay` and the server has history, the server sends it
**before adding the client to the live fan-out** — so everything after the block
is guaranteed to be the present:

```json
{"type": "replay", "seq": 812, "wall": "2026-07-31 19:09:52", "elapsed": 9470.52, "text": "login:"}
{"type": "replay", "seq": 813, "wall": "2026-07-31 19:09:53", "elapsed": 9471.88, "text": "root@target:~#"}
{"type": "replay_end", "count": 2, "from": "2026-07-31 19:09:52", "to": "2026-07-31 19:09:53"}
```

- Replayed lines are their **own message type**, never `rx`: they are the past
  and must not be mistaken for what is happening now. `wall` and `elapsed` are
  the server's, from when the line actually arrived.
- `replay_end` always follows, even with `count: 0` — a client waiting for it must
  not hang against a server that has no history.
- A client should display these distinctly (uart-proxy dims them between
  `── replayed … ──` and `── live ──` dividers). They must **not** be fed back
  through a recorder or a plugin pipeline: the server already did that.

### Roles

| Role | May read | May write (`tx`) |
|------|----------|------------------|
| `full` | ✅ | ✅ |
| `readonly` | ✅ | ❌ (rejected with a `notice`) |

Roles are bound to auth codes server-side. `readonly` is the intended limited
mode (e.g. a mobile viewer).

## Server → client (after auth)

```json
{"type": "rx", "seq": 12, "wall": "2026-06-12 08:40:20", "elapsed": 10.0042, "hex": "48656c6c6f", "text": "Hello"}
{"type": "notice", "text": "grep[ERROR] #1: ...", "meta": {}}
{"type": "status", "state": "connected", "meta": {}}
{"type": "pong"}
```

- `rx` — device output, **live only**. `hex` is authoritative (raw bytes); `text`
  is a UTF-8 best-effort decode for display. `wall` is the server's local time
  (`%Y-%m-%d %H:%M:%S`); `elapsed` is seconds since the server session started.
  History is never sent as `rx` — see Replay above.
- `seq` is a monotonically increasing counter.

## Client → server (after auth)

```json
{"type": "tx", "hex": "636d640d"}
{"type": "tx", "text": "cmd", "eol": "cr"}
{"type": "ping"}
```

- `tx` — bytes to write to the device. Provide either `hex` (raw) or `text`
  plus an optional `eol` ∈ {`crlf`, `lf`, `cr`, `none`} (default **`cr`** — the
  Unix-console convention; `crlf` can cause a double newline / double prompt).
  uart-proxy's own client always sends `hex`.
- A `tx` from a `readonly` client is rejected (the server returns a `notice` and
  does not write).
- `ping` → server replies `{"type": "pong"}`.

## Robustness expectations

- The server must fan out to multiple clients without letting a slow client
  stall the serial read loop (per-client send queue; drop the client if its
  queue overflows).
- One writer at a time to the device (serialise `tx`).
- `recv()` returning empty = orderly close; a `recv` timeout is **not** EOF.

## Reference implementations

- Server (full session/bus integration): `proxy/server.py`.
- Standalone broker for a `uart_helper`-owned port (stdlib + uart_helper only,
  drop-in for `uart_helper.broker`): `examples/uart_helper_broker.py`.
- Client: `io/socket_source.py` (used by `uart-proxy remote`).
- Interop test proving an unmodified uart-proxy client attaches to the broker:
  `tests/test_broker_interop.py`.
