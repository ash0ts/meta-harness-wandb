# SPDX-License-Identifier: MIT
"""In-executor self-check (spec 3.13): environment names, credential-shaped names, egress probes, versions,
the tokenizer vocabulary file and the proof of machine, Python version and package set.

Standard library only; imports nothing from mhforge. Shipped verbatim into the executor::

    python mhf_selfcheck.py --job JOB.json [--probe-egress]      # prints one JSON line; exit 0 always
    python mhf_selfcheck.py --job JOB.json --import-check NAME    # exit 0 OK, 2 bad name, 3 foreign class, 4 no class

Only names of environment variables are ever reported, never a value. Egress probes run only with
``--probe-egress`` and only by literal address (with DNS denied a by-name probe proves nothing about packets).
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import socket
import sys
import tempfile
import threading

SCHEMA = "mhforge.selfcheck/1"
# Copies of wire.CREDENTIAL_NAME_PATTERN and wire.CREDENTIAL_NAME_EXEMPT (a host-side test pins them).
CREDENTIAL_NAME_PATTERN = r"(?:^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)"
CREDENTIAL_NAME_EXEMPT = ("OPENROUTER_API_KEY", "TIKTOKEN_RS_CACHE_DIR")
CANDIDATE_RE = r"[a-z][a-z0-9_]{0,47}"
IDENTITY_TOKEN_FILE = "/var/run/secrets/sandbox/identity-token/token"
RESOLV_CONF = "/etc/resolv.conf"
VERSION_PACKAGES = ("litellm", "openai", "openai-harmony", "datasets", "tenacity")
TOKENIZER_SUBDIR = ("litellm_core_utils", "tokenizers")
PROBE_TIMEOUT_S = 3.0
TCP_TARGET = ("1.1.1.1", 443)
DNS_NAME = "api.inference.wandb.ai"
METADATA_TARGET = ("169.254.169.254", 80)
IPV6_TARGET = ("2606:4700:4700::1111", 443)
CACHE_ENV = "TEXT_CLASSIFICATION_LLM_CACHE_DIR"

# Upstream's own import line (meta_harness.py L197-L208), then the loaded-class assertion: the class upstream's
# loader would pick (inner_loop.py L500-L502: first MemorySystem subclass in inspect.getmembers order) must be
# defined in the candidate's own module. "{name}" is replaced by the checked candidate name.
IMPORT_CHECK_CODE = """\
from text_classification.agents.{name} import *
import importlib as _mhf_il, inspect as _mhf_in, sys as _mhf_sys
from text_classification.memory_system import MemorySystem as _MhfBase
_mhf_mod = _mhf_il.import_module("text_classification.agents.{name}")
_mhf_cls = None
for _mhf_n, _mhf_o in _mhf_in.getmembers(_mhf_mod, _mhf_in.isclass):
    if issubclass(_mhf_o, _MhfBase) and _mhf_o is not _MhfBase:
        _mhf_cls = _mhf_o
        break
if _mhf_cls is None:
    print("NO_CLASS")
    _mhf_sys.stdout.flush()
    _mhf_sys.exit(4)
if _mhf_cls.__module__ != _mhf_mod.__name__:
    print("FOREIGN_CLASS %s.%s" % (_mhf_cls.__module__, _mhf_cls.__qualname__))
    _mhf_sys.stdout.flush()
    _mhf_sys.exit(3)
print("OK")
"""


def is_credential_name(name):
    return name not in CREDENTIAL_NAME_EXEMPT and re.search(CREDENTIAL_NAME_PATTERN, name.upper()) is not None


def credential_names(environ):
    """Credential-shaped names whose value is non-empty (never a value)."""
    return sorted(name for name, value in environ.items() if value and is_credential_name(name))


def wandb_api_key_state(environ):
    if "WANDB_API_KEY" not in environ:
        return "absent"
    return "set" if environ["WANDB_API_KEY"] else "empty"


# ---------------------------------------------------------------------------------------------------- proof

def normalize_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def freeze_lines(pairs):
    """Sorted, de-duplicated '<name>==<version>' lines for (name, version) pairs; names normalised."""
    return sorted({"%s==%s" % (normalize_name(n), v) for n, v in pairs if n and v is not None})


def freeze_sha256(pairs):
    lines = freeze_lines(pairs)
    return hashlib.sha256("".join(line + "\n" for line in lines).encode("utf-8")).hexdigest(), len(lines)


def installed_pairs():
    out = []
    for dist in importlib.metadata.distributions():
        try:
            out.append((dist.metadata["Name"], dist.version))
        except Exception:      # broken metadata: not counted
            continue
    return out


def expect_problems(expect, machine, python, freeze):
    """What differs from job.expect: machine, Python version prefix, and the freeze hash when expected."""
    problems = []
    if not isinstance(expect, dict):
        return problems
    want_machine = expect.get("machine")
    if want_machine is not None and want_machine != machine:
        problems.append("machine: expected %s, got %s" % (want_machine, machine))
    want_python = expect.get("python")
    if want_python is not None:
        prefix = str(want_python).split(".")
        if python.split(".")[:len(prefix)] != prefix:
            problems.append("python: expected %s, got %s" % (want_python, python))
    want_freeze = expect.get("freeze_sha256")
    if want_freeze is not None and want_freeze != freeze:
        problems.append("freeze_sha256: expected %s, got %s" % (want_freeze, freeze))
    return problems


def versions():
    out = {}
    for name in VERSION_PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = None
    return out


def find_tokenizer_dir():
    """LiteLLM's bundled tokenizers directory, located without importing LiteLLM; None when absent."""
    try:
        spec = importlib.util.find_spec("litellm")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    roots = list(spec.submodule_search_locations or [])
    if not roots and spec.origin:
        roots = [os.path.dirname(spec.origin)]
    for root in roots:
        path = os.path.join(root, *TOKENIZER_SUBDIR)
        if os.path.isdir(path):
            return path
    return None


def vocab_file_present(tokenizer_dir, vocab_file):
    if vocab_file is None:
        return True
    if not tokenizer_dir or not isinstance(vocab_file, str) or "/" in vocab_file:
        return False
    return os.path.isfile(os.path.join(tokenizer_dir, vocab_file))


def cache_writable(job_env, cwd):
    """Whether the worker's cache directory (job.env TEXT_CLASSIFICATION_LLM_CACHE_DIR) can be written."""
    path = job_env.get(CACHE_ENV) if isinstance(job_env, dict) else None
    if not path:
        return False
    if not os.path.isabs(path) and isinstance(cwd, str):
        path = os.path.join(cwd, path)
    try:
        os.makedirs(path, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".mhf-selfcheck-", dir=path)
        os.close(fd)
        os.unlink(name)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------------- egress

def _fmt(host, port):
    return "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)


def tcp_connect(host, port, timeout=PROBE_TIMEOUT_S):
    """'open' when a TCP connect to the literal address succeeds within timeout, else 'blocked'."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        sock = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return "blocked"
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        return "open"
    except (OSError, OverflowError, ValueError):     # a port outside 0..65535 or a bad host name: no packet left
        return "blocked"
    finally:
        sock.close()


def dns_lookup(name, timeout=PROBE_TIMEOUT_S):
    """'open' when one lookup of name answers within timeout, else 'blocked' (getaddrinfo has no timeout: a thread)."""
    result = []

    def look():
        try:
            result.append(bool(socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)))
        except OSError:
            result.append(False)

    worker = threading.Thread(target=look, daemon=True)
    worker.start()
    worker.join(timeout)
    return "open" if result and result[0] else "blocked"


def first_nameserver(resolv_conf=RESOLV_CONF):
    try:
        with open(resolv_conf, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    return parts[1].split("%")[0]
    except OSError:
        pass
    return None


def _split_endpoint(text):
    """'host:port' or '[v6]:port' -> (host, port), or None."""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:([0-9]{1,5})", text) or re.fullmatch(r"([^\s:\[\]]+):([0-9]{1,5})", text)
    if m is None or not 0 < int(m.group(2)) < 65536:
        return None
    return m.group(1), int(m.group(2))


def probe_plan(environ, probe_endpoints, resolv_conf=RESOLV_CONF):
    """The recorded (non-gating) probes as [(name, host, port) or (name, None, target_text)]."""
    plan = [("metadata",) + METADATA_TARGET]
    ns = first_nameserver(resolv_conf)
    plan.append(("resolver", ns, 53) if ns else ("resolver", None, ""))
    host = environ.get("KUBERNETES_SERVICE_HOST")
    if host:
        port = environ.get("KUBERNETES_SERVICE_PORT") or "443"
        plan.append(("cluster", host, int(port) if port.isdigit() else 443))
    else:
        plan.append(("cluster", None, ""))
    plan.append(("ipv6",) + IPV6_TARGET)
    for entry in probe_endpoints if isinstance(probe_endpoints, (list, tuple)) else []:
        hp = _split_endpoint(entry)
        name = "extra:%s" % entry if isinstance(entry, str) else "extra:?"
        plan.append((name,) + hp if hp else (name, None, str(entry)))
    return plan


def egress_report(environ, probe_endpoints, *, probe, resolv_conf=RESOLV_CONF, connect=tcp_connect, resolve=dns_lookup):
    """The egress block. Without probe every result is 'skipped'. Only tcp and dns gate (on the host side)."""
    if probe:
        tcp = connect(*TCP_TARGET)
        dns = resolve(DNS_NAME)
    else:
        tcp = dns = "skipped"
    probes = []
    for name, host, port in probe_plan(environ, probe_endpoints, resolv_conf):
        if host is None:
            probes.append({"name": name, "target": port, "result": "skipped"})
            continue
        result = connect(host, port) if probe else "skipped"
        probes.append({"name": name, "target": _fmt(host, port), "result": result})
    return {"tcp": tcp, "dns": dns, "probes": probes}


# ---------------------------------------------------------------------------------------------------- report

def build_report(job, environ, *, probe=False, resolv_conf=RESOLV_CONF, connect=tcp_connect, resolve=dns_lookup,
                 pairs=None):
    machine = platform.machine()
    python = platform.python_version()
    freeze, count = freeze_sha256(installed_pairs() if pairs is None else pairs)
    tok = find_tokenizer_dir()
    env = job.get("env") if isinstance(job.get("env"), dict) else {}
    return {
        "schema": SCHEMA,
        "python": python,
        "machine": machine,
        "freeze_sha256": freeze,
        "distributions": count,
        "env_names": sorted(environ),
        "credential_names": credential_names(environ),
        "wandb_api_key": wandb_api_key_state(environ),
        "identity_token_file": os.path.exists(IDENTITY_TOKEN_FILE),
        "egress": egress_report(environ, job.get("probe_endpoints"), probe=probe, resolv_conf=resolv_conf,
                                connect=connect, resolve=resolve),
        "versions": versions(),
        "tokenizer_dir": tok,
        "vocab_file_present": vocab_file_present(tok, job.get("vocab_file")),
        "cache_writable": cache_writable(env, job.get("cwd")),
        "expect_problems": expect_problems(job.get("expect"), machine, python, freeze),
    }


def _load_job(path):
    try:
        with open(path, "rb") as fh:
            job = json.loads(fh.read().decode("utf-8"))
        return job if isinstance(job, dict) else None
    except (OSError, ValueError):
        return None


def import_check(job, name):
    """execve python -c IMPORT_CHECK_CODE with job.env and cwd job.cwd; returns 2 for a bad name or job file."""
    if not isinstance(name, str) or re.fullmatch(CANDIDATE_RE, name) is None:
        sys.stderr.write("mhf_selfcheck: bad candidate name\n")
        return 2
    env, cwd = job.get("env"), job.get("cwd")
    if not isinstance(env, dict) or not isinstance(cwd, str):
        sys.stderr.write("mhf_selfcheck: job file lacks env or cwd\n")
        return 2
    code = IMPORT_CHECK_CODE.replace("{name}", name)
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(cwd)
    os.execve(sys.executable, [sys.executable, "-c", code], {str(k): str(v) for k, v in env.items()})
    return 2   # not reached


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mhf_selfcheck.py")
    parser.add_argument("--job", required=True)
    parser.add_argument("--probe-egress", action="store_true")
    parser.add_argument("--import-check", metavar="NAME")
    args = parser.parse_args(argv)
    job = _load_job(args.job)
    if args.import_check is not None:
        if re.fullmatch(CANDIDATE_RE, args.import_check) is None:
            sys.stderr.write("mhf_selfcheck: bad candidate name\n")
            return 2
        if job is None:
            sys.stderr.write("mhf_selfcheck: job file unreadable\n")
            return 2
        return import_check(job, args.import_check)
    report = build_report(job or {}, os.environ, probe=args.probe_egress)
    if job is None:
        report["expect_problems"].append("job_file: unreadable")
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
