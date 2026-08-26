"""CCI realtime feed — ccidx WebSocket listener as a background thread.

One process-wide singleton (started lazily by the futures board) keeps a
latest-quote snapshot for every index the exchange pushes (~3s cadence
during CN trading sessions).  Reconnects with backoff; a dead feed never
crashes the board — the UI simply keeps the last daily close and the
status line reports the feed as down.
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any

WS_HOST = "www.ccidx.com"
WS_PORT = 80
WS_PATH = "/CCI-ZZZS/realTimeWebsocket/idx"
FEED_SOURCE = "ccidx realtime WS"

_BACKOFF = [2, 5, 15, 30, 60]


def _ws_connect() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect((WS_HOST, WS_PORT))
    key = base64.b64encode(b"stamm-cci-feed-001").decode()
    nl = "\r\n"
    req = (
        f"GET {WS_PATH} HTTP/1.1{nl}Host: {WS_HOST}{nl}"
        f"Upgrade: websocket{nl}Connection: Upgrade{nl}"
        f"Sec-WebSocket-Key: {key}{nl}Sec-WebSocket-Version: 13{nl}"
        f"Origin: http://www.ccidx.com{nl}{nl}"
    )
    s.send(req.encode())
    # The 101 response carries a large header block (~20 KB of cache
    # headers); read until the terminator so no header bytes leak into
    # the frame parser (stray 'X' = 0x58 parses as a CLOSE opcode).
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("handshake eof")
        buf += chunk
        if len(buf) > 65536:
            raise ConnectionError("handshake headers too large")
    if b"101" not in buf.split(b"\r\n", 1)[0]:
        raise ConnectionError(f"handshake failed: {buf[:80]!r}")
    # The first data frames usually arrive in the same TCP segment as
    # the headers; hand the post-terminator remainder to the parser
    # instead of discarding it mid-frame.
    _head, _, remainder = buf.partition(b"\r\n\r\n")
    return s, remainder


def _send_client_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Masked client frame (RFC 6455 §5.3: client→server MUST mask;
    an unmasked pong makes a compliant server abort the connection)."""
    import os as _os

    mask = _os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    sock.sendall(header + mask + masked)


def _read_frames(sock: socket.socket, deadline: float,
                  buf: bytes = b"") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            raise ConnectionError("closed by server")
        buf += chunk
        fragment = b""
        while len(buf) >= 2:
            frame_head = buf[0]
            opcode = frame_head & 0x0F
            length = buf[1] & 0x7F
            offset = 2
            if length == 126:
                if len(buf) < 4:
                    break
                length = struct.unpack("!H", buf[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buf) < 10:
                    break
                length = struct.unpack("!Q", buf[2:10])[0]
                offset = 10
            if len(buf) < offset + length:
                break
            payload = buf[offset: offset + length]
            buf = buf[offset + length:]
            fin = bool(frame_head & 0x80)
            if opcode == 9:
                try:
                    _send_client_frame(sock, 0xA, payload)
                except OSError:
                    raise ConnectionError("pong failed") from None
            elif opcode == 8:
                raise ConnectionError("CLOSE received")
            elif opcode in (1, 2):
                # The server fragments one large JSON array across
                # 8192-byte frames (first 0x01 no-FIN, continuations
                # 0x00, last with FIN) — reassemble before parsing.
                if fin:
                    fragment = payload
                    try:
                        out.append(json.loads(fragment.decode()))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                    fragment = b""
                else:
                    fragment = payload
            elif opcode == 0:
                fragment += payload
                if fin:
                    try:
                        out.append(json.loads(fragment.decode()))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                    fragment = b""
    return out


class CciFeed(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="cci-feed")
        self.snapshot: dict[str, dict[str, Any]] = {}
        self.updated_at: str = ""
        self.connected = False
        self._stop = threading.Event()

    def run(self) -> None:
        reconnects = 0
        while not self._stop.is_set():
            sock = None
            try:
                sock, remainder = _ws_connect()
                self.connected = True
                reconnects = 0
                while not self._stop.is_set():
                    for data in _read_frames(sock, time.time() + 5.0,
                                             buf=remainder):
                        remainder = b""
                        items = data if isinstance(data, list) else [data]
                        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
                        for item in items:
                            index_id = str(item.get("indexId") or "")
                            if index_id:
                                self.snapshot[index_id] = {
                                    "last": item.get("lastPrice"),
                                    "chg_pct": item.get("realTimeFluRange"),
                                    "ts": now,
                                }
                        self.updated_at = now
            except (ConnectionError, OSError):
                pass
            finally:
                self.connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            backoff = _BACKOFF[min(reconnects, len(_BACKOFF) - 1)]
            reconnects += 1
            self._stop.wait(backoff)

    def stop(self) -> None:
        self._stop.set()


_FEED: CciFeed | None = None
_FEED_LOCK = threading.Lock()


def feed() -> CciFeed:
    """Process-wide feed singleton; starts on first use."""
    global _FEED
    with _FEED_LOCK:
        if _FEED is None or not _FEED.is_alive():
            _FEED = CciFeed()
            _FEED.start()
        return _FEED
