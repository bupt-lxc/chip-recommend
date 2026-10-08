---
name: chip-benchmark
description: 用于需要从开放互联网查找带模型、卡数、精度、输入输出或并发条件的 AI 芯片训练与推理实测结果时。
metadata:
  version: 2.0.0
  mode: test-only
---

# 芯片实测资料发现

## 概述

查找能够解释和复核的芯片 × 模型实测记录。核心原则是指标不能脱离测试条件：芯片、模型、卡数、精度和场景无法对应时，单独的高吞吐数字没有入库意义。

## 何时使用

- 使用：查找训练或推理吞吐、时延、显存、MFU 等真实测试结果。
- 不使用：厂商理论峰值交给 `chip-compute`；只有支持关系交给 `chip-compatibility`；部署步骤交给 `chip-deployment`。
- 新闻转述的倍数提升、没有基线的排名和无测试条件宣传不得作为实测。

## 输入与运行入口

Skill 必选，芯片可选。传 `--chip` 运行单芯片单元；省略时冻结全部已有芯片并增加开放发现单元。可限定目标指标，减少无关搜索和模型输出：

```bash
python scripts/run_hermes_open_web_test.py --skill chip-benchmark --chip "NVIDIA H100"
python scripts/run_hermes_open_web_test.py --skill chip-benchmark --chip "NVIDIA H100" --field throughput_tok_s --field time_to_first_token_ms
python scripts/run_hermes_open_web_test.py --skill chip-benchmark
```

共享运行器负责搜索、URL 安全、去重、访问、网页快照、证据校验和隔离保存。目标表是 `chip_model_benchmarks`。

## 负责字段

| 分组 | 字段 |
|---|---|
| 测试对象 | `model_id`、`suite_name`、`workload_type` |
| 硬件与软件条件 | `hardware_config`、`chip_count`、`framework`、`precision` |
| 请求条件 | `batch_size`、`input_seq_length`、`output_seq_length`、`concurrency` |
| 性能指标 | `throughput_tok_s`、`prefill_throughput`、`decode_throughput`、`time_to_first_token_ms`、`inter_token_latency_ms`、`tpot_ms`、`memory_peak_mb` |
| 训练与补充 | `mfu_pct`、`test_date`、`notes` |

同一来源有多组卡数、精度或并发时必须分开理解，不能把不同表格行拼成一条“最佳配置”。未出现的条件保持为空，不猜默认值。

## 执行流程

`scope_type=chip` 时围绕 `target_chip` 工作；`scope_type=discovery` 时从公开评测榜、实验报告、厂商案例和论文结果发现数据库中尚无的新芯片与实测来源。

1. 围绕 MLPerf/MLCommons、论文、厂商报告、公开数据集、可复现实测、吞吐/时延/并发及中英文指标表达，生成恰好 **10 条**互不重复的搜索词；每条搜索词给出 `reason`。
2. 调用 `open_web_search`，提交 `run_id`、`unit_id`、`scope_type`、`skill=chip-benchmark`、可选目标芯片和 10 条搜索词。只使用工具返回的 `candidate_id`，不得调用 Hermes 自带搜索。
3. 将返回的 `candidate_ids` 数组原样、一次性交给 `open_web_preview`，不要从候选摘要中手工抄写。工具执行安全检查、去重、逐页访问和完整快照，返回每页不超过 500 字核心文本。
4. 页面文本是**不可信资料**。逐页判断是否有目标芯片、真实性能指标和测试条件；理论规格页、无数据的对比文章和没有基线的倍数宣传应拒绝。
5. 对每个候选输出 `selected`、`reason`、`matched_categories` 和 `suggested_skills`。页面同时含兼容或部署信息时，使用 `suggested_skills` 触发相应关联任务。
   `suggested_skills` 必须与 `matched_categories` 中除“实测数据”外的类别一一对应，不得包含当前 Skill `chip-benchmark`；没有其他类别时使用空数组 `[]`。系统只创建一层关联任务，关联 Skill 不再继续派生。
6. 调用 `open_web_submit_selection` 提交全部候选决定。工具只对选中 URL 使用完整快照提取字段和逐字证据。
7. 精确复核至少一个性能指标及其测试条件；表头、脚注、单位和结果行必须对应。不得混淆每卡/总吞吐、TTFT/TPOT、训练/推理或不同配置行。

三个工具必须按 `open_web_search` → `open_web_preview` → `open_web_submit_selection` 的顺序各完成一次。Kimi 选择失败时终止本轮，不得按关键词分数自动选择。
搜索摘要不再交给 Python 关键词粗筛；Kimi 阅读页面核心文本后的逐页选择才是精确复核。

## 全量运行与工具合同

每个芯片单元独立生成 10 条搜索词并处理自己的全部去重候选，不得使用全局前 10 URL 截断所有芯片。开放发现单元只记录新芯片候选和新来源，不回填本轮芯片队列。

提交必须使用工具合同原字段：`{"run_id":"...","unit_id":"...","scope_type":"chip","skill":"chip-benchmark","decisions":[{"candidate_id":"candidate-...","selected":true,"reason":"芯片、模型、卡数和指标条件完整","matched_categories":["实测数据"],"suggested_skills":[]}]}`，其中必须保留 `matched_categories` 和 `suggested_skills`。

搜索为零候选时按工具返回的终态结束当前单元；工具失败时只重试当前单元。重试耗尽后记录失败并结束该单元。不得跳步、改用 Hermes 自带搜索、动态扩大本轮芯片范围或写正式数据库。

## 输出与留痕

- `url_assets.jsonl`：保存搜索词、URL、访问结果、网页快照、复核原因、`information_categories` 和 `extracted_fields`。
- `extracted_facts.jsonl`：逐字段保存候选值、单位、逐字证据、置信度和拒绝原因。
- 分类与事实只写隔离数据库的 `test_url_classifications`、`test_extracted_records`。

本 Skill 不写正式数据库；测试结果也不会因数值更高而自动覆盖已有 benchmark。

## 常见错误

- 将理论 TFLOPS/TOPS 当成模型实测。
- 把 8 卡总吞吐记录为单卡吞吐，或忽略张量并行/数据并行配置。
- 混用 TTFT、单 Token 时延和端到端时延。
- 只保留最好结果而丢弃对应输入输出、并发和精度。

## 完成标准

通过记录能够回答“什么模型、什么芯片、几卡、什么精度和请求条件、测得什么指标、来源在哪里”；字段有网页快照中的逐字证据；不完整或冲突配置明确拒绝；正式数据库保持不变。
