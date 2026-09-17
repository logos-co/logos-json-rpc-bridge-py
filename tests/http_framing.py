"""The bridge's HTTP framing rules (ws_server.cpp, bridge c8135ec), as checks on a listener.

``tests/integration/test_live_gating.py`` runs them against a real bridge and
``tests/unit/test_fake.py`` against FakeBridge, so both assert the same rules. Each
``assert_*`` takes the port of an HTTP listener that accepts ``Host: 127.0.0.1:<port>``.
"""

from __future__ import annotations

import http.client
import json
import socket
import time

from logos_bridge.testing.live import rpc_http

PING = {"jsonrpc": "2.0", "id": 1, "method": "rpc.ping"}
EVIL = {"Origin": "http://evil.example"}
JSON = {"Content-Type": "application/json"}
PROMPT = 5.0  # seconds: a stalled request waits out lws's 15 s timer

Exchange = tuple[int, str, str, bytes | None, dict[str, str]]

#: POST framings the bridge cannot read: 411, then a close.
UNREADABLE_LENGTHS: dict[str, tuple[str, ...]] = {
    "no Content-Length": (),
    "a non-numeric Content-Length": ("Content-Length: abc",),
    "a signed Content-Length": ("Content-Length: +2",),
    "a repeated Content-Length": ("Content-Length: 2", "Content-Length: 2"),
    "chunked": ("Transfer-Encoding: chunked",),
    "chunked with a Content-Length": ("Transfer-Encoding: chunked", "Content-Length: 2"),
    "any Transfer-Encoding": ("Transfer-Encoding: identity", "Content-Length: 2"),
}
#: Answers that leave a declared body unread, so they say Connection: close.
UNREAD_BODIES: dict[str, Exchange] = {
    "403 (POST)": (403, "POST", "/rpc", json.dumps(PING).encode(), {**JSON, **EVIL}),
    "415": (415, "POST", "/rpc", b"x", {"Content-Type": "text/plain"}),
    "403 (GET with a body)": (403, "GET", "/modules", b"{}", EVIL),
    "200 (GET with a body)": (200, "GET", "/healthz", b"{}", {}),
}
#: Refusals of requests without a body, which keep their connection.
BODYLESS_REFUSALS: dict[str, Exchange] = {
    "403 (GET)": (403, "GET", "/modules", None, EVIL),
    "403 (empty POST)": (403, "POST", "/rpc", b"", {**JSON, **EVIL}),  # Content-Length: 0
    "415 (empty POST)": (415, "POST", "/rpc", b"", {"Content-Type": "text/plain"}),
}
#: Served as GET; a HEAD answer carries the body too.
OTHER_METHODS = ("PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")


def read_until_closed(sock: socket.socket, *, reset_ok: bool = True) -> bytes:
    data = b""
    while True:
        try:
            chunk = sock.recv(65536)
        except ConnectionResetError:
            assert reset_ok, f"the connection was reset after {data[:80]!r}"
            return data
        if not chunk:
            return data
        data += chunk


def read_answer(sock: socket.socket) -> bytes:
    """One HTTP answer (status line through body); what arrived if the peer closed first."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = next((int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                   if line.lower().startswith(b"content-length:")), 0)
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def answer_head(answer: bytes) -> tuple[str, dict[str, str]]:
    """An answer's status line and its headers, with lower-case names."""
    lines = answer.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
    fields = (line.partition(":") for line in lines[1:])
    return lines[0], {name.strip().lower(): value.strip() for name, _, value in fields}


def request_head(port: int, method: str, path: str, *extra: str) -> bytes:
    return "\r\n".join([f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", *extra, "", ""]).encode()


def post_head(port: int, length: int, *extra: str) -> bytes:
    return request_head(port, "POST", "/rpc", "Content-Type: application/json", *extra, f"Content-Length: {length}")


def ping_over(conn: http.client.HTTPConnection) -> float:
    """rpc.ping over ``conn``; how long its answer took."""
    started = time.monotonic()
    conn.request("POST", "/rpc", body=json.dumps(PING), headers=JSON)
    answer = conn.getresponse()
    assert (answer.status, json.loads(answer.read())["result"]) == (200, "pong")
    return time.monotonic() - started


def assert_length_required(port: int, framing: tuple[str, ...]) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request_head(port, "POST", "/rpc", "Content-Type: application/json", *framing) + b"{}")
        status, headers = answer_head(read_answer(sock))
        assert (status, headers.get("connection")) == ("HTTP/1.1 411 Length Required", "close")
        assert read_until_closed(sock, reset_ok=False) == b""


def assert_unread_body_closes(port: int, exchange: Exchange) -> None:
    status, method, path, body, headers = exchange
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        answer = conn.getresponse()
        page = answer.read()
        assert (answer.status, answer.getheader("Connection")) == (status, "close")
        assert len(page) == int(answer.getheader("Content-Length", "-1")), "the answer was cut short"
        assert conn.sock is None, "http.client did not drop the connection it was told to close"
        assert ping_over(conn) < PROMPT, "the retry on a new connection was slow"
    finally:
        conn.close()


def assert_bodyless_refusal_keeps(port: int, exchange: Exchange) -> None:
    status, method, path, body, headers = exchange
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        kept = conn.sock
        refused = conn.getresponse()
        refused.read()
        assert (refused.status, refused.getheader("Connection")) == (status, None)
        assert ping_over(conn) < PROMPT, "the next request waited out lws's 15 s content timer"
        assert kept is not None and conn.sock is kept, "the connection was not reused"
    finally:
        conn.close()


def assert_late_refused_body_dropped(port: int) -> None:
    body, ping = b"{" + b" " * 199_998 + b"}", json.dumps(PING).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(post_head(port, len(body), "Origin: http://evil.example"))
        status, headers = answer_head(read_answer(sock))
        assert (status, headers.get("connection")) == ("HTTP/1.1 403 Forbidden", "close")
        # The body, and a request behind it, are read and dropped: nothing more comes, and no reset.
        sock.sendall(body + post_head(port, len(ping)) + ping)
        assert read_until_closed(sock, reset_ok=False) == b""
        time.sleep(0.2)
        sock.sendall(b" ")
        assert read_until_closed(sock, reset_ok=False) == b""
    started = time.monotonic()
    assert rpc_http(port, PING).json()["result"] == "pong"  # the retry, on a new connection
    assert time.monotonic() - started < PROMPT


def assert_served_as_get(port: int, method: str) -> None:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request_head(port, method, "/healthz", "Content-Length: 0"))
        answer = read_answer(sock)
    assert answer_head(answer)[0] == "HTTP/1.1 200 OK"
    assert json.loads(answer.partition(b"\r\n\r\n")[2])["status"] == "ok"
