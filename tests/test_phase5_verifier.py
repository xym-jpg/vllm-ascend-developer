import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "phase5"


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "verifier_under_test",
        SCRIPTS / "verifier.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


class DeterministicVerifierTests(unittest.TestCase):
    def test_extracts_completion_chat_and_stream_outputs(self):
        verifier = _load_verifier()

        self.assertEqual(
            verifier.extract_actual_output(
                '{"choices":[{"text":"answer"}]}', False
            ),
            "answer",
        )
        self.assertEqual(
            verifier.extract_actual_output(
                '{"choices":[{"message":{"content":"chat answer"}}]}',
                False,
            ),
            "chat answer",
        )
        stream = (
            'data: {"choices":[{"delta":{"content":"value="}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"42"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        self.assertEqual(verifier.extract_actual_output(stream, True), "value=42")

    def test_exact_contains_and_regex_write_normalized_jsonl(self):
        verifier = _load_verifier()
        config = yaml.safe_load(
            (FIXTURES / "verify-cases.yaml").read_text(encoding="utf-8")
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test_results.jsonl"
            source.write_text(
                (FIXTURES / "test-results.jsonl").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            original = source.read_bytes()
            output = Path(temp_dir) / "verification_results.jsonl"
            summary = verifier.verify_results(config, source, output)
            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(source.read_bytes(), original)

        self.assertTrue(summary["success"])
        self.assertEqual(summary["verified"], 3)
        self.assertEqual(summary["passed"], 3)
        self.assertEqual(
            [item["actual_output"] for item in records],
            ["answer", "这里包含关键结果。", "value=42"],
        )
        self.assertEqual(
            [item["comparison_mode"] for item in records],
            ["exact", "contains", "regex"],
        )
        self.assertTrue(all(item["comparison_passed"] for item in records))
        self.assertTrue(all(item["quality_passed"] for item in records))
        self.assertTrue(all(item["passed"] for item in records))

    def test_transport_or_parse_failure_becomes_verification_failure(self):
        verifier = _load_verifier()
        config = {
            "tests": [
                {
                    "name": "broken",
                    "prompts": ["a", "b"],
                    "expected_output": "ok",
                    "comparison_mode": "exact",
                }
            ]
        }
        source_records = [
            {
                "test_index": 0,
                "prompt_index": 0,
                "success": False,
                "stage": "request",
                "error": "HTTP 500",
                "response": {"status": 500, "body": "server error"},
            },
            {
                "test_index": 0,
                "prompt_index": 1,
                "success": True,
                "stage": "complete",
                "response": {"status": 200, "body": "not-json"},
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test_results.jsonl"
            source.write_text(
                "".join(json.dumps(item) + "\n" for item in source_records),
                encoding="utf-8",
            )
            output = Path(temp_dir) / "verification_results.jsonl"
            summary = verifier.verify_results(config, source, output)
            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(summary["success"])
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(records[0]["error"], "HTTP 500")
        self.assertIn("无法解析响应", records[1]["error"])
        self.assertIsNone(records[0]["actual_output"])
        self.assertIsNone(records[1]["actual_output"])

    def test_detects_strong_garbled_chars_and_consecutive_repetition(self):
        verifier = _load_verifier()

        garbled = verifier.check_output_quality("normal\ufffdtext")
        repeated = verifier.check_output_quality("prefix abcabcabc suffix")
        clean = verifier.check_output_quality("这是正常的中文回复。")

        self.assertFalse(garbled["passed"])
        self.assertIn("garbled_character", garbled["issues"])
        self.assertFalse(repeated["passed"])
        self.assertIn("consecutive_repetition", repeated["issues"])
        self.assertEqual(clean, {"passed": True, "issues": []})

    def test_expected_output_may_be_per_prompt_but_must_match_count(self):
        verifier = _load_verifier()
        config = {
            "tests": [
                {
                    "prompts": ["one", "two"],
                    "expected_output": ["A", "B"],
                    "comparison_mode": "exact",
                }
            ]
        }

        expectations = verifier.build_expectations(config)

        self.assertEqual(expectations[(0, 0)]["expected_output"], "A")
        self.assertEqual(expectations[(0, 1)]["expected_output"], "B")

        config["tests"][0]["expected_output"] = ["only-one"]
        with self.assertRaisesRegex(ValueError, "数量"):
            verifier.build_expectations(config)

    def test_unknown_mode_and_duplicate_or_missing_records_fail_closed(self):
        verifier = _load_verifier()
        invalid = {
            "tests": [
                {
                    "prompts": ["one"],
                    "expected_output": "A",
                    "comparison_mode": "prefix",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "comparison_mode"):
            verifier.build_expectations(invalid)

        config = {
            "tests": [
                {
                    "prompts": ["one", "two"],
                    "expected_output": "A",
                    "comparison_mode": "exact",
                }
            ]
        }
        duplicate = {
            "test_index": 0,
            "prompt_index": 0,
            "success": True,
            "response": {
                "status": 200,
                "body": '{"choices":[{"text":"A"}]}',
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "test_results.jsonl"
            source.write_text(
                json.dumps(duplicate) + "\n" + json.dumps(duplicate) + "\n",
                encoding="utf-8",
            )
            output = Path(temp_dir) / "verification_results.jsonl"
            with self.assertRaisesRegex(ValueError, "重复"):
                verifier.verify_results(config, source, output)

            source.write_text(json.dumps(duplicate) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "缺少"):
                verifier.verify_results(config, source, output)

    def test_cli_exit_code_tracks_verification_result(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import path_policy
        finally:
            sys.path.remove(str(SCRIPTS))

        config = yaml.safe_load(
            (FIXTURES / "verify-cases.yaml").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            workspace = path_policy.bootstrap_project(project)
            config_path = Path(workspace["config_dir"]) / "test.yaml"
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            source = Path(workspace["logs"]) / "test_results.jsonl"
            source.write_text(
                (FIXTURES / "test-results.jsonl").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(SCRIPTS / "verifier.py"),
                "--project-root",
                str(project),
            ]

            passed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            config["tests"][0]["expected_output"] = "wrong"
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            failed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )

        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertTrue(json.loads(passed.stdout)["success"])
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertFalse(json.loads(failed.stdout)["success"])

    def test_verifier_is_trusted_and_workflow_uses_fixed_command(self):
        path_policy = (SCRIPTS / "path_policy.py").read_text(encoding="utf-8")
        workflow = (ROOT / "workflows" / "precision-diagnosis.md").read_text(
            encoding="utf-8"
        )
        module = (ROOT / "modules" / "verifier.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "diagnose" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn('"verifier.py"', path_policy)
        self.assertIn("scripts/verifier.py", workflow)
        self.assertIn("scripts/verifier.py", module)
        self.assertIn("verification_results.jsonl", module)
        self.assertIn("verifier.py", skill)

        spec = importlib.util.spec_from_file_location(
            "phase5_verifier_path_policy",
            SCRIPTS / "path_policy.py",
        )
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        policy.validate_bash_command(
            (
                f'python3 "{(SCRIPTS / "verifier.py").as_posix()}" '
                f'--project-root "{ROOT.as_posix()}"'
            ),
            ROOT,
            ROOT,
        )


if __name__ == "__main__":
    unittest.main()
