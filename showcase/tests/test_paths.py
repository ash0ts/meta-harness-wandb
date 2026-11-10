# SPDX-License-Identifier: MIT
import json
import os
import tempfile
import unittest
from pathlib import Path

from mhforge import paths
from mhforge.errors import StateError
from mhforge.paths import StatePaths
from tests import helpers


class PathsTest(unittest.TestCase):
    def test_layout_matches_spec(self):
        root = Path("/x/sc/state/mh-test")
        p = StatePaths(root)
        expected = {
            "runner_lock": "runner.lock", "manifest": "manifest.json", "manifest_sha256": "manifest.sha256",
            "engine": "engine.json", "amendments": "amendments.jsonl", "account_root": "account",
            "account_file": "account/account.json", "runtime": "runtime.json", "keydigests": "keydigests.json",
            "stopped": "stopped.json", "ledger": "ledger.sqlite", "replay": "replay.sqlite", "circuit": "circuit.json",
            "journal": "journal.jsonl", "workspace_root": "workspace", "ws": "workspace/text_classification",
            "pristine": "workspace/pristine.json", "calls_root": "calls", "jobs_root": "jobs",
            "quarantine_root": "quarantine", "scratch_root": "scratch", "qualification_root": "qualification",
            "qualification_passed": "qualification/passed.json", "calibration": "qualification/calibration.json",
            "noise": "qualification/noise.json", "export_root": "export", "export_lock": "export/export.lock",
            "export_netrc": "export/empty.netrc", "export_wandb_local": "export/wandb-local", "publish_root": "publish",
        }
        for name, rel in expected.items():
            self.assertEqual(getattr(p, name), root / rel, name)
        self.assertEqual(p.smoke_receipt, Path("/x/sc/state/_smoke/receipt.json"))
        self.assertEqual(p.logs_dir("mh-test"), root / "workspace/text_classification/logs/mh-test")
        self.assertEqual(p.calls_dir("val.x.USPTO.a1"), root / "calls/val.x.USPTO.a1")
        self.assertEqual(p.calls_dir(None), root / "calls/_unknown")
        self.assertEqual(p.job_dir("val.no_memory.USPTO"), root / "jobs/val.no_memory.USPTO")
        self.assertEqual(p.attempt_dir("val.no_memory.USPTO", 2), root / "jobs/val.no_memory.USPTO/a2")
        self.assertEqual(p.import_check_dir(3, 1), root / "import_checks/iter-003/a1")
        self.assertEqual(p.session_dir(3, 2), root / "proposer/iter-003/s2")
        self.assertEqual(p.iteration_file(0), root / "iterations/iter-000.json")
        self.assertEqual(p.iteration_file(12), root / "iterations/iter-012.json")
        self.assertEqual(p.qualification_dir(1), root / "qualification/attempt-1")
        unit = "job:val.fewshot_all.LawBench.a1"
        self.assertEqual(p.plan_dir(unit), root / "export/plans/job__val.fewshot_all.LawBench.a1")
        self.assertEqual(p.scan_file(unit), root / "export/scan/job__val.fewshot_all.LawBench.a1.json")
        self.assertEqual(p.receipt_file(unit, "agent_lens"),
                         root / "export/receipts/job__val.fewshot_all.LawBench.a1.agent_lens.json")
        self.assertEqual(p.stored_spans_file(unit), root / "export/stored-spans/job__val.fewshot_all.LawBench.a1.json")
        # path components that could escape the tree are refused
        for bad in (lambda: p.job_dir("../x"), lambda: p.calls_dir("a/b"), lambda: p.attempt_dir("k", 0),
                    lambda: p.logs_dir("../etc"), lambda: p.session_dir(-1, 1), lambda: p.plan_dir("job:../x"),
                    lambda: p.receipt_file("job:x", "a/b"), lambda: p.qualification_dir(True)):
            with self.assertRaises(ValueError):
                bad()

    def test_unit_slug(self):
        self.assertEqual(paths.unit_slug("job:val.no_memory.USPTO.a1"), "job__val.no_memory.USPTO.a1")
        self.assertEqual(paths.unit_slug("proposer:iter003.s1"), "proposer__iter003.s1")
        self.assertEqual(paths.unit_slug("eval:val.no_memory"), "eval__val.no_memory")
        self.assertEqual(paths.unit_slug("propose:mem_v2"), "propose__mem_v2")
        self.assertEqual(paths.unit_slug("loop:iter001"), "loop__iter001")
        for bad in ("job:a/b", "", ":x", "job:x y", None, 3):        # a non-string is a ValueError, not an AttributeError
            with self.assertRaises(ValueError, msg=bad):
                paths.unit_slug(bad)

    def test_resolve_state_is_beside_the_manifest_and_requires_literal_state_dir(self):
        with tempfile.TemporaryDirectory(prefix="mhf-paths-") as tmp:
            sc = Path(tmp) / "sc"
            manifest = helpers.make_manifest()
            mpath = helpers.write_manifest(sc, manifest)
            sp = paths.resolve_state(manifest, mpath)
            self.assertEqual(sp.root, mpath.resolve().parent / "state" / "mh-test")
            self.assertEqual(sp.root, sc.resolve() / "state" / "mh-test")
            # a relative manifest path resolves the same way
            cwd = os.getcwd()
            os.chdir(sc)
            try:
                self.assertEqual(paths.resolve_state(manifest, Path("manifest.json")).root, sp.root)
            finally:
                os.chdir(cwd)
            for bad in ("state/other", "./state/mh-test", "state/mh-test/", "/abs/state/mh-test", None):
                m = helpers.make_manifest(**{"state_dir": bad})
                with self.assertRaises(StateError, msg=bad):
                    paths.resolve_state(m, mpath)
            m = helpers.make_manifest(**{"campaign": "../x", "state_dir": "state/../x"})
            with self.assertRaises(StateError):
                paths.resolve_state(m, mpath)
            self.assertFalse((sc / "state").exists(), "resolve_state created a directory")

    def test_sibling_and_proposer_scratch(self):
        p = StatePaths(Path("/x/sc/state/mh-forge-01"))
        self.assertEqual(p.sibling("mh-preflight-01").root, Path("/x/sc/state/mh-preflight-01"))
        self.assertEqual(p.sibling("mh-preflight-01").ledger, Path("/x/sc/state/mh-preflight-01/ledger.sqlite"))
        with self.assertRaises(ValueError):
            p.sibling("../escape")
        self.assertEqual(paths.proposer_scratch("/private/tmp/mhf-proposer", "mh-forge-01", "iter003.s1"),
                         Path("/private/tmp/mhf-proposer/mh-forge-01/iter003.s1"))
        for campaign, key in (("../x", "iter001.s1"), ("mh-forge-01", "../iter001"), ("mh-forge-01", "a/b")):
            with self.assertRaises(ValueError):
                paths.proposer_scratch("/private/tmp/mhf-proposer", campaign, key)

    def test_helpers_tmp_state_and_manifest_fixture(self):
        # the shared fixture is the draft of 2.3 with the changes 5 lists; every owner relies on these values
        m = helpers.make_manifest()
        self.assertEqual((m["campaign"], m["state_dir"], m["campaign_kind"]), ("mh-test", "state/mh-test", "measured"))
        self.assertEqual(m["decisions"], {"live_spend_approved": True, "key_provisioning": "dedicated",
                                          "entity": "example-entity", "project": "mh-forge-test",
                                          "proposer_permission_mode": "upstream", "evaltables_backend": "ces",
                                          "publish_approved": True, "dataset_text_publication": "identifiers_only",
                                          "uspto_text_cleared": False})
        self.assertEqual(m["upstream"]["tree_sha256"], "0" * 64)
        self.assertEqual(m["upstream"]["worker_lock_sha256"], "0" * 64)
        self.assertEqual(m["sandbox"]["env_mode"], "image")
        self.assertEqual(m["sandbox"]["image"], "registry.example.com/mhforge/worker@sha256:" + "0" * 64)
        self.assertEqual((m["proposer"]["cli_path"], m["proposer"]["cli_sha256"], m["proposer"]["cli_version"]),
                         ("/nonexistent/claude", "0" * 64, "2.1.317"))
        self.assertEqual(m["proposer"]["max_budget_usd_per_session"], "25.00")
        self.assertEqual(m["budgets"]["proposer_list_usd"], "100.00")
        self.assertEqual(m["proposer"]["session_estimate_usd"], "10.00")
        self.assertEqual(set(m["proposer"]["list_prices"]), {"claude-opus-5-5", "claude-opus-4-6", "claude-haiku-5-5"})
        self.assertEqual(m["executor"]["kind"], "local_relay")
        self.assertTrue(all(rule == "hash" for rules in m["telemetry"]["content"].values() for rule in rules.values()))
        # overrides apply on a deep copy, by dotted path, double underscore or DELETE
        m2 = helpers.make_manifest({"provider.kind": "stub"}, gateway__max_in_flight=2,
                                   **{"budgets.solver_usd.phases.search": "1.00", "notes": helpers.DELETE})
        self.assertEqual((m2["provider"]["kind"], m2["gateway"]["max_in_flight"]), ("stub", 2))
        self.assertEqual(m2["budgets"]["solver_usd"]["phases"]["search"], "1.00")
        self.assertNotIn("notes", m2)
        self.assertEqual(helpers.make_manifest()["provider"]["kind"], "forge")
        # tmp_state writes the manifest into a temp SC whose state lands beside it
        with helpers.tmp_state(manifest=True) as st:
            self.assertEqual(st.campaign, "mh-test")
            self.assertTrue(st.root.is_dir())
            self.assertEqual(st.manifest_path, st.sc / "manifest.json")
            written = json.loads(st.manifest_path.read_text("utf-8"))
            self.assertTrue(os.path.isabs(written["upstream"]["example_dir"]))
            self.assertTrue(os.path.isabs(written["upstream"]["worker_python"]))
            self.assertEqual(written["upstream"]["worker_python"], str(helpers.example_python()))
            self.assertEqual(paths.resolve_state(written, st.manifest_path).root, st.root)
            sc = st.sc
        self.assertFalse(sc.exists())
        with helpers.tmp_state("mh-other", manifest=True) as st:
            self.assertEqual(st.manifest["state_dir"], "state/mh-other")
            self.assertEqual(st.root.name, "mh-other")
        m3 = helpers.make_manifest(**{"upstream.worker_python": "/usr/bin/python3"})
        with helpers.tmp_state(manifest=m3, create=False) as st:
            self.assertFalse(st.root.exists())
            self.assertEqual(json.loads(st.manifest_path.read_text())["upstream"]["worker_python"], "/usr/bin/python3")


if __name__ == "__main__":
    unittest.main()
