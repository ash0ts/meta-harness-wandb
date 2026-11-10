# SPDX-License-Identifier: MIT
"""In-executor relay stub (spec 3.13): loopback HTTP server, uplink (stdio or Unix socket), worker supervisor, bridge.

Standard library only; imports its sibling ``mhf_frames`` and nothing from mhforge. Shipped verbatim into the
executor as ``<box>/bin/mhf_relay_stub.py``::

    python mhf_relay_stub.py serve  --job JOB.json --uplink stdio|unix:PATH [--daemonize] [--linger 30]
    python mhf_relay_stub.py bridge --uplink unix:PATH

The worker's model calls arrive on ``127.0.0.1:<port>``; each one is sent to the host as a ``REQ`` frame and answered
with the host's ``RES``. Only method, path and body leave the stub. The uplink is read with ``os.read`` and the process
leaves through ``os._exit`` (a daemon thread reading the buffered standard input aborts at interpreter exit
with -6).
The one environment variable read is ``MHF_STUB_BEARER``; its value is never written to a file or a frame.
"""

import argparse
import hmac
import json
import os
import queue
import select
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import mhf_frames


def _refusal_body(message, code):
    return json.dumps({"error": {"message": message, "type": "mhforge_refusal", "code": code}},
                      separators=(",", ":")).encode("ascii")


# Copies of the wire.REFUSALS rows the stub issues itself (a host-side test pins them to wire).
LOCAL_REFUSALS = {
    "relay_unavailable": (403, _refusal_body("mhforge relay: host link unavailable", "relay_unavailable")),
    "request_too_large": (403, _refusal_body("mhforge gateway: request exceeds the size bound", "request_too_large")),
}
REFUSAL_HEADERS = (("content-type", "application/json"), ("x-should-retry", "false"), ("x-mhforge-source", "gateway"))
FORWARD_HEADERS = ("content-type", "x-should-retry", "x-mhforge-source")
RELAY_API_KEY_PLACEHOLDER = "mhf-relay"
ROUTE1_WORKER_BEARER = "local"                  # llm.py L213-L215: the worker's bearer on Route 1
BEARER_ENV = "MHF_STUB_BEARER"
API_BASE_ARG = "{api_base}"
READ_CHUNK = 65536
WRITE_CHUNK = 32768
MAX_UNIX_PATH = 90
KILL_GRACE_S = 10.0
READY_WAIT_S = 20.0
CLIENT_POLL_S = 1.0
FLUSH_WAIT_S = 10.0
EXIT_UPLINK_LOST = 75
EXIT_NO_SOCKET = 3
REQUEST_QUEUE_SIZE = 128
JOB_FIELDS = ("job_id", "argv", "env", "cwd", "route", "port", "timeout_s", "reply_timeout_s", "ping_interval_s",
              "pong_timeout_s", "max_request_bytes", "run_dir")


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _write_json_atomic(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class Diag:
    """One JSON line per event, appended to <run_dir>/stub.txt with one os.write each. Never a value of a secret."""

    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError:
            self.fd = None

    def __call__(self, event, **fields):
        if self.fd is None:
            return
        row = {"t": time.time(), "event": event}
        row.update(fields)
        try:
            os.write(self.fd, (json.dumps(row, sort_keys=True, default=str) + "\n").encode("utf-8", "replace"))
        except OSError:
            pass


def load_job(path):
    """The job file (2.6) as a dict; ValueError when a field the stub reads is missing or has the wrong type."""
    with open(path, "rb") as fh:
        job = json.loads(fh.read().decode("utf-8"))
    if not isinstance(job, dict):
        raise ValueError("job file is not an object")
    missing = [k for k in JOB_FIELDS if k not in job]
    if missing:
        raise ValueError("job file lacks: " + ", ".join(missing))
    if not isinstance(job["argv"], list) or not job["argv"] or not all(isinstance(a, str) for a in job["argv"]):
        raise ValueError("job argv must be a non-empty list of strings")
    if not isinstance(job["env"], dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in job["env"].items()):
        raise ValueError("job env must map strings to strings")
    if job["route"] not in (1, 2):
        raise ValueError("job route must be 1 or 2")
    for key in ("port", "max_request_bytes"):
        if not isinstance(job[key], int) or isinstance(job[key], bool) or job[key] < 0:
            raise ValueError("job %s must be a non-negative integer" % key)
    for key in ("timeout_s", "reply_timeout_s", "ping_interval_s", "pong_timeout_s"):
        if not isinstance(job[key], (int, float)) or isinstance(job[key], bool) or job[key] <= 0:
            raise ValueError("job %s must be a positive number" % key)
    if not isinstance(job["cwd"], str) or not isinstance(job["run_dir"], str):
        raise ValueError("job cwd and run_dir must be strings")
    return job


# ---------------------------------------------------------------------------------------------------- uplink

class Conn:
    """One attached uplink channel: its own FrameDecoder, one reader thread and one writer thread fed from a queue.
    The reader never writes; everything outgoing goes through send()."""

    def __init__(self, stub, name, read_fn, write_fn, close_fn):
        self.stub, self.name = stub, name
        self._read, self._write, self._close = read_fn, write_fn, close_fn
        self.decoder = mhf_frames.FrameDecoder()
        self.alive = True
        self._q = queue.Queue()
        self._dead_once = threading.Lock()

    def start(self):
        for target in (self._writer, self._reader):
            threading.Thread(target=target, name="%s-%s" % (self.name, target.__name__), daemon=True).start()

    def send(self, frame):
        if self.alive:
            self._q.put(frame)

    def flush(self, timeout):
        """Wait until everything queued before this call is written (or the channel died)."""
        done = threading.Event()
        self._q.put(done)
        end = time.monotonic() + timeout
        while self.alive and not done.is_set() and time.monotonic() < end:
            done.wait(0.05)
        return done.is_set()

    def close(self):
        self.alive = False
        self._q.put(None)
        try:
            self._close()
        except OSError:
            pass

    def _died(self):
        if self._dead_once.acquire(blocking=False):
            self.alive = False
            self._q.put(None)
            self.stub.on_conn_dead(self)

    def _writer(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            if isinstance(item, threading.Event):
                item.set()
                continue
            try:
                for i in range(0, len(item), WRITE_CHUNK):
                    self._write(item[i:i + WRITE_CHUNK])
                self.stub.count("frames_out")
            except OSError:
                self._died()
                return

    def _reader(self):
        while True:
            try:
                data = self._read(READ_CHUNK)
            except OSError:
                data = b""
            if not data:
                self._died()
                return
            for frame in self.decoder.feed(data.decode("ascii", errors="replace")):
                self.stub.on_frame(self, frame)


class Pending:
    __slots__ = ("rid", "frame", "event", "reply", "sent", "conn")

    def __init__(self, rid, frame):
        self.rid, self.frame = rid, frame
        self.event = threading.Event()
        self.reply = None
        self.sent = False          # sent to the host at least once
        self.conn = None           # the connection it was last sent on


# ---------------------------------------------------------------------------------------------------- stub core

class Stub:
    def __init__(self, job, mode, bearer, diag, linger=30.0):
        self.job, self.mode, self.diag, self.linger = job, mode, diag, float(linger)
        self._bearer = bearer
        self.lock = threading.Lock()          # covers the connection swap, the HELLO snapshot and every REQ write
        self.current = None
        self.pending = {}
        self.counter = 0
        self.local_refusals = {code: 0 for code in LOCAL_REFUSALS}
        self.unauthorized = 0
        self.stats = {"frames_in": 0, "frames_out": 0, "attaches": 0, "pings": 0, "cancelled": 0, "requests": 0}
        self.proc = None
        self.end_reason = None                # "killed" | "timeout" | "uplink_lost"
        self.link_lost = False
        self.exit_meta = None
        self.worker_done = threading.Event()
        self.bye = threading.Event()
        self.last_frame = time.monotonic()
        self.port = 0

    # -- bookkeeping
    def count(self, key, n=1):
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def _hello(self):
        state = {"state": "running", "returncode": None}
        if self.exit_meta is not None:
            state = {"state": "exited", "returncode": self.exit_meta["returncode"]}
        meta = {"proto": 1, "mode": self.mode, "pid": os.getpid(), "port": self.port,
                "pending": [p.rid for p in self._pending_in_order()], "worker": state}
        return mhf_frames.encode_frame("HELLO", "0", meta)

    def _pending_in_order(self):
        return sorted(self.pending.values(), key=lambda p: int(p.rid[1:]))

    def attach(self, conn):
        """Make conn the current connection: HELLO with the pending ids, every pending REQ again, EXIT if ended."""
        with self.lock:
            old, self.current = self.current, conn
            self.stats["attaches"] += 1
            conn.send(self._hello())
            for p in self._pending_in_order():
                if p.conn is not conn:
                    conn.send(p.frame)
                    p.conn, p.sent = conn, True
            if self.exit_meta is not None:
                conn.send(mhf_frames.encode_frame("EXIT", "0", self.exit_meta))
        conn.start()
        if old is not None and old is not conn:
            old.close()
        self.diag("attach", conn=conn.name)

    def on_conn_dead(self, conn):
        with self.lock:
            if self.current is conn:
                self.current = None
        self.diag("conn_dead", conn=conn.name)
        if self.mode == "stdio":
            self.lose_link("uplink_lost")

    def submit(self, path, body):
        with self.lock:
            self.counter += 1
            rid = "r%d" % self.counter
        p = Pending(rid, mhf_frames.encode_frame("REQ", rid, {"method": "POST", "path": path}, body))
        with self.lock:
            self.pending[rid] = p
            self.stats["requests"] += 1
            conn = self.current
            if conn is not None and conn.alive:
                conn.send(p.frame)
                p.conn, p.sent = conn, True
        return p

    def drop(self, p):
        """Remove p from pending and send CAN; False when a reply already took it."""
        with self.lock:
            if self.pending.get(p.rid) is not p:
                return False
            del self.pending[p.rid]
            self.stats["cancelled"] += 1
            conn = self.current
            if conn is not None and conn.alive:
                conn.send(mhf_frames.encode_frame("CAN", p.rid, {}))
        return True

    # -- frames from the host
    def on_frame(self, conn, frame):
        ftype, rid, meta, body = frame
        self.last_frame = time.monotonic()
        self.count("frames_in")
        if ftype == "RES":
            status, headers = meta.get("status"), meta.get("headers", [])
            if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                return
            if not isinstance(headers, list):
                headers = []
            with self.lock:
                p = self.pending.pop(rid, None)
            if p is not None:
                p.reply = (status, headers, body)
                p.event.set()
        elif ftype == "PING":
            t = meta.get("t")
            if not isinstance(t, (int, float)) or isinstance(t, bool):
                t = 0.0
            self.count("pings")
            conn.send(mhf_frames.encode_frame("PONG", "0", {"t": t}))
        elif ftype == "KILL":
            self.diag("kill_frame", reason=str(meta.get("reason", ""))[:64])
            self.kill("killed")
        elif ftype == "BYE":
            if self.worker_done.is_set():
                self.bye.set()
            elif self.mode == "stdio":
                self.lose_link("killed")
            else:
                self.diag("bye_ignored_worker_running")

    def lose_link(self, reason):
        """Stdio only: the host link is gone; kill the worker (exit 75 follows once it has ended)."""
        with self.lock:
            if self.link_lost or self.exit_meta is not None or self.worker_done.is_set():
                return
            self.link_lost = True
        self.diag("uplink_lost", reason=reason)
        self.kill(reason)

    def kill(self, reason):
        with self.lock:
            if self.end_reason is None:
                self.end_reason = reason
            proc = self.proc
        if proc is not None and not self.worker_done.is_set():
            threading.Thread(target=self._terminate, args=(proc,), daemon=True).start()

    def _terminate(self, proc):
        _killpg(proc.pid, signal.SIGTERM)
        if not self.worker_done.wait(KILL_GRACE_S):
            _killpg(proc.pid, signal.SIGKILL)

    def watchdog(self):
        """Stdio: no frame of any type for 3 * ping_interval_s + pong_timeout_s means the link is gone."""
        limit = 3 * float(self.job["ping_interval_s"]) + float(self.job["pong_timeout_s"])
        while not self.worker_done.is_set():
            if time.monotonic() - self.last_frame > limit:
                self.lose_link("uplink_lost")
                return
            self.worker_done.wait(min(0.5, limit / 4.0))

    # -- HTTP side
    def _authorized(self, value):
        if self._bearer is None:
            return True
        got = (value or "").encode("latin-1", "replace")
        ok = hmac.compare_digest(got, ("Bearer " + self._bearer).encode("latin-1", "replace"))
        if self.job["route"] == 1:
            ok = hmac.compare_digest(got, ("Bearer " + ROUTE1_WORKER_BEARER).encode("ascii")) or ok
        return ok

    def refuse(self, handler, code, *, counted=True):
        if counted:
            with self.lock:
                self.local_refusals[code] += 1
        status, body = LOCAL_REFUSALS[code]
        self._respond(handler, status, list(REFUSAL_HEADERS), body)

    @staticmethod
    def _respond(handler, status, headers, body):
        try:
            handler.send_response(status)
            for name, value in headers:
                handler.send_header(name, value)
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(body)
            handler.wfile.flush()
        except OSError:
            handler.close_connection = True

    @staticmethod
    def _read_body(handler, limit):
        """The request body, or None when it exceeds limit (the rest is read and discarded)."""
        rfile, parts, size, over = handler.rfile, [], 0, False

        def take(n):
            nonlocal size, over
            while n > 0:
                data = rfile.read(min(n, READ_CHUNK))
                if not data:
                    raise ConnectionError("short body")
                n -= len(data)
                size += len(data)
                if size > limit:
                    over = True
                    parts.clear()
                elif not over:
                    parts.append(data)

        if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
            while True:
                line = rfile.readline(1024)
                try:
                    n = int(line.split(b";")[0].strip() or b"x", 16)
                except ValueError:
                    raise ConnectionError("bad chunk size") from None
                if n == 0:
                    while rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                take(n)
                rfile.readline(4)
        else:
            try:
                n = int(handler.headers.get("Content-Length") or "0")
            except ValueError:
                n = 0
            take(max(n, 0))
        return None if over else b"".join(parts)

    @staticmethod
    def _client_gone(sock):
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable:
                return False
            return sock.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def handle_http(self, handler, method):
        try:
            if not self._authorized(handler.headers.get("Authorization")):
                self._read_body(handler, 0)
                with self.lock:
                    self.unauthorized += 1
                self.diag("unauthorized")
                self.refuse(handler, "relay_unavailable", counted=False)
                return
            path = urlsplit(handler.path).path
            if method != "POST" or not path.endswith("/chat/completions"):
                self._read_body(handler, 0)
                self.refuse(handler, "relay_unavailable")
                return
            body = self._read_body(handler, int(self.job["max_request_bytes"]))
        except ConnectionError:
            handler.close_connection = True
            return
        if body is None:
            self.refuse(handler, "request_too_large")
            return
        try:
            p = self.submit(path, body)
        except mhf_frames.FrameError:               # larger than one MHF1 frame can carry (16 MiB)
            self.refuse(handler, "request_too_large")
            return
        deadline = time.monotonic() + float(self.job["reply_timeout_s"])
        while not p.event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.drop(p):
                    self.refuse(handler, "relay_unavailable")
                    return
                p.event.wait()
                break
            if p.event.wait(min(CLIENT_POLL_S, remaining)):
                break
            if self._client_gone(handler.connection) and self.drop(p):
                self.diag("client_gone", rid=p.rid)
                handler.close_connection = True
                return
        status, headers, rbody = p.reply
        out = []
        for item in headers:
            if (isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item)
                    and item[0].lower() in FORWARD_HEADERS and "\r" not in item[1] and "\n" not in item[1]):
                out.append((item[0].lower(), item[1]))
        self._respond(handler, status, out, rbody)

    # -- supervisor
    def run_worker(self):
        job, run_dir = self.job, self.job["run_dir"]
        base = "http://127.0.0.1:%d/v1" % self.port
        env = dict(job["env"])
        argv = list(job["argv"])
        if job["route"] == 2:
            env["OPENROUTER_API_BASE"] = base
            env["OPENROUTER_API_KEY"] = self._bearer if self._bearer is not None else RELAY_API_KEY_PLACEHOLDER
        else:
            argv = [base if a == API_BASE_ARG else a for a in argv]
        t_start = time.time()
        deadline = time.monotonic() + float(job["timeout_s"])
        returncode = 127
        try:
            with open(os.path.join(run_dir, "stdout.txt"), "wb") as out, \
                    open(os.path.join(run_dir, "stderr.txt"), "wb") as err:
                proc = subprocess.Popen(argv, env=env, cwd=job["cwd"], stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=err, start_new_session=True)
        except (OSError, ValueError) as exc:       # ValueError: a NUL byte or an illegal name in job argv/env
            self.diag("worker_start_failed", error=type(exc).__name__)
            proc = None
        unanswered = 0
        if proc is not None:
            with self.lock:
                self.proc = proc
                early = self.end_reason is not None
            if early:
                threading.Thread(target=self._terminate, args=(proc,), daemon=True).start()
            self.diag("worker_started", pid=proc.pid)
            while True:
                try:
                    returncode = proc.wait(timeout=max(0.01, min(0.2, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline and self.end_reason is None:
                        self.kill("timeout")
        with self.lock:
            unanswered = sum(1 for p in self.pending.values() if p.sent)
            reason = self.end_reason or "exit"
        self.worker_done.set()
        if proc is not None:
            _killpg(proc.pid, signal.SIGKILL)           # whatever the worker left in its group
        if reason == "timeout":
            returncode = 124
        with self.lock:
            meta = {"returncode": int(returncode), "reason": reason, "timed_out": reason == "timeout",
                    "local_refusals": dict(self.local_refusals), "unanswered_at_exit": unanswered}
        record = {"schema": "mhforge.exit/1", "t_start": t_start, "t_end": time.time()}
        record.update(meta)
        try:
            _write_json_atomic(os.path.join(run_dir, "exit.json"), record)
        except OSError as exc:
            self.diag("exit_file_failed", error=type(exc).__name__)
        with self.lock:
            self.exit_meta = meta
            conn = self.current
            if conn is not None and conn.alive:
                conn.send(mhf_frames.encode_frame("EXIT", "0", meta))
        self.diag("worker_ended", returncode=meta["returncode"], reason=reason, unanswered_at_exit=unanswered)
        return conn

    def summary(self):
        with self.lock:
            self.diag("summary", unauthorized=self.unauthorized, local_refusals=dict(self.local_refusals),
                      pending=len(self.pending), **self.stats)


# ---------------------------------------------------------------------------------------------------- servers

class _Server(ThreadingHTTPServer):
    request_queue_size = REQUEST_QUEUE_SIZE
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn, a reverse lookup that can hang with DNS denied.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mhf-relay-stub"
    sys_version = ""

    def log_message(self, format, *args):    # noqa: A002 - the base class's name
        pass

    def _any(self):
        self.server.stub.handle_http(self, self.command)

    do_POST = do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _any


def _unix_listener(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    os.chmod(path, 0o600)
    sock.listen(8)
    return sock


def _accept_loop(stub, listener):
    n = 0
    while True:
        try:
            sock, _ = listener.accept()
        except OSError:
            return
        n += 1

        def closer(s=sock):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

        stub.attach(Conn(stub, "unix%d" % n, sock.recv, sock.sendall, closer))


def _daemonize(run_dir):
    """Double fork and setsid. Returns None in the daemon; in the original process returns the exit code."""
    ready = os.path.join(run_dir, "stub.ready")
    try:
        os.unlink(ready)
    except FileNotFoundError:
        pass
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        end = time.monotonic() + READY_WAIT_S
        while time.monotonic() < end:
            try:
                with open(ready, "rb") as fh:
                    doc = json.loads(fh.read().decode("utf-8"))
                if isinstance(doc, dict) and "pid" in doc and "port" in doc:
                    return 0
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        return 2
    os.setsid()
    if os.fork():
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    log = os.open(os.path.join(run_dir, "stub.txt"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(devnull, 0)
    os.dup2(log, 1)
    os.dup2(log, 2)
    os.close(devnull)
    os.close(log)
    return None


def serve(job_path, uplink, daemonize=False, linger=30.0):
    try:
        job = load_job(job_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("mhf_relay_stub: bad job file: %s\n" % exc)
        return 2
    if uplink == "stdio":
        mode, sock_path = "stdio", None
    elif uplink.startswith("unix:") and len(uplink) > 5:
        mode, sock_path = "daemon", uplink[5:]
        if len(sock_path.encode("utf-8")) > MAX_UNIX_PATH:
            sys.stderr.write("mhf_relay_stub: socket path longer than %d bytes\n" % MAX_UNIX_PATH)
            return 2
    else:
        sys.stderr.write("mhf_relay_stub: --uplink must be stdio or unix:PATH\n")
        return 2
    if daemonize and mode != "daemon":
        sys.stderr.write("mhf_relay_stub: --daemonize needs a unix uplink\n")
        return 2
    run_dir = job["run_dir"]
    os.makedirs(run_dir, exist_ok=True)
    if daemonize:
        code = _daemonize(run_dir)
        if code is not None:
            return code
    diag = Diag(os.path.join(run_dir, "stub.txt"))
    bearer = os.environ.get(BEARER_ENV) or None
    stub = Stub(job, mode, bearer, diag, linger)

    def on_term(signum, frame):
        proc = stub.proc
        if proc is not None:
            _killpg(proc.pid, signal.SIGKILL)
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, on_term)
    try:
        server = _Server(("127.0.0.1", int(job["port"])), _Handler)
    except OSError as exc:
        diag("bind_failed", error=type(exc).__name__)
        sys.stderr.write("mhf_relay_stub: cannot bind 127.0.0.1:%s\n" % job["port"])
        os._exit(2)
    server.stub = stub
    stub.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="http", daemon=True).start()

    listener = None
    if mode == "stdio":
        frame_fd = os.dup(1)                 # fd 1 carries frames only: stray writes to sys.stdout go to stub.txt
        if diag.fd is not None:
            os.dup2(diag.fd, 1)
        stub.attach(Conn(stub, "stdio", lambda n: os.read(0, n), lambda b: _write_all(frame_fd, b), lambda: None))
        threading.Thread(target=stub.watchdog, name="watchdog", daemon=True).start()
    else:
        try:
            listener = _unix_listener(sock_path)
        except OSError as exc:
            diag("listen_failed", error=type(exc).__name__)
            os._exit(2)
        threading.Thread(target=_accept_loop, args=(stub, listener), name="accept", daemon=True).start()
        _write_json_atomic(os.path.join(run_dir, "stub.ready"), {"pid": os.getpid(), "port": stub.port})
    diag("serving", mode=mode, port=stub.port, job_id=str(job["job_id"])[:128], bearer=bearer is not None)

    conn = stub.run_worker()
    if mode == "stdio":
        if stub.link_lost:
            stub.summary()
            os._exit(EXIT_UPLINK_LOST)
        if conn is not None:
            conn.flush(FLUSH_WAIT_S)
        stub.summary()
        os._exit(0)
    stub.bye.wait(stub.linger)
    with stub.lock:
        conn = stub.current
    if conn is not None:
        conn.flush(2.0)
    try:
        listener.close()
        os.unlink(sock_path)
    except OSError:
        pass
    stub.summary()
    os._exit(0)


def bridge(uplink):
    """Copy fd 0 to the socket and the socket to fd 1 until either side closes; exit 3 without a socket."""
    if not uplink.startswith("unix:") or len(uplink) <= 5:
        sys.stderr.write("mhf_relay_stub: bridge needs --uplink unix:PATH\n")
        return 2
    path = uplink[5:]
    if not os.path.exists(path):
        os._exit(EXIT_NO_SOCKET)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        os._exit(EXIT_NO_SOCKET)
    done = threading.Event()

    def up():
        try:
            while True:
                data = os.read(0, READ_CHUNK)
                if not data:
                    break
                sock.sendall(data)
        except OSError:
            pass
        done.set()

    def down():
        try:
            while True:
                data = sock.recv(READ_CHUNK)
                if not data:
                    break
                _write_all(1, data)
        except OSError:
            pass
        done.set()

    for target in (up, down):
        threading.Thread(target=target, daemon=True).start()
    done.wait()
    os._exit(0)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mhf_relay_stub.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--job", required=True)
    p_serve.add_argument("--uplink", required=True)
    p_serve.add_argument("--daemonize", action="store_true")
    p_serve.add_argument("--linger", type=float, default=30.0)
    p_bridge = sub.add_parser("bridge")
    p_bridge.add_argument("--uplink", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "serve":
        return serve(args.job, args.uplink, daemonize=args.daemonize, linger=args.linger)
    return bridge(args.uplink)


if __name__ == "__main__":
    sys.exit(main())
