# 旧 Hermes Skill 档案

原始版本来自 Git `f9a22a5`（实施基线）。所有原始文件按 Git blob 字节保存，原版本/frontmatter 不修改；这些 V2 合同仅解释旧任务、历史记录与历史回归，不是当前 V3 的安装或运行证据。特别是此处旧 chip-compute 与权威新同名包完全分离，Task 1 的 domain.json 不属于档案。

| 旧包 | 迁移或范围 |
|---|---|
| chip-catalog | 身份、类型、生命周期迁入 chip-identity |
| chip-basic | SKU、单卡、单位迁入 chip-specs |
| chip-compute | 精度与理论口径迁入新 chip-compute |
| chip-interconnect | 方向/聚合/接口迁入 chip-specs |
| chip-ecosystem | 明确支持声明迁入 chip-compatibility；成熟度不在本轮 |
| chip-price | 价格不在本轮六域 |
| benchmark-ingest | 条件/实测规则迁入 chip-benchmark |
| deployment-ingest | 分入 chip-compatibility / chip-deployment |
| url-discovery | 旧出链队列归档；领域开放搜索不使用该入口 |
| data-quality-review | 旧质量队列归档；完整领域证据复核替代 |
| data-update-orchestrator | 旧正式队列协调归档 |
| data-update-orchestrator-test | 旧三域测试归档 |
| data-update-orchestrator-local | 旧本地协调入口归档 |
| aishperf-data-heartbeat | 旧来源直写心跳归档，本轮不启用 |
| source-refresh | 旧来源巡检合同归档，程序资料可复用 |
| chip-enrich | 旧人工写库工具归档，不进入自动候选链路 |

独立 model-catalog、model-activity、chip-recommend-cli、chip-selector-agent 仍在权威目录中作为独立资料保留，不自动安装六域主链路。迁移不卸载服务器现有人工工具、不调整远端配置或 cron。旧安装器必须在写入前退出；新入口为 scripts/install_hermes_open_web_plugin.sh，六域唯一安装源是 .hermes-skills。
