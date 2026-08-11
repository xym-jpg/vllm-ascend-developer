#!/usr/bin/env python3
"""从 test.yaml 在目标容器中执行全部 HTTP 测试并写入本地 JSONL。"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

import path_policy
import ssh_utils


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


DEFAULT_TIMEOUT = 300
RESULTS_FILE_NAME = "test_results.jsonl"

REMOTE_HTTP_CODE = r'''
import json
import sys
import time
import urllib.error
import urllib.request

spec = json.loads(sys.argv[1])
timeout = spec["timeout"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def send(url, method, payload=None):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "status": response.status,
                "body": body,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": exc.code,
            "body": body,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": "HTTPError: %s" % exc,
        }
    except Exception as exc:
        return {
            "status": None,
            "body": "",
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


health = send(spec["health_url"], "GET")
if health["status"] != 200:
    result = {
        "success": False,
        "stage": "health",
        "health": health,
        "response": None,
        "error": health.get("error") or "健康检查 HTTP 状态不是 200",
    }
else:
    response = send(spec["endpoint"], "POST", spec["payload"])
    success = response["status"] is not None and 200 <= response["status"] < 300
    result = {
        "success": success,
        "stage": "complete" if success else "request",
        "health": health,
        "response": response,
        "error": "" if success else (
            response.get("error") or "推理请求 HTTP 状态不是 2xx"
        ),
    }

print(json.dumps(result, ensure_ascii=False))
'''.strip()


def build_payload(endpoint, params, prompt):
    """复制全部参数，并按 OpenAI endpoint 注入当前 prompt。"""
    if not isinstance(params, dict):
        raise ValueError("test.yaml 的 params 必须是 YAML 映射")
    payload = dict(params)
    path = urlsplit(endpoint).path.rstrip("/")
    if path.endswith("/chat/completions"):
        payload.pop("prompt", None)
        payload["messages"] = [{"role": "user", "content": prompt}]
    else:
        payload.pop("messages", None)
        payload["prompt"] = prompt
    return payload


def health_url_for(endpoint):
    """从推理 endpoint 保留 scheme/host，得到严格健康检查地址。"""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"test.yaml endpoint 不是有效 HTTP URL: {endpoint!r}")
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1/models", "", ""))


def build_remote_command(container_python, request_spec):
    """构造固定远程请求命令，不依赖 curl、上传脚本或 Shell 拼接。"""
    request_json = json.dumps(request_spec, ensure_ascii=False)
    return " ".join(
        shlex.quote(part)
        for part in (
            container_python or "python3",
            "-c",
            REMOTE_HTTP_CODE,
            request_json,
        )
    )


def _positive_timeout(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} 必须是正数")
    return value


def expand_requests(config):
    """按 test.yaml 顺序展开全部 test × prompt。"""
    if not isinstance(config, dict):
        raise ValueError("test.yaml 顶层必须是 YAML 映射")
    default_timeout = _positive_timeout(
        config.get("timeout", DEFAULT_TIMEOUT),
        "test.yaml timeout",
    )
    tests = config.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("test.yaml tests 必须是非空列表")

    requests = []
    for test_index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise ValueError(f"tests[{test_index}] 必须是 YAML 映射")
        endpoint = test.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError(f"tests[{test_index}].endpoint 必须是非空字符串")
        health_url = health_url_for(endpoint)
        timeout = _positive_timeout(
            test.get("timeout", default_timeout),
            f"tests[{test_index}].timeout",
        )
        params = test.get("params", {})
        if not isinstance(params, dict):
            raise ValueError(f"tests[{test_index}].params 必须是 YAML 映射")
        prompts = test.get("prompts")
        if not isinstance(prompts, list) or not prompts:
            raise ValueError(f"tests[{test_index}].prompts 必须是非空列表")

        for prompt_index, prompt in enumerate(prompts):
            if not isinstance(prompt, str):
                raise ValueError(
                    f"tests[{test_index}].prompts[{prompt_index}] 必须是字符串"
                )
            requests.append(
                {
                    "test_index": test_index,
                    "prompt_index": prompt_index,
                    "name": test.get("name") or f"test_{test_index}",
                    "endpoint": endpoint,
                    "health_url": health_url,
                    "timeout": timeout,
                    "payload": build_payload(endpoint, params, prompt),
                }
            )
    return requests


def _parse_remote_result(response):
    if not response.get("success"):
        error = response.get("stderr") or "远程容器命令执行失败"
        return None, "remote", error
    raw = str(response.get("stdout") or "").strip()
    try:
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            raise ValueError("输出缺少布尔字段 success")
        if result.get("stage") not in {"health", "request", "complete"}:
            raise ValueError("输出包含未知 stage")
        return result, None, ""
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, "runner", f"无法解析远程测试输出: {exc}; 原始输出: {raw[-4000:]!r}"


def _write_jsonl_atomically(output_path, records):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_test_config(config, node_ref, output_path):
    """执行全部配置请求；健康检查失败或 runner 异常时立即停止。"""
    requests = expand_requests(config)
    node = ssh_utils.resolve_node(node_ref)
    container_python = (node.get("docker") or {}).get("container_python") or "python3"
    records = []
    request_succeeded = 0
    stopped_stage = None
    stopped_error = ""

    for item in requests:
        started_at = datetime.now(timezone.utc).isoformat()
        request_spec = {
            "health_url": item["health_url"],
            "endpoint": item["endpoint"],
            "payload": item["payload"],
            "timeout": item["timeout"],
        }
        command = build_remote_command(container_python, request_spec)
        response = dict(
            ssh_utils.docker_exec_command(
                node_ref,
                command,
                timeout=item["timeout"] + 30,
            )
        )
        remote_result, parse_stage, parse_error = _parse_remote_result(response)
        stage = parse_stage or remote_result["stage"]
        success = bool(remote_result and remote_result["success"])
        if success:
            request_succeeded += 1
        record = {
            **item,
            "started_at": started_at,
            "success": success,
            "stage": stage,
            "health": remote_result.get("health") if remote_result else None,
            "response": remote_result.get("response") if remote_result else None,
            "error": parse_error or remote_result.get("error", ""),
            "node_ref": response.get("node_ref", node_ref),
            "scope": response.get("scope", "container"),
            "cwd": response.get("cwd"),
            "exit_code": response.get("exit_code"),
        }
        records.append(record)

        if parse_stage or stage == "health":
            stopped_stage = stage
            stopped_error = record["error"]
            break

    _write_jsonl_atomically(output_path, records)
    success = len(records) == len(requests) and request_succeeded == len(requests)
    request_failed = len(records) - request_succeeded
    return {
        "success": success,
        "action": "test",
        "node_ref": node_ref,
        "scheduled": len(requests),
        "executed": len(records),
        "request_succeeded": request_succeeded,
        "request_failed": request_failed,
        "stopped_stage": stopped_stage,
        "error": stopped_error or (
            f"{request_failed} 个推理请求失败；详见 results_file"
            if request_failed
            else ""
        ),
        "results_file": str(Path(output_path).resolve(strict=False)),
    }


def run_project(node_ref, project_root=None, config_dir=None):
    project = path_policy.get_project_root(project_root)
    resolved_config = path_policy.get_config_dir(project, explicit=config_dir)
    run_dir = path_policy.get_run_dir(project)
    output = path_policy.validate_local_write(
        run_dir / "logs" / RESULTS_FILE_NAME,
        project_root=project,
    )
    test_config_path = resolved_config / "test.yaml"
    if not test_config_path.is_file():
        raise ValueError(f"找不到测试配置: {test_config_path}")
    with test_config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    os.environ["VLLM_ASCEND_PROJECT_ROOT"] = str(project)
    os.environ["VLLM_ASCEND_CONFIG_DIR"] = str(resolved_config)
    return run_test_config(config, node_ref, output)


def main():
    parser = argparse.ArgumentParser(description="在目标容器执行 test.yaml 全部 HTTP 测试")
    parser.add_argument("--project-root", help="用户项目根目录")
    parser.add_argument("--config-dir", help="配置目录，默认优先项目私有配置")
    parser.add_argument(
        "node_ref",
        help="请求发起节点: standalone, pd-separated.p[0], pd-separated.d[0]",
    )
    args = parser.parse_args()

    try:
        result = run_project(args.node_ref, args.project_root, args.config_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        result = {
            "success": False,
            "action": "test",
            "node_ref": args.node_ref,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
