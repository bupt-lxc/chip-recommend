---
name: data-quality-review
description: 当一次更新周期结束后，需要只读检查重复实体、证据冲突、失效来源、关键字段缺失或来源质量升级时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, quality, provenance, review]
    related_skills: [chip-catalog, url-discovery]
---

# 数据质量复核

对候选与现有数据做只读质量检查，输出可执行的问题清单。此 Skill 不负责字段提取，也不直接发布修正。

## 何时使用

- 使用：一轮任务结束后，或指定实体/来源需要冲突、重复、完整性与溯源检查时。
- 不使用：网页还未抓取、需要直接修改字段、或需要人工凭印象决定正确值。

## 输入契约

可执行输入必须包含运行 ID、候选列表、来源访问结果、相关实体现值和溯源记录组成的 `review_bundle`。当前 planner 创建的旧 `agent_discovery` 只有 `scope/max_entities`，信息不足，遇到这类任务必须 `rejected` 并说明“缺少 review_bundle”，不能以空结果假成功。

## 负责字段

本 Skill 不拥有业务字段。检查：重复实体与别名、同一字段冲突、低等级证据覆盖高等级证据、失效 URL、孤立关系、单位异常、关键字段缺失、非官方来源可升级机会。

## 执行流程

1. 按实体与字段聚合现值、候选值和来源等级。
2. 标出确定重复、疑似冲突、来源失效和单位异常。
3. 区分“数据错误”“证据不足”“覆盖率缺口”“可升级来源”。
4. 为每项问题给出实体、字段、当前值、候选值、来源和下一步建议。
5. 需要重新抓取或由其他 Skill 处理时创建明确任务建议，不自行写库。

## 输出契约

服务端只消费 `facts`、`inbox`、`discovered_sources`，因此本 Skill 固定返回 `facts: []`：

- 可由其他 Skill 处理的问题写入 `inbox`，每项包含有效 `to_skill`、清晰 `topic`，并在 `payload` 中放严重级别、实体/字段、当前值、候选值、相关 URL 和理由。
- 找到更高等级的真实 HTTPS 来源时写入 `discovered_sources`，包含 `url`、`description`、`category` 与 `role`。
- 没有问题时仍以 `succeeded` 返回三个空数组，并在任务摘要中说明检查范围。

```json
{"facts":[],"inbox":[{"to_skill":"chip-basic","topic":"H100 显存带宽来源冲突","payload":{"field_name":"vram_bw_gb_s","severity":"high","reason":"两个官方页面数值不同"}}],"discovered_sources":[]}
```

## 拒绝与失败

- 证据来源不可比或单位未知：通过 `inbox` 标记“需复核”，不判定谁正确。
- 无 provenance：报告溯源缺失，不制造来源。
- 输入缺 `review_bundle`：`rejected`；运行中读取失败才用 `failed`。finish 不存在 partial 状态。

## 完成标准

检查范围透明，每个问题可定位、可分派、可复核；没有隐式数据修改，没有把统计默认值当成真实事实。
