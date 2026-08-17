#!/usr/bin/env python3
"""验证 test_results.jsonl，并输出标准化的模型回复与比较结果。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

import path_policy


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SOURCE_FILE_NAME = "test_results.jsonl"
RESULTS_FILE_NAME = "verification_results.jsonl"
COMPARISON_MODES = {"exact", "contains", "regex"}
REPETITION_PATTERN = re.compile(r"(.{3,100}?)\1\1", re.DOTALL)


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise ValueError("message.content 包含无法识别的内容片段")
            parts.append(item["text"])
        return "".join(parts)
    raise ValueError("模型回复不是字符串")


def _choice_text(choice):
    if not isinstance(choice, dict):
        raise ValueError("choices[0] 不是 JSON 对象")
    if isinstance(choice.get("text"), str):
        return choice["text"]
    message = choice.get("message")
    if isinstance(message, dict) and "content" in message:
        return _content_text(message["content"])
    delta = choice.get("delta")
    if isinstance(delta, dict) and "content" in delta:
        return _content_text(delta["content"])
    raise ValueError("choices[0] 不包含 text、message.content 或 delta.content")


def _extract_json_output(body):
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"响应 body 不是有效 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("响应 body 顶层不是 JSON 对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("响应缺少非空 choices")
    return _choice_text(choices[0])


def _extract_stream_output(body):
    parts = []
    events = 0
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            raise ValueError(f"流式响应包含无法识别的行: {line[:200]!r}")
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"流式响应 data 不是有效 JSON: {exc}") from exc
        events += 1
        if not isinstance(event, dict):
            raise ValueError("流式响应事件顶层不是 JSON 对象")
        choices = event.get("choices")
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("流式响应 choices[0] 不是 JSON 对象")
        if isinstance(choice.get("text"), str):
            parts.append(choice["text"])
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta.get("content") is not None:
            parts.append(_content_text(delta["content"]))
    if not events:
        raise ValueError("流式响应没有可解析的 data 事件")
    return "".join(parts)


def extract_actual_output(body, stream=False):
    """从原始 OpenAI-compatible body 提取统一字符串回复。"""
    if not isinstance(body, str):
        raise ValueError("response.body 必须是字符串")
    return _extract_stream_output(body) if stream else _extract_json_output(body)


def check_output_quality(output):
    """检测高置信度乱码字符和连续三次的重复片段。"""
    issues = []
    if "\ufffd" in output or any(
        ord(char) < 32 and char not in "\n\r\t" for char in output
    ):
        issues.append("garbled_character")
    if REPETITION_PATTERN.search(output):
        issues.append("consecutive_repetition")
    return {"passed": not issues, "issues": issues}


def build_expectations(config):
    """把 test.yaml 展开为由 test/prompt 索引定位的验证规则。"""
    if not isinstance(config, dict):
        raise ValueError("test.yaml 顶层必须是 YAML 映射")
    tests = config.get("tests")
    if not isinstance(tests, list) or not tests:
        raise ValueError("test.yaml tests 必须是非空列表")

    expectations = {}
    for test_index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise ValueError(f"tests[{test_index}] 必须是 YAML 映射")
        prompts = test.get("prompts")
        if not isinstance(prompts, list) or not prompts:
            raise ValueError(f"tests[{test_index}].prompts 必须是非空列表")
        mode = test.get("comparison_mode")
        if mode not in COMPARISON_MODES:
            raise ValueError(
                f"tests[{test_index}].comparison_mode 必须是 exact、contains 或 regex"
            )
        expected = test.get("expected_output")
        if isinstance(expected, str):
            expected_values = [expected] * len(prompts)
        elif isinstance(expected, list) and all(
            isinstance(item, str) for item in expected
        ):
            if len(expected) != len(prompts):
                raise ValueError(
                    f"tests[{test_index}].expected_output 数量必须与 prompts 一致"
                )
            expected_values = expected
        else:
            raise ValueError(
                f"tests[{test_index}].expected_output 必须是字符串或字符串列表"
            )
        if mode == "regex":
            for pattern in expected_values:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(
                        f"tests[{test_index}].expected_output 不是有效正则表达式: {exc}"
                    ) from exc

        for prompt_index, expected_value in enumerate(expected_values):
            expectations[(test_index, prompt_index)] = {
                "name": test.get("name") or f"test_{test_index}",
                "stream": bool((test.get("params") or {}).get("stream", False)),
                "expected_output": expected_value,
                "comparison_mode": mode,
            }
    return expectations


def _read_result_records(source_path):
    records = {}
    with Path(source_path).open(encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, 1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"test_results.jsonl 第 {line_number} 行不是有效 JSON: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise ValueError(
                    f"test_results.jsonl 第 {line_number} 行不是 JSON 对象"
                )
            key = (record.get("test_index"), record.get("prompt_index"))
            if not all(isinstance(index, int) and not isinstance(index, bool) for index in key):
                raise ValueError(
                    f"test_results.jsonl 第 {line_number} 行缺少整数索引"
                )
            if key in records:
                raise ValueError(f"test_results.jsonl 包含重复记录: {key}")
            records[key] = record
    return records


def _compare(actual, expected, mode):
    if mode == "exact":
        return actual == expected
    if mode == "contains":
        return expected in actual
    return re.search(expected, actual) is not None


def _verify_record(source, rule):
    actual = None
    comparison_passed = False
    quality = {"passed": False, "issues": []}
    errors = []

    if not source.get("success"):
        errors.append(str(source.get("error") or "推理请求未成功"))
    else:
        response = source.get("response")
        body = response.get("body") if isinstance(response, dict) else None
        try:
            actual = extract_actual_output(body, rule["stream"])
            comparison_passed = _compare(
                actual,
                rule["expected_output"],
                rule["comparison_mode"],
            )
            quality = check_output_quality(actual)
        except ValueError as exc:
            errors.append(f"无法解析响应: {exc}")

    if actual is not None and not comparison_passed:
        errors.append(f"{rule['comparison_mode']} 比较不通过")
    if actual is not None and not quality["passed"]:
        errors.append("输出质量检查不通过: " + ", ".join(quality["issues"]))

    passed = actual is not None and comparison_passed and quality["passed"]
    return {
        "actual_output": actual,
        "expected_output": rule["expected_output"],
        "comparison_mode": rule["comparison_mode"],
        "comparison_passed": comparison_passed,
        "quality_passed": quality["passed"],
        "quality_issues": quality["issues"],
        "passed": passed,
        "error": "; ".join(errors),
    }


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


def verify_results(config, source_path, output_path):
    """验证固定结果矩阵；重复、缺失或未知索引均拒绝继续。"""
    expectations = build_expectations(config)
    sources = _read_result_records(source_path)
    unknown = set(sources) - set(expectations)
    missing = set(expectations) - set(sources)
    if unknown:
        raise ValueError(f"test_results.jsonl 包含未知记录: {sorted(unknown)}")
    if missing:
        raise ValueError(f"test_results.jsonl 缺少记录: {sorted(missing)}")

    records = []
    for key, rule in expectations.items():
        source = sources[key]
        verification = _verify_record(source, rule)
        response = source.get("response")
        records.append(
            {
                "test_index": key[0],
                "prompt_index": key[1],
                "name": rule["name"],
                **verification,
                "http_status": (
                    response.get("status") if isinstance(response, dict) else None
                ),
                "source_stage": source.get("stage"),
                "node_ref": source.get("node_ref"),
                "scope": source.get("scope"),
                "cwd": source.get("cwd"),
            }
        )

    _write_jsonl_atomically(output_path, records)
    passed = sum(record["passed"] for record in records)
    comparison_failed = sum(
        record["actual_output"] is not None
        and not record["comparison_passed"]
        for record in records
    )
    quality_failed = sum(
        record["actual_output"] is not None and not record["quality_passed"]
        for record in records
    )
    return {
        "success": passed == len(records),
        "action": "verify",
        "verified": len(records),
        "passed": passed,
        "failed": len(records) - passed,
        "comparison_failed": comparison_failed,
        "quality_failed": quality_failed,
        "source_file": str(Path(source_path).resolve(strict=False)),
        "results_file": str(Path(output_path).resolve(strict=False)),
    }


def run_project(project_root=None, config_dir=None):
    project = path_policy.get_project_root(project_root)
    resolved_config = path_policy.get_config_dir(project, explicit=config_dir)
    run_dir = path_policy.get_run_dir(project)
    source = run_dir / "logs" / SOURCE_FILE_NAME
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"找不到安全的测试结果文件: {source}")
    output = path_policy.validate_local_write(
        run_dir / "logs" / RESULTS_FILE_NAME,
        project_root=project,
    )
    config_path = resolved_config / "test.yaml"
    if not config_path.is_file():
        raise ValueError(f"找不到测试配置: {config_path}")
    with config_path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    return verify_results(config, source, output)


def main():
    parser = argparse.ArgumentParser(description="验证 test_results.jsonl 中的模型输出")
    parser.add_argument("--project-root", help="用户项目根目录")
    parser.add_argument("--config-dir", help="配置目录，默认优先项目私有配置")
    args = parser.parse_args()

    try:
        result = run_project(args.project_root, args.config_dir)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        result = {"success": False, "action": "verify", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
