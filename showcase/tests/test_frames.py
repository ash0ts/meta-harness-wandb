# SPDX-License-Identifier: MIT
"""Tests for the MHF1 frame codec (spec 2.9, 3.12, 5.2)."""

import base64
import json
import random
import unittest
import zlib

from mhforge.insandbox import mhf_frames as F


def _replace_decode_chunks(data: bytes, cuts):
    """Split bytes at the given offsets and decode each piece alone with replacement."""
    out, prev = [], 0
    for c in list(cuts) + [len(data)]:
        out.append(data[prev:c].decode("utf-8", errors="replace"))
        prev = c
    return out


def _feed_all(dec, chunks):
    got = []
    for c in chunks:
        got.extend(dec.feed(c))
    return got


def _manual_frame(ftype, rid, flags, raw_len, payload_b64: str, crc: str) -> str:
    return "%s%s %s %s %d %d %s\n%s\n" % (F.MAGIC, ftype, rid, flags, raw_len, len(payload_b64), crc, payload_b64)


def _crc(raw: bytes) -> str:
    return "%08x" % (zlib.crc32(raw) & 0xFFFFFFFF)


BIN = bytes(range(256)) * 4


class TestEncode(unittest.TestCase):
    def test_roundtrip_all_types_binary_body(self):
        for compress in (True, False):
            for i, ftype in enumerate(sorted(F.TYPES)):
                meta = {"k": i, "s": "café", "l": [1, None, True]}
                frame = F.encode_frame(ftype, "r%d" % i, meta, BIN, compress=compress)
                self.assertIsInstance(frame, bytes)
                frame.decode("ascii")  # ASCII only
                dec = F.FrameDecoder()
                got = dec.feed(frame.decode("ascii"))
                self.assertEqual(got, [(ftype, "r%d" % i, meta, BIN)])
                self.assertEqual((dec.noise_chars, dec.errors, dec.pending_chars), (0, 0, 0))

    def test_header_shape_and_flags(self):
        frame = F.encode_frame("REQ", "r1", {"method": "POST"}, b"x" * 2000).decode("ascii")
        header, payload, tail = frame.split("\n")
        self.assertEqual(tail, "")
        parts = header.split(" ")
        self.assertEqual(parts[0] + " ", F.MAGIC)
        self.assertEqual(parts[1:4], ["REQ", "r1", "z"])
        raw = b'{"method":"POST"}\n' + b"x" * 2000
        self.assertEqual(int(parts[4]), len(raw))
        self.assertEqual(int(parts[5]), len(payload))
        self.assertEqual(parts[6], _crc(raw))
        self.assertEqual(zlib.decompress(base64.b64decode(payload)), raw)
        # Incompressible or tiny raw is sent uncompressed.
        small = F.encode_frame("PING", "0", {"t": 1.5}).decode("ascii")
        self.assertEqual(small.split(" ")[3], "-")
        plain = F.encode_frame("REQ", "r1", {}, b"x" * 2000, compress=False).decode("ascii")
        self.assertEqual(plain.split(" ")[3], "-")

    def test_encode_rejects_bad_type_rid_meta(self):
        for bad in (dict(ftype="NOPE", rid="r1"), dict(ftype="req", rid="r1"),
                    dict(ftype="REQ", rid=""), dict(ftype="REQ", rid="R1"),
                    dict(ftype="REQ", rid="r1\n"), dict(ftype="REQ", rid="a" * 17),
                    dict(ftype="REQ", rid="r-1")):
            with self.assertRaises(F.FrameError):
                F.encode_frame(bad["ftype"], bad["rid"], {})
        with self.assertRaises(F.FrameError):
            F.encode_frame("REQ", "r1", ["not", "a", "dict"])
        with self.assertRaises(F.FrameError):
            F.encode_frame("REQ", "r1", {"x": object()})
        with self.assertRaises(F.FrameError):
            F.encode_frame("REQ", "r1", {"x": float("nan")})
        self.assertTrue(issubclass(F.FrameError, ValueError))

    def test_can_and_kill_frames_round_trip(self):
        can = F.encode_frame("CAN", "r42", {})
        kill = F.encode_frame("KILL", "0", {"reason": "deadline"})
        dec = F.FrameDecoder()
        got = dec.feed((can + kill).decode("ascii"))
        self.assertEqual(got, [("CAN", "r42", {}, b""), ("KILL", "0", {"reason": "deadline"}, b"")])


class TestDecoder(unittest.TestCase):
    def _frames(self, n, seed=0):
        rnd = random.Random(seed)
        frames, expect = [], []
        types = sorted(F.TYPES)
        for i in range(n):
            ftype = types[i % len(types)]
            body = bytes(rnd.getrandbits(8) for _ in range(rnd.randrange(0, 600)))
            if i % 3 == 0:
                body = b"abc" * rnd.randrange(0, 400)
            meta = {"i": i, "path": "/v1/chat/completions"}
            rid = "r%d" % i
            frames.append(F.encode_frame(ftype, rid, meta, body))
            expect.append((ftype, rid, meta, body))
        return b"".join(frames).decode("ascii"), expect

    def test_feed_any_chunking(self):
        stream, expect = self._frames(3)
        for cut in range(len(stream) + 1):
            dec = F.FrameDecoder()
            got = _feed_all(dec, [stream[:cut], stream[cut:]])
            self.assertEqual(got, expect, "split at %d" % cut)
            self.assertEqual((dec.noise_chars, dec.errors), (0, 0))
        stream, expect = self._frames(200, seed=7)
        rnd = random.Random(1234)
        for _ in range(3):
            pos, chunks = 0, []
            while pos < len(stream):
                step = rnd.choice((1, 2, 7, 64, 513, 4096, 70000))
                chunks.append(stream[pos:pos + step])
                pos += step
            dec = F.FrameDecoder()
            self.assertEqual(_feed_all(dec, chunks), expect)
            self.assertEqual((dec.noise_chars, dec.errors, dec.pending_chars), (0, 0, 0))

    def test_multibyte_payload_survives_split_and_replace_decoding(self):
        body = ("café 漢字 \U0001f642 " * 40).encode("utf-8")
        data = F.encode_frame("RES", "r7", {"status": 200, "headers": [["content-type", "application/json"]]}, body)
        rnd = random.Random(5)
        for cut in range(0, len(data) + 1, 3):
            dec = F.FrameDecoder()
            got = _feed_all(dec, _replace_decode_chunks(data, [cut]))
            self.assertEqual(got[0][3], body)
        cuts = sorted(rnd.sample(range(1, len(data)), 40))
        got = _feed_all(F.FrameDecoder(), _replace_decode_chunks(data, cuts))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][3].decode("utf-8"), body.decode("utf-8"))

    def test_unarmoured_utf8_is_corrupted_by_the_same_path(self):
        text = "café 漢字 \U0001f642 " * 40
        data = text.encode("utf-8")
        cut = data.index("漢".encode("utf-8")) + 1  # inside a 3-byte character
        rebuilt = "".join(_replace_decode_chunks(data, [cut]))
        self.assertNotEqual(rebuilt, text)
        self.assertIn("�", rebuilt)

    def test_noise_between_frames_is_skipped_and_counted(self):
        a = F.encode_frame("LOG", "0", {"level": "info", "msg": "a"}).decode("ascii")
        b = F.encode_frame("PONG", "0", {"t": 2.0}).decode("ascii")
        noise1 = "pip warning: something ��\n"
        noise2 = "#MHF partial-magic text #MH\n"
        dec = F.FrameDecoder()
        got = dec.feed(noise1 + a + noise2 + b + "trailing")
        self.assertEqual([g[0] for g in got], ["LOG", "PONG"])
        self.assertEqual(dec.errors, 0)
        self.assertEqual(dec.noise_chars, len(noise1) + len(noise2) + len("trailing"))
        # A magic followed by junk is a rejected frame, then resync.
        dec = F.FrameDecoder()
        got = dec.feed("#MHF1 junk here\n" + a)
        self.assertEqual([g[0] for g in got], ["LOG"])
        self.assertEqual(dec.errors, 1)
        self.assertEqual(dec.noise_chars, len("#MHF1 junk here\n"))

    def test_magic_prefix_split_across_chunks(self):
        a = F.encode_frame("BYE", "0", {}).decode("ascii")
        dec = F.FrameDecoder()
        self.assertEqual(dec.feed("noise#MH"), [])
        self.assertEqual(dec.pending_chars, 3)
        self.assertEqual(dec.feed("F1" + a[5:]), [("BYE", "0", {}, b"")])
        self.assertEqual(dec.noise_chars, 5)

    def test_corrupt_crc_length_or_base64_resyncs(self):
        good = F.encode_frame("HELLO", "0", {"proto": 1, "mode": "stdio", "pending": []}).decode("ascii")
        raw = b'{"a":1}\n' + b"body"
        p = base64.b64encode(raw).decode("ascii")
        bad_frames = {
            "crc": _manual_frame("REQ", "r1", "-", len(raw), p, "00000000"),
            "raw_len": _manual_frame("REQ", "r1", "-", len(raw) + 1, p, _crc(raw)),
            "b64_len_short": "%sREQ r1 - %d %d %s\n%s\n" % (F.MAGIC, len(raw), len(p) - 4, _crc(raw), p),
            "b64_len_long": "%sREQ r1 - %d %d %s\n%s\n" % (F.MAGIC, len(raw), len(p) + 4, _crc(raw), p),
            "bad_padding": _manual_frame("REQ", "r1", "-", len(raw), "A===" + p[4:], _crc(raw)),
            "zlib_garbage": _manual_frame("REQ", "r1", "z", len(raw), p, _crc(raw)),
            "meta_not_object": _manual_frame("REQ", "r1", "-", 4, base64.b64encode(b"[1]\n").decode(), _crc(b"[1]\n")),
            "no_meta_newline": _manual_frame("REQ", "r1", "-", 4, base64.b64encode(b"{}{}").decode(), _crc(b"{}{}")),
            "bad_flags": _manual_frame("REQ", "r1", "x", len(raw), p, _crc(raw)),
            "bad_type": _manual_frame("NOPE", "r1", "-", len(raw), p, _crc(raw)),
        }
        for name, bad in bad_frames.items():
            for chunked in (False, True):
                dec = F.FrameDecoder()
                stream = bad + good
                chunks = list(stream) if chunked else [stream]
                got = _feed_all(dec, chunks)
                self.assertEqual([g[0] for g in got], ["HELLO"], name)
                self.assertGreaterEqual(dec.errors, 1, name)
                self.assertGreater(dec.noise_chars, 0, name)
                self.assertEqual(dec.pending_chars, 0, name)

    def test_oversize_header_and_payload_rejected(self):
        good = F.encode_frame("PING", "0", {"t": 0.0}).decode("ascii")
        long_header = F.MAGIC + "REQ" + " r1" * 200  # no newline within 256 characters
        dec = F.FrameDecoder()
        got = dec.feed(long_header + "\n" + good)
        self.assertEqual([g[0] for g in got], ["PING"])
        self.assertEqual(dec.errors, 1)
        # A long header is rejected before its newline arrives.
        dec = F.FrameDecoder()
        dec.feed(F.MAGIC + "REQ r1 - 1 4 " + "0" * 300)
        self.assertEqual(dec.errors, 1)
        self.assertLess(dec.pending_chars, F.MAX_HEADER_CHARS)
        # b64_len over the decoder's limit: rejected from the header alone.
        frame = F.encode_frame("RES", "r1", {"status": 200}, bytes(range(256)) * 20, compress=False).decode("ascii")
        dec = F.FrameDecoder(max_b64_len=1024)
        self.assertEqual(dec.feed(frame + good)[-1][0], "PING")
        self.assertEqual(dec.errors, 1)
        dec = F.FrameDecoder()
        self.assertEqual(dec.feed("%sREQ r1 - 10 %d 00000000\n" % (F.MAGIC, F.MAX_B64_LEN + 4)), [])
        self.assertEqual((dec.errors, dec.pending_chars), (1, 0))
        # The encoder refuses a frame past the limits.
        with self.assertRaises(F.FrameError):
            F.encode_frame("REQ", "r1", {}, b"\x00" * (13 * 1024 * 1024), compress=False)
        with self.assertRaises(F.FrameError):
            F.encode_frame("REQ", "r1", {}, b"\x00" * (F.MAX_RAW_LEN + 1))

    def test_compression_bomb_rejected(self):
        good = F.encode_frame("PONG", "0", {"t": 1.0}).decode("ascii")
        bomb_raw = b"{}\n" + b"\x00" * (40 * 1024 * 1024)
        packed = base64.b64encode(zlib.compress(bomb_raw, 9)).decode("ascii")
        self.assertLess(len(packed), 100_000)
        for claimed in (100, F.MAX_RAW_LEN, F.MAX_RAW_LEN + 1):
            dec = F.FrameDecoder()
            got = dec.feed(_manual_frame("REQ", "r1", "z", claimed, packed, _crc(bomb_raw)) + good)
            self.assertEqual([g[0] for g in got], ["PONG"], claimed)
            self.assertEqual(dec.errors, 1)

    def test_decoder_holds_one_partial_frame(self):
        a = F.encode_frame("REQ", "r1", {"method": "POST"}, bytes(range(256)) * 300).decode("ascii")
        b = F.encode_frame("REQ", "r2", {"method": "POST"}, b"z" * 100).decode("ascii")
        dec = F.FrameDecoder()
        half = len(b) // 2
        got = dec.feed(a + a + b[:half])
        self.assertEqual([g[1] for g in got], ["r1", "r1"])
        self.assertEqual(dec.pending_chars, half)
        self.assertEqual(dec.feed(b[half:]), [("REQ", "r2", {"method": "POST"}, b"z" * 100)])
        self.assertEqual(dec.pending_chars, 0)
        # Long noise without a magic is not held.
        dec = F.FrameDecoder()
        dec.feed("x" * 500_000)
        self.assertEqual(dec.pending_chars, 0)
        self.assertEqual(dec.noise_chars, 500_000)
        # A partial frame never exceeds its own length.
        dec = F.FrameDecoder()
        for ch in a[:-1]:
            dec.feed(ch)
            self.assertLessEqual(dec.pending_chars, len(a))
        self.assertEqual(dec.pending_chars, len(a) - 1)

    def test_non_base64_character_rejects_frame_at_once(self):
        big = F.encode_frame("REQ", "r1", {"method": "POST"}, random.Random(3).randbytes(45_000)).decode("ascii")
        self.assertGreater(len(big), 60_000)
        cut = big[:30_000]
        hello = F.encode_frame("HELLO", "0", {"proto": 1, "mode": "daemon", "pid": 1, "port": 2, "pending": ["r1"],
                                              "worker": {"state": "running", "returncode": None}}).decode("ascii")
        exit_ = F.encode_frame("EXIT", "0", {"returncode": 0, "reason": "ok", "timed_out": False,
                                             "local_refusals": {}, "unanswered_at_exit": 0}).decode("ascii")
        dec = F.FrameDecoder()
        got = dec.feed(cut + hello + exit_)
        self.assertEqual([g[0] for g in got], ["HELLO", "EXIT"])
        self.assertEqual(dec.errors, 1)
        self.assertEqual(dec.noise_chars, len(cut))
        # Fed in separate chunks: HELLO comes out as soon as it is complete.
        dec = F.FrameDecoder()
        self.assertEqual(dec.feed(cut), [])
        self.assertEqual([g[0] for g in dec.feed(hello)], ["HELLO"])
        self.assertEqual([g[0] for g in dec.feed(exit_)], ["EXIT"])

    def test_wrong_terminator_rejected(self):
        good = F.encode_frame("BYE", "0", {}).decode("ascii")
        frame = F.encode_frame("LOG", "0", {"level": "x", "msg": "y"}).decode("ascii")
        dec = F.FrameDecoder()
        got = dec.feed(frame[:-1] + "X" + good)
        self.assertEqual([g[0] for g in got], ["BYE"])
        self.assertEqual(dec.errors, 1)

    def test_feed_never_raises_on_random_input(self):
        rnd = random.Random(99)
        good = F.encode_frame("PING", "0", {"t": 3.0}).decode("ascii")
        alphabet = F.MAGIC + "ABCxyz019+/=\n -#�é"
        for _ in range(200):
            junk = "".join(rnd.choice(alphabet) for _ in range(rnd.randrange(0, 400)))
            dec = F.FrameDecoder()
            dec.feed(junk)
            dec.feed(None)  # not a str: ignored
            got = dec.feed("\n" * 2 + good)
            self.assertIsInstance(got, list)
        # Decoding a random byte stream with replacement never raises either.
        dec = F.FrameDecoder()
        dec.feed(bytes(rnd.getrandbits(8) for _ in range(20000)).decode("utf-8", errors="replace"))

    def test_deeply_nested_meta_is_rejected_not_raised(self):
        # Review regression: json.loads raised RecursionError out of feed().
        raw = b'{"a":' + b"[" * 100_000 + b"]" * 100_000 + b"}\n"
        packed = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
        good = F.encode_frame("PING", "0", {"t": 1.0}).decode("ascii")
        dec = F.FrameDecoder()
        got = dec.feed(_manual_frame("LOG", "0", "z", len(raw), packed, _crc(raw)) + good)
        self.assertEqual([g[0] for g in got], ["PING"])
        self.assertEqual(dec.errors, 1)

    def test_one_large_feed_of_small_frames_is_linear(self):
        # Review regression: a single multi-megabyte feed was quadratic in its length.
        import time
        one = F.encode_frame("LOG", "0", {"level": "i", "msg": "x" * 50}).decode("ascii")
        s = one * 64_000  # about 4.7 MB
        t0 = time.perf_counter()
        got = F.FrameDecoder().feed(s)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(got), 64_000)
        self.assertLess(elapsed, 3.0)

    def test_decoders_are_independent(self):
        frame = F.encode_frame("REQ", "r1", {}, b"q" * 5000, compress=False).decode("ascii")
        hello = F.encode_frame("HELLO", "0", {"proto": 1}).decode("ascii")
        d1, d2 = F.FrameDecoder(), F.FrameDecoder()
        self.assertEqual(d1.feed(frame[:1000]), [])
        self.assertEqual([g[0] for g in d2.feed(hello)], ["HELLO"])
        self.assertEqual(d1.pending_chars, 1000)

    def test_imports_nothing_from_mhforge(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(F))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0] or ".")
        self.assertEqual(names, {"base64", "binascii", "json", "re", "zlib"})


if __name__ == "__main__":
    unittest.main()
