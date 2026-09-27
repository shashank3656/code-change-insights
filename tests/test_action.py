"""Cross-repository action boundaries; no external services are called."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from change_intelligence.__main__ import ingestion_range, store_from_env
from change_intelligence.store import GitHubStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_action.py"
spec = importlib.util.spec_from_file_location("insights_action", SCRIPT)
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)


class SharedActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runner_temp = self.root / "runner"
        self.runner_temp.mkdir()
        self.output_file = self.root / "outputs"
        self.env = {
            "GITHUB_WORKSPACE": str(self.workspace), "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_OUTPUT": str(self.output_file), "GITHUB_REPOSITORY": "customer/application",
            "GITHUB_TOKEN": "fake-caller-token", "GITHUB_REF": "refs/heads/main",
            "GITHUB_API_URL": "https://api.github.com", "INSIGHTS_OPERATION": "ingest",
            "INSIGHTS_SOURCE_DIRECTORY": ".", "INSIGHTS_SUBMIT": "false",
            "CHANGE_DEFAULT_BRANCH": "main",
        }

    def report(self, filename, value):
        (Path(os.environ["CHANGE_OUTPUT_DIR"]) / filename).write_text(json.dumps(value))

    def test_ingestion_uses_caller_repo_and_fresh_output_directory(self):
        (self.workspace / "application").mkdir()
        self.env["INSIGHTS_SOURCE_DIRECTORY"] = "application"

        def command(args):
            self.assertEqual(args, ["ingest"])
            self.assertEqual(Path.cwd().resolve(), (self.workspace / "application").resolve())
            store = store_from_env()
            self.assertEqual(store.root, "https://api.github.com/repos/customer/application")
            self.assertEqual(store.headers["Authorization"], "Bearer fake-caller-token")
            self.assertTrue(Path(os.environ["CHANGE_OUTPUT_DIR"]).is_relative_to(self.runner_temp))
            self.report("ingestion.json", [{"sha": "a" * 40, "status": "completed"}])
            return 0

        cwd = Path.cwd()
        with patch.dict(os.environ, self.env), patch.object(action, "run_command", side_effect=command):
            self.assertEqual(action.run(), 0)
        self.assertEqual(Path.cwd(), cwd)
        output = self.output_file.read_text()
        self.assertIn("artifact-directory<<", output)
        self.assertIn("assessment-count<<", output)

    def test_preview_does_not_submit_or_require_setuops_credentials(self):
        self.env["INSIGHTS_OPERATION"] = "change-request"

        def command(args):
            self.assertEqual(args, ["prepare"])
            self.report("change-request.json", {"risk_level": "high"})
            return 0

        with patch.dict(os.environ, self.env), patch.object(action, "run_command", side_effect=command) as run:
            self.assertEqual(action.run(), 0)
            run.assert_called_once()
        self.assertNotIn("change-number<<", self.output_file.read_text())

    def test_submit_follows_prepare_and_exports_change_identity(self):
        self.env.update(INSIGHTS_OPERATION="change-request", INSIGHTS_SUBMIT="true")

        def command(args):
            if args == ["prepare"]:
                self.report("change-request.json", {"risk_level": "medium"})
            else:
                self.assertEqual(args, ["submit"])
                self.report("setuops-response.json", {"id": 42, "change_number": "CHG0000042"})
            return 0

        with patch.dict(os.environ, self.env), patch.object(action, "run_command", side_effect=command) as run:
            self.assertEqual(action.run(), 0)
            self.assertEqual([call.args[0] for call in run.call_args_list], [["prepare"], ["submit"]])
        self.assertIn("CHG0000042", self.output_file.read_text())

    def test_failed_prepare_cannot_submit(self):
        self.env.update(INSIGHTS_OPERATION="change-request", INSIGHTS_SUBMIT="true")
        with patch.dict(os.environ, self.env), patch.object(action, "run_command", return_value=1) as run:
            self.assertEqual(action.run(), 1)
            run.assert_called_once_with(["prepare"])

    def test_invalid_modes_branches_and_source_paths_fail_before_running(self):
        cases = [
            {"INSIGHTS_OPERATION": "shell"}, {"INSIGHTS_SUBMIT": "yes"},
            {"INSIGHTS_SUBMIT": "true"}, {"GITHUB_REF": "refs/pull/1/merge"},
            {"INSIGHTS_SOURCE_DIRECTORY": "../runner"},
        ]
        for case in cases:
            with self.subTest(case=case), patch.dict(os.environ, {**self.env, **case}), \
                    patch.object(action, "run_command") as run:
                self.assertEqual(action.run(), 1)
                run.assert_not_called()

    def test_isolated_bootstrap_cannot_import_application_modules(self):
        marker = self.workspace / "executed"
        # Both stdlib and application-package shadowing must be ineffective.
        malicious = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        (self.workspace / "json.py").write_text(malicious)
        package = self.workspace / "change_intelligence"
        package.mkdir()
        (package / "__init__.py").write_text(malicious)
        env = {**os.environ, **self.env, "PYTHONPATH": str(self.workspace), "INSIGHTS_OPERATION": "invalid"}
        result = subprocess.run([sys.executable, "-I", str(SCRIPT)], cwd=self.workspace,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("operation must be ingest or change-request", result.stderr)
        self.assertFalse(marker.exists())

    def test_multiple_callers_have_separate_storage_roots(self):
        roots = []
        for repository in ("customer/app-one", "customer/app-two"):
            with patch.dict(os.environ, {**self.env, "GITHUB_REPOSITORY": repository}):
                roots.append(store_from_env().root)
        self.assertEqual(roots, ["https://api.github.com/repos/customer/app-one", "https://api.github.com/repos/customer/app-two"])

    def test_custom_default_branch_in_event_and_pr_metadata(self):
        path = self.root / "event.json"
        path.write_text(json.dumps({"ref": "refs/heads/master", "before": "a" * 40, "after": "b" * 40}))
        with patch.dict(os.environ, {**self.env, "CHANGE_DEFAULT_BRANCH": "master",
                                    "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(path)}):
            self.assertEqual(ingestion_range(), ("a" * 40, "b" * 40))
            store = store_from_env()
            self.assertEqual(store.default_branch, "master")
        pr = {"number": 1, "html_url": "https://github.com/customer/app/pull/1", "title": "Update",
              "merged_at": "2026-01-01", "base": {"ref": "master"}}
        with patch.object(GitHubStore, "call", return_value=[pr]):
            self.assertEqual(len(store.pull_requests("b" * 40)), 1)


if __name__ == "__main__":
    unittest.main()
