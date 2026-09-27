import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from change_intelligence import analysis, common
from change_intelligence.__main__ import ingest, ingestion_range
from change_intelligence.analysis import Analyzer, VERSION, risk_level
from change_intelligence.change_request import (
    assessment_path, build_payload, endpoint_url, load_release, markdown, submit,
)
from change_intelligence.common import Failure, HTTPFailure
from change_intelligence.git_source import GitSource, MAX_BATCH_BYTES, ZERO, redact
from change_intelligence.store import GitHubStore


def assessment(level="medium"):
    return {"summary": "Changes request timeout handling.", "changes": ["Retries temporary failures."],
            "risk_level": level, "risk_factors": [{"level": level, "reason": "Retries can increase load.",
                                                   "files": ["app.py"]}],
            "testing": ["Check retry limits under load."], "rollback": ["Restore the previous application version."],
            "limitations": []}


def response(value):
    return {"type": "result", "subtype": "success", "is_error": False,
            "result": json.dumps(value)}


class MemoryStore:
    repository = "example/Application_Onboarding"

    def __init__(self):
        self.records = {}

    def ensure_branch(self):
        pass

    def read(self, path):
        return copy.deepcopy(self.records.get(path)), None

    def save(self, path, record):
        self.records[path] = copy.deepcopy(record)
        return record

    def pull_requests(self, sha):
        return [{"number": 7, "title": "Update timeout", "url": "https://github.com/example/app/pull/7"}]


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.base = self.commit({"app.py": "timeout = 10\n"})
        self.source = GitSource(self.root)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL).decode().strip()

    def commit(self, files, message="Update application"):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.unlink()
            elif isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content)
        self.git("add", "--all")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def fake_analyzer(self):
        analyzer = Mock(model="test-model")
        analyzer.analyze.return_value = assessment()
        return analyzer

    def test_multicommit_release_uses_every_saved_assessment(self):
        first = self.commit({"app.py": "timeout = 20\n"})
        target = self.commit({"app.py": "timeout = 30\n"})
        store, ai = MemoryStore(), self.fake_analyzer()
        with patch.dict(os.environ, {"CHANGE_OUTPUT_DIR": str(self.root / "output")}):
            ingest(self.source, store, self.base, target, lambda: ai)
        self.assertEqual(ai.analyze.call_count, 2)
        records = load_release(self.source, store, self.base, target)
        self.assertEqual([r["sha"] for r in records], [first, target])
        payload = build_payload(store.repository, self.base, target, records,
                                service="app", environment="prod", title="Release 1")
        self.assertEqual(payload["previous_sha"], self.base)
        self.assertEqual(payload["sha"], target)
        self.assertEqual(payload["status"], "new")
        self.assertEqual(payload["change_type"], "normal")
        self.assertEqual(payload["pr_number"], 7)
        self.assertIn(first[:12], payload["summary"])
        self.assertIn(target[:12], payload["summary"])
        # Reusing assessments must not trigger another paid AI call.
        with patch.dict(os.environ, {"CHANGE_OUTPUT_DIR": str(self.root / "output")}):
            ingest(self.source, store, self.base, target, lambda: ai)
        self.assertEqual(ai.analyze.call_count, 2)

    def test_failed_analysis_is_saved_retried_and_blocks_request(self):
        target = self.commit({"app.py": "timeout = 30\n"})
        store, ai = MemoryStore(), self.fake_analyzer()
        ai.analyze.side_effect = Failure("AI analysis was incomplete")
        with patch.dict(os.environ, {"CHANGE_OUTPUT_DIR": str(self.root / "output")}):
            with self.assertRaisesRegex(Failure, "Ingestion failed"):
                ingest(self.source, store, self.base, target, lambda: ai)
            self.assertEqual(store.records[assessment_path(target)]["status"], "failed")
            with self.assertRaisesRegex(Failure, "Missing or failed"):
                load_release(self.source, store, self.base, target)
            ai.analyze.side_effect = None
            ingest(self.source, store, self.base, target, lambda: ai)
        self.assertEqual(len(load_release(self.source, store, self.base, target)), 1)

    def test_merge_diff_is_against_main_parent(self):
        self.git("checkout", "-b", "feature")
        feature = self.commit({"feature.py": "feature = True\n"})
        self.git("checkout", "main")
        main = self.commit({"main.py": "main = True\n"})
        self.git("merge", "--no-ff", "feature", "-m", "Merge feature")
        target = self.git("rev-parse", "HEAD")
        self.assertEqual(self.source.transitions(self.base, target), [main, target])
        self.assertNotIn(feature, self.source.transitions(self.base, target))
        self.assertEqual(self.source.evidence(target)["files"], ["feature.py"])
        self.assertEqual(self.source.parent(target), main)

    def test_initial_commit_and_deleted_files_are_analyzed(self):
        evidence = self.source.evidence(self.base)
        self.assertEqual(evidence["base_sha"], ZERO)
        self.assertIn("+timeout = 10", evidence["batches"][0][0]["patch"])
        target = self.commit({"app.py": None})
        self.assertIn("-timeout = 10", self.source.evidence(target)["batches"][0][0]["patch"])

    def test_filenames_are_literal_not_shell_or_git_pathspecs(self):
        name = "$(touch hacked)[abc].txt"
        target = self.commit({name: "literal content\n"})
        evidence = self.source.evidence(target)
        self.assertEqual(evidence["files"], [name])
        self.assertIn("+literal content", evidence["batches"][0][0]["patch"])
        self.assertFalse((self.root / "hacked").exists())

    def test_divergent_and_option_like_shas_are_rejected(self):
        self.git("checkout", "-b", "feature")
        feature = self.commit({"feature.py": "x=1"})
        self.git("checkout", "main")
        target = self.commit({"main.py": "x=2"})
        with self.assertRaisesRegex(Failure, "first-parent"):
            self.source.transitions(feature, target)
        with self.assertRaisesRegex(Failure, "full, lowercase"):
            self.source.transitions(self.base, "--output=/tmp/bad")

    def test_manual_target_must_be_on_main(self):
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.source.require_main(self.base)
        self.git("checkout", "-b", "feature")
        feature = self.commit({"feature.py": "x=1"})
        with self.assertRaisesRegex(Failure, "already be on the configured default branch"):
            self.source.require_main(feature)

    def test_missing_mismatched_empty_and_unknown_baselines(self):
        target = self.commit({"app.py": "timeout=50"})
        store = MemoryStore()
        with self.assertRaisesRegex(Failure, "Missing or failed"):
            load_release(self.source, store, self.base, target)
        with self.assertRaisesRegex(Failure, "actual previously deployed"):
            load_release(self.source, store, ZERO, target)
        with self.assertRaisesRegex(Failure, "No changes"):
            load_release(self.source, store, target, target)
        store.records[assessment_path(target)] = {"status": "completed", "sha": self.base}
        with self.assertRaisesRegex(Failure, "identity/version"):
            load_release(self.source, store, self.base, target)

    def test_binary_sensitive_truncated_and_redacted_content_is_flagged(self):
        target = self.commit({".env": "PASSWORD=should-not-leave\n", "app.py": "x" * 30_000,
                              "blob.bin": b"a\0b", "config.ini": "api_key=not-for-model\n"})
        evidence = self.source.evidence(target)
        serialized = json.dumps(evidence["batches"])
        self.assertNotIn("should-not-leave", serialized)
        self.assertNotIn("not-for-model", serialized)
        self.assertTrue(any("Binary" in s for s in evidence["limitations"]))
        self.assertTrue(any("truncated" in s for s in evidence["limitations"]))
        self.assertTrue(any("redacted" in s for s in evidence["limitations"]))
        self.assertTrue(all(len(json.dumps(b).encode()) <= MAX_BATCH_BYTES for b in evidence["batches"]))

    def test_batches_cover_all_files(self):
        target = self.commit({f"file-{i}.txt": "x" * 12_000 for i in range(8)})
        evidence = self.source.evidence(target)
        self.assertGreater(len(evidence["batches"]), 1)
        self.assertEqual(sorted(item["file"] for batch in evidence["batches"] for item in batch), evidence["files"])


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {"title": "Update", "files": ["app.py"], "limitations": [],
                         "batches": [[{"file": "app.py", "patch": "+timeout = 30"}]]}

    def cursor(self, value, returncode=0):
        def run(command, **kwargs):
            kwargs["stdout"].write(json.dumps(value).encode())
            return Mock(returncode=returncode)
        return run

    @patch("change_intelligence.analysis.subprocess.run")
    @patch("change_intelligence.analysis.shutil.which", return_value="/usr/bin/cursor-agent")
    def test_cursor_contract_secret_isolation_and_risk_floor(self, which, run):
        value = assessment("high")
        value["risk_level"] = "low"
        captured = {}

        def invoke(command, **kwargs):
            captured.update(command=command, **kwargs)
            kwargs["stdout"].write(json.dumps(response(value)).encode())
            return Mock(returncode=0)

        run.side_effect = invoke
        with patch.dict(os.environ, {"GITHUB_TOKEN": "github-secret",
                                     "CHANGE_WEBHOOK_SECRET": "setuops-secret",
                                     "AWS_SECRET_ACCESS_KEY": "cloud-secret"}):
            result = Analyzer("cursor-secret", "test-model").analyze(self.evidence)
        self.assertEqual(result["risk_level"], "high")
        self.assertEqual(captured["command"], ["/usr/bin/cursor-agent", "--print",
                                               "--output-format", "json", "--model", "test-model"])
        self.assertIn("untrusted DATA", captured["input"].decode())
        self.assertIn("+timeout = 30", captured["input"].decode())
        self.assertEqual(captured["env"]["CURSOR_API_KEY"], "cursor-secret")
        for secret in ("GITHUB_TOKEN", "CHANGE_WEBHOOK_SECRET", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(secret, captured["env"])
        self.assertTrue(Path(captured["cwd"]).name.startswith("cursor-change-analysis-"))
        self.assertEqual(captured["env"]["HOME"], captured["cwd"])

    @patch("change_intelligence.analysis.subprocess.run")
    @patch("change_intelligence.analysis.shutil.which", return_value="/usr/bin/cursor-agent")
    def test_unsuccessful_invalid_and_hallucinated_output_fails(self, which, run):
        invalid = assessment()
        invalid["risk_factors"][0]["files"] = ["invented.py"]
        for value in ({"type": "result", "subtype": "error", "is_error": True},
                      response({"summary": "missing fields"}), response(invalid),
                      response("not-json-object")):
            with self.subTest(value=value):
                run.side_effect = self.cursor(value)
                with self.assertRaises(Failure):
                    Analyzer("fake-key", "test-model").analyze(self.evidence)

    @patch("change_intelligence.analysis.subprocess.run")
    @patch("change_intelligence.analysis.shutil.which", return_value="/usr/bin/cursor-agent")
    def test_truncation_cannot_be_reported_as_low_risk(self, which, run):
        self.evidence["limitations"] = ["Patch truncated: app.py"]
        run.side_effect = self.cursor(response(assessment("low")))
        result = Analyzer("fake", "test-model").analyze(self.evidence)
        self.assertEqual(result["risk_level"], "unknown")
        self.assertIn("Patch truncated: app.py", result["limitations"])

    @patch("change_intelligence.analysis.subprocess.run")
    @patch("change_intelligence.analysis.shutil.which")
    def test_empty_commit_needs_no_remote_call(self, which, run):
        self.evidence["batches"] = []
        result = Analyzer("fake", "test-model").analyze(self.evidence)
        self.assertEqual(result["risk_level"], "low")
        which.assert_not_called()
        run.assert_not_called()

    @patch("change_intelligence.analysis.subprocess.run")
    @patch("change_intelligence.analysis.shutil.which", return_value="/usr/bin/cursor-agent")
    def test_oversized_batch_aggregate_fails_before_completion(self, which, run):
        self.evidence["batches"] *= 2
        value = assessment()
        value["summary"] = "x" * 7000
        run.side_effect = self.cursor(response(value))
        with self.assertRaisesRegex(Failure, "invalid value"):
            Analyzer("fake", "test-model").analyze(self.evidence)

    @patch("change_intelligence.analysis.subprocess.run", return_value=Mock(returncode=1))
    @patch("change_intelligence.analysis.shutil.which", return_value="/usr/bin/cursor-agent")
    def test_cursor_failure_is_safe(self, which, run):
        with self.assertRaisesRegex(Failure, "verify CURSOR_API_KEY"):
            Analyzer("fake", "test-model").analyze(self.evidence)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.store = GitHubStore("example/app", "fake-token")

    def test_concurrent_branch_initialization(self):
        self.store.call = Mock(side_effect=[HTTPFailure(404), {"sha": "tree"}, {"sha": "commit"},
                                           HTTPFailure(422), {"object": {"sha": "other-commit"}}])
        self.store.ensure_branch()
        self.assertEqual(self.store.call.call_args_list[2].args[2]["parents"], [])

    @patch("change_intelligence.store.time.sleep")
    def test_storage_conflict_retries_with_current_blob(self, sleep):
        self.store.read = Mock(side_effect=[(None, None), ({"status": "failed"}, "new-blob")])
        self.store.call = Mock(side_effect=[HTTPFailure(409), {}])
        record = {"sha": "a" * 40, "status": "completed"}
        self.assertEqual(self.store.save(assessment_path(record["sha"]), record), record)
        self.assertEqual(self.store.call.call_args.args[2]["sha"], "new-blob")

    def test_completed_record_cannot_be_downgraded_or_overwritten(self):
        completed = {"sha": "a" * 40, "status": "completed"}
        self.store.read = Mock(return_value=(completed, "blob"))
        self.store.call = Mock()
        self.assertEqual(self.store.save(assessment_path(completed["sha"]),
                                         {"sha": completed["sha"], "status": "failed"}), completed)
        self.store.call.assert_not_called()

    def test_pr_associations_paginate_and_filter_main_merges(self):
        prs = [{"number": i, "html_url": f"https://github.com/example/app/pull/{i}",
                "title": "Update", "merged_at": "2026-01-01", "base": {"ref": "main"}} for i in range(100)]
        self.store.call = Mock(side_effect=[prs, [{"merged_at": None, "base": {"ref": "main"}}]])
        self.assertEqual(len(self.store.pull_requests("a" * 40)), 100)
        self.assertIn("page=2", self.store.call.call_args.args[1])


class DeliveryTests(unittest.TestCase):
    def records(self):
        return [{"sha": "b" * 40, "title": "Application change", "files": ["app.py"],
                 "assessment": assessment("unknown"), "pull_requests": []}]

    def payload(self):
        return build_payload("example/app", "a" * 40, "b" * 40, self.records(),
                             service="app", environment="prod", title="Release")

    def test_unknown_maps_to_high_with_clear_explanation(self):
        payload = self.payload()
        self.assertEqual(payload["risk_level"], "high")
        self.assertIn("UNKNOWN", payload["summary"])
        self.assertIn("manual review", payload["summary"])
        self.assertEqual(risk_level(["low", "high", "medium"]), "high")

    @patch("change_intelligence.change_request.request_json")
    def test_submit_existing_setuops_contract_and_no_auto_retry(self, call):
        call.return_value = {"status": "ok", "id": 17, "change_number": "CHG0000017", "created": True}
        result = submit(self.payload(), "https://setuops.example/api/webhooks/change", "fake-secret")
        self.assertEqual(result["change_number"], "CHG0000017")
        self.assertEqual(call.call_args.args[1], "https://setuops.example/api/webhooks/change")
        self.assertEqual(call.call_args.args[2]["X-CI-Secret"], "fake-secret")
        self.assertEqual(call.call_args.kwargs["attempts"], 1)

    @patch("change_intelligence.change_request.request_json")
    def test_http_or_invalid_response_is_not_success(self, call):
        call.side_effect = HTTPFailure(401)
        with self.assertRaises(HTTPFailure):
            submit(self.payload(), "https://setuops.example", "fake")
        call.side_effect = None
        call.return_value = {"status": "ok"}
        with self.assertRaisesRegex(Failure, "unexpected response"):
            submit(self.payload(), "https://setuops.example", "fake")

    def test_endpoint_validation_and_markdown_safety(self):
        for value in ("http://bad.example", "https://u:p@bad.example", "https://ok.example/wrong", "https://ok.example?token=x"):
            with self.assertRaises(Failure):
                endpoint_url(value)
        self.assertEqual(endpoint_url("https://ok.example/"), "https://ok.example/api/webhooks/change")
        self.assertNotIn("![", markdown("![external](https://example.com/a) <script>"))
        self.assertNotIn("<script>", markdown("<script>"))


class TransportTests(unittest.TestCase):
    @patch("change_intelligence.common.time.sleep")
    @patch("change_intelligence.common.build_opener")
    def test_transient_errors_retry_but_credentials_are_not_printed(self, opener, sleep):
        result = Mock()
        result.__enter__ = Mock(return_value=result)
        result.__exit__ = Mock(return_value=False)
        result.read.return_value = b'{"ok": true}'
        opener.return_value.open.side_effect = [HTTPError("https://example.test", 503, "", {}, io.BytesIO(b"secret-data")), result]
        self.assertEqual(common.request_json("GET", "https://example.test", {}), {"ok": True})
        self.assertEqual(opener.return_value.open.call_count, 2)

    @patch("change_intelligence.common.build_opener")
    def test_auth_errors_are_not_retried_and_response_body_is_hidden(self, opener):
        opener.return_value.open.side_effect = HTTPError("https://example.test", 401, "", {}, io.BytesIO(b"secret-data"))
        with self.assertRaises(HTTPFailure) as caught:
            common.request_json("GET", "https://example.test", {})
        self.assertNotIn("secret-data", str(caught.exception))
        self.assertEqual(opener.return_value.open.call_count, 1)

    def test_redirects_are_refused(self):
        self.assertIsNone(common.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))


class EventTests(unittest.TestCase):
    def test_push_reads_actual_event_before_and_after(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            event = {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40}
            path.write_text(json.dumps(event))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(path)}):
                self.assertEqual(ingestion_range(), ("a" * 40, "b" * 40))
                for addition in ({"deleted": True}, {"forced": True}, {"ref": "refs/heads/feature"}):
                    path.write_text(json.dumps({**event, **addition}))
                    with self.assertRaises(Failure):
                        ingestion_range()

    def test_credential_redaction(self):
        text, changed = redact("api_key=hidden\npassword: hidden\n" + "ghp_" + "x" * 36)
        self.assertTrue(changed)
        self.assertNotIn("hidden", text)
        self.assertNotIn("ghp_", text)


if __name__ == "__main__":
    unittest.main()
