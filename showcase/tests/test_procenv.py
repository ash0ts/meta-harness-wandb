# SPDX-License-Identifier: MIT
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path

from mhforge import procenv, wire
from mhforge.errors import WorkspaceError
from tests import helpers

WORKER_NAMES = {"PATH", "HOME", "TMPDIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8",
                "TEXT_CLASSIFICATION_LLM_CACHE_DIR", "LITELLM_LOCAL_MODEL_COST_MAP", "LITELLM_MODE",
                "TIKTOKEN_RS_CACHE_DIR", "NO_PROXY"}
FIXED = {"PYTHONPATH", "UV_CACHE_DIR", "UV_OFFLINE", "UV_PYTHON_DOWNLOADS", "PYTHONDONTWRITEBYTECODE",
         "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS"}


def _fake_venv(root: Path, *, vocab: str = helpers.VOCAB_FILE) -> Path:
    """<root>/venv with pyvenv.cfg, bin/python and LiteLLM's tokenizers directory holding the vocabulary file."""
    venv = root / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python").write_text("")
    tok = venv / "lib" / "python3.13" / "site-packages" / "litellm" / "litellm_core_utils" / "tokenizers"
    tok.mkdir(parents=True)
    if vocab:
        (tok / vocab).write_text("x")
    return venv


class ProcenvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mhf-procenv-")
        self.root = Path(self.tmp.name)
        self.manifest = helpers.make_manifest()

    def tearDown(self):
        self.tmp.cleanup()

    def _proposer(self, base, manifest=None):
        return procenv.proposer_env(manifest or self.manifest, base, workspace_root=Path("/st/workspace"),
                                    example_venv=Path("/ex/.venv"), scratch_dir=Path("/scr/mh-test/iter001.s1"))

    def test_worker_env_exact_names(self):
        env = procenv.worker_env(path="/usr/local/bin:/usr/bin:/bin", home="/mhf/home", tmp="/mhf/tmp",
                                 pythonpath="/mhf/pkg", cache_dir="/mhf/cache", tokenizer_dir="/tok")
        self.assertEqual(set(env), WORKER_NAMES)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["LITELLM_LOCAL_MODEL_COST_MAP"], "True")
        self.assertEqual(env["LITELLM_MODE"], "PRODUCTION")
        self.assertEqual(env["NO_PROXY"], "127.0.0.1,localhost")
        self.assertEqual(env["TIKTOKEN_RS_CACHE_DIR"], "/tok")
        self.assertEqual(env["TEXT_CLASSIFICATION_LLM_CACHE_DIR"], "/mhf/cache")
        extra = procenv.worker_env(path="p", home="h", tmp="t", pythonpath="pp", cache_dir="c", tokenizer_dir="k",
                                   extra={"OPENROUTER_API_BASE": "http://127.0.0.1:9/v1"})
        self.assertEqual(set(extra), WORKER_NAMES | {"OPENROUTER_API_BASE"})

    def test_route_env(self):
        self.assertEqual(procenv.route_env(2, base_url="http://127.0.0.1:5/v1", api_key="mhf-relay"),
                         {"OPENROUTER_API_BASE": "http://127.0.0.1:5/v1", "OPENROUTER_API_KEY": "mhf-relay"})
        self.assertEqual(procenv.route_env(1, base_url="http://127.0.0.1:5/v1", api_key="x"), {})
        for bad in (3, 0, True):
            with self.assertRaises(ValueError):
                procenv.route_env(bad, base_url="u", api_key="k")

    def test_proposer_env_is_allow_list(self):
        base = {"HOME": "/Users/u", "USER": "u", "LANG": "en_US.UTF-8", "PATH": "/usr/bin:/bin", "SHELL": "/bin/zsh"}
        for i in range(30):
            base[f"CLAUDE_CODE_X{i}"] = "1"
        base.update({f"OTEL_VAR_{i}": "v" for i in range(5)})
        base.update({"MHFORGE_FORGE_API_KEY": "k", "WANDB_API_KEY": "k", "ANTHROPIC_API_KEY": "k",
                     "OPENROUTER_API_KEY": "k", "CLAUDECODE": "1"})
        self.assertGreaterEqual(len([k for k in base if k.startswith(("CLAUDE", "OTEL_")) or "KEY" in k]), 40)
        env = self._proposer(base)
        allowed = set(self.manifest["proposer"]["env_allow"]) | set(self.manifest["proposer"]["env_set"]) | FIXED | {"PATH"}
        self.assertLessEqual(set(env), allowed)
        for name in env:
            self.assertFalse(name.startswith("OTEL_") or (name.startswith("CLAUDE") and name not in FIXED), name)
            self.assertNotIn("KEY", name)
        self.assertEqual(env["HOME"], "/Users/u")
        self.assertNotIn("LOGNAME", env)                     # allowed but absent from base

    def test_proposer_placeholders(self):
        uvdir = self.root / "uvbin"
        uvdir.mkdir()
        uv = uvdir / "uv"
        uv.write_text("#!/bin/sh\n")
        uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
        env = self._proposer({"PATH": f"/nowhere:{uvdir}"})
        self.assertEqual(env["VIRTUAL_ENV"], "/ex/.venv")
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], "/ex/.venv")
        self.assertEqual(env["PATH"].split(":"), ["/ex/.venv/bin", str(uvdir), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        no_uv = self._proposer({"PATH": "/nowhere"})
        self.assertEqual(no_uv["PATH"].split(":"), ["/ex/.venv/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        m = helpers.make_manifest(**{"proposer.env_set": {"X_ROOT": "{workspace_root}/x", "X_UV": "{uv_dir}"}})
        env2 = self._proposer({"PATH": "/nowhere"}, m)
        self.assertEqual(env2["X_ROOT"], "/st/workspace/x")
        self.assertNotIn("X_UV", env2)                       # {uv_dir} unresolved: the entry is dropped
        with self.assertRaises(ValueError):
            self._proposer({}, helpers.make_manifest(**{"proposer.env_set": {"X": "{nope}"}}))

    def test_proposer_fixed_env_is_closed_list(self):
        self.assertEqual(set(procenv.PROPOSER_FIXED_ENV), FIXED)
        self.assertEqual(len(procenv.PROPOSER_FIXED_ENV), 8)
        expected = {"PYTHONPATH": "/st/workspace", "UV_CACHE_DIR": "/scr/mh-test/iter001.s1/.uv-cache",
                    "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "PYTHONDONTWRITEBYTECODE": "1",
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTOUPDATER": "1",
                    "CLAUDE_CODE_STARTUP_FAILURE_RESULTS": "1"}
        for mode in ("upstream", "restricted", "contained"):
            m = helpers.make_manifest(**{"decisions.proposer_permission_mode": mode})
            # a base that tries to override every fixed name, and an env_set entry that tries too
            m["proposer"]["env_set"]["UV_OFFLINE"] = "0"
            m["proposer"]["env_allow"].append("PYTHONPATH")
            env = self._proposer({k: "base" for k in FIXED} | {"PATH": "/usr/bin"}, m)
            self.assertEqual({k: env[k] for k in FIXED}, expected, mode)

    def test_proposer_deny_list_never_survives(self):
        base = {name: "x" for name in procenv.PROPOSER_DENY_NAMES}
        base.update({prefix + "ANY": "x" for prefix in procenv.PROPOSER_DENY_PREFIXES})
        base["PATH"] = "/usr/bin"
        m = helpers.make_manifest()
        m["proposer"]["env_allow"] = list(m["proposer"]["env_allow"]) + sorted(base)   # even if allowed by hand
        with self.assertRaises(ValueError):                  # the assertion stops a manifest that lets them through
            self._proposer(base, m)
        env = self._proposer(base)                           # the closed build from the fixture drops them all
        for name in base:
            if name != "PATH":
                self.assertNotIn(name, env)
        clean = self._proposer({"PATH": "/usr/bin"})
        for name in sorted(procenv.PROPOSER_DENY_NAMES) + [p + "ANY" for p in procenv.PROPOSER_DENY_PREFIXES]:
            with self.assertRaises(ValueError) as ctx:
                procenv.assert_proposer_env(dict(clean, **{name: "secret-value-xyz"}))
            self.assertIn(name, str(ctx.exception))
            self.assertNotIn("secret-value-xyz", str(ctx.exception))
        with self.assertRaises(ValueError):
            procenv.assert_proposer_env(dict(clean, CLAUDE_SOMETHING="1"))
        with self.assertRaises(ValueError) as ctx:
            procenv.assert_proposer_env(dict(clean, HTTPS_GATE="http://127.0.0.1:8123/v1"))
        self.assertIn("HTTPS_GATE", str(ctx.exception))
        procenv.assert_proposer_env(clean)

    def test_proposer_and_worker_env_never_return_a_credential_name(self):
        m = helpers.make_manifest()
        m["proposer"]["env_allow"].append("GITHUB_TOKEN")             # validation refuses this; the builder must too
        with self.assertRaises(ValueError) as ctx:
            self._proposer({"GITHUB_TOKEN": "ghp-value-zzz", "PATH": "/usr/bin"}, m)
        self.assertIn("GITHUB_TOKEN", str(ctx.exception))
        self.assertNotIn("ghp-value-zzz", str(ctx.exception))
        with self.assertRaises(ValueError):
            procenv.worker_env(path="p", home="h", tmp="t", pythonpath="pp", cache_dir="c", tokenizer_dir="k",
                               extra={"MHFORGE_FORGE_API_KEY": "real-key"})
        env = procenv.worker_env(path="p", home="h", tmp="t", pythonpath="pp", cache_dir="c", tokenizer_dir="k",
                                 extra=procenv.route_env(2, base_url="http://127.0.0.1:5/v1", api_key="mhfjob_x"))
        self.assertEqual(env["OPENROUTER_API_KEY"], "mhfjob_x")      # the Route 2 job token is the one exception

    def test_exporter_env_has_no_key_sets_netrc_and_weave_off(self):
        base = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "MHFORGE_EXPORT_API_KEY": "k", "WANDB_API_KEY": "k",
                "HOME": "/elsewhere", "OTEL_X": "1"}
        netrc = self.root / "state" / "export" / "empty.netrc"
        env = procenv.exporter_env(self.manifest, base, home="/h", package_root=Path("/sc"), netrc_file=netrc)
        self.assertEqual(env, {"PATH": "/usr/bin:/bin", "HOME": "/h", "LANG": "C.UTF-8", "PYTHONPATH": "/sc",
                               "NETRC": str(netrc), "WANDB_CONFIG_DIR": "/h", "WANDB_DISABLE_WEAVE": "true"})
        no_lang = procenv.exporter_env(self.manifest, {"PATH": "/bin"}, home="/h", package_root=Path("/sc"),
                                       netrc_file=netrc)
        self.assertEqual(set(no_lang), {"PATH", "HOME", "PYTHONPATH", "NETRC", "WANDB_CONFIG_DIR", "WANDB_DISABLE_WEAVE"})

    def test_exporter_env_closes_ld9_credential_paths(self):
        # LD9: HOME and WANDB_CONFIG_DIR are the run's own empty directory; none of OTEL_*, CES_*, CWSANDBOX_*,
        # WANDB_* (but the two the exporter sets), NETRC of the caller or a proxy variable is carried
        netrc = self.root / "empty.netrc"
        base = {"PATH": "/usr/bin:/bin", "HOME": "/Users/someone", "NETRC": "/Users/someone/.netrc",
                "WANDB_BASE_URL": "https://elsewhere", "WANDB__EXTRA_HTTP_HEADERS": "{}", "WANDB_CONFIG_DIR": "/c",
                "CES_CUSTOM_HEADERS": "Authorization: x", "OTEL_EXPORTER_OTLP_HEADERS": "a=b", "OTEL_SDK_DISABLED": "true",
                "CWSANDBOX_BASE_URL": "https://elsewhere", "HTTPS_PROXY": "http://proxy", "REQUESTS_CA_BUNDLE": "/x"}
        env = procenv.exporter_env(self.manifest, base, home="/state/export/homes/u.1", package_root=Path("/sc"),
                                   netrc_file=netrc)
        self.assertEqual((env["HOME"], env["WANDB_CONFIG_DIR"], env["NETRC"]),
                         ("/state/export/homes/u.1", "/state/export/homes/u.1", str(netrc)))
        self.assertFalse([n for n in env if n.startswith(("OTEL_", "CES_", "CWSANDBOX_")) or "PROXY" in n
                          or n == "REQUESTS_CA_BUNDLE"])
        self.assertEqual(sorted(n for n in env if n.startswith("WANDB_")), ["WANDB_CONFIG_DIR", "WANDB_DISABLE_WEAVE"])
        with self.assertRaises(ValueError):                          # the caller's own HOME is never the child's
            procenv.exporter_env(self.manifest, base, home="/Users/someone/", package_root=Path("/sc"), netrc_file=netrc)

    def test_minimal_env(self):
        self.assertEqual(procenv.minimal_env({"PATH": "/opt/x", "SECRET_TOKEN": "s"}, home="/h"),
                         {"PATH": "/usr/bin:/bin", "HOME": "/h"})

    def test_passthrough_env(self):
        self.assertEqual(procenv.passthrough_env(home="/h", python_bin_dir="/v/bin"),
                         {"PATH": "/v/bin:/usr/bin:/bin", "HOME": "/h", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})

    def test_find_tokenizer_dir(self):
        venv = _fake_venv(self.root)
        found = procenv.find_tokenizer_dir(venv / "bin" / "python", helpers.VOCAB_FILE)
        self.assertEqual(found, venv / "lib" / "python3.13" / "site-packages" / "litellm" / "litellm_core_utils"
                         / "tokenizers")
        with self.assertRaises(WorkspaceError):
            procenv.find_tokenizer_dir(venv / "bin" / "python", "0" * 40)
        with self.assertRaises(WorkspaceError):
            procenv.find_tokenizer_dir(venv / "bin" / "python", "../x")
        with self.assertRaises(WorkspaceError):              # no pyvenv.cfg: not a venv
            procenv.find_tokenizer_dir(self.root / "nothing" / "bin" / "python", helpers.VOCAB_FILE)

    def test_venv_root_is_lexical(self):
        venv = _fake_venv(self.root)
        other = self.root / "base" / "bin"
        other.mkdir(parents=True)
        (other / "python3").write_text("")
        (venv / "bin" / "python").unlink()
        os.symlink(other / "python3", venv / "bin" / "python")
        self.assertEqual(procenv.venv_root(venv / "bin" / "python"), venv)
        self.assertEqual(procenv.venv_root(Path(str(venv / "bin") + "/../bin/python")), venv)

    def test_assert_no_secret_names(self):
        procenv.assert_no_secret_names({"PATH": "x", "HOME": "y", "TIKTOKEN_RS_CACHE_DIR": "z"})
        for bad in ("MHFORGE_FORGE_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "db_password", "MY_APIKEY",
                    "GOOGLE_APPLICATION_CREDENTIALS", "KEY"):
            with self.assertRaises(ValueError) as ctx:
                procenv.assert_no_secret_names({"PATH": "x", bad: "value-zzz"})
            self.assertIn(bad, str(ctx.exception))
            self.assertNotIn("value-zzz", str(ctx.exception))
        procenv.assert_no_secret_names({"OPENROUTER_API_KEY": "mhf-relay"}, allow=["OPENROUTER_API_KEY"])
        for fine in ("KEYCHAIN_PATH", "MONKEY", "TOKENIZERS_PARALLELISM", "HOTKEY_DIR"):
            self.assertFalse(procenv.is_credential_name(fine), fine)

    def test_tiktoken_cache_dir_is_not_a_credential_name(self):
        self.assertFalse(procenv.is_credential_name("TIKTOKEN_RS_CACHE_DIR"))
        self.assertTrue(procenv.is_credential_name("RS_TOKEN"))
        self.assertIn("TIKTOKEN_RS_CACHE_DIR", wire.CREDENTIAL_NAME_EXEMPT)

    def test_no_environment_read_or_resolve_in_source(self):
        src = Path(procenv.__file__).read_text()
        for forbidden in ("os.environ", "os.getenv", ".resolve(", "realpath"):
            self.assertFalse(forbidden in src, forbidden)
        self.assertIsNone(re.search(r"environ\s*\.\s*copy", src))

    @helpers.needs_example_venv
    def test_worker_python_is_never_resolved(self):
        python = helpers.example_python()
        root = procenv.venv_root(python)
        self.assertTrue((root / "pyvenv.cfg").is_file())
        resolved_root = Path(os.path.realpath(python)).parent.parent
        self.assertFalse((resolved_root / "pyvenv.cfg").is_file())
        tok = procenv.find_tokenizer_dir(python, helpers.VOCAB_FILE)
        self.assertEqual(tok, helpers.tokenizer_dir())
        self.assertTrue((tok / helpers.VOCAB_FILE).is_file())


if __name__ == "__main__":
    unittest.main()
