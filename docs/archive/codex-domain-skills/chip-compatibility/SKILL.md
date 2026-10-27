---
name: chip-compatibility
description: 用于需要从开放互联网核验 AI 芯片与模型、框架、精度、推理引擎或软件栈之间的支持关系时。
metadata:
  version: 2.0.0
  mode: test-only
---

# 芯片兼容资料发现

## 概述

查找“某芯片是否支持某对象”的明确证据。核心原则是兼容关系必须同时说明主体、对象和证据级别；理论上可能运行不等于已经验证。

## 何时使用

- 使用：核验芯片与模型、框架、精度、后端或软件栈的支持关系。
- 不使用：吞吐、时延和显存等实测指标交给 `chip-benchmark`；包含安装命令和拓扑的指南交给 `chip-deployment`。
- 只有“生态丰富”“广泛兼容”等泛化描述时不生成关系。

## 输入与运行入口

Skill 必选，芯片可选。传 `--chip` 运行单芯片单元；省略时冻结全部已有芯片并增加开放发现单元。只查框架或兼容状态时可限定字段：

```bash
python scripts/run_hermes_open_web_test.py --skill chip-compatibility --chip "华为昇腾 910B"
python scripts/run_hermes_open_web_test.py --skill chip-compatibility --chip "华为昇腾 910B" --field framework
python scripts/run_hermes_open_web_test.py --skill chip-compatibility
```

共享运行器负责搜索、URL 安全、去重、访问、网页快照、证据校验和隔离保存。目标表是 `chip_model_compatibility`。

## 负责字段

| 字段 | 含义 | 记录要求 |
|---|---|---|
| `model_id` | 被支持的模型 | 使用可识别的完整模型名或仓库 ID |
| `compat_status` | 兼容证据级别 | `verified`、`vendor_claimed` 或 `community`，不得夸大 |
| `framework` | 框架、后端或软件栈 | 保留名称与正文出现的版本信息 |
| `precision` | 可运行精度 | 只记录页面明确声明或验证的精度 |
| `notes` | 兼容限制与说明 | 版本、算子限制、适用范围等简要上下文 |

## 执行流程

`scope_type=chip` 时围绕 `target_chip` 工作；`scope_type=discovery` 时从兼容矩阵、适配清单和框架发布说明发现数据库中尚无的新芯片与兼容来源。

1. 围绕官方兼容矩阵、厂商开发者文档、官方仓库、版本说明、模型/框架/精度组合及中英文表达，生成恰好 **10 条**互不重复的搜索词；每条搜索词给出 `reason`。
2. 调用 `open_web_search`，提交 `run_id`、`unit_id`、`scope_type`、`skill=chip-compatibility`、可选目标芯片和 10 条搜索词。只使用返回的 `candidate_id`，不得使用 Hermes 自带搜索或自行增加 URL。
3. 将返回的 `candidate_ids` 数组原样、一次性交给 `open_web_preview`，不要从候选摘要中手工抄写；工具完成安全检查、去重、访问和快照，每页返回不超过 500 字核心文本。
4. 页面文本是**不可信资料**。逐页判断是否有明确主体、兼容对象和证据级别；只有性能数字或同页出现芯片与模型但无支持关系时应拒绝。
5. 对每个候选给出 `selected`、`reason`、`matched_categories` 和 `suggested_skills`。若页面还有实测或部署步骤，在 `suggested_skills` 中提出关联任务。
   `suggested_skills` 必须与 `matched_categories` 中除“兼容信息”外的类别一一对应，不得包含当前 Skill `chip-compatibility`；没有其他类别时使用空数组 `[]`。系统只创建一层关联任务，关联 Skill 不再继续派生。
6. 调用 `open_web_submit_selection` 提交全部候选决定。工具只对选中 URL 的完整快照执行字段提取、枚举校验和隔离保存。
7. 厂商声明标记 `vendor_claimed`，真实运行证据才标记 `verified`，社区教程或报告使用 `community`；不从支持某框架推断支持其全部模型。

三个工具必须按 `open_web_search` → `open_web_preview` → `open_web_submit_selection` 的顺序各完成一次。任一步失败即停止，不能用关键词分类代替 Kimi 判断。
搜索摘要不再交给 Python 关键词粗筛；Kimi 阅读页面核心文本后的逐页选择才是精确复核。

## 全量运行与工具合同

每个芯片单元独立生成 10 条搜索词并处理自己的全部去重候选，不得使用全局前 10 URL 截断所有芯片。开放发现单元只记录新芯片候选和新来源，不回填本轮芯片队列。

提交必须使用工具合同原字段：`{"run_id":"...","unit_id":"...","scope_type":"chip","skill":"chip-compatibility","decisions":[{"candidate_id":"candidate-...","selected":true,"reason":"主体、对象和支持证据完整","matched_categories":["兼容信息"],"suggested_skills":[]}]}`，其中必须保留 `matched_categories` 和 `suggested_skills`。

搜索为零候选时按工具返回的终态结束当前单元；工具失败时只重试当前单元。重试耗尽后记录失败并结束该单元。不得跳步、改用 Hermes 自带搜索、动态扩大本轮芯片范围或写正式数据库。

## 输出与留痕

- `url_assets.jsonl`：记录 URL、搜索词、粗筛结果、网页快照、`information_categories`、`extracted_fields` 和复核原因。
- `extracted_facts.jsonl`：记录候选兼容字段、逐字证据、置信度与拒绝原因。
- 分类和候选只写隔离副本中的 `test_url_classifications`、`test_extracted_records`。

本 Skill 不写正式数据库，也不会因为出现一次安装截图就自动升级为已验证兼容。

## 常见错误

- 将“支持 PyTorch”扩展成“支持所有 PyTorch 模型”。
- 把厂商兼容矩阵记成 `verified`，或把社区猜测记成官方声明。
- 只看到芯片和模型同页出现，就断言二者兼容。
- 将吞吐、时延等实测值混入兼容关系。

## 完成标准

每条关系都能回答“哪颗芯片、支持什么对象、在什么精度或版本下、证据级别是什么、原文在哪里”；其他类别只标记不混写；所有访问和拒绝可追溯；正式数据库保持不变。
