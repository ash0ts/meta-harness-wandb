# SPDX-License-Identifier: MIT
"""Per-example capture wrapper around ``python -m text_classification.inner_loop`` (spec 3.15, F9).

    python mhf_capture_entry.py --sidecar PATH [--max-text-chars N] -- <text_classification.inner_loop argv...>

Requires ``PYTHONPATH`` to contain the parent of the ``text_classification`` package. Standard library only;
reads no environment variable and no job-file field. Three plain attribute replacements are made before the
worker's ``__main__`` runs:

  H1  text_classification.llm.ProviderLLM._generate_one   one ``call`` row per invocation, cache hit or miss
  H2  text_classification.llm.LLM.batch                   prompt hashes (after ``self._truncate``) per thread
  H3  text_classification.data.load_dataset_splits_3way / load_dataset_splits
                                                          same values, evaluator wrapped: one ``eval`` row per call

then ``runpy.run_module("text_classification.inner_loop", run_name="__main__", alter_sys=True)``.
Rows follow ``mhforge.sidecar/1`` (spec 2.8). The wrappers return exactly what the wrapped callables return and
re-raise exactly what they raise; any failure of the recording code is swallowed and counted in
``capture_errors``. No prompt or reply text is ever written. Nothing in ``inner_loop`` and not the completion
call is patched. This file imports nothing from ``mhforge``: it is shipped verbatim into the executor.
"""

import functools
import hashlib
import importlib
import json
import os
import platform
import runpy
import sys
import threading
import time

SCHEMA = "mhforge.sidecar/1"
INNER_MODULE = "text_classification.inner_loop"
LLM_MODULE = "text_classification.llm"
DATA_MODULE = "text_classification.data"
DEFAULT_MAX_TEXT_CHARS = 20000
SPLITS = ("train", "val", "test")
USAGE = "usage: mhf_capture_entry.py --sidecar PATH [--max-text-chars N] -- <inner_loop argv...>"
_SCALARS = (bool, int, float, type(None))


def sha256_text(text):
    """Lower-case hex SHA-256 of the UTF-8 bytes of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def example_key(raw_question, target):
    """Example identity: ``sha256(raw_question + "\\0" + target)``."""
    return sha256_text(str(raw_question) + "\0" + str(target))


def parse_args(argv):
    """Split our own flags from the worker's argv. Returns ``(sidecar, max_text_chars, rest)``; exit 2 on misuse."""
    argv = list(argv)
    if "--" not in argv:
        _usage("missing '--' before the inner_loop arguments")
    own, rest = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    sidecar, max_chars, i = None, DEFAULT_MAX_TEXT_CHARS, 0
    while i < len(own):
        flag = own[i]
        if flag in ("--sidecar", "--max-text-chars") and i + 1 < len(own):
            value = own[i + 1]
            i += 2
        elif flag.startswith(("--sidecar=", "--max-text-chars=")):
            flag, value = flag.split("=", 1)
            i += 1
        else:
            _usage("unexpected argument %r" % flag)
        if flag == "--sidecar":
            sidecar = value
        else:
            try:
                max_chars = int(value, 10)
            except ValueError:
                max_chars = -1
            if max_chars < 0:
                _usage("--max-text-chars must be a non-negative integer")
    if not sidecar:
        _usage("--sidecar is required")
    return sidecar, max_chars, rest


def _usage(message):
    sys.stderr.write("%s\nmhf_capture_entry: error: %s\n" % (USAGE, message))
    raise SystemExit(2)


def wanted_splits(inner_argv):
    """``--val-output`` makes ``val`` wanted, ``--test-output`` makes ``test`` wanted (unique prefixes too)."""
    wanted = set()
    for token in inner_argv:
        name = token.split("=", 1)[0]
        if len(name) >= 4 and "--val-output".startswith(name):
            wanted.add("val")
        elif len(name) >= 5 and "--test-output".startswith(name):
            wanted.add("test")
    return [s for s in SPLITS if s in wanted]


def exit_code(code):
    """The process exit status Python derives from ``SystemExit(code)``."""
    if code is None:
        return 0
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return 1


class SidecarWriter:
    """Append-only JSONL file, one ``os.write`` per row. A file that cannot be written costs one stderr line."""

    def __init__(self, path, stderr=None):
        self._stderr = stderr if stderr is not None else sys.stderr
        self._warned = False
        self._fd = None
        try:
            self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        except OSError as exc:
            self._warn(exc)

    def write(self, data):
        if self._fd is None:
            return
        try:
            written = os.write(self._fd, data)
            while written < len(data):
                written += os.write(self._fd, data[written:])
        except OSError as exc:
            self._warn(exc)

    def _warn(self, exc):
        if self._warned:
            return
        self._warned = True
        try:
            self._stderr.write("mhf_capture_entry: sidecar not writable (%s); capture disabled\n" % type(exc).__name__)
            self._stderr.flush()
        except Exception:
            pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


def _json_default(obj):
    return str(obj)


def _plain_str(obj):
    """``str(obj)`` as an exact ``str``: a ``__str__`` may return a str subclass whose slicing or length lies."""
    text = str(obj)
    return text if type(text) is str else str.__str__(text)


def _safe_kwargs(kwargs):
    """Model kwargs with scalar values kept and anything else replaced by its type name (never text)."""
    if not isinstance(kwargs, dict):
        return {}
    return {str(k): (v if isinstance(v, _SCALARS) else "<%s>" % type(v).__name__) for k, v in kwargs.items()}


def _bind(args, kwargs, names):
    values = list(args[: len(names)])
    for name in names[len(values) :]:
        values.append(kwargs[name])
    return values


class Capture:
    """Recording state shared by the three hooks. ``clock`` is injectable for tests."""

    def __init__(self, writer, *, wanted=(), max_text_chars=DEFAULT_MAX_TEXT_CHARS, clock=time.time):
        self._writer = writer
        self._wanted = tuple(wanted)
        self._max = max_text_chars
        self._clock = clock
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._seq = 0
        self._tls = threading.local()
        self._index = {}
        self._rotation = {}
        self._passes = {}
        self.capture_errors = 0
        self.hooks = {"generate_one": False, "batch": False, "loader": False}

    # -- rows ---------------------------------------------------------------------------------------------
    def emit(self, kind, fields, *, schema=False):
        """Write one row with the next ``seq`` and return that ``seq``. Raises on a bad row (callers count it)."""
        with self._write_lock:
            seq = self._seq + 1
            row = {"kind": kind, "seq": seq}
            if schema:
                row["schema"] = SCHEMA
            row.update(fields)
            line = json.dumps(row, ensure_ascii=True, default=_json_default) + "\n"
            self._seq = seq
            self._writer.write(line.encode("ascii"))
            return seq

    def error(self):
        with self._state_lock:
            self.capture_errors += 1

    def emit_meta(self, inner_argv):
        try:
            self.emit("meta", {"t": self._clock(), "pid": os.getpid(), "argv": list(inner_argv),
                               "python": platform.python_version(), "main_thread": threading.main_thread().ident,
                               "hooks": dict(self.hooks)}, schema=True)
        except Exception:
            self.error()

    def emit_done(self, code, exception):
        try:
            self.emit("done", {"t": self._clock(), "exit": code, "exception": exception,
                               "capture_errors": self.capture_errors})
        except Exception:
            self.error()

    # -- thread-local lists ------------------------------------------------------------------------------
    def _list(self, name):
        value = getattr(self._tls, name, None)
        if value is None:
            value = []
            setattr(self._tls, name, value)
        return value

    def _drain(self):
        calls, batch = self._list("calls"), self._list("batch")
        self._tls.calls, self._tls.batch = [], []
        return calls, batch

    # -- H1 -----------------------------------------------------------------------------------------------
    def wrap_generate_one(self, original):
        cap = self

        @functools.wraps(original)
        def _generate_one(self, *args, **kwargs):
            pre = cap._before_call(self, args, kwargs)
            try:
                content = original(self, *args, **kwargs)
            except BaseException as exc:
                cap._after_call(pre, None, exc)
                raise
            cap._after_call(pre, content, None)
            return content

        return _generate_one

    def _before_call(self, provider, args, kwargs):
        pre = {"t_start": None, "bound": None, "cache_hit": None}
        try:
            pre["t_start"] = self._clock()
            pre["bound"] = _bind(args, kwargs, ("prompt", "system_prompt", "kwargs"))
            prompt, system_prompt, model_kwargs = pre["bound"]
            pre["cache_hit"] = provider._load_cache(provider._cache_path(prompt, system_prompt, model_kwargs)) is not None
        except Exception:
            self.error()
        return pre

    def _after_call(self, pre, content, exc):
        try:
            prompt, system_prompt, model_kwargs = pre["bound"] if pre["bound"] is not None else (None, None, None)
            text = None if content is None else str(content)
            seq = self.emit("call", {
                "t_start": pre["t_start"], "t_end": self._clock(), "thread": threading.get_ident(),
                "user_sha256": None if prompt is None else sha256_text(str(prompt)),
                "prompt_chars": None if prompt is None else len(prompt),
                "system_sha256": None if system_prompt is None else sha256_text(str(system_prompt)),
                "kwargs": _safe_kwargs(model_kwargs), "cache_hit": pre["cache_hit"],
                "content_sha256": None if text is None else sha256_text(text),
                "content_chars": None if text is None else len(text),
                "error": None if exc is None else type(exc).__name__,
            })
            self._list("calls").append(seq)
        except Exception:
            self.error()

    # -- H2 -----------------------------------------------------------------------------------------------
    def wrap_batch(self, original):
        cap = self

        @functools.wraps(original)
        def batch(self, *args, **kwargs):
            cap._before_batch(self, args, kwargs)
            return original(self, *args, **kwargs)

        return batch

    def _before_batch(self, llm, args, kwargs):
        try:
            prompts = args[0] if args else kwargs["prompts"]
            if not isinstance(prompts, (list, tuple)):
                return  # an iterator would be consumed by hashing it: record nothing rather than change it
            hashes = [sha256_text(str(llm._truncate(p))) for p in prompts]
            self._list("batch").extend(hashes)
        except Exception:
            self.error()

    # -- H3 -----------------------------------------------------------------------------------------------
    def wrap_loader(self, original, three_way):
        cap = self

        @functools.wraps(original)
        def loader(*args, **kwargs):
            result = original(*args, **kwargs)
            return cap._after_load(result, args, kwargs, three_way)

        return loader

    def _after_load(self, result, args, kwargs, three_way):
        try:
            if not isinstance(result, tuple) or len(result) != (4 if three_way else 3):
                return result
            lists = {"train": result[0], "val": result[1] if three_way else [], "test": result[-2]}
            index = {}
            for split in SPLITS:
                for i, ex in enumerate(lists[split]):
                    try:
                        index.setdefault(example_key(ex["raw_question"], ex["target"]), []).append((split, i))
                    except Exception:
                        self.error()
            wrapped = self.wrap_evaluator(result[-1])
            with self._state_lock:
                self._index = index
            self.emit("splits", {
                "t": self._clock(), "dataset": args[0] if args else kwargs.get("task"),
                "sizes": {s: len(lists[s]) for s in SPLITS}, "wanted": list(self._wanted),
                "duplicate_keys": sum(1 for v in index.values() if len(v) > 1),
            })
            return result[:-1] + (wrapped,)
        except Exception:
            self.error()
            return result

    def wrap_evaluator(self, evaluator):
        cap = self

        @functools.wraps(evaluator)
        def check_answer(*args, **kwargs):
            try:
                raw = evaluator(*args, **kwargs)
            except BaseException:
                try:
                    cap._drain()
                except Exception:
                    cap.error()
                raise
            cap._record_eval(args, kwargs, raw)
            return raw

        return check_answer

    def _resolve(self, key):
        with self._state_lock:
            entries = self._index.get(key) if key is not None else None
            ambiguous = False
            if not entries:
                split, index, pass_key = None, None, (None, key)
            else:
                candidates = [e for e in entries if e[0] in self._wanted] or entries
                if len(candidates) == 1:
                    split, index = candidates[0]
                else:
                    k = self._rotation.get(key, 0)
                    self._rotation[key] = k + 1
                    split, index = candidates[k % len(candidates)]
                    ambiguous = True
                pass_key = (split, index)
            self._passes[pass_key] = self._passes.get(pass_key, 0) + 1
            return split, index, self._passes[pass_key], ambiguous

    def _record_eval(self, args, kwargs, raw):
        try:
            call_seqs, batch_hashes = self._drain()
            pred = args[0] if len(args) > 0 else kwargs.get("pred", kwargs.get("prediction"))
            target = args[1] if len(args) > 1 else kwargs.get("target")
            raw_question = kwargs.get("raw_question")
            if isinstance(raw, dict):
                ok, metrics = raw["was_correct"], raw.get("metrics", {})
            else:
                ok, metrics = bool(raw), {}
            pred_text, target_text = _plain_str(pred), _plain_str(target)
            key = None if raw_question is None else example_key(raw_question, target_text)
            split, index, n_pass, ambiguous = self._resolve(key)
            self.emit("eval", {
                "t": self._clock(), "thread": threading.get_ident(), "split": split, "index": index, "pass": n_pass,
                "example_sha256": key,
                "question_sha256": None if raw_question is None else sha256_text(str(raw_question)),
                "prediction": pred_text[: self._max], "prediction_chars": len(pred_text),
                "prediction_sha256": sha256_text(pred_text),
                "target": target_text[: self._max], "target_sha256": sha256_text(target_text),
                "was_correct": bool(ok), "metrics": metrics if isinstance(metrics, dict) else {},
                "call_seqs": call_seqs, "batch_user_sha256": batch_hashes, "ambiguous": ambiguous,
            })
        except Exception:
            self.error()

    # -- installation -------------------------------------------------------------------------------------
    def install(self):
        """Replace the three seams that exist; a missing target leaves ``hooks.<name>`` false."""
        try:
            llm = importlib.import_module(LLM_MODULE)
        except Exception:
            llm = None
        for hook, cls_name, attr, wrap in (("generate_one", "ProviderLLM", "_generate_one", self.wrap_generate_one),
                                           ("batch", "LLM", "batch", self.wrap_batch)):
            cls = getattr(llm, cls_name, None) if llm is not None else None
            original = getattr(cls, attr, None) if cls is not None else None
            if callable(original):
                setattr(cls, attr, wrap(original))
                self.hooks[hook] = True
        try:
            data = importlib.import_module(DATA_MODULE)
        except Exception:
            data = None
        patched = 0
        for name, three_way in (("load_dataset_splits_3way", True), ("load_dataset_splits", False)):
            original = getattr(data, name, None) if data is not None else None
            if callable(original):
                setattr(data, name, self.wrap_loader(original, three_way))
                patched += 1
        self.hooks["loader"] = patched == 2
        return dict(self.hooks)


def _dash_m_sys_path():
    """``python -m`` puts the working directory first on ``sys.path``; a script puts its own directory there."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if sys.path and os.path.abspath(sys.path[0] or os.curdir) == here:
            sys.path[0] = os.getcwd()
    except Exception:
        pass


def main(argv=None):
    sidecar, max_text_chars, rest = parse_args(sys.argv[1:] if argv is None else argv)
    capture = Capture(SidecarWriter(sidecar), wanted=wanted_splits(rest), max_text_chars=max_text_chars)
    _dash_m_sys_path()
    capture.install()
    capture.emit_meta(rest)
    sys.argv = [INNER_MODULE, *rest]
    try:
        runpy.run_module(INNER_MODULE, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        capture.emit_done(exit_code(exc.code), None)
        raise
    except BaseException as exc:
        capture.emit_done(1 if isinstance(exc, Exception) else None, type(exc).__name__)
        raise
    capture.emit_done(0, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
