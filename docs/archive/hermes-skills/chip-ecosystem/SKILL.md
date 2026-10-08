---
name: chip-ecosystem
description: 当网页快照提供芯片软件栈、框架支持、云端可用性或生态成熟度的具体证据时使用。
metadata:
  hermes:
    version: 2.0.0
    tags: [aishperf, chip, ecosystem, frameworks]
    related_skills: [deployment-ingest, data-quality-review]
---

# 芯片生态

把“可验证的软件与服务支持”转化为生态候选，避免把营销词或理论可安装性误当作已验证兼容。

## 何时使用

- 使用：官方文档、版本说明、云产品页或可靠测试页明确列出软件栈、框架、版本、云可用性。
- 不使用：只有“生态完善”等宣传语，或内容实际是芯片×模型部署/实测；这些交给相应 Skill。

## 输入契约

URL 来自 `job.input.url`。`evidence_scope=snapshot` 读取完整 `snapshot_path`；为 `diff` 时只读取 `diff_path` 新增 `+` 行。fact 的 `source_url` 原样复制输入 URL，`evidence_location` 与 scope 一致。所有版本、日期、平台区域都必须来自受信任正文。

## 负责字段

目标表固定为 `chips`：`software_stack`、`compatible_frameworks`、`framework_compat`、`sw_stack`、`cloud_available`、`ecosystem_notes`、`maturity_level`。

- `software_stack` / `sw_stack`：SDK、编译器、运行时、驱动、推理引擎及明确版本。
- `compatible_frameworks` / `framework_compat`：沿用现有可读文本或逗号列表，写清 PyTorch、TensorFlow、JAX 等版本与支持程度。
- `cloud_available`：只能写字符串 `"0"`/`"1"`；能够实际创建/租用才为 1，预告不算可用。
- `ecosystem_notes`：无法结构化但有价值的生态事实。
- `maturity_level`：只能写字符串 `"0"`–`"5"`；仅在项目规则与多项事实足以支持时提交，不能根据单句宣传判断。

## 执行流程

1. 确认软件支持对应哪个芯片系列/型号及哪个日期或版本。
2. 区分“官方支持”“社区适配”“理论可安装”和“已验证运行”。
3. 为自有字段生成 `facts`，逐项附 `entity_key`、`source_url`、逐字 `evidence_text`。
4. 每个 fact 显式填写真实 `source_type`、当前 `evidence_location` 和 `confidence`（`high`/`medium`/`low`）。
5. 具体芯片×模型兼容/部署进入 `inbox` 给 `deployment-ingest`；实测进入 `benchmark-ingest`。
6. 发布说明或兼容矩阵只有存在于 `input.outbound_links` 时才写入 `discovered_sources`。

## 输出契约

返回 `facts`、`inbox`、`discovered_sources`。事实的 `target_table` 必须是 `chips`。`inbox` 为 `{to_skill,topic,payload}`；来源为 `{url,description,category,vendor,role}`。finish envelope 使用 `{job_id,worker_id,status,output:{facts,inbox,discovered_sources},error_summary}`。

## 拒绝与失败

- “supports AI frameworks”但未列名称/版本：拒绝。
- 第三方成功安装不能升级为官方支持：可记录社区证据或转入 `inbox`。
- 云实例仅预告、无可用区域/产品页：不得写 `cloud_available=1`。
- 快照缺失：`failed`。

## 完成标准

每项候选都说明“什么软件/框架、什么版本或时间、支持到什么程度、对应哪个芯片”，证据可逐字定位；只产生候选，不直接改库。
