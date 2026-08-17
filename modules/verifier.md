# verifier.md — 结果验证器模块

## 概述

使用固定脚本解析 `test_results.jsonl`，提取模型回复并执行 exact、contains、regex 比较和基础输出质量检查。

## 固定命令

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/verifier.py" --project-root "${CLAUDE_PROJECT_DIR}"
```

## 前置条件

- test-runner 已生成 `{run_dir}/logs/test_results.jsonl`
- 测试已执行，`config/test.yaml` 中每条用例已配置 `expected_output` 和 `comparison_mode`

---

## 验证步骤

## 固定行为

1. 按 `test_index` 和 `prompt_index` 将 test.yaml 与测试结果严格对应；重复、缺失或未知记录直接拒绝验证。
2. completion 提取 `choices[0].text`；chat completion 提取 `choices[0].message.content`；流式响应合并 SSE 中的 `text` 或 `delta.content`。
3. `expected_output` 可以是所有 prompts 共用的字符串，也可以是与 `prompts` 等长的字符串列表。
4. 执行 `exact`、`contains` 或 `regex`；未知模式和非法正则直接拒绝。
5. 检查 Unicode replacement character、异常控制字符和连续三次的重复片段。
6. 原始 `{run_dir}/logs/test_results.jsonl` 保持不变；标准化结果原子覆盖写入 `{run_dir}/logs/verification_results.jsonl`。

每行验证结果包含 `actual_output`、`expected_output`、`comparison_mode`、`comparison_passed`、`quality_passed`、`quality_issues`、`passed` 和错误信息。因此模型生成的回复可以直接从 `actual_output` 查看，同时原始 HTTP body 仍保留在源文件中。

- 命令 `success: true`：所有请求均完成解析、比较通过且质量检查通过。
- 命令 `success: false`：查看 `results_file` 中失败记录，然后进入日志分析；不要让 Agent 手工重做比较或再次发送请求。

---

## 验证结果示例

```json
{"test_index": 0, "prompt_index": 0, "actual_output": "A", "expected_output": "B", "comparison_mode": "exact", "comparison_passed": false, "quality_passed": true, "quality_issues": [], "passed": false, "error": "exact 比较不通过"}
```

---

## 常见问题

1. **输出为空** — 空字符串仍是有效回复，可以按配置参与比较；解析失败时 `actual_output` 为 null。
2. **输出包含额外字符** — 明确需要子串语义时使用 `contains`。
3. **随机性输出** — 固定 seed 或使用符合需求的 regex；脚本不会擅自改变比较模式。
