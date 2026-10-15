---
name: deployment-ingest
description: 当网页快照记录芯片与模型兼容性声明、已验证运行配置、推理后端或具体部署指南时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, compatibility, deployment, runtime]
    related_skills: [chip-ecosystem, benchmark-ingest]
---

# 兼容与部署

记录芯片 × 模型的兼容等级以及可操作的部署参考。兼容声明和真实验证必须分开。

## 何时使用

- 使用：文档明确对应芯片、模型、框架/后端、精度或版本，并提供适配/验证证据。
- 不使用：只有芯片生态的一般框架支持，或只有性能结果但没有部署信息。

## 输入契约

URL 从 `job.input.url` 读取。`evidence_scope=snapshot` 时使用完整 `snapshot_path`，为 `diff` 时只使用 `diff_path` 的新增 `+` 行；fact 的 `source_url` 原样复制输入 URL，`evidence_location` 与 scope 一致。部署指南必须是可访问的具体文档，不能仅引用首页或搜索摘要。

## 负责字段

- `chip_model_compatibility`：`compat_status`、`framework`、`precision`、`verified_at`、`notes`。
- `deployment_guides`：`backend`、`notes`。

`compat_status` 只使用 `verified`、`vendor_claimed`、`community`：真实复现实测才是 `verified`；厂商矩阵是 `vendor_claimed`；社区教程/报告是 `community`。

## 执行流程

1. 锁定精确芯片、模型、框架/引擎、版本、精度和验证日期。
2. 判断证据级别，禁止把“理论支持”升级为 verified。
3. 按目标表生成 `facts`，使用能唯一定位关系的 `entity_key`，附 `source_url`、逐字 `evidence_text`、`evidence_location`、准确 `source_type` 和 `high/medium/low` 置信度。兼容关系键必须含精确 `chip_model`、完整 `model_id` 和 `compat_status`；部署指南键必须含真实 HTTPS `url` 与非空 `title`，若正文明确适用范围还要带 `chip_model`、`model_id` 和表内 `source_type`。
4. 性能结果通过 `inbox` 给 `benchmark-ingest`；一般生态证据给 `chip-ecosystem`。
5. 具体指南、版本矩阵或仓库链接只有出现在 `input.outbound_links` 中才加入 `discovered_sources`。

## 输出契约

返回 `facts`、`inbox`、`discovered_sources`。兼容示例的 `entity_key` 为 `{"chip_model":"H100","model_id":"org/model","compat_status":"community"}`；限定芯片和模型的指南应使用 `{"url":"https://example.com/guide","title":"H100 vLLM 部署指南","chip_model":"H100","model_id":"org/model","source_type":"official_doc"}`。只有正文确实不限定芯片/模型时才省略范围字段。注意 fact 顶层 `source_type` 只写 provenance，不会自动填入指南表，因此表内 `source_type` 必须放入指南 `entity_key`。每项事实必须标明 `target_table`；指南版本、启动参数写入 `notes`，不得创造 `version` 字段。`inbox` 为 `{to_skill,topic,payload}`，来源为 `{url,description,category,vendor,role}`，finish envelope 为 `{job_id,worker_id,status,output:{facts,inbox,discovered_sources},error_summary}`。

## 拒绝与失败

- 芯片或模型只有模糊系列名：`rejected`。
- 未给版本/配置且无法判断是否真的运行：最多 `vendor_claimed` 或 `community`，不得 verified。
- 链接是首页、登录页或失效页：不作为部署指南。
- 快照缺失：`failed`。

## 完成标准

关系实体明确，兼容等级与证据强度一致，版本/精度/日期尽可能完整，跨域内容已联动；仅提交候选，不直接改库。
