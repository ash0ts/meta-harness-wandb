# SPDX-License-Identifier: MIT
"""Fake exec streams for the relay tests (spec 5.1).

``FaultChannel`` is a ``relay.FrameChannel`` over the pipes of a real subprocess (the stub in stdio mode, or the stub's
``bridge`` command): it re-chunks stdout at seeded random boundaries, decodes each chunk alone with
``errors="replace"`` (as the SDK does), counts retained bytes, can drop the stream after N frames (cutting the next
frame in the middle), go silent without closing (reads block; writes are swallowed or block), inject noise text and
delay writes.

``ScriptedChannel`` has no process behind it: a test feeds the text the host reads and inspects the frames the host
wrote. Its writes can be held to model a far side that does not read.
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time

from mhforge.errors import ChannelClosed
from mhforge.insandbox import mhf_frames
from mhforge.relay import POLL_S, _fd_write, _wait_fd

MAGIC = mhf_frames.MAGIC.encode("ascii")
DEFAULT_NOISE = b"noise \xc3 from the stream \xe2\x82\n"      # contains cut UTF-8 sequences


class FaultChannel:
    def __init__(self, proc, *, seed: int = 0, min_chunk: int = 1, max_chunk: int = 4096,
                 drop_after_frames: int | None = None, cut_fraction: float = 0.5,
                 stall_after_frames: int | None = None, stall_writes: str = "swallow", noise_rate: float = 0.0,
                 noise: bytes = DEFAULT_NOISE, write_delay_s: float = 0.0, write_timeout_s: float | None = None,
                 read_chunk: int = 65536) -> None:
        if stall_writes not in ("swallow", "block"):
            raise ValueError("stall_writes is 'swallow' or 'block'")
        self._proc = proc
        self._rfd, self._wfd = proc.stdout.fileno(), proc.stdin.fileno()
        os.set_blocking(self._wfd, False)
        self._rng = random.Random(seed)
        self.min_chunk, self.max_chunk = max(1, min_chunk), max(1, max_chunk, min_chunk)
        self.drop_after_frames, self.cut_fraction = drop_after_frames, cut_fraction
        self.stall_after_frames, self.stall_writes = stall_after_frames, stall_writes
        self.noise_rate, self.noise = noise_rate, noise
        self.write_delay_s, self.write_timeout_s = write_delay_s, write_timeout_s
        self.read_chunk = read_chunk
        self.block_writes = False                  # set True: every write blocks until the write timeout
        self.retained_bytes = 0
        self.frames_delivered = 0
        self.noise_injected = 0
        self.write_sizes: list[int] = []
        self.swallowed = 0
        self.read_threads: set[threading.Thread] = set()
        self.write_threads: set[threading.Thread] = set()
        self.stalled = threading.Event()
        self.dropped = False
        self._closed = threading.Event()
        self._raw = bytearray()
        self._ready = bytearray()
        self._src_eof = False
        self._dropping = False
        self._rlock, self._wlock = threading.Lock(), threading.Lock()

    # -------------------------------------------------------------- triggers
    def stall(self) -> None:
        """Go silent now without closing."""
        self.stalled.set()

    def drop(self) -> None:
        """Drop the stream now: the far end sees its pipes close; reads return None."""
        self._dropping = True
        self._ready.clear()
        self._do_drop()

    def set_write_timeout(self, seconds: float) -> None:
        if self.write_timeout_s is None:
            self.write_timeout_s = float(seconds)

    # -------------------------------------------------------------- FrameChannel
    def read_text(self) -> str | None:
        self.read_threads.add(threading.current_thread())
        while True:
            if self._closed.is_set():
                return None
            if self._ready:
                k = self._rng.randint(self.min_chunk, self.max_chunk)
                chunk = bytes(self._ready[:k])
                del self._ready[:k]
                self.retained_bytes += len(chunk)
                return chunk.decode("utf-8", errors="replace")
            if self._dropping:
                self._do_drop()
                return None
            if self.stalled.is_set():
                self._closed.wait()
                return None
            if self._segment():
                continue
            if self._src_eof:
                if not self._raw:
                    return None
                self._ready += self._raw
                self._raw.clear()
                continue
            if not _wait_fd(self._rfd, False, POLL_S):
                continue
            with self._rlock:
                if self._closed.is_set():
                    return None
                try:
                    data = os.read(self._rfd, self.read_chunk)
                except BlockingIOError:
                    continue
                except (OSError, ValueError):
                    data = b""
            if data:
                self._raw += data
            else:
                self._src_eof = True

    def write(self, data: bytes) -> None:
        self.write_threads.add(threading.current_thread())
        self.write_sizes.append(len(data))
        if self.write_delay_s:
            time.sleep(self.write_delay_s)
        if self._closed.is_set() or self.dropped:
            raise ChannelClosed("fault channel closed")
        if self.stalled.is_set() and self.stall_writes == "swallow":
            self.swallowed += 1
            return
        if self.block_writes or self.stalled.is_set():
            self._closed.wait(self.write_timeout_s)
            raise ChannelClosed("write did not return in time")
        _fd_write(self._wfd, data, self.write_timeout_s, self._closed.is_set, self._wlock)

    def close(self) -> None:
        self._closed.set()
        self._close_pipes()

    # -------------------------------------------------------------- internals
    def _do_drop(self) -> None:
        self.dropped = True
        self._close_pipes()

    def _close_pipes(self) -> None:
        for lock, stream in ((self._wlock, self._proc.stdin), (self._rlock, self._proc.stdout)):
            with lock:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _segment(self) -> bool:
        """Move one unit (noise or one whole frame) from raw to ready, applying the faults. False: need more bytes."""
        raw = self._raw
        if not raw:
            return False
        i = raw.find(MAGIC)
        if i != 0:
            if i < 0:
                keep = next((k for k in range(min(len(MAGIC) - 1, len(raw)), 0, -1) if MAGIC.startswith(raw[-k:])), 0)
                i = len(raw) - keep
                if i <= 0:
                    return False
            self._ready += raw[:i]
            del raw[:i]
            return True
        nl = raw.find(b"\n")
        if nl < 0:
            if len(raw) <= mhf_frames.MAX_HEADER_CHARS + 1:
                return False
            nl = len(MAGIC) - 1                    # not a header: pass the magic on as it is
        try:
            b64_len = int(bytes(raw[:nl]).split(b" ")[5])
        except (IndexError, ValueError):
            self._ready += raw[:nl + 1]
            del raw[:nl + 1]
            return True
        if self.drop_after_frames is not None and self.frames_delivered >= self.drop_after_frames:
            cut = nl + 1 + max(1, int(b64_len * self.cut_fraction))
            if len(raw) < cut and not self._src_eof:
                return False
            self._ready += raw[:cut]
            raw.clear()
            self._dropping = True
            return True
        total = nl + 1 + b64_len + 1
        if len(raw) < total:
            return False
        if self.noise_rate and self._rng.random() < self.noise_rate:
            self._ready += self.noise
            self.noise_injected += 1
        self._ready += raw[:total]
        del raw[:total]
        self.frames_delivered += 1
        if self.stall_after_frames is not None and self.frames_delivered >= self.stall_after_frames:
            self.stalled.set()
        return True


class ScriptedChannel:
    """A FrameChannel driven by the test: feed() what the host reads; frames() is what the host wrote."""

    def __init__(self, *, write_timeout_s: float | None = None) -> None:
        self._q: queue.Queue = queue.Queue()
        self.write_timeout_s = write_timeout_s
        self.decoder = mhf_frames.FrameDecoder()
        self.written = bytearray()
        self.write_sizes: list[int] = []
        self.received: list[tuple] = []
        self.read_threads: set[threading.Thread] = set()
        self.write_threads: set[threading.Thread] = set()
        self.closed = threading.Event()
        self._gate = threading.Event()
        self._gate.set()
        self.blocked_writes = 0
        self._cond = threading.Condition()

    # -------------------------------------------------------------- test side
    def feed(self, text: str) -> None:
        self._q.put(text)

    def feed_frame(self, ftype: str, rid: str = "0", meta: dict | None = None, body: bytes = b"") -> str:
        text = mhf_frames.encode_frame(ftype, rid, meta or {}, body).decode("ascii")
        self.feed(text)
        return text

    def end(self) -> None:
        self._q.put(None)

    def hold_writes(self) -> None:
        self._gate.clear()

    def release_writes(self) -> None:
        self._gate.set()

    def of_type(self, ftype: str, rid: str | None = None) -> list[tuple]:
        with self._cond:
            return [f for f in self.received if f[0] == ftype and (rid is None or f[1] == rid)]

    def expect(self, ftype: str, rid: str | None = None, *, n: int = 1, timeout: float = 10.0) -> tuple:
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                hits = [f for f in self.received if f[0] == ftype and (rid is None or f[1] == rid)]
                if len(hits) >= n:
                    return hits[n - 1]
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError("no %s #%d (rid %s); got %s" % (ftype, n, rid,
                                                                         [(f[0], f[1]) for f in self.received]))
                self._cond.wait(min(left, 0.05))

    # -------------------------------------------------------------- FrameChannel
    def set_write_timeout(self, seconds: float) -> None:
        if self.write_timeout_s is None:
            self.write_timeout_s = float(seconds)

    def read_text(self) -> str | None:
        self.read_threads.add(threading.current_thread())
        while not self.closed.is_set():
            try:
                item = self._q.get(timeout=0.05)
            except queue.Empty:
                continue
            return item
        return None

    def write(self, data: bytes) -> None:
        self.write_threads.add(threading.current_thread())
        if self.closed.is_set():
            raise ChannelClosed("scripted channel closed")
        if not self._gate.is_set():
            with self._cond:
                self.blocked_writes += 1
            end = None if self.write_timeout_s is None else time.monotonic() + self.write_timeout_s
            while not self._gate.wait(0.02):
                if self.closed.is_set() or (end is not None and time.monotonic() >= end):
                    raise ChannelClosed("write did not return in time")
        with self._cond:
            self.write_sizes.append(len(data))
            self.written += data
            self.received.extend(self.decoder.feed(bytes(data).decode("ascii", errors="replace")))
            self._cond.notify_all()

    def close(self) -> None:
        self.closed.set()
