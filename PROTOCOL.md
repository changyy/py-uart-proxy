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
{"type": "auth", "code": "123456", "replay": 500, "client": "uart-proxy mcp (claude-ai)"}
```

Server replies with exactly one of:

```json
{"type": "auth_ok", "role": "full", "source": "/dev/tty.usbserial @ 115200", "replay_available": 1832, "elapsed": 9482.11}
{"type": "auth_fail", "reason": "invalid code"}
```

On `auth_fail` the server closes the connection. If it carries
`"retry": true` — uart-proxy sends it as `server full (N connections)` — the
refusal is temporary and a client should retry; without it, retrying with the
same code cannot succeed. `reason` is for people, not
for parsing; uart-proxy sends `invalid code`, `bad message`, `expected auth`,
and — once an address has failed too often — `too many failed attempts; try
again in <N>s`, sent straight after connecting, before any `auth` is read. A
client should not retry blindly on `auth_fail`: every retry with a wrong code
counts towards that refusal.

- `replay` (optional, client) — ask for up to N lines of recent history before the
  live stream starts. Omit it, or send `0`, for live only.
- `client` (optional, client) — a name for this client, at most 64 characters,
  e.g. `uart-proxy mcp (claude-ai)`. The server shows it to whoever owns the
  session and puts it on what the client writes (SPEC S40). Omit it to stay
  unnamed.
- `replay_available` (optional, server) — how many lines the server *could* have
  offered. Informational.
- `device` (optional, server) — the device's health right now (SPEC S43):
  `{"state", "since", "since_epoch", "error", "reconnects", "last_output",
  "last_output_epoch"}`, `state` being the last `status` the session
  published (`connecting`, `connected`, `waiting`, `reconnecting`, `error`,
  `disconnected`). A client must not assume `connected` when this says
  otherwise; a server without it is taken as connected.
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
{"type": "status", "state": "connected", "meta": {}, "since": "2026-10-04 09:31:02", "since_epoch": 1791077462.1, "reconnects": 1, "error": null}
{"type": "tx_echo", "seq": 13, "wall": "2026-06-12 08:40:21", "elapsed": 11.2, "text": "reboot"}
{"type": "pong"}
```

- `tx_echo` (optional) — a line someone typed into the device, sent only when
  the server runs with `--echo-tx`, and never to the client that typed it. A
  client shows it; it must **not** write it (it already reached the device).
  Clients that don't know it should ignore it, as with any unknown `type`.

- `status` — the device's state changed. `since`, `since_epoch`,
  `reconnects` (connected again after a first connection) and `error` are
  optional (S43).
- `rx` — device output, **live only**. `hex` is authoritative (raw bytes); `text`
  is a UTF-8 best-effort decode for display. `wall` is the server's local time
  (`%Y-%m-%d %H:%M:%S`); `elapsed` is seconds since the server session started.
  History is never sent as `rx` — see Replay above.
- `seq` is a monotonically increasing counter.
- `trigger` (optional, S47) — a rule of the session fired: `{"type":
  "trigger", "seq", "rule", "name", "owner": {"kind", "client"}, "wall",
  "elapsed", "line", "groups", "context", "mark", "notify", "actions"}`. Sent
  to every client (each can read every line anyway). `seq` is the triggers'
  own counter, not `rx`'s.

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
- `resize` (optional) — `{"type": "resize", "cols": 120, "rows": 36}`: the
  client's window size, for a device that can carry one (`ssh://`,
  `telnet://`). Honoured from `full` clients only; the latest wins; ignored for a
  device that can't use it. A client sends it after authenticating and again on
  every resize.

## Triggers (optional, S47)

A client may ask the session to watch for something, and — with the `full`
role — propose a rule that acts. What it may do is the session owner's
policy, never the client's.

```json
{"type": "watch_add", "name": "kernel panic", "when": {"text": "panic"}, "context": 3}
{"type": "watch_remove", "id": "r4"}
{"type": "watch_list"}
{"type": "rule_propose", "rule": {"name": "auto-login", "when": {"text": "login:"},
                                  "actions": [{"kind": "send", "text": "root", "eol": "cr"}]}}
```

- `watch_add` → `{"type": "watch_ok", "id"}` or `{"type": "watch_fail",
  "reason"}`. A watch only makes events: a message with `actions` is refused.
  `when` is one of `text`, `regex`, `hex`, `silence` (seconds), `state`
  (`connected`, `disconnected`, `reconnected`) — S46. At most the owner's
  `max_watches` per connection (default 3); a client's watches end when it
  disconnects, and every AI-made rule when the share stops.
- `watch_remove` → `watch_ok` (only the client's own); `watch_list` →
  `{"type": "watch_list", "watches": [...]}`.
- `rule_propose` → `{"type": "proposal", "id", "status": "refused", "reason"}`
  at once (read-only, no one to ask, an invalid or unsafe rule), or
  `"status": "pending"`, then later `accepted` (with `rule`, its id) or
  `declined`. The client stays served while the owner decides.

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
