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
        f"Sec-WebSocket-Key: {key}{nl}Sec-WebSocket-Version: 13{nl}{nl}"
    )
    s.send(req.encode())
    resp = s.recv(4096)
    if b"101" not in resp.split(b"\r\n")[0]:
        raise ConnectionError(f"handshake failed: {resp[:80]!r}")
    return s


def _read_frames(sock: socket.socket, deadline: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    buf = b""
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            raise ConnectionError("closed by server")
        buf += chunk
        while len(buf) >= 2:
            opcode = buf[0] & 0x0F
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
            if opcode == 9:
                try:
                    sock.send(bytes([0x8A, len(payload)]) + payload)
                except OSError:
                    raise ConnectionError("pong failed") from None
            elif opcode == 8:
                raise ConnectionError("CLOSE received")
            elif opcode in (1, 2):
                try:
                    out.append(json.loads(payload.decode()))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
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
                sock = _ws_connect()
                self.connected = True
                reconnects = 0
                while not self._stop.is_set():
                    for data in _read_frames(sock, time.time() + 5.0):
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
