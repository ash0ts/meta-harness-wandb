# SPDX-License-Identifier: MIT
"""MHF1 frame codec for the exec-stream relay (spec 2.9, 3.12).

Standard library only (base64, json, re, zlib). This file is shipped verbatim into
the executor next to mhf_relay_stub.py and imported there as ``import mhf_frames``;
the host imports it as ``mhforge.insandbox.mhf_frames``. It imports nothing from
mhforge.

Frame layout (ASCII only)::

    #MHF1 <TYPE> <rid> <flags> <raw_len> <b64_len> <crc32hex>\\n
    <base64 payload, exactly b64_len characters>\\n

``raw = json(meta) + b"\\n" + body``; the payload is base64(zlib(raw)) with flags
``z`` when that is shorter, else base64(raw) with flags ``-``.
"""

import base64
import binascii
import json
import re
import zlib

MAGIC = "#MHF1 "
TYPES = frozenset({"HELLO", "REQ", "RES", "CAN", "EXIT", "LOG", "PING", "PONG", "KILL", "BYE"})
MAX_B64_LEN = 16 * 1024 * 1024
MAX_HEADER_CHARS = 256
MAX_RAW_LEN = 16 * 1024 * 1024

_RID = re.compile(r"[a-z0-9]{1,16}")
_NUM = re.compile(r"[0-9]{1,9}")
_CRC = re.compile(r"[0-9a-f]{8}")
_TYPE = re.compile(r"[A-Z]{2,8}")
_BAD_B64 = re.compile(r"[^A-Za-z0-9+/=]")
_BAD_HEADER = re.compile(r"[^A-Za-z0-9 \-]")
_FLAGS = ("z", "-")
_FEED_SLICE = 65536


class FrameError(ValueError):
    """A frame that cannot be encoded (bad type, rid, meta or size)."""


def encode_frame(ftype: str, rid: str, meta: dict, body: bytes = b"", *, compress: bool = True) -> bytes:
    if not isinstance(ftype, str) or ftype not in TYPES:
        raise FrameError("unknown frame type")
    if not isinstance(rid, str) or not _RID.fullmatch(rid):
        raise FrameError("bad rid")
    if not isinstance(meta, dict):
        raise FrameError("meta must be a dict")
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise FrameError("body must be bytes")
    try:
        head = json.dumps(meta, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FrameError("meta is not JSON serialisable") from exc
    raw = head.encode("ascii") + b"\n" + bytes(body)
    if len(raw) > MAX_RAW_LEN:
        raise FrameError("frame too large")
    data, flags = raw, "-"
    if compress:
        packed = zlib.compress(raw, 6)
        if len(packed) < len(raw):
            data, flags = packed, "z"
    payload = base64.b64encode(data)
    if len(payload) > MAX_B64_LEN:
        raise FrameError("frame too large")
    crc = "%08x" % (zlib.crc32(raw) & 0xFFFFFFFF)
    header = "%s%s %s %s %d %d %s" % (MAGIC, ftype, rid, flags, len(raw), len(payload), crc)
    if len(header) > MAX_HEADER_CHARS:
        raise FrameError("header too long")
    return header.encode("ascii") + b"\n" + payload + b"\n"


def _parse_header(line: str, max_b64_len: int):
    """Return (type, rid, flags, raw_len, b64_len, crc) or None."""
    parts = line[len(MAGIC):].split(" ")
    if len(parts) != 6:
        return None
    ftype, rid, flags, raw_s, b64_s, crc = parts
    if not _TYPE.fullmatch(ftype) or ftype not in TYPES:
        return None
    if not _RID.fullmatch(rid) or flags not in _FLAGS:
        return None
    if not _NUM.fullmatch(raw_s) or not _NUM.fullmatch(b64_s) or not _CRC.fullmatch(crc):
        return None
    raw_len, b64_len = int(raw_s), int(b64_s)
    if raw_len < 1 or raw_len > MAX_RAW_LEN:
        return None
    if b64_len < 4 or b64_len > max_b64_len or b64_len % 4:
        return None
    return ftype, rid, flags, raw_len, b64_len, crc


def _decode_payload(hdr, payload: str):
    """Return (type, rid, meta, body) or None when any check fails."""
    ftype, rid, flags, raw_len, _b64_len, crc = hdr
    try:
        data = base64.b64decode(payload.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None
    if flags == "z":
        dec = zlib.decompressobj()
        try:
            raw = dec.decompress(data, raw_len + 1)
        except zlib.error:
            return None
        if len(raw) != raw_len or not dec.eof:
            return None
    else:
        raw = data
        if len(raw) != raw_len:
            return None
    if "%08x" % (zlib.crc32(raw) & 0xFFFFFFFF) != crc:
        return None
    cut = raw.find(b"\n")
    if cut < 0:
        return None
    try:
        meta = json.loads(raw[:cut].decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(meta, dict):
        return None
    return ftype, rid, meta, raw[cut + 1:]


class FrameDecoder:
    """Incremental decoder for one channel. Holds at most one partial frame.

    ``feed`` accepts text chunks of any size (decoded chunk by chunk with
    replacement) and never raises on bad input. Discarded characters are counted
    in ``noise_chars``; rejected frames in ``errors``.
    """

    def __init__(self, max_b64_len: int = MAX_B64_LEN) -> None:
        self.max_b64_len = int(max_b64_len)
        self.noise_chars = 0
        self.errors = 0
        self._buf = ""          # scan state: text from a candidate magic (or a magic prefix)
        self._hdr = None        # payload state: parsed header
        self._hdr_chars = 0     # characters of the header line, newline included
        self._parts = []        # payload pieces received so far
        self._have = 0          # characters in _parts
        self._term = False      # payload complete, waiting for its newline

    @property
    def pending_chars(self) -> int:
        """Characters held for the one partial frame (or a partial magic)."""
        if self._hdr is None:
            return len(self._buf)
        return self._hdr_chars + self._have

    def _reject_payload(self) -> None:
        self.errors += 1
        self.noise_chars += self._hdr_chars + self._have
        self._reset()

    def _reset(self) -> None:
        self._hdr = None
        self._hdr_chars = 0
        self._parts = []
        self._have = 0
        self._term = False

    def feed(self, text: str) -> list:
        out = []
        if not isinstance(text, str):
            return out
        if len(text) > _FEED_SLICE:
            # Slicing is invisible to the decoder; it keeps one large feed linear.
            for i in range(0, len(text), _FEED_SLICE):
                out.extend(self.feed(text[i:i + _FEED_SLICE]))
            return out
        while text:
            if self._hdr is not None:
                text = self._feed_payload(text, out)
            else:
                self._buf += text
                text = self._scan()
        return out

    def _feed_payload(self, text: str, out: list) -> str:
        if not self._term:
            need = self._hdr[4] - self._have
            piece = text[:need]
            bad = _BAD_B64.search(piece)
            if bad is not None:
                # Reject at once; the offending character may start the next frame.
                self._have += bad.start()
                self._reject_payload()
                return text[bad.start():]
            self._parts.append(piece)
            self._have += len(piece)
            text = text[len(piece):]
            if self._have < self._hdr[4]:
                return text
            self._term = True
            if not text:
                return text
        # Terminator.
        if text[0] != "\n":
            self._reject_payload()
            return text
        frame = _decode_payload(self._hdr, "".join(self._parts))
        if frame is None:
            self.errors += 1
            self.noise_chars += self._hdr_chars + self._have + 1
        else:
            out.append(frame)
        self._reset()
        return text[1:]

    def _scan(self) -> str:
        """Process self._buf in scan state; return text left for the payload state."""
        buf = self._buf
        while True:
            i = buf.find(MAGIC)
            if i < 0:
                keep = 0
                for k in range(min(len(MAGIC) - 1, len(buf)), 0, -1):
                    if MAGIC.startswith(buf[-k:]):
                        keep = k
                        break
                self.noise_chars += len(buf) - keep
                self._buf = buf[len(buf) - keep:] if keep else ""
                return ""
            if i:
                self.noise_chars += i
                buf = buf[i:]
            window = buf[len(MAGIC):MAX_HEADER_CHARS + 1]
            nl = window.find("\n")
            region = window if nl < 0 else window[:nl]
            bad = _BAD_HEADER.search(region)
            if bad is not None or (nl < 0 and len(buf) > MAX_HEADER_CHARS):
                self._drop_magic()
                buf = buf[len(MAGIC):]
                continue
            if nl < 0:
                self._buf = buf
                return ""
            line_end = len(MAGIC) + nl
            hdr = _parse_header(buf[:line_end], self.max_b64_len)
            if hdr is None:
                self._drop_magic()
                buf = buf[len(MAGIC):]
                continue
            self._hdr = hdr
            self._hdr_chars = line_end + 1
            self._buf = ""
            return buf[line_end + 1:]

    def _drop_magic(self) -> None:
        self.errors += 1
        self.noise_chars += len(MAGIC)
