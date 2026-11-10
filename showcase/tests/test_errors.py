# SPDX-License-Identifier: MIT
import pickle
import unittest

from mhforge import errors as E


class ErrorsTest(unittest.TestCase):
    def test_exceptions_take_keyword_fields_and_one_line_message(self):
        cases = [
            (E.ManifestError, {"problems": ["a: bad", "b: worse"]}),
            (E.LiveGateError, {"blockers": [("decisions.entity", "unset")]}),
            (E.StateLockError, {"holder": {"pid": 12, "command": "search"}}),
            (E.BudgetError, {"kind": "ceiling", "phase": "search", "detail": "job_usd"}),
            (E.MeterCapExceeded, {"meter": "lens_bytes", "bucket": "main"}),
            (E.ExecutorError, {"kind": "quota", "retryable": True}),
            (E.PlanError, {"problems": ["x"]}),
            (E.SecretFound, {"findings": []}),
            (E.TranscriptError, {"kind": "version"}),
            (E.SmokeFailed, {"invariants": ["2", "6"]}),
            (E.CampaignStop, {"reason": "budget:search", "detail": "cap", "phase": "search"}),
        ]
        for cls, fields in cases:
            with self.subTest(cls=cls.__name__):
                exc = cls(**fields)
                for k, v in fields.items():
                    self.assertEqual(getattr(exc, k), v)
                self.assertEqual(len(exc.args), 1)
                self.assertIsInstance(exc.args[0], str)
                self.assertTrue(exc.args[0])
                self.assertNotIn("\n", exc.args[0])
                # an explicit message is kept, collapsed to one line
                exc2 = cls("first line\nsecond line", **fields)
                self.assertEqual(exc2.args[0], "first line second line")
                # fields survive pickling (multiprocessing, subprocess reporting)
                back = pickle.loads(pickle.dumps(exc2))
                self.assertIs(type(back), cls)
                self.assertEqual(back.args, exc2.args)
                for k, v in fields.items():
                    self.assertEqual(getattr(back, k), v)
        with self.assertRaises(TypeError):
            E.ManifestError(problem=["typo"])
        plain = E.StateError("bad file")
        self.assertEqual(plain.args, ("bad file",))
        self.assertIsInstance(plain, E.MhforgeError)
        self.assertEqual(E.ManifestError().problems, [])
        self.assertIsNot(E.ManifestError().problems, E.ManifestError().problems)
        self.assertTrue(issubclass(E.SecretFound, E.ExportError))
        self.assertTrue(issubclass(E.AdapterUnavailable, E.ExportError))

    def test_campaign_stop_is_base_exception_not_exception(self):
        self.assertTrue(issubclass(E.CampaignStop, BaseException))
        self.assertFalse(issubclass(E.CampaignStop, Exception))
        caught = None
        try:
            try:
                raise E.CampaignStop(reason="operator", detail="halt command", phase=None)
            except Exception:                       # what upstream does; must not swallow the stop
                self.fail("except Exception swallowed CampaignStop")
        except E.CampaignStop as exc:
            caught = exc
        self.assertEqual(caught.reason, "operator")
        with self.assertRaises(ValueError):
            E.CampaignStop(reason="proposer_usage_limit", detail="")
        self.assertEqual(E.exit_code_for(caught), E.EXIT_STOPPED)

    def test_reason_sets_and_exit_codes(self):
        self.assertTrue(E.HALT_REASONS <= E.STOP_REASONS)
        for reason in ("ledger_fault", "baseline_incomplete", "internal_error", "budget:calibration", "budget:controls",
                       "provider_quota"):
            self.assertIn(reason, E.STOP_REASONS)
        proposer = {"proposer_spend_limit", "proposer_limit_wait", "proposer_auth", "proposer_transient", "proposer_model",
                    "proposer_sandbox", "proposer_settings", "proposer_integrity", "proposer_cli", "proposer_environment"}
        self.assertEqual(len(proposer), 10)
        self.assertTrue(proposer <= E.STOP_REASONS)
        self.assertEqual({r for r in E.STOP_REASONS if r.startswith("proposer_")}, proposer)
        self.assertNotIn("proposer_usage_limit", E.STOP_REASONS)
        for phase in ("qualification", "calibration", "baseline", "search", "test", "controls", "recovery"):
            self.assertIn(f"budget:{phase}", E.HALT_REASONS)
        codes = [E.EXIT_OK, E.EXIT_ERROR, E.EXIT_STOPPED, E.EXIT_REFUSED, E.EXIT_LOCKED, E.EXIT_SMOKE_FAILED]
        self.assertEqual(codes, [0, 1, 2, 3, 4, 5])
        self.assertEqual(len(set(codes)), 6)
        self.assertEqual(E.exit_code_for(None), 0)
        self.assertEqual(E.exit_code_for(RuntimeError("x")), 1)
        self.assertEqual(E.exit_code_for(E.LiveGateError(blockers=[])), 3)
        self.assertEqual(E.exit_code_for(E.ManifestError(problems=[])), 3)
        self.assertEqual(E.exit_code_for(E.StateLockError(holder={})), 4)
        self.assertEqual(E.exit_code_for(E.SmokeFailed(invariants=["1"])), 5)

    def test_executor_error_kinds_and_transcript_error(self):
        kinds = {"create", "quota", "upload", "exec", "selfcheck", "credential_visible", "egress_open", "image_mismatch",
                 "auth", "env_setup", "billing", "permission"}
        self.assertEqual(E.EXECUTOR_KINDS, kinds)
        for kind in kinds:
            exc = E.ExecutorError(kind=kind)
            self.assertEqual(exc.retryable, kind not in {"image_mismatch", "auth", "billing", "permission"}, kind)
        self.assertFalse(E.ExecutorError(kind="exec", retryable=False).retryable)
        with self.assertRaises(ValueError):
            E.ExecutorError(kind="auth", retryable=True)
        with self.assertRaises(ValueError):
            E.ExecutorError(kind="explode")
        self.assertEqual(E.TranscriptError(kind="version").kind, "version")
        self.assertEqual(E.TranscriptError(kind="shape").kind, "shape")
        with self.assertRaises(ValueError):
            E.TranscriptError(kind="other")
        self.assertTrue(issubclass(E.TranscriptError, E.MhforgeError))
        for kind in ("invalid", "halted", "unreconciled", "total", "cap", "settled"):
            self.assertEqual(E.BudgetError(kind=kind).kind, kind)
        with self.assertRaises(ValueError):
            E.BudgetError(kind="ceiling")           # a ceiling names which one
        with self.assertRaises(ValueError):
            E.BudgetError(kind="nope")


if __name__ == "__main__":
    unittest.main()
