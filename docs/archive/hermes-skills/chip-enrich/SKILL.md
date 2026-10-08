---
name: chip-enrich
description: 对已有芯片执行旧式批量信息扩充，按字段组检索证据并通过统一接口写入字段级来源；队列任务不得调用。
version: 2.0.0
metadata:
  hermes:
    tags: [chip, data, enrichment, specs]
    related_skills: [chip-catalog]
---

# 芯片批量扩充

## 何时使用

仅供人工发起的旧式批量扩充流程使用。收到 `data_agent` 队列任务时不得使用本 Skill，也不得直接写库；应改用任务指定的 `chip-basic`、`chip-compute`、`chip-interconnect`、`chip-ecosystem` 或 `chip-price`，再由统一发布器处理候选事实。

## 工作目录与依赖

```bash
cd /root/chip-recommend
```

依赖 `data/data.db`、`schema.sql` 和 `chip_model/database.py`。芯片必须已由 `chip-catalog` 建立。

## 字段与证据规则

关键字段组包括 `memory`、`precision`、`clock_power_physical`、`architecture`、`interconnect`、`compute_units`、`cache`、`software` 和 `pricing`。这些值必须来自实际网页，不得使用模型记忆补齐。

非关键字段组为 `description`、`ecosystem` 和 `lifecycle`。确无公开来源时可使用模型整理，但必须标记：

- `source_type="llm_curated"`
- `source_url="LLM curated"`
- `confidence="medium"`
- `is_official="0"`

数值字段只保存纯数字字符串；精度性能使用 `BF16=1980TF,FP8=3960TF` 形式；布尔字段使用 `"0"` 或 `"1"`。找不到可靠来源时保留 NULL。

## 写入要求

必须调用 `chip_model.database.update_chip_fields()`，由该接口更新旧值并逐字段写入 `field_provenance`。禁止直接执行 `UPDATE chips` 或自行插入来源表。

```python
source = {
    "source_type": "official_datasheet",
    "source_url": "https://www.nvidia.com/en-us/data-center/h100/",
    "source_detail": "规格表 > 显存",
    "confidence": "high",
    "is_official": "1",
}
fields = {"vram_gb": "80", "vram_type": "HBM3", "vram_bw_gb_s": "3350"}
update_chip_fields(conn, chip_id=3, fields=fields, source=source)
```

## 执行流程

1. 用 `python scripts/run_cli.py chip search --limit 50` 找出字段缺失的芯片。
2. 明确本次芯片范围，逐颗按字段组处理。
3. 先搜索厂商官网，再搜索可靠第三方来源，并打开具体页面核验原文。
4. 归一化字段值，按来源分组调用 `update_chip_fields()`。
5. 矛盾或口径不明的值保持为空并记录原因。
6. 用 `python scripts/run_cli.py chip profile "<chip_model>"` 验证。

## 完成标准

报告处理芯片数、新增字段数、网页证据字段数、模型整理字段数、来源记录数以及仍缺失或冲突的关键字段。所有写入必须可追溯。
