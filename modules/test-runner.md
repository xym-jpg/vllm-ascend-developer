# test-runner.md — 测试执行器模块

## 概述

读取 `config/test.yaml` 中的测试用例列表，逐条发送推理请求，输出结果用于后续验证。

## 使用方式

```
在当前会话中，确认服务已启动后，执行测试。
```

## 前置条件

- vLLM 服务已成功启动并通过健康检查
- 已正确配置 `config/test.yaml`（测试用例列表）
- 远程操作通过 `scripts/ssh_utils.py` 执行

---

## 执行步骤

### 步骤 1：执行固定测试命令

不要让 Agent 逐条拼接 curl、上传脚本或自行选择结果路径。单机模式直接执行：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/test_runner.py" --project-root "${CLAUDE_PROJECT_DIR}" standalone
```

PD 分离模式使用可访问 `test.yaml` endpoint 的已配置节点；默认从第一个 P 节点发起：

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/test_runner.py" --project-root "${CLAUDE_PROJECT_DIR}" pd-separated.p[0]
```

`test_runner.py` 固定完成以下操作：

1. 按顺序遍历全部 `tests[]` 和每条用例的全部 `prompts[]`。
2. 保留 `params` 中的全部请求参数；completion 使用 `prompt`，chat completion 使用 `messages`。
3. 每次推理请求前，在同一目标容器中请求对应服务的 `/v1/models`，仅 HTTP 200 才继续。
4. 禁用 HTTP 代理，直接发送请求；不生成、不上传远程 curl 脚本。
5. 将本次结果原子覆盖写入本地 `{run_dir}/logs/test_results.jsonl`。

### 步骤 2：配置与结果字段

从 `config/test.yaml` 读取 `tests[]` 列表，每条用例包含：

| 字段 | 说明 |
|------|------|
| `name` | 测试名称 |
| `endpoint` | 请求端点 URL |
| `params` | 请求参数（max_tokens, temperature, top_p, stream 等） |
| `prompts` | 测试 prompt 列表（每条 prompt 独立发起一次请求） |
| `expected_output` | 预期输出 |
| `comparison_mode` | 比较方式：exact / contains / regex（由后续 verifier 使用） |

每个 JSONL 记录包含用例和 prompt 索引、完整 payload、健康检查结果、HTTP 响应正文、执行节点、`scope`、`cwd` 和错误信息。命令摘要中的 `success` 只表示健康检查及 HTTP 请求均成功，不代表模型输出已经通过精度验证。

以下情况不允许 Agent 改写命令或读取工具源码猜测：

- 健康检查失败：立即停止，不发送后续推理请求。
- 远程输出无法解析：立即停止并报告原始错误，不重试。
- 单条推理返回非 2xx：记录失败，继续执行剩余用例，最后返回失败摘要。

### 手工兼容入口

`scripts/generate_curl.py` 暂时保留，供开发者明确要求生成单条 curl 脚本时使用；它不再是精度诊断主流程的执行入口。

---

## 配置一致性检查清单

- [ ] `test.yaml` 中的 `endpoint` 端口与 `service.yaml` 中对应模式的 `service_port` 一致
- [ ] 端点中的模型名称与 `aisbench.yaml` 中的 `model_name` 一致
- [ ] PD分离模式下确认请求发往正确的节点（P 或 D）
- [ ] 命令返回的 `results_file` 位于 `{run_dir}/logs/test_results.jsonl`
