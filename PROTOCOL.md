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
```

Server replies with exactly one of:

```json
{"type": "auth_ok", "role": "full", "source": "/dev/tty.usbserial @ 115200"}
{"type": "auth_fail", "reason": "invalid code"}
```

On `auth_fail` the server closes the connection.

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

- `rx` — device output. `hex` is authoritative (raw bytes); `text` is a UTF-8
  best-effort decode for display. `wall` is the server's local time
  (`%Y-%m-%d %H:%M:%S`); `elapsed` is seconds since the server session started.
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
