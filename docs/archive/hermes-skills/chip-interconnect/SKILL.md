---
name: chip-interconnect
description: 当芯片页面说明加速卡间互联技术、单设备互联带宽或外部网络接口时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, chip, interconnect, networking]
    related_skills: [chip-basic, deployment-ingest]
---

# 芯片互联

提取单卡互联技术、互联带宽和网络接口，重点防止把链路、节点或整机聚合带宽误写成单卡带宽。

## 何时使用

- 使用：`agent_extract` 快照明确描述具体芯片/板卡的卡间互联或外部网络接口。
- 不使用：仅有 PCIe 主机总线（交给 `chip-basic`）、只有服务器拓扑、或只有集群网络总带宽。

## 输入契约

任务地址从 `job.input.url` 读取。`evidence_scope=snapshot` 时读取完整 `snapshot_path`；为 `diff` 时仅使用 `diff_path` 的新增 `+` 行。fact 的 `source_url` 原样复制 `input.url`，`evidence_location` 与 scope 一致。必须核对型号、端口数、单链路/聚合、单向/双向及卡间/节点间口径。

## 负责字段

| 字段 | 规则 |
|---|---|
| `interconnect_bw_gb_s` | 可确认的单卡聚合互联带宽；`proposed_value` 只放纯数字，`unit` 写 GB/s，方向与聚合口径留在证据/说明 |
| `interconnect_tech` | NVLink、Infinity Fabric、HCCS、RoCE 等原文技术名称及代际 |
| `network_interface` | 设备暴露的 Ethernet/InfiniBand/RoCE 接口和速率；不写机架交换机规格 |

目标表仅为 `chips`。

## 执行流程

1. 识别页面中的主体是芯片、板卡、服务器还是集群。
2. 解析端口数、每端口速率、方向和聚合方式；仅做可验证换算。
3. 对每个确定字段创建独立 `facts`，使用精确 `entity_key` 与逐字 `evidence_text`。
4. 显式写 `source_type` 与 `confidence`（`high`/`medium`/`low`），避免社区数据被默认成官方。
5. 部署拓扑、后端版本等信息放入 `inbox` 给 `deployment-ingest`；详情页只能从 `input.outbound_links` 选择。
6. 返回候选，由服务端校验并决定发布。

## 输出契约

返回 `facts`、`inbox`、`discovered_sources`。每个事实必须含 `target_table`、`entity_key`、`field_name`、纯数字 `proposed_value`、`unit`、`source_url`、`evidence_text`、`evidence_location`、`source_type`、`confidence`。`inbox` 使用 `{to_skill,topic,payload}`，来源使用 `{url,description,category,vendor,role}`；finish envelope 为 `{job_id,worker_id,status,output,error_summary}`。

## 拒绝与失败

- 只有单链路速率但端口数不明：不推算聚合值。
- 只有整机多卡总带宽：不除卡数猜测。
- bit/s 与 byte/s、单向与双向无法确认：`rejected` 并说明歧义。
- 快照不可用：`failed`。

## 完成标准

每个带宽值都能追溯到同一型号、同一互联层级和明确方向/聚合口径；跨领域内容已进入 `inbox`，且未直接执行 SQL 或修改正式库。
