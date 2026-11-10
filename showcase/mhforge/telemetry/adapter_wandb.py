# SPDX-License-Identifier: MIT
"""W&B sink (UNVERIFIED until the live preflight): wandb run, EvalTable, Artifact and read-back (spec 3.25).

SDKs are imported only inside functions, one `_sdk_*` function per SDK operation. Never `wandb.login`: the key goes in
`wandb.Settings(api_key=)` and `wandb.Api(api_key=)`, with the base URL pinned in both. Before the first import: refuse
with `weave` importable, any WANDB_* or CES_* variable the adapter does not set itself, or a workspace settings file in
the working directory; NETRC, WANDB_CONFIG_DIR and the SDK's directories point into STATE/export; console, git and weave
are off (LD9)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Mapping, MutableMapping, Sequence

from ..errors import AdapterUnavailable, ExportError
from ..paths import StatePaths
from .adapter_agentlens import _read_key, key_facts

__all__ = ["WandbSdkSink", "ADAPTER", "DEFAULT_BASE_URL", "REFUSED_ENV", "CES_MARKER", "TABLE_MARKER", "OWN_ENV",
           "config_dir"]

ADAPTER = {"name": "wandb", "version": "0.31.0", "unverified": True}
DEFAULT_BASE_URL = "https://api.forge.coreweave.com"   # wandb 0.31.0 Settings.base_url default (sdk/lib/urls.py:7)
REFUSED_ENV = ("WANDB_BASE_URL", "CES_BASE_URL", "WANDB_APP_URL", "WANDB_MODE", "CES_CUSTOM_HEADERS")
# The only WANDB_* names the process may hold: the ones this adapter (or exporter_env) sets. Any other WANDB_<FIELD> or
# WANDB__<X> sets a Settings field (wandb_settings.py:1975-2016: api_key, identity_token_file, https_proxy,
# x_extra_http_headers, insecure_disable_ssl, settings_system ...); WANDB_SERVICE attaches to another wandb-core.
OWN_ENV = ("WANDB_DISABLE_WEAVE", "WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_DATA_DIR", "WANDB_CONSOLE", "WANDB_DISABLE_GIT",
           "WANDB_CONFIG_DIR")
_SDK_SET_ENV = ("WANDB_SERVICE",)                 # written by wandb itself once imported (service_token.py:77)
CES_MARKER = "eval-table-ces"                     # summary _type of an EvalTable logged with backend "ces"
TABLE_MARKER = "table-file"                       # summary _type of a wandb.Table
CES_FEATURES = ("EVAL_TABLES_CES",)               # wandb.proto ServerFeature name, the check EvalTable.bind_to_run makes
CES_PROBES = 3                                    # the probe also answers false on a timeout [rep ET-V]
PROJECT_QUERY = ("query MhforgeProject($name: String!, $entity: String!) { project(name: $name, entityName: $entity) "
                 "{ name access readOnly weavePermissions { canWrite } } }")
FEATURES_QUERY = "query MhforgeServerFeatures { serverInfo { features { name isEnabled } } }"   # unused since 0.31.0


def config_dir(paths: StatePaths) -> Path:
    """WANDB_CONFIG_DIR of an in-process export: an empty directory under STATE (the global settings file lives there)."""
    return paths.export_root / "wandb-config"


# ---------------------------------------------------------------- the SDK operations, one function each (UNVERIFIED)

def _import_wandb():
    try:
        import wandb
    except ImportError:
        raise AdapterUnavailable("wandb is not importable") from None
    return wandb


def _sdk_settings(wandb, key: str):
    return wandb.Settings(api_key=key, base_url=DEFAULT_BASE_URL, disable_git=True, save_code=False, console="off",
                          x_disable_meta=True, x_disable_stats=True, x_disable_machine_info=True,
                          x_save_requirements=False)


def _sdk_init(wandb, run: Mapping, settings, local_dir: Path):
    return wandb.init(entity=run["entity"], project=run["project"], id=run["id"], group=run["group"],
                      job_type=run["job_type"], name=run["name"], tags=list(run.get("tags") or []),
                      config=dict(run.get("config") or {}), dir=str(local_dir),
                      resume="allow" if run.get("resume") else None, settings=settings)


def _sdk_define_metric(run, name: str, step_metric: str | None) -> None:
    run.define_metric(name) if step_metric is None else run.define_metric(name, step_metric=step_metric)


def _sdk_log(run, row: Mapping) -> None:
    run.log(dict(row))                            # never a step argument (2.15)


def _sdk_artifact(wandb, art: Mapping):
    return wandb.Artifact(art["name"], type=art["type"], metadata=dict(art.get("metadata") or {}))


def _sdk_add_file(artifact, local_path: Path, name: str) -> None:
    artifact.add_file(str(local_path), name=name)


def _sdk_log_artifact(run, artifact, aliases: Sequence[str]) -> None:
    run.log_artifact(artifact, aliases=list(aliases)).wait()


def _sdk_use_artifact(run, ref: str) -> None:
    run.use_artifact(ref)


def _sdk_eval_table(wandb, t: Mapping):
    return wandb.EvalTable(columns=list(t["columns"]), data=[list(r) for r in t["rows"]],
                           input_columns=list(t["input_columns"]), output_columns=list(t["output_columns"]),
                           score_columns=list(t["score_columns"]), backend="ces", unsupported_media_mode="raise")


def _sdk_table(wandb, t: Mapping):
    return wandb.Table(columns=list(t["columns"]), data=[list(r) for r in t["rows"]])


def _sdk_summary_update(run, summary: Mapping) -> None:
    run.summary.update(dict(summary))


def _sdk_urls(run) -> tuple[str | None, str]:     # (run URL, the base URL the run really used: a file can move it)
    url, base = getattr(run, "url", None), getattr(getattr(run, "settings", None), "base_url", None)
    return url if isinstance(url, str) else None, base if isinstance(base, str) and base else DEFAULT_BASE_URL


def _sdk_finish(run) -> None:
    run.finish()


def _sdk_api(wandb, key: str):
    return wandb.Api(overrides={"base_url": DEFAULT_BASE_URL}, api_key=key)   # verifies the key over the network


def _sdk_graphql(api, query: str, variables: Mapping) -> Mapping:
    # wandb 0.31.0 has no Api.client and no wandb_gql: GraphQL goes through wandb-core (apis/public/service_api.py:135)
    return api._service_api.execute_graphql(query, variables=dict(variables))


def _sdk_feature_enabled(api, name: str) -> bool:
    # the same check EvalTable.bind_to_run makes (eval_table.py:202-209); False on a timeout or an error
    return api._service_api.feature_enabled(name) is True


def _sdk_run_summary(api, run_path: str) -> Mapping:
    # summary_metrics is a plain dict; Run.summary nests HTTP SummarySubDict objects, which are not Mappings
    return dict(api.run(run_path).summary_metrics)


def _sdk_artifact_exists(api, ref: str) -> bool:
    try:
        return api.artifact(ref) is not None
    except Exception:                             # noqa: BLE001 - the SDK raises its own CommError for "not found"
        return False


class WandbSdkSink:                               # ------------------------------------------------ the sink
    adapter = ADAPTER

    def __init__(self, manifest: dict, *, environ: MutableMapping[str, str], paths: StatePaths) -> None:
        name, value = _read_key(manifest, environ)               # ExportError when missing or empty
        self._key, self.key_facts = value, key_facts(name, value)
        self.manifest, self.paths, self.campaign = manifest, paths, manifest["campaign"]
        self._backends: dict[str, str] = {}
        self._refuse_env(environ)

    def __repr__(self) -> str:                    # never the key
        return f"WandbSdkSink(campaign={self.campaign!r})"

    @staticmethod
    def _refuse_env(environ: Mapping[str, str]) -> None:
        for name in REFUSED_ENV:                  # presence only; a value is never read
            if name in environ or name in os.environ:
                raise ExportError(f"{name} is set: the W&B adapter refuses to run with it")

    def _refuse_process_env(self) -> None:
        """LD9: no WANDB_* or CES_* name but the adapter's own; WANDB_CONFIG_DIR, when set, under STATE and without a
        settings file; no workspace settings file (<cwd>/wandb/settings or <cwd>/.wandb/settings, read before env)."""
        allowed = set(OWN_ENV) | (set(_SDK_SET_ENV) if "wandb" in sys.modules else set())
        for name in sorted(os.environ):           # presence only; a value is never read or shown
            if name.startswith(("WANDB_", "CES_")) and name not in allowed:
                raise ExportError(f"{name} is set: the W&B adapter refuses to run with it")
        cfg = os.environ.get("WANDB_CONFIG_DIR")
        if cfg is not None:
            root = Path(self.paths.root).resolve()
            if root not in Path(cfg).resolve().parents or (Path(cfg) / "settings").exists():
                raise ExportError("WANDB_CONFIG_DIR is outside the state directory or holds a settings file")
        cwd = Path.cwd()
        if (cwd / "wandb" / "settings").exists() or (cwd / ".wandb" / "settings").exists():
            raise ExportError("the working directory holds a wandb settings file, which wandb would read")

    def _prepare_process(self) -> None:           # everything that must hold before the first SDK import
        self._refuse_env({})
        self._refuse_process_env()
        if importlib.util.find_spec("weave") is not None:
            raise AdapterUnavailable("the weave package is importable: wandb.init would trace every call a second time")
        if "WANDB_CONFIG_DIR" not in os.environ:
            cfg = config_dir(self.paths)
            cfg.mkdir(parents=True, exist_ok=True)
            os.chmod(cfg, 0o700)
            os.environ["WANDB_CONFIG_DIR"] = str(cfg)
        os.environ["WANDB_DISABLE_WEAVE"] = "true"
        netrc, local = self.paths.export_netrc, self.paths.export_wandb_local
        netrc.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(netrc, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
        os.chmod(netrc, 0o600)
        for sub in ("cache", "data"):
            (local / sub).mkdir(parents=True, exist_ok=True)
        os.environ.update({"NETRC": str(netrc), "WANDB_DIR": str(local), "WANDB_CACHE_DIR": str(local / "cache"),
                           "WANDB_DATA_DIR": str(local / "data"), "WANDB_CONSOLE": "off", "WANDB_DISABLE_GIT": "true"})

    def _wandb(self):
        self._prepare_process()                   # always before the import
        return _import_wandb()

    def _api(self):
        return _sdk_api(self._wandb(), self._key)

    def feature_ces(self) -> bool:
        """The CES probe, tried up to three times: it also answers false on a timeout."""
        api = self._api()
        for _ in range(CES_PROBES):
            try:
                if any(_sdk_feature_enabled(api, name) for name in CES_FEATURES):
                    return True
            except Exception:                     # noqa: BLE001 - a failed probe counts as false and is retried
                continue
        return False

    def project_access(self, entity: str, project: str) -> dict | None:
        """{"access", "read_only", "can_write"}; None when the project does not exist. Reads only; creates nothing."""
        data = _sdk_graphql(self._api(), PROJECT_QUERY, {"name": project, "entity": entity})
        if not isinstance(proj := (data or {}).get("project"), Mapping):
            return None
        perms = proj.get("weavePermissions") if isinstance(proj.get("weavePermissions"), Mapping) else {}
        return {"access": proj.get("access"), "read_only": proj.get("readOnly"), "can_write": perms.get("canWrite")}

    @staticmethod
    def _file(files_root: Path, source: str) -> Path:
        path = (root := Path(files_root).resolve()) / source
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise ExportError(f"artifact file {source[:80]!r} is not a regular file under the state directory")
        return path

    def log_run(self, run_plan: Mapping, table_plans: Sequence[Mapping], files_root: Path) -> dict:
        wandb, run_doc, problems, keys, backend = self._wandb(), run_plan["run"], [], [], None
        files = {f["source"]: self._file(files_root, f["source"]) for art in run_doc.get("artifacts") or ()
                 if art.get("op") == "log" for f in art.get("files") or ()}       # refused before any SDK call
        run = _sdk_init(wandb, run_doc, _sdk_settings(wandb, self._key), self.paths.export_wandb_local)
        try:
            for name, step in run_doc.get("define_metrics") or ():
                _sdk_define_metric(run, name, step)
            for row in run_doc.get("history") or ():
                _sdk_log(run, row)
            for art in run_doc.get("artifacts") or ():
                if art.get("op") == "log":
                    artifact = _sdk_artifact(wandb, art)
                    for f in art.get("files") or ():
                        _sdk_add_file(artifact, files[f["source"]], f["name"])
                    _sdk_log_artifact(run, artifact, art.get("aliases") or ())
                else:
                    try:
                        _sdk_use_artifact(run, f"{art['name']}:{art.get('alias') or self.campaign}")
                    except Exception as exc:      # noqa: BLE001 - a failed use is a receipt problem
                        if not art.get("best_effort"):
                            raise
                        problems.append(f"use_artifact_failed:{art['name']}:{type(exc).__name__}")
            for t in table_plans:
                backend = t.get("backend")
                table = _sdk_eval_table(wandb, t) if backend == "ces" else _sdk_table(wandb, t)
                _sdk_log(run, {t["key"]: table})  # once under the plan's key
                keys.append(t["key"])
            if run_doc.get("summary"):
                _sdk_summary_update(run, run_doc["summary"])
            url, base_url = _sdk_urls(run)
        finally:
            _sdk_finish(run)
        run_path = f"{run_doc['entity']}/{run_doc['project']}/{run_doc['id']}"
        if backend:
            self._backends[run_path] = backend
        return {"run_path": run_path, "url": url, "table_keys": keys, "backend": backend, "base_url": base_url,
                "problems": problems}

    def confirm(self, run_path: str, table_keys: Sequence[str], artifact_names: Sequence[str]) -> list[str]:
        """Problems; empty = the summary carries the table marker for every key and every Artifact exists."""
        api = self._api()
        try:
            summary = _sdk_run_summary(api, run_path)
        except Exception as exc:                  # noqa: BLE001 - not found yet: a problem, not a crash
            return [f"run_not_found:{type(exc).__name__}"]
        used = self._backends.get(run_path)       # unknown after a restart: either marker of a table backend
        markers = {"ces": {CES_MARKER}, "wandb_table": {TABLE_MARKER}}.get(used, {CES_MARKER, TABLE_MARKER})
        problems = [f"table_marker_missing:{k}" for k in table_keys
                    if not isinstance(summary.get(k), Mapping) or summary[k].get("_type") not in markers]
        entity_project = "/".join(str(run_path).split("/")[:2])
        return problems + [f"artifact_missing:{n}" for n in artifact_names
                           if not _sdk_artifact_exists(api, f"{entity_project}/{n}:{self.campaign}")]
