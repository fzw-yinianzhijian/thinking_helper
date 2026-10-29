"""Traceable method evaluations, including real offline unittest subprocesses."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.core import Store
from app.evaluation import ROOT, get_evaluation, list_evaluations, plan_evaluation, run_evaluation


class EvaluationScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        (self.root / "app").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "app/__init__.py").write_text("")
        # The actual product contracts are executed by a separate interpreter.
        shutil.copyfile(ROOT / "app/planning.py", self.root / "app/planning.py")
        shutil.copyfile(ROOT / "tests/test_product_scenarios.py", self.root / "tests/test_product_scenarios.py")
        self.output = self.root / "data/evaluations"
        self.store = Store(Path(self.temp.name) / "workspace.sqlite3")
        self.topic = self.store.create_topic("持续思考", paused=True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def evaluate(self, **kwargs):
        return run_evaluation(self.store, topic_id=self.topic["id"], checks=["planning"],
                              project_root=self.root, output_dir=self.output, **kwargs)

    def test_real_subprocess_creates_baseline_and_audit_without_changing_strategy(self):
        before = self.store.get_topic(self.topic["id"])
        record = self.evaluate()
        self.assertEqual(record["execution_mode"], "subprocess")
        self.assertEqual(record["status"], "baseline_only")
        self.assertTrue(record["checks_ok"])
        self.assertGreaterEqual(record["results"][0]["tests_run"], 10)
        self.assertEqual(record["results"][0]["returncode"], 0)
        self.assertFalse(record["strategy_changed"])
        self.assertFalse(record["product_value_verified"])
        self.assertEqual(self.store.get_topic(self.topic["id"]), before)
        persisted = get_evaluation(record["id"], output_dir=self.output)
        self.assertEqual(persisted["event_id"], self.store.list_events(self.topic["id"])[0]["id"])
        self.assertEqual(self.store.list_events(self.topic["id"])[0]["kind"], "system.evaluated")
        self.assertEqual(list_evaluations(output_dir=self.output, topic_id=self.topic["id"])[0]["id"], record["id"])

    def test_changed_source_passes_only_listed_regressions_with_real_baseline(self):
        baseline = self.evaluate()
        (self.root / "app/change_marker.py").write_text("REPORT_LIMIT = 80\n")
        result = self.evaluate(baseline_ref=baseline["id"], rollback_ref="known-good-manual-commit",
                               change_ref="manual-report-limit-change")
        self.assertEqual(result["baseline_revision"], baseline["runtime_revision"])
        self.assertNotEqual(result["runtime_revision"], result["baseline_revision"])
        self.assertEqual(result["status"], "checks_passed")
        self.assertFalse(result["product_value_verified"])
        self.assertFalse(result["rollback_verified"])
        self.assertFalse(result["strategy_changed"])

    def test_same_revision_or_opaque_reference_cannot_prove_a_change(self):
        baseline = self.evaluate()
        unchanged = self.evaluate(baseline_ref=baseline["id"], change_ref="I say this improved")
        self.assertEqual(unchanged["status"], "baseline_only")
        opaque = self.evaluate(baseline_ref="some-git-label", change_ref="some-other-git-label")
        self.assertIsNone(opaque["baseline_revision"])
        self.assertEqual(opaque["status"], "baseline_only")

    def test_accepting_a_proposal_is_not_implementation_or_validation(self):
        self.store.resume_topic(self.topic["id"])
        self.store.enqueue_job(self.topic["id"], "selfupdate")
        job = self.store.claim_job("review-test")
        candidate = self.store.finish_job(job["id"], "review-test", {"summary": "尝试短汇报", "proposal": {"change": "short reports", "validation": "compare samples", "rollback": "restore previous limit"}})
        self.store.apply_candidate(candidate["id"])
        result = self.evaluate(candidate_id=candidate["id"])
        self.assertEqual(self.store.get_candidate(candidate["id"])["status"], "accepted")
        self.assertEqual(result["status"], "baseline_only")
        self.assertEqual(result["candidate_id"], candidate["id"])
        self.assertFalse(result["strategy_changed"])
        self.assertEqual(self.store.get_topic(self.topic["id"])["version"], 1)

    def test_real_failing_check_is_preserved_and_marked_failed(self):
        (self.root / "tests/test_product_scenarios.py").write_text(
            "import unittest\nclass BrokenRegression(unittest.TestCase):\n"
            " def test_fixture(self):\n  self.fail('intentional isolated regression fixture')\n")
        record = self.evaluate()
        self.assertEqual(record["status"], "checks_failed")
        self.assertFalse(record["checks_ok"])
        self.assertIn("intentional isolated regression", record["results"][0]["stderr"])
        self.assertEqual(get_evaluation(record["id"], output_dir=self.output)["status"], "checks_failed")

    def test_timeout_retains_record_and_does_not_claim_tests_passed(self):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial test output")
        record = self.evaluate(_runner=timeout)
        self.assertEqual(record["status"], "checks_failed")
        self.assertTrue(record["results"][0]["timed_out"])
        self.assertIsNone(record["results"][0]["returncode"])
        self.assertIn("partial test output", record["results"][0]["stdout"])
        self.assertTrue(Path(record["record_path"]).is_file())

    def test_unknown_commands_are_rejected_before_running_anything(self):
        adapter = Mock()
        with self.assertRaises(ValueError):
            run_evaluation(self.store, checks=["python -c dangerous_model_command"],
                           project_root=self.root, output_dir=self.output, _runner=adapter)
        adapter.assert_not_called()
        self.assertEqual(list_evaluations(output_dir=self.output), [])

    def test_no_tests_or_changed_source_during_check_cannot_pass(self):
        empty = self.evaluate(_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", "Ran 0 tests in 0.0s\nOK"))
        self.assertEqual(empty["status"], "checks_failed")
        def changing(command, **kwargs):
            (self.root / "app/change_during_run.py").write_text("VALUE = 2\n")
            return subprocess.CompletedProcess(command, 0, "", "Ran 10 tests in 0.01s\nOK")
        changed = self.evaluate(_runner=changing)
        self.assertTrue(changed["source_changed_during_run"])
        self.assertEqual(changed["status"], "checks_failed")

    def test_fixed_plan_is_dry_and_adapter_gets_isolated_database_and_credentials(self):
        plan = plan_evaluation(checks=["planning"], project_root=self.root)
        self.assertFalse(plan["executed"])
        self.assertIn("test_product_scenarios.py", plan["commands"][0]["command"])
        self.assertFalse(self.output.exists())
        seen = {}
        def adapter(command, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(command, 0, "", "Ran 10 tests in 0.01s\nOK")
        record = self.evaluate(_runner=adapter)
        self.assertNotEqual(seen["env"]["TH_DATABASE"], self.store.path)
        self.assertEqual(seen["env"]["DASHSCOPE_API_KEY"], "evaluation-offline-no-credential")
        self.assertEqual(record["execution_mode"], "test_adapter")
        self.assertEqual(record["status"], "baseline_only")
        all_plan = plan_evaluation(checks=["all"], project_root=self.root)
        self.assertNotIn("-p", all_plan["commands"][0]["command"])


if __name__ == "__main__":
    unittest.main()
