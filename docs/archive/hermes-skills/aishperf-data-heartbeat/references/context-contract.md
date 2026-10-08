# 心跳上下文约定

预检查脚本最后一行必须是以下两种结果之一：

```json
{"wakeAgent": false}
```

或：

```json
{
  "wakeAgent": true,
  "context": {
    "source_refresh": {
      "trigger": "hermes_ticker",
      "run_id": 12,
      "status": "partial",
      "started_at": "2026-09-09T02:00:01+08:00",
      "finished_at": "2026-09-09T02:00:18+08:00",
      "duration_ms": 17000,
      "counts": {
        "selected": 19,
        "new": 0,
        "changed": 1,
        "unchanged": 14,
        "failed": 4
      },
      "checked_sources": [
        {
          "link_id": 20,
          "vendor": "示例厂商",
          "description": "产品规格页",
          "requested_url": "https://vendor.example/spec",
          "final_url": "https://vendor.example/products/spec",
          "checked_at": "2026-09-09T02:00:01+08:00",
          "duration_ms": 850,
          "http_status": 200,
          "outcome": "changed",
          "attempt_count": 1,
          "response_bytes": 24580
        }
      ],
      "changed_sources": [],
      "new_sources": [],
      "failing_sources": [],
      "business_tables_modified": false
    }
  }
}
```

## 状态含义

- `new`：首次成功获取，建立对比基线。
- `changed`：规范化正文变化，已生成候选差异，等待人工复核。
- `unchanged`：正文未变化或服务端返回 304。
- `failed`：来源返回不可恢复错误，例如 404。
- `retry_wait`：超时、429 或 5xx，已进入退避。
- `unsupported`：当前抓取器不支持该内容类型。

## 与业务字段的关系

候选字段目前覆盖显存容量、显存带宽、功耗、精度性能、制程、互联带宽、架构和计算单元。Hermes Agent 根据新增原文生成更新 payload，由受限自动发布执行器完成官方域名、唯一芯片、候选字段、字段格式、原文证据和影子数据库检查。全部通过后直接写入正式业务表和 `field_provenance`；失败则拒绝该条变化，不进入人工审核。

## 运行记录字段

- `started_at` / `finished_at`：整轮抓取开始和结束时间。
- `duration_ms`：整轮耗时。
- `checked_sources`：本轮实际处理的全部来源，用于完整列出抓取链接和逐来源结果。
- `requested_url` / `final_url`：配置的原始链接和重定向后的最终链接。
- `checked_at` / `duration_ms`：单条来源开始检查的时间和耗时。
- `attempt_count` / `response_bytes`：请求尝试次数和正文大小。
- `http_status` / `outcome`：HTTP 状态和巡检结论。
