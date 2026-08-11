import importlib.util
import json
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "phase5"


def _load_test_runner(fake_ssh_utils):
    spec = importlib.util.spec_from_file_location(
        "test_runner_under_test",
        SCRIPTS / "test_runner.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"ssh_utils": fake_ssh_utils}):
        sys.path.insert(0, str(SCRIPTS))
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path.remove(str(SCRIPTS))
    return module


def _outer_response(remote_result):
    return {
        "success": True,
        "stdout": json.dumps(remote_result, ensure_ascii=False) + "\n",
        "stderr": "",
        "exit_code": 0,
        "node_ref": "standalone",
        "scope": "container",
        "cwd": "/workspace",
    }


class CoreTestRunnerTests(unittest.TestCase):
    def setUp(self):
        self.ssh_utils = types.ModuleType("ssh_utils")

    def _module(self):
        return _load_test_runner(self.ssh_utils)

    def test_payload_preserves_all_params_and_maps_endpoint_shape(self):
        runner = self._module()
        params = {
            "model": "demo-model",
            "max_tokens": 16,
            "temperature": 0,
            "top_p": 0.8,
            "top_k": 10,
            "repetition_penalty": 1.05,
            "stream": True,
            "seed": 123,
            "custom_option": "kept",
        }

        completion = runner.build_payload(
            "http://host:8088/v1/completions", params, "hello"
        )
        chat = runner.build_payload(
            "http://host:8088/v1/chat/completions", params, "hello"
        )

        self.assertEqual(completion["prompt"], "hello")
        self.assertNotIn("messages", completion)
        self.assertEqual(chat["messages"], [{"role": "user", "content": "hello"}])
        self.assertNotIn("prompt", chat)
        for key, value in params.items():
            self.assertEqual(completion[key], value)
            self.assertEqual(chat[key], value)

    def test_remote_command_uses_configured_python_and_fixed_http_probe(self):
        runner = self._module()
        request = {
            "health_url": "http://host:8088/v1/models",
            "endpoint": "http://host:8088/v1/completions",
            "payload": {"model": "demo", "prompt": "hello"},
            "timeout": 30,
        }

        command = runner.build_remote_command("/usr/bin/python3", request)
        tokens = shlex.split(command)

        self.assertEqual(tokens[0], "/usr/bin/python3")
        self.assertEqual(tokens[1], "-c")
        self.assertIn("ProxyHandler({})", tokens[2])
        self.assertIn("/v1/models", tokens[3])
        self.assertEqual(json.loads(tokens[3]), request)
        self.assertNotIn("curl", command)

    def test_runs_every_test_and_prompt_and_atomically_writes_jsonl(self):
        runner = self._module()
        config = yaml.safe_load(
            (FIXTURES / "test-matrix.yaml").read_text(encoding="utf-8")
        )
        remote_success = json.loads(
            (FIXTURES / "remote-success.json").read_text(encoding="utf-8")
        )
        self.ssh_utils.resolve_node = Mock(
            return_value={"docker": {"container_python": "/usr/bin/python3"}}
        )
        self.ssh_utils.docker_exec_command = Mock(
            side_effect=[_outer_response(remote_success) for _ in range(3)]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "test_results.jsonl"
            summary = runner.run_test_config(config, "standalone", output)
            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary["success"])
        self.assertEqual(summary["scheduled"], 3)
        self.assertEqual(summary["executed"], 3)
        self.assertEqual(summary["request_succeeded"], 3)
        self.assertEqual(len(records), 3)
        self.assertEqual(
            [(item["test_index"], item["prompt_index"]) for item in records],
            [(0, 0), (0, 1), (1, 0)],
        )
        self.assertEqual(records[0]["payload"]["top_p"], 0.9)
        self.assertEqual(
            records[2]["payload"]["messages"],
            [{"role": "user", "content": "gamma"}],
        )
        self.assertEqual(records[0]["scope"], "container")
        self.assertEqual(records[0]["cwd"], "/workspace")
        self.assertFalse(output.with_suffix(".jsonl.tmp").exists())
        self.assertEqual(self.ssh_utils.docker_exec_command.call_count, 3)

    def test_health_failure_stops_before_later_inference_requests(self):
        runner = self._module()
        config = yaml.safe_load(
            (FIXTURES / "test-matrix.yaml").read_text(encoding="utf-8")
        )
        health_failure = json.loads(
            (FIXTURES / "remote-health-failure.json").read_text(encoding="utf-8")
        )
        self.ssh_utils.resolve_node = Mock(
            return_value={"docker": {"container_python": "python3"}}
        )
        self.ssh_utils.docker_exec_command = Mock(
            return_value=_outer_response(health_failure)
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "test_results.jsonl"
            summary = runner.run_test_config(config, "standalone", output)
            records = output.read_text(encoding="utf-8").splitlines()

        self.assertFalse(summary["success"])
        self.assertEqual(summary["stopped_stage"], "health")
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(len(records), 1)
        self.ssh_utils.docker_exec_command.assert_called_once()

    def test_malformed_remote_output_fails_without_retry(self):
        runner = self._module()
        config = {
            "tests": [
                {
                    "name": "one",
                    "endpoint": "http://host:8088/v1/completions",
                    "params": {"model": "demo"},
                    "prompts": ["hello"],
                }
            ]
        }
        self.ssh_utils.resolve_node = Mock(
            return_value={"docker": {"container_python": "python3"}}
        )
        self.ssh_utils.docker_exec_command = Mock(
            return_value={
                "success": True,
                "stdout": "not-json\n",
                "stderr": "",
                "exit_code": 0,
                "node_ref": "standalone",
                "scope": "container",
                "cwd": "/workspace",
            }
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "test_results.jsonl"
            summary = runner.run_test_config(config, "standalone", output)

        self.assertFalse(summary["success"])
        self.assertEqual(summary["stopped_stage"], "runner")
        self.assertIn("无法解析远程测试输出", summary["error"])
        self.ssh_utils.docker_exec_command.assert_called_once()

    def test_runner_is_trusted_and_canonical_workflow_uses_it(self):
        path_policy = (SCRIPTS / "path_policy.py").read_text(encoding="utf-8")
        workflow = (ROOT / "workflows" / "precision-diagnosis.md").read_text(
            encoding="utf-8"
        )
        module = (ROOT / "modules" / "test-runner.md").read_text(encoding="utf-8")
        verifier = (ROOT / "modules" / "verifier.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "diagnose" / "SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn('"test_runner.py"', path_policy)
        self.assertIn("scripts/test_runner.py", workflow)
        self.assertIn("scripts/test_runner.py", module)
        self.assertIn("test_results.jsonl", module)
        self.assertIn("test_results.jsonl", verifier)
        self.assertNotIn("docker-exec standalone", verifier)
        self.assertIn("test_runner.py", skill)

        spec = importlib.util.spec_from_file_location(
            "phase5_path_policy",
            SCRIPTS / "path_policy.py",
        )
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        policy.validate_bash_command(
            (
                f'python3 "{(SCRIPTS / "test_runner.py").as_posix()}" '
                f'--project-root "{ROOT.as_posix()}" standalone'
            ),
            ROOT,
            ROOT,
        )


if __name__ == "__main__":
    unittest.main()
