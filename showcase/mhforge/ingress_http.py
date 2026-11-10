# SPDX-License-Identifier: MIT
"""Loopback HTTP ingress (spec 3.11): one listener on 127.0.0.1 per job, between open() and close().

The listening socket identifies the job. With a token (Route 2) the request must also carry `Authorization: Bearer
<token>`; a mismatch is handed to the gateway as job=None, which refuses it as unknown_job and records it. Nothing is
logged: no header, no token, no request line.
"""

from __future__ import annotations

import hmac
import re
import socketserver
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import util, wire
from .wire import GatewayReply, HandlesRequests, JobIdentity

__all__ = ["Endpoint", "LoopbackIngress", "HOST", "IDLE_TIMEOUT_S"]

HOST = "127.0.0.1"                                 # never a parameter
IDLE_TIMEOUT_S = 120.0                             # an idle keep-alive connection is closed after this


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    base_url: str                                  # "http://127.0.0.1:<port>/v1"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mhforge-ingress/0.1"
    sys_version = ""
    timeout = IDLE_TIMEOUT_S

    def log_message(self, format: str, *args: object) -> None:      # never logs (no header, no token)
        return

    def _send(self, reply: GatewayReply, *, close: bool = False, head: bool = False) -> None:
        try:
            self.send_response(reply.status)
            for name, value in reply.headers:
                if name.lower() not in ("content-length", "connection", "transfer-encoding"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            if close or self.server.closed:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if not head:
                self.wfile.write(reply.body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            self.close_connection = True           # the worker has gone; the gateway's work stands (G9)

    def _refuse(self, code: str, *, head: bool = False) -> None:
        self._send(wire.refusal_reply(code), close=True, head=head)

    def do_POST(self) -> None:
        srv: _Server = self.server                 # type: ignore[assignment]
        length_text = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding") is not None or length_text is None \
                or not re.fullmatch(r"[0-9]{1,18}", length_text.strip()):    # isdigit() accepts "²", int() does not
            self._refuse("not_allowed")
            return
        length = int(length_text.strip())
        if length > srv.max_request_bytes:
            self._refuse("request_too_large")      # refused unread; the connection is closed
            return
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self.close_connection = True
            return
        if len(body) < length:                     # the client went away mid-body
            self.close_connection = True
            return
        try:
            envelope = wire.make_envelope("POST", self.path, body)
        except ValueError:
            self._refuse("not_allowed")
            return
        job = None if srv.closed else srv.job
        if srv.token is not None:
            given = (self.headers.get("Authorization") or "").encode("latin-1", "replace")
            if not hmac.compare_digest(given, srv.bearer):
                job = None                         # handled as an unknown job (and recorded by the gateway)
        try:
            reply = srv.gateway.handle(job, envelope, ingress="loopback")
        except Exception:                          # noqa: BLE001 - the gateway never raises; a fake might
            reply = wire.refusal_reply("gateway_internal")
        self._send(reply)

    def _not_allowed(self) -> None:
        self._refuse("not_allowed")

    do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _not_allowed

    def do_HEAD(self) -> None:
        self._refuse("not_allowed", head=True)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, gateway: HandlesRequests, job: JobIdentity, token: str | None, *, backlog: int,
                 max_request_bytes: int) -> None:
        self.request_queue_size = backlog          # read by server_activate (listen); H2 inherited 5
        self.gateway, self.job, self.token = gateway, job, token
        self.bearer = b"" if token is None else ("Bearer " + token).encode("latin-1", "replace")
        self.max_request_bytes = max_request_bytes
        self.closed = False
        super().__init__((HOST, 0), _Handler)

    def server_bind(self) -> None:                 # no reverse lookup of the address
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = HOST, self.server_address[1]

    def __repr__(self) -> str:                     # never shows the token
        return f"_Server(job={self.job.job_id!r}, port={self.server_port})"


class LoopbackIngress:
    def __init__(self, gateway: HandlesRequests, manifest: dict) -> None:
        self.gateway = gateway
        self.backlog = int(util.lookup(manifest, "gateway.loopback_backlog"))
        self.max_request_bytes = int(util.lookup(manifest, "gateway.max_request_bytes"))
        self._lock = threading.Lock()
        self._open: dict[str, tuple[_Server, threading.Thread]] = {}

    def open(self, job: JobIdentity, token: str | None) -> Endpoint:
        """Bind ("127.0.0.1", 0) for this job; ValueError when the job already has a listener."""
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("a job token must be a non-empty string or None")
        with self._lock:
            if job.job_id in self._open:
                raise ValueError(f"job {job.job_id} already has a listener")
            server = _Server(self.gateway, job, token, backlog=self.backlog, max_request_bytes=self.max_request_bytes)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                      name=f"mhf-ingress-{server.server_port}", daemon=True)
            self._open[job.job_id] = (server, thread)
        thread.start()
        port = server.server_port
        return Endpoint(host=HOST, port=port, base_url=f"http://{HOST}:{port}/v1")

    def close(self, job_id: str) -> None:
        with self._lock:
            entry = self._open.pop(job_id, None)
        if entry is None:
            return
        server, thread = entry
        server.closed = True                       # requests on surviving keep-alive connections: unknown_job
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    def open_ports(self) -> list[int]:
        with self._lock:
            return sorted(server.server_port for server, _ in self._open.values())

    def close_all(self) -> None:
        for job_id in list(self._open):
            self.close(job_id)
