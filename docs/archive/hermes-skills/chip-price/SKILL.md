---
name: chip-price
description: 当网页快照给出可归属到具体芯片、加速卡、销售渠道或云租赁实例的价格，且具备足够口径与日期信息时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, chip, price, market]
    related_skills: [chip-catalog, data-quality-review]
---

# 芯片价格

提取可比较、可追溯的价格候选。价格必须同时回答：哪个型号、什么商品口径、什么币种、什么时间、一次性还是租赁、来自哪类来源。

## 何时使用

- 使用：官方采购报价、可识别卖方和日期的渠道报价，以及仅用于补充说明的云租赁页，且能唯一对应芯片或加速卡。
- 不使用：询价、库存为零的占位价、论坛猜测、服务器/整机总价无法拆分，或币种/单位不明。

## 输入契约

URL 从 `job.input.url` 读取。`evidence_scope=snapshot` 使用完整 `snapshot_path`；为 `diff` 时只使用 `diff_path` 新增 `+` 行。fact 的 `source_url` 原样复制输入 URL，`evidence_location` 与 scope 一致。记录商品名称、配置、价格、币种、租赁计费周期、税费、地区、报价日期和来源类型。

## 负责字段

目标表固定为 `chips`：

| 字段 | 用途 |
|---|---|
| `price_usd` | 明确的一次性单芯片/单卡美元采购价；不做实时汇率猜测，禁止写云实例小时/月租金 |
| `price_cny_wan` | 一次性单芯片/单卡人民币采购价（万元）；禁止写云实例租金 |
| `price_period` | 报价时间口径，如 `2025 Q2` 或 `2026-09-26`；不是 hour/month 租赁周期 |
| `price_notes` | 单卡/整机、官方/渠道/云租赁、租赁周期、地区、税费和配置说明 |

云实例按小时/月计费的金额只能写入 `price_notes`，不得写入 `price_usd` 或 `price_cny_wan`，否则推荐算法会把租金误当成硬件采购价。

## 执行流程

1. 锁定完整型号、显存容量和形态，确认卖的是芯片、板卡、整机还是云实例。
2. 提取原始金额、币种和周期；只做确定性单位换算，不用未知汇率补美元价。
3. 将价格与完整口径分别写为 `facts`；所有值带精确 `entity_key`、任务 `source_url` 和逐字 `evidence_text`。
4. 每个 fact 显式填写 `source_type`（官方/渠道/社区等）和 `confidence`（`high`/`medium`/`low`）。
5. 同页若有规格、生态或实测内容，使用 `inbox` 交给相应 Skill；详情页只能从 `input.outbound_links` 中选择。
6. 返回候选，由 Publisher 统一验证、去重和留痕。

## 输出契约

必须返回 `facts`、`inbox`、`discovered_sources`。推荐把数值和口径一起提交：

```json
{"target_table":"chips","entity_key":{"chip_model":"Example 80GB","vendor":"Example"},"field_name":"price_cny_wan","proposed_value":"8.8","source_url":"https://example.com/product","evidence_text":"80GB PCIe 加速卡，含税价人民币 88,000 元","evidence_location":"snapshot","source_type":"channel_quote","confidence":"medium"}
```

`inbox` 使用 `{to_skill,topic,payload}`，`discovered_sources` 使用 `{url,description,category,vendor,role}`。最终 finish envelope 使用 `{job_id,worker_id,status,output:{facts,inbox,discovered_sources},error_summary}`。

## 拒绝与失败

- 整机价、八卡服务器价无法拆分：`rejected`，不得除以卡数。
- 云租赁价格：只生成 `price_notes` 候选，不生成两个采购价标量。
- 仅写 `$9.99` 而商品/周期不明：拒绝。
- 二手或渠道价可作低等级候选，但必须在 `price_notes` 标明；不得冒充官方价。
- 快照无法访问或价格由脚本动态加载且正文没有：`failed`。

## 完成标准

价格、口径、时间和来源四要素完整，实体可唯一匹配，逐字段证据存在。没有可靠口径就不发布；本 Skill 不直接修改正式数据库。
