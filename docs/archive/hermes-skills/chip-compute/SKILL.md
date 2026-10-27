---
name: chip-compute
description: 当芯片网页快照包含某一加速器型号支持的数值精度、峰值算力、架构、制程或计算单元规格时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, chip, compute, precision]
    related_skills: [chip-catalog, chip-basic, benchmark-ingest]
---

# 芯片计算参数

提取芯片支持精度与理论峰值算力，并完整保留 Dense/Sparse、TFLOPS/TOPS、是否使用张量单元等口径。

## 何时使用

- 使用：`agent_extract` 任务的快照明确列出某一芯片型号的精度、峰值算力、架构、制程或计算单元。
- 不使用：实测吞吐/MFU、整机集群算力、仅宣传“高性能”、或无法确认单卡口径。实测交给 `benchmark-ingest`。

## 输入契约

从 `job.input.url` 取得任务 URL。`evidence_scope=snapshot` 时读取完整 `snapshot_path`；为 `diff` 时只读取 `diff_path` 的新增 `+` 行。fact 的 `source_url` 必须原样复制 `input.url`，`evidence_location` 必须与 scope 一致。先确认厂商、完整型号、板卡版本和性能脚注。

## 负责字段

目标表固定为 `chips`：

| 字段 | 提取规则 |
|---|---|
| `precision_support` | 完整列出 FP64 至 FP4、BF16、TF32、INT8/INT4 等明确支持的格式 |
| `precision_perf` | 严格保存为 `BF16=1980TF,FP8=3960TF,INT8=3960TOPS` 这类 `TAG=VALUE` 逗号列表；Sparse 不能覆盖 Dense，口径写入证据/说明 |
| `architecture` | 对外架构名称，如 Hopper、CDNA3、达芬奇 |
| `process_node_nm` | 明确制程数字；多 Die 制程写入证据，禁止擅自取一个 |
| `compute_units` | CUDA Core、Tensor Core、AI Core 等数量及类型，保留原文含义 |

## 执行流程

1. 锁定实体：同系列不同显存/形态若算力不同，必须使用完整 SKU。
2. 读取表头、列名和脚注，区分单卡/双卡、Dense/Sparse、峰值/实测、TFLOPS/TOPS。
3. 仅在单位和口径明确时归一化；无法可靠换算时保留原文候选或拒绝。
4. 每个字段单独写入 `facts`，以逐字 `evidence_text` 支撑值和口径。
5. 每个 fact 显式填写真实 `source_type`、`evidence_location` 和 `confidence`（`high`/`medium`/`low`）。
6. 发现基础规格、实测或兼容内容时通过 `inbox` 创建对应任务；详情页仅可从 `input.outbound_links` 中登记，禁止猜 URL。

## 输出契约

返回 `facts`、`inbox`、`discovered_sources` 三个数组。事实必须包含 `target_table: "chips"`、精确 `entity_key`、`field_name`、`proposed_value`、任务 `source_url`、逐字 `evidence_text` 和置信度。

```json
{"target_table":"chips","entity_key":{"chip_model":"MI300X","vendor":"AMD"},"field_name":"precision_perf","proposed_value":"FP16=1307TF","source_url":"https://example.com/mi300x","evidence_text":"Peak half precision 1307 TFLOPS","evidence_location":"snapshot","source_type":"official_datasheet","confidence":"high"}
```

`inbox` 必须是 `{to_skill,topic,payload}`；`discovered_sources` 必须是 `{url,description,category,vendor,role}`。最终 finish envelope 为 `{job_id,worker_id,status,output:{facts,inbox,discovered_sources},error_summary}`。

## 拒绝与失败

- 表头或脚注缺失，无法判断 Dense/Sparse：`rejected`。
- 数值是服务器、节点或集群汇总：拒绝，不除以卡数猜测。
- 只有 benchmark 结果：交给 `benchmark-ingest`，本 Skill 不写理论字段。
- 快照缺失/无法解析：`failed`。

## 完成标准

所有算力值均能回答“哪个型号、什么精度、什么单位、Dense 还是 Sparse、单卡还是整机”，且证据来自同一任务快照。只返回候选，不直接写数据库。
