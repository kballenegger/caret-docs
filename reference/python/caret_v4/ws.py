"""RFC 6455, hand-rolled, standard library only.

Python ships no WebSocket implementation, and this repository takes no
third-party dependencies, so the framing lives here. It is the small
subset caret/v4 needs and no more: one connection, one message at a
time, no extensions, no compression, no subprotocols. Fragmented
messages are reassembled because a peer may send them; nothing here
sends one.

The same file serves both halves. The server side takes over a socket
that `http.server` has already read the request line from; the client
side dials, which is what the conformance checker uses to behave like a
keyboard.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: Refuse a frame larger than this outright. The protocol's own
#: max_frame_bytes is smaller and is enforced by the session; this is the
#: backstop that keeps a hostile length header from allocating a gigabyte.
MAX_PAYLOAD = 8 << 20

#: How long `close` spends reading the peer out before closing the
#: socket. A socket closed with unread inbound data is reset, and a reset
#: can discard the peer's receive buffer along with the terminal event
#: just written. §8 lets a backend answer a replayed operation from cache
#: while the client is still uploading, so this is an ordinary path.
CLOSE_DRAIN_SECONDS = 0.5


class WSError(Exception):
    """A malformed frame or a failed handshake."""


class CloseError(Exception):
    """The peer closed. `code` is the WebSocket close code."""

    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"websocket closed {code} {reason!r}")
        self.code = code
        self.reason = reason


def accept_key(key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's key."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class Connection:
    """One WebSocket connection. Not safe for concurrent readers; writes
    are serialized by the socket's own `sendall`."""

    def __init__(self, sock: socket.socket, is_client: bool) -> None:
        self.sock = sock
        self.is_client = is_client
        self.reader = sock.makefile("rb")
        self._closed = False
        self._saw_close = False

    # ---------------------------------------------------------- reading

    def set_timeout(self, seconds: float | None) -> None:
        self.sock.settimeout(seconds)

    def _read_exactly(self, count: int) -> bytes:
        data = self.reader.read(count)
        if data is None or len(data) < count:
            raise WSError("connection closed mid-frame")
        return data

    def _read_frame(self) -> tuple[int, bytes, bool]:
        header = self._read_exactly(2)
        final = bool(header[0] & 0x80)
        if header[0] & 0x70:
            raise WSError("reserved bits set")
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exactly(8))[0]
        if length > MAX_PAYLOAD:
            raise WSError(f"frame of {length} bytes exceeds the {MAX_PAYLOAD} byte ceiling")
        # RFC 6455 §5.1: a client masks, a server does not.
        if masked == self.is_client:
            raise WSError("frame masking does not match the connection role")
        mask = self._read_exactly(4) if masked else b""
        payload = self._read_exactly(length) if length else b""
        if masked and payload:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode in (OP_CLOSE, OP_PING, OP_PONG) and (not final or length > 125):
            raise WSError("control frames must be final and short")
        return opcode, payload, final

    def read_message(self) -> tuple[bool, bytes]:
        """The next data message as `(is_binary, payload)`.

        Control frames are handled here and never surface. A close frame
        raises `CloseError`, which is how every caller learns the
        operation is over.
        """
        assembled = bytearray()
        in_fragment = False
        binary = False
        while True:
            opcode, payload, final = self._read_frame()
            if opcode == OP_PING:
                self._write_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._saw_close = True
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                raise CloseError(code, payload[2:].decode("utf-8", "replace"))
            if opcode in (OP_TEXT, OP_BINARY):
                if in_fragment:
                    raise WSError("interleaved data frame")
                binary = opcode == OP_BINARY
                assembled = bytearray(payload)
            elif opcode == OP_CONTINUATION:
                if not in_fragment:
                    raise WSError("continuation without a start frame")
                assembled.extend(payload)
            else:
                raise WSError(f"unknown opcode {opcode:#x}")
            if final:
                return binary, bytes(assembled)
            in_fragment = True
            if len(assembled) > MAX_PAYLOAD:
                raise WSError("fragmented message exceeds the payload ceiling")

    # ---------------------------------------------------------- writing

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WSError("connection is closed")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        mask_bit = 0x80 if self.is_client else 0x00
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        if self.is_client:
            mask = os.urandom(4)
            header.extend(mask)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + payload)

    def send_text(self, text: str) -> None:
        self._write_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._write_frame(OP_BINARY, data)

    def send_ping(self, data: bytes = b"") -> None:
        self._write_frame(OP_PING, data)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
        try:
            self._closed = False  # _write_frame refuses a closed connection
            self._write_frame(OP_CLOSE, payload)
        except OSError:
            pass
        finally:
            self._closed = True
        if not self._saw_close:
            deadline = time.monotonic() + CLOSE_DRAIN_SECONDS
            try:
                while time.monotonic() < deadline:
                    self.sock.settimeout(max(0.01, deadline - time.monotonic()))
                    if not self.sock.recv(4096):
                        break
            except OSError:
                pass
        try:
            self.reader.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ------------------------------------------------------------ server side


def upgrade(handler) -> Connection:
    """Complete the handshake on a `BaseHTTPRequestHandler` and take the
    socket over. Raises `WSError` when the request is not a WebSocket
    upgrade, which the caller answers with a plain HTTP status."""
    headers = handler.headers
    if headers.get("Upgrade", "").lower() != "websocket":
        raise WSError("not a websocket upgrade")
    connection_tokens = [t.strip().lower() for t in headers.get("Connection", "").split(",")]
    if "upgrade" not in connection_tokens:
        raise WSError("missing Connection: Upgrade")
    if headers.get("Sec-WebSocket-Version", "") != "13":
        raise WSError("only WebSocket version 13 is supported")
    key = headers.get("Sec-WebSocket-Key", "")
    if not key:
        raise WSError("missing Sec-WebSocket-Key")

    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
    )
    handler.wfile.write(response.encode("ascii"))
    handler.wfile.flush()
    handler.close_connection = True
    return Connection(handler.connection, is_client=False)


# ------------------------------------------------------------ client side


def dial(url: str, headers: dict | None = None, timeout: float = 30.0,
         verify_tls: bool = True) -> tuple[Connection | None, int, str]:
    """Open a client connection.

    Returns `(connection, status, reason)`. On anything but 101 the
    connection is `None` and the status is the server's, so a caller can
    tell "refused my credential with 401" from "the socket died".
    """
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    port = parts.port or (443 if secure else 80)
    host = parts.hostname or "localhost"
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        sock = context.wrap_socket(sock, server_hostname=host)

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}" if parts.port else f"Host: {host}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for name, value in (headers or {}).items():
        request.append(f"{name}: {value}")
    sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("ascii"))

    reader = sock.makefile("rb")
    status_line = reader.readline().decode("latin-1").strip()
    parsed_headers = {}
    while True:
        line = reader.readline().decode("latin-1").strip()
        if not line:
            break
        name, _, value = line.partition(":")
        parsed_headers[name.strip().lower()] = value.strip()

    fields = status_line.split(" ", 2)
    status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
    reason = fields[2] if len(fields) > 2 else ""
    if status != 101:
        reader.close()
        sock.close()
        return None, status, reason
    if parsed_headers.get("sec-websocket-accept") != accept_key(key):
        reader.close()
        sock.close()
        raise WSError("the server's Sec-WebSocket-Accept does not match the key we sent")

    conn = Connection(sock, is_client=True)
    # Reuse the reader that already consumed the response headers, so no
    # bytes of the first frame are stranded in a discarded buffer.
    conn.reader = reader
    return conn, status, reason
