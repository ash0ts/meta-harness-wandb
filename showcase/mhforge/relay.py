# SPDX-License-Identifier: MIT
"""Host side of the exec-stream relay (spec 3.12): FrameChannel, PipeChannel, Bridge, RelaySession.

One RelaySession per job attempt. Each attached channel (a bridge exec, or the stub's own stdio) becomes a Bridge with
its own MHF1 decoder and one reader thread. Everything the host sends goes through one writer queue served by one writer
thread; reader threads never write. Requests are answered by a pool of daemon threads calling ``gateway.handle`` with
the session's own job identity. Standard library only.
"""

from __future__ import annotations

import codecs
import collections
import os
import re
import select
import subprocess
import threading
import time
from typing import Protocol, runtime_checkable

from . import util, wire
from .errors import ChannelClosed, RelayProtocolError
from .insandbox import mhf_frames

FRAME_ERROR_LIMIT = 8          # frame errors in one bridge that mark it dead
LOG_KEEP = 50                  # LOG frames kept per session
FLOOD_FACTOR = 4               # live requests allowed per pool thread
POLL_S = 0.2                   # how often blocked channel calls look at their closed flag
LOG_MSG_CHARS = 512
_RID_RE = re.compile(r"[a-z0-9]{1,16}")
_COUNTERS = ("attaches", "rotations", "frames_in", "frames_out", "bytes_out", "duplicate_requests", "resent_replies",
             "cancelled", "flood_refusals", "pings", "pongs", "logs", "gateway_errors")


@runtime_checkable
class FrameChannel(Protocol):
    def read_text(self) -> str | None: ...         # next chunk of text as the transport delivers it; None at the end
    def write(self, data: bytes) -> None: ...      # ChannelClosed when the far end is gone or the write did not return
    def close(self) -> None: ...                   # in relay.pong_timeout_s


# ---------------------------------------------------------------------------------------------- pipe channel

def _wait_fd(fd: int, writable: bool, timeout: float) -> bool:
    """True when fd is ready (or in error, so the next call fails fast); False at the timeout."""
    timeout = max(0.0, timeout)
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(fd, select.POLLOUT if writable else select.POLLIN)
        return bool(poller.poll(timeout * 1000.0))
    r, w, x = select.select([] if writable else [fd], [fd] if writable else [], [fd], timeout)
    return bool(r or w or x)


def _fd_write(fd: int, data: bytes, timeout_s: float | None, closed, lock: threading.Lock) -> None:
    """Write all of data to a non-blocking fd; ChannelClosed when closed, the far end is gone, or the time ran out."""
    view = memoryview(data)
    deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
    while view:
        if closed():
            raise ChannelClosed("channel closed")
        wait = POLL_S
        if deadline is not None:
            wait = min(POLL_S, deadline - time.monotonic())
            if wait <= 0:
                raise ChannelClosed("write did not return in time")
        if not _wait_fd(fd, True, wait):
            continue
        with lock:
            if closed():
                raise ChannelClosed("channel closed")
            try:
                n = os.write(fd, view)
            except BlockingIOError:
                continue
            except (OSError, ValueError) as exc:
                raise ChannelClosed("far end gone: " + type(exc).__name__) from None
        view = view[n:]


class PipeChannel:
    """Local subprocess pipes standing in for the exec stream. The process must be started with stdin=PIPE,
    stdout=PIPE and bufsize=0. With mimic_sdk each chunk read is decoded alone with errors="replace", the call the SDK
    makes on every stdout frame, and counted in retained_bytes (the SDK keeps every stdout byte)."""

    def __init__(self, proc: subprocess.Popen, *, mimic_sdk: bool = True, chunk: int = 65536,
                 write_timeout_s: float | None = None) -> None:
        if proc.stdout is None or proc.stdin is None:
            raise ValueError("PipeChannel needs a process with stdin and stdout pipes")
        self._proc = proc
        self._rfd, self._wfd = proc.stdout.fileno(), proc.stdin.fileno()
        os.set_blocking(self._wfd, False)
        self.mimic_sdk = bool(mimic_sdk)
        self.chunk = max(1, int(chunk))
        self.write_timeout_s = write_timeout_s
        self.retained_bytes = 0
        self._closed = False
        self._eof = False
        self._rlock, self._wlock = threading.Lock(), threading.Lock()
        self._utf8 = None if self.mimic_sdk else codecs.getincrementaldecoder("utf-8")("replace")

    def set_write_timeout(self, seconds: float) -> None:
        """Called by RelaySession.attach with relay.pong_timeout_s unless the constructor was given one."""
        if self.write_timeout_s is None:
            self.write_timeout_s = float(seconds)

    def read_text(self) -> str | None:
        while True:
            if self._closed or self._eof:
                return None
            if not _wait_fd(self._rfd, False, POLL_S):
                continue
            with self._rlock:
                if self._closed:
                    return None
                try:
                    data = os.read(self._rfd, self.chunk)
                except BlockingIOError:
                    continue
                except (OSError, ValueError):
                    data = b""
            if not data:
                self._eof = True
                tail = self._utf8.decode(b"", final=True) if self._utf8 is not None else ""
                return tail or None
            if self._utf8 is None:
                self.retained_bytes += len(data)
                return data.decode("utf-8", errors="replace")
            return self._utf8.decode(data)

    def write(self, data: bytes) -> None:
        _fd_write(self._wfd, data, self.write_timeout_s, lambda: self._closed, self._wlock)

    def close(self) -> None:
        self._closed = True
        for lock, stream in ((self._wlock, self._proc.stdin), (self._rlock, self._proc.stdout)):
            with lock:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------------------------- bridge

class Bridge:
    """One attached channel, with its own FrameDecoder and reader thread."""

    def __init__(self, session: "RelaySession", channel: FrameChannel, index: int) -> None:
        self._session, self.channel, self.index = session, channel, index
        self.decoder = mhf_frames.FrameDecoder()
        self.chars_in = 0
        self.alive = True
        self.dead_reason: str | None = None        # eof | write | frame_errors | silent | detached | closed
        self.hello: dict | None = None
        self.last_seen = session.clock.monotonic()
        self.resent: set[str] = set()              # rids re-answered from the done cache since this bridge's HELLO
        self.reader_thread: threading.Thread | None = None

    def detach(self) -> None:
        """Stop reading; close the channel."""
        self._session._mark_dead(self, "detached")

    def __repr__(self) -> str:
        return "Bridge(%d, alive=%s, reason=%s)" % (self.index, self.alive, self.dead_reason)


class _Out:
    __slots__ = ("kind", "rid", "data")

    def __init__(self, kind: str, rid: str, data: bytes) -> None:
        self.kind, self.rid, self.data = kind, rid, data


class _DaemonPool:
    """At most max_workers daemon threads, started on demand (a ThreadPoolExecutor's threads are joined at exit)."""

    def __init__(self, max_workers: int, name: str) -> None:
        self._max, self._name = max(1, int(max_workers)), name
        self._q: collections.deque = collections.deque()
        self._cond = threading.Condition()
        self._threads = self._idle = 0
        self._stopped = False
        self.started: list[threading.Thread] = []

    def submit(self, fn, *args) -> None:
        with self._cond:
            self._q.append((fn, args))
            if len(self._q) > self._idle and self._threads < self._max:
                self._threads += 1
                t = threading.Thread(target=self._run, name="%s-%d" % (self._name, self._threads), daemon=True)
                self.started.append(t)
                t.start()
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                self._idle += 1
                while not self._q and not self._stopped:
                    self._cond.wait()
                self._idle -= 1
                if not self._q:
                    self._threads -= 1
                    return
                fn, args = self._q.popleft()
            fn(*args)

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._q.clear()
            self._cond.notify_all()


# ---------------------------------------------------------------------------------------------- session

def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check_hello(meta: dict) -> dict | None:
    """The HELLO meta when every field has its type (2.9), else None. Advisory only."""
    pending, worker = meta.get("pending"), meta.get("worker")
    if meta.get("proto") != 1 or meta.get("mode") not in ("stdio", "daemon"):
        return None
    if not _is_int(meta.get("pid")) or not _is_int(meta.get("port")) or not isinstance(pending, list):
        return None
    if not all(isinstance(r, str) and _RID_RE.fullmatch(r) for r in pending):
        return None
    if not isinstance(worker, dict) or worker.get("state") not in ("running", "exited"):
        return None
    rc = worker.get("returncode")
    if rc is not None and not _is_int(rc):
        return None
    return {"proto": 1, "mode": meta["mode"], "pid": meta["pid"], "port": meta["port"], "pending": list(pending),
            "worker": {"state": worker["state"], "returncode": rc}}


def _check_exit(meta: dict) -> dict:
    """EXIT meta with each field type-checked; a field of the wrong type becomes the value that reads worst."""
    rc, reason = meta.get("returncode"), meta.get("reason")
    refusals = meta.get("local_refusals")
    clean = {}
    if isinstance(refusals, dict):
        clean = {k[:64]: v for k, v in refusals.items() if isinstance(k, str) and _is_int(v) and v >= 0}
    unanswered = meta.get("unanswered_at_exit")
    return {"returncode": rc if _is_int(rc) else None,
            "reason": reason[:64] if isinstance(reason, str) else "invalid",
            "timed_out": meta.get("timed_out") if isinstance(meta.get("timed_out"), bool) else False,
            "local_refusals": clean,
            "unanswered_at_exit": unanswered if _is_int(unanswered) and unanswered >= 0 else 1,
            "valid": _is_int(rc) and isinstance(reason, str) and isinstance(refusals, dict) and _is_int(unanswered)}


class RelaySession:
    """One per job attempt: bridges, the in-flight set, the writer queue, the done cache and the liveness timer."""

    def __init__(self, gateway: wire.HandlesRequests, job: wire.JobIdentity, manifest: dict, *,
                 clock: util.Clock | None = None) -> None:
        self.gateway, self.job = gateway, job
        self.clock = clock or util.SystemClock()
        get = lambda key, default: util.lookup(manifest, "relay." + key, default)  # noqa: E731
        self.max_workers = max(1, int(get("max_workers", 64)))
        self.write_chunk = max(1, int(get("write_chunk_bytes", 32768)))
        self.done_cache_max = max(0, int(get("done_cache_max", 256)))
        self.ping_interval_s = float(get("ping_interval_s", 20))
        self.pong_timeout_s = float(get("pong_timeout_s", 60))
        self.flood_limit = FLOOD_FACTOR * self.max_workers
        self._lock = threading.RLock()
        self._wcond = threading.Condition(self._lock)
        self._bridges: list[Bridge] = []
        self._current: Bridge | None = None
        self._mode: str | None = None
        self._in_flight: dict[str, bool] = {}        # rid -> live (not cancelled)
        self._done: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self._outq: collections.deque[_Out] = collections.deque()
        self._writing = False
        self._closed = False
        self._exit_meta: dict | None = None
        self._exit_event = threading.Event()
        self._chars_total = 0
        self._c = {k: 0 for k in _COUNTERS}
        self.log_lines: list[dict] = []
        self._pool = _DaemonPool(self.max_workers, "mhf-relay-pool")
        self._writer = threading.Thread(target=self._write_loop, name="mhf-relay-writer", daemon=True)
        self._timer = threading.Thread(target=self._timer_loop, name="mhf-relay-timer", daemon=True)
        self._writer.start()
        self._timer.start()

    # ------------------------------------------------------------------ public
    def attach(self, channel: FrameChannel) -> Bridge:
        with self._lock:
            if self._closed:
                raise RelayProtocolError("relay session is closed")
            bridge = Bridge(self, channel, len(self._bridges) + 1)
            self._bridges.append(bridge)
            self._current = bridge
            self._c["attaches"] += 1
        setter = getattr(channel, "set_write_timeout", None)
        if callable(setter):
            setter(self.pong_timeout_s)
        bridge.reader_thread = threading.Thread(target=self._read_loop, args=(bridge,), daemon=True,
                                                name="mhf-relay-reader-%d" % bridge.index)
        bridge.reader_thread.start()
        with self._wcond:
            self._wcond.notify_all()
        return bridge

    def wait_exit(self, timeout: float | None) -> dict | None:
        if not self._exit_event.wait(timeout):
            return None
        with self._lock:
            return dict(self._exit_meta) if self._exit_meta is not None else None

    def send_kill(self, reason: str) -> bool:
        frame = mhf_frames.encode_frame("KILL", "0", {"reason": str(reason)[:64]})
        with self._wcond:
            if self._live_bridge() is None:
                return False
            self._outq.append(_Out("KILL", "0", frame))
            self._wcond.notify_all()
        return True

    def total_chars_in(self) -> int:
        with self._lock:
            return self._chars_total

    def counters(self) -> dict:
        with self._lock:
            out = dict(self._c)
            out.update(mode=self._mode, chars_in=self._chars_total,
                       noise_chars=sum(b.decoder.noise_chars for b in self._bridges),
                       frame_errors=sum(b.decoder.errors for b in self._bridges),
                       in_flight=len(self._in_flight), live=sum(1 for v in self._in_flight.values() if v),
                       done_cached=len(self._done))
            return out

    def note_rotation(self) -> None:
        with self._lock:
            self._c["rotations"] += 1

    @property
    def current(self) -> Bridge | None:
        return self._current

    @property
    def bridges(self) -> list[Bridge]:
        with self._lock:
            return list(self._bridges)

    def close(self, *, send_bye: bool = True) -> None:
        with self._wcond:
            if self._closed:
                return
            if send_bye and self._live_bridge() is not None:
                self._outq.append(_Out("BYE", "0", mhf_frames.encode_frame("BYE", "0", {})))
                self._wcond.notify_all()
                end = time.monotonic() + min(5.0, self.pong_timeout_s)
                while (any(o.kind == "BYE" for o in self._outq) or self._writing) and self._live_bridge() is not None:
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    self._wcond.wait(min(left, 0.05))
            self._closed = True
            self._outq.clear()
            bridges = list(self._bridges)
            self._wcond.notify_all()
        self._pool.stop()
        for bridge in bridges:
            self._mark_dead(bridge, "closed")

    # ------------------------------------------------------------------ bridges
    def _live_bridge(self) -> Bridge | None:
        b = self._current
        return b if b is not None and b.alive else None

    def _mark_dead(self, bridge: Bridge, reason: str) -> None:
        with self._wcond:
            if not bridge.alive:
                return
            bridge.alive = False
            bridge.dead_reason = reason
            self._wcond.notify_all()
        try:
            bridge.channel.close()
        except Exception:  # noqa: BLE001 - a channel's close must never break the session
            pass

    def _read_loop(self, bridge: Bridge) -> None:
        try:
            self._read_frames(bridge)
        finally:
            self._release_partial(bridge)

    def _release_partial(self, bridge: Bridge) -> None:
        """A dead bridge keeps its counts but not its partial frame (3.12: one partial frame per LIVE bridge).
        The abandoned characters are discarded, so they count as noise of that bridge's decoder."""
        old = bridge.decoder
        if old.pending_chars:
            fresh = mhf_frames.FrameDecoder()
            fresh.noise_chars, fresh.errors = old.noise_chars + old.pending_chars, old.errors
            with self._lock:
                bridge.decoder = fresh

    def _read_frames(self, bridge: Bridge) -> None:
        while bridge.alive:
            try:
                text = bridge.channel.read_text()
            except Exception:  # noqa: BLE001 - a transport fault is an end of stream
                text = None
            if text is None:
                self._mark_dead(bridge, "eof")
                return
            if not bridge.alive:
                return
            if not isinstance(text, str) or not text:
                continue
            with self._lock:
                bridge.chars_in += len(text)
                self._chars_total += len(text)
            frames = bridge.decoder.feed(text)
            for frame in frames:
                if not bridge.alive:
                    return
                bridge.last_seen = self.clock.monotonic()
                with self._lock:
                    self._c["frames_in"] += 1
                try:
                    self._on_frame(bridge, frame)
                except Exception:  # noqa: BLE001 - nothing a frame carries may stop the reader
                    pass
            if bridge.decoder.errors >= FRAME_ERROR_LIMIT:
                self._mark_dead(bridge, "frame_errors")
                return

    # ------------------------------------------------------------------ frames from the stub (reader threads)
    def _on_frame(self, bridge: Bridge, frame) -> None:
        ftype, rid, meta, body = frame
        if ftype == "REQ":
            self._on_req(bridge, rid, meta, body)
        elif ftype == "CAN":
            with self._lock:
                if self._in_flight.get(rid):
                    self._in_flight[rid] = False
                    self._c["cancelled"] += 1
                self._done.pop(rid, None)
        elif ftype == "PONG":
            with self._lock:
                self._c["pongs"] += 1
        elif ftype == "HELLO":
            self._on_hello(bridge, _check_hello(meta))
        elif ftype == "EXIT":
            with self._lock:
                if self._exit_meta is None:
                    self._exit_meta = _check_exit(meta)
            self._exit_event.set()
        elif ftype == "LOG":
            with self._lock:
                self._c["logs"] += 1
                if len(self.log_lines) < LOG_KEEP:
                    level, msg = meta.get("level"), meta.get("msg")
                    self.log_lines.append({"level": level[:16] if isinstance(level, str) else "?",
                                           "msg": msg[:LOG_MSG_CHARS] if isinstance(msg, str) else ""})
        # RES, PING, KILL and BYE only travel host to stub: ignored.

    def _on_hello(self, bridge: Bridge, hello: dict | None) -> None:
        if hello is None:
            return
        with self._wcond:
            bridge.hello = hello
            if self._mode is None:
                self._mode = hello["mode"]
            if bridge is not self._current:
                return                               # an older bridge's snapshot is stale
            pending = set(hello["pending"])
            for rid, live in list(self._in_flight.items()):
                if live and rid not in pending:       # dropped by the stub while its CAN had no uplink
                    self._in_flight[rid] = False
                    self._c["cancelled"] += 1
            for rid in [r for r in self._done if r not in pending]:
                del self._done[rid]
            bridge.resent.clear()
            for rid in hello["pending"]:
                frame = self._done.get(rid)
                if frame is not None:
                    bridge.resent.add(rid)
                    self._c["resent_replies"] += 1
                    self._outq.append(_Out("RES", rid, frame))
            self._wcond.notify_all()

    def _on_req(self, bridge: Bridge, rid: str, meta: dict, body: bytes) -> None:
        with self._wcond:
            if self._closed:
                return
            if rid in self._in_flight:
                self._c["duplicate_requests"] += 1
                return
            cached = self._done.get(rid)
            if cached is not None:
                if rid in bridge.resent:
                    self._c["duplicate_requests"] += 1
                    return
                bridge.resent.add(rid)
                self._c["resent_replies"] += 1
                self._queue(_Out("RES", rid, cached))
                return
            method, path = meta.get("method"), meta.get("path")
            try:
                envelope = wire.make_envelope(method, path, body)
            except (ValueError, TypeError):
                self._queue(_Out("RES", rid, self._refusal_frame(rid, "invalid_request")))
                return
            live = sum(1 for v in self._in_flight.values() if v)
            if live >= self.flood_limit:
                self._c["flood_refusals"] += 1
                self._queue(_Out("RES", rid, self._refusal_frame(rid, "relay_overloaded")))
                return
            self._in_flight[rid] = True
        del body
        self._pool.submit(self._serve, rid, envelope)

    @staticmethod
    def _refusal_frame(rid: str, code: str) -> bytes:
        reply = wire.refusal_reply(code)
        meta = {"status": reply.status, "headers": [list(h) for h in reply.headers]}
        return mhf_frames.encode_frame("RES", rid, meta, reply.body)

    # ------------------------------------------------------------------ pool threads
    def _serve(self, rid: str, envelope: bytes) -> None:
        if self._closed:
            return
        try:
            reply = self.gateway.handle(self.job, envelope, ingress="relay")
            frame = mhf_frames.encode_frame("RES", rid, {"status": int(reply.status),
                                                         "headers": [[str(k), str(v)] for k, v in reply.headers]},
                                            bytes(reply.body))
        except BaseException as exc:  # noqa: BLE001 - a pool thread must answer the stub whatever happens
            with self._lock:
                self._c["gateway_errors"] += 1
            frame = self._refusal_frame(rid, "gateway_internal" if isinstance(exc, Exception) else "halted")
        del envelope
        with self._wcond:
            self._in_flight.pop(rid, None)
            if self._closed:
                return
            if self._mode == "daemon" and self.done_cache_max > 0:
                self._done[rid] = frame
                while len(self._done) > self.done_cache_max:
                    self._done.popitem(last=False)
            self._queue(_Out("RES", rid, frame))

    # ------------------------------------------------------------------ writer and timer threads
    def _queue(self, out: _Out) -> None:
        """Caller holds the lock."""
        self._outq.append(out)
        self._wcond.notify_all()

    def _write_loop(self) -> None:
        while True:
            with self._wcond:
                while True:
                    if self._closed:
                        return
                    target = self._live_bridge()
                    if self._outq and target is not None:
                        out = self._outq.popleft()
                        self._writing = True
                        break
                    if self._outq and self._current is not None and not self._current.alive:
                        keep = [o for o in self._outq if o.kind == "RES" and self._mode == "daemon"]
                        self._outq = collections.deque(keep)
                    self._wcond.wait(POLL_S)
            ok = True
            try:
                data = out.data
                for i in range(0, len(data), self.write_chunk):
                    target.channel.write(data[i:i + self.write_chunk])
            except Exception:  # noqa: BLE001 - ChannelClosed or any transport fault: the bridge is dead
                ok = False
            with self._wcond:
                self._writing = False
                if ok:
                    self._c["frames_out"] += 1
                    self._c["bytes_out"] += len(out.data)
                elif out.kind == "RES" and self._mode == "daemon" and not self._closed:
                    self._outq.appendleft(out)
                self._wcond.notify_all()
            if not ok:
                self._mark_dead(target, "write")

    def _timer_loop(self) -> None:
        tick = max(0.01, min(0.25, self.ping_interval_s / 4.0, self.pong_timeout_s / 4.0))
        next_ping = self.clock.monotonic() + self.ping_interval_s
        while True:
            with self._wcond:
                if self._closed:
                    return
                now = self.clock.monotonic()
                if now >= next_ping:
                    next_ping = now + self.ping_interval_s
                    if self._live_bridge() is not None:
                        # At the head of the queue: a PING goes out between frames whatever backlog there is (2.9),
                        # so the host's own write backlog cannot starve the PONG that keeps the bridge alive.
                        self._c["pings"] += 1
                        self._outq.appendleft(_Out("PING", "0",
                                                   mhf_frames.encode_frame("PING", "0", {"t": self.clock.now()})))
                        self._wcond.notify_all()
                silent = [b for b in self._bridges if b.alive and now - b.last_seen > self.pong_timeout_s]
            for bridge in silent:
                self._mark_dead(bridge, "silent")
            time.sleep(tick)
