# Tavily 身份域隔离链路验证

2026-10-10，用户同意在仅替换搜索工具后运行一次不指定芯片的身份域测试。

- 运行：`run-38636fbf424341959620fd7adaab7878`
- 请求键：`tavily-identity-20261010-v1`
- 使用 `chip-recommend:hermes-acceptance-final7` 既有隔离镜像，仅挂载新版 search.py 并为测试进程注入 DOMAIN_SEARCH_BACKEND=tavily 和受限 Tavily key。正文工具与镜像中的 Skill 未修改。
- 新测试数据目录：`/home/ubuntu/utils/tavily-identity-acceptance/data`。基线通过 SQLite backup 从既有验收副本复制，正式数据库没有挂载到本测试容器。
- 时间预算 900 秒；测试容器 `chip-recommend-tavily-identity-test` 已在终态后停止，保留容器与运行产物。

| 环节 | 结果 |
| --- | --- |
| Hermes 生成词 | 10 条中英文词，不指定芯片 |
| Tavily 实际搜索 | 10/10 成功，每次 5 条；累计搜索请求耗时 17.253 秒 |
| 去重 | 50 条搜索结果形成 48 个主要候选 |
| 原正文工具 | 36 个页面保存快照，12 个页面最终未取得快照 |
| 页面访问 | 62 次尝试，36 次成功，26 次失败（包含重试） |
| Hermes 筛选 | 三批共 30 条决定：15 select、15 reject；剩余 18 条未提交 |
| 最终提交 | finalized=false |
| 字段提取 / 校验 | 未进入；不能统计有效字段或已验证新芯片 |
| 运行终态 | partial，active budget exhausted（900 秒） |

失败尝试分布：network_error 17、http_error 6、unsafe_url 2、max_bytes 1。网络失败包含 Google 文档页连接超时与重复尝试。选中的型号线索仍只是模型页面筛选结果，未经身份和字段校验，不能视为发现了数据库外的新芯片。

结论：项目实际调用链已经使用 Tavily；搜索、原工具正文抓取和 Hermes 分批筛选都有真实证据。仅更换搜索工具不足以使此轮全链路通过。搜索请求累计耗时只占预算约 1.9%，下一步应优先约束预览候选数量、针对稳定访问故障控制重试，并检查模型分批筛选耗时，而不是继续修改搜索词或仅扩大运行时间。这些改进尚未实施。

本轮镜像的生成词偏向数据中心，消费级训练显卡覆盖未充分验证；应在后续 Skill 对齐时单独复验，不能宣称本轮已覆盖消费卡。此次保持旧 Skill 是为了隔离搜索工具替换的影响。

正式服务启动时间仍为 `2026-09-30T10:30:32.132586456Z`；无正式业务写入、无心跳启用。服务器证据：测试目录内 summary.json、compact-summary.json 及 data/domain_runs 下完整持久运行记录。本地脱敏记录：`.tmp/tavily-identity-compact-remote.log`。
