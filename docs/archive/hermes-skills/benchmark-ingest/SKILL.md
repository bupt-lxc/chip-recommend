---
name: benchmark-ingest
description: 当网页快照报告芯片与模型的训练或推理实测结果，并给出足够的工作负载和硬件条件以便复现时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, benchmark, training, inference]
    related_skills: [chip-compute, deployment-ingest]
---

# 芯片 × 模型实测数据

提取训练或推理实测。理论峰值、厂商声明、社区实测必须分开标识，不能把缺少测试条件的新闻数字当成 benchmark。

## 何时使用

- 使用：页面明确给出芯片、模型、卡数和至少一个指标，并能识别场景与测试条件。
- 不使用：只有理论算力、估算值、兼容声明，或无法确认芯片/模型/卡数。

## 输入契约

任务 URL 从 `job.input.url` 读取。`evidence_scope=snapshot` 时可使用完整 `snapshot_path`；为 `diff` 时只能使用 `diff_path` 新增 `+` 行。fact 的 `source_url` 原样复制输入 URL，`evidence_location` 与 scope 一致。表头、脚注、图例与上下文必须在受信任正文中，不得凭常识补默认 batch、精度或并发。

## 负责字段

目标表 `chip_model_benchmarks`。字段分为：

- 条件：`scenario`、`task`、`hardware_config`、`framework`、`precision`、`batch_size`、`input_seq_length`、`output_seq_length`、`concurrency`。
- 推理指标：`prefill_throughput`、`decode_throughput`、`time_to_first_token_ms`、`inter_token_latency_ms`、`memory_peak_mb`、`throughput_tok_s`、`throughput_samples_s`、`tpot_ms`。
- 训练指标：`mfu_pct`、`gpu_hours`、`training_tokens_T`、`training_gpu_count`、`training_workload_type`。
- 证据：`test_date`、`notes`、`citation`、`evidence_level`、`methodology`、`reference_url`。

## 执行流程

1. 构造精确 `entity_key`：`chip_model`、`model_id`、`workload_type`、`suite_name`、`chip_count`、`precision` 六个键缺一不可；`workload_type` 只能是 `training`、`inference` 或 `quantization`。
2. 按一组完整测试条件建立记录，不能仅按“芯片 + 模型”去重。
3. 从同一行/图例提取指标和单位；`mfu_pct` 使用 0–100 百分数。
4. 为每个确定字段创建 `facts`，共享同一完整 `entity_key`，保留逐字 `evidence_text`，显式写当前 `evidence_location`、准确 `source_type`（测试套件/厂商/社区）和 `high/medium/low` 置信度。
5. 兼容/部署证据按 `{to_skill,topic,payload}` 写入 `inbox`；报告/数据集只有出现在 `input.outbound_links` 时才写入 `discovered_sources`。

## 输出契约

返回 `facts`、`inbox`、`discovered_sources`。例如：

```json
{"target_table":"chip_model_benchmarks","entity_key":{"chip_model":"H800","model_id":"deepseek-ai/DeepSeek-V3","workload_type":"inference","suite_name":"vendor-serving-2026","chip_count":"8","precision":"BF16"},"field_name":"throughput_tok_s","proposed_value":"16612","unit":"tokens/s","source_url":"https://example.com/result","evidence_text":"Throughput 16612 tokens/s","evidence_location":"snapshot","source_type":"vendor_benchmark","confidence":"medium"}
```

`reference_url` 应等于原始结果页，`citation` 保存可读出处。`discovered_sources` 格式为 `{url,description,category,vendor,role}`；finish envelope 为 `{job_id,worker_id,status,output:{facts,inbox,discovered_sources},error_summary}`。

## 拒绝与失败

- 缺芯片、模型、卡数或指标：`rejected`。
- 只有“提升 2 倍”而无基线/绝对值：不生成数值事实。
- 多配置表格无法对应具体行：拒绝，不拼接不同配置。
- 快照不可读：`failed`。

## 完成标准

记录足以回答“什么模型、什么芯片、几卡、什么精度和输入条件、测得什么指标、何时由谁测”，并可由逐字证据复核；不直接写库。
