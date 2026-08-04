"""Length-prefixed JSON socket framing.

Wire format: a 4-byte big-endian unsigned length, followed by that many bytes
of UTF-8 JSON. Both the MCP server (this module) and the Ableton Remote Script
use an identical implementation so the two halves stay in lockstep.

This replaces the upstream "accumulate chunks and retry json.loads until it
parses" heuristic, which is O(n^2) on large payloads (e.g. browser trees) and
can mis-detect completion when a prefix happens to be valid JSON.
"""

import json
import socket
import struct

# Reject absurd frame lengths up front (corruption / desync) but stay well
# above any realistic browser-tree response.
MAX_FRAME = 32 * 1024 * 1024  # 32 MB


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from sock, looping over partial reads."""
    chunks = []
    got = 0
    while got < n:
        chunk = sock.recv(min(65536, n - got))
        if not chunk:
            raise ConnectionError("peer closed connection mid-frame")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def recv_frame(sock: socket.socket):
    """Read one length-prefixed JSON frame and return the decoded object."""
    header = _recv_exactly(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME:
        raise ValueError("invalid frame length: %d" % length)
    payload = _recv_exactly(sock, length)
    return json.loads(payload.decode("utf-8"))


def send_frame(sock: socket.socket, obj) -> None:
    """Encode obj as JSON and send it as one length-prefixed frame."""
    payload = json.dumps(obj).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ValueError("frame too large to send: %d bytes" % len(payload))
    sock.sendall(struct.pack(">I", len(payload)) + payload)
