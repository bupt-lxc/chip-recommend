# Hermes 六域开放发现验收记录

记录日期：2026-10-10（Asia/Shanghai）；实际网络/预检 2026-10-09 20:05–20:10 UTC，之后完成本地测试与文档校验。分支 `codex/hermes-domain-discovery`，Task16 实现起点 `c9dcdc298de541ac67d08b0fb64b23eb1d9ceedb`。本轮仅本地实现、只读环境检查和隔离诊断；未部署远程、发布、启用周期服务或修改 SSH 信任配置。

**整体未通过，待真实宿主机验证。** Task1–15 实现及受控六域正向/失败路径已完成并经过 SPEC→QUALITY 审查。Task16 探针、runbook 和本机诊断可审阅；实际宿主机预检、六域真实 Hermes 不绑定芯片运行与每域候选抽查均未执行。不能将受控 HTTP/model/SDK 样例当作真实安装/模型/互联网验收。唯一完整 pytest 的实际结果仍为失败；后续相关回归通过不等于重新执行全套。

## 证据范围与运行 ID

| 范围 | 实际执行与证据 | 限制 |
|---|---|---|
| Task15 fixture | `tests/test_domain_acceptance.py::test_six_domains_full_run_http_frozen_models_and_readonly_history` 六个参数域，真实生产工具 HTTP、RunStore、冻结规则、ModelService、独立语义复核、比较和只读历史 | 搜索、页面、Hermes runner 和模型返回受控；不是实际 SDK/开放网络 |
| Task14 fixture 浏览器 | Chromium 桌面 1715×924、窄屏 390×844；ID `fixture-reviewed/fixture-history/fixture-conflict/fixture-failed` | 隔离可控产物/API，不是六域真实发现 run_id |
| Task16 本机搜索 | 9 个实际来源请求，另 2 个 Bing 请求/重定向观察；默认 adapter 代理路线 | 未调用 Hermes/后端模型，无发现候选或 runtime run_id |
| Task16 本机预检 | `hermes_domain_worker.py --data-root <worktree>/data --check` 实际 failed/exit 2 | 只读本机 Windows 检查，不证明远程安装或写事务 |
| 真实宿主六域 | identity/specs/compute/compatibility/benchmark/deployment 全部**未执行**，run_id 全部**无** | 不为已知缺 SDK 重复创建六个失败运行 |

Task15 动态 fixture run_id 位于 pytest 临时目录，按测试名识别证据，未伪造持久真实 run_id。Task14 浏览器材料位于忽略目录 `.tmp/task14-ui/`：`desktop.png`、`narrow.png`、`narrow-right.png`、`raw-rejections.png`、`legacy-failed-stages.png`、`stage-scopes.png`、`browser-results.json`、`uuid-fallback-results.json`。结果 JSON 明示 fixture_only；raw-rejections/stage-scopes 使用实际 ModelService 生产拒绝/阶段投影。浏览器手动启动记录包含六域、202 队列、响应丢失同键重试、修改范围换键和 UUID fallback，不能作为六域网络运行成功证据。

## A01–A14：每项分别给出判定

“Fixture 通过”只表示既有受控测试证据；实际 host 未执行仍阻止整体验收。下表不得解释为 14 项真实网络全部通过。

| 项 | Fixture/本地判定 | 主要可复查证据 | 实际宿主判定 |
|---|---|---|---|
| A01 默认开放发现 | **Fixture 通过** | `test_discovery_one_run_never_creates_per_baseline_chip_units`（0/1/1000 chips）；六域正向测试 chip=None、单个域运行 | **未执行**；六域无 chip 启动与真实 run_id 未取到 |
| A02 可选芯片/字段全集/拒绝旧参数 | **Fixture 通过** | `tests/test_domain_contracts.py`、`test_initial_unknown_chip_clue_reaches_stable_backend_context_and_resume`、`test_known_chip_clue_does_not_exclude_generic_metadata_with_relevant_full_body`；CLI/API 同合同，旧字段输入在 mutation/runner 前拒绝 | **未执行**；runbook 列出的字段全集已从权威 domain.json 生成 |
| A03 唯一规则源/冻结 | **本地校验通过、Fixture 通过** | `sync_domain_skills.py --check`：20 文件，规则 3.0.0，bundle `46c150c2f29095af3e328fd14db234940a06d27bc84612d864cd8a9099a0ff4a`；`test_actual_resolver_report_must_match_every_frozen_file`、不可变/漂移测试 | **未执行**；本机安装副本缺失；远程实际 20 文件 resolver 路径/hash 未证明 |
| A04 各阶段规则起作用 | **Fixture 通过** | 六域正向测试核对 hermes/backend_extract/backend_review loaded+executed、全部参考 hash；`test_history_projects_actual_skill_executor_evidence`、`stage-scopes.png` | **未执行**；实际 SDK/插件/模型调用与冻结规则使用证据未获得 |
| A05 来源诊断/有界调整 | **Fixture 通过；本机默认来源检查失败** | `test_provider_fallback_preserves_original_query_diagnostics_without_partial`、`test_explicit_replacement_closes_intent_retains_failure_and_charges_queries`、`test_exact_initial_and_two_supplement_quota_healthy_empty_is_no_findings`；本轮 9 来源探针与 2 Bing HTTP 观察 | **未执行**；DDG parse_error，Bing 相关性退化未解决；固定诊断词不证明 Hermes 词设计 |
| A06 访问/决定覆盖与未完成清单 | **Fixture 通过** | `test_23_urls_all_paged_visited_decided_and_healthy_empty_pages_do_not_fail`、`test_budget_stop_keeps_each_pending_url_and_finalization_reason_without_fake_reject`、未 finalize 回归 | **未执行**；没有实际主要 URL 访问/决定覆盖统计 |
| A07 500 字主体定位/detail/全文 | **Fixture 通过** | `tests/test_domain_pages.py`、`test_full_run_uses_real_text_and_pdf_snapshot_parsers`；immutable snapshot、core offset、detail 三次额度与全文 chunk coverage | **未执行**；真实页面全文 hash、定位和覆盖待抽查 |
| A08 多实体与真实新芯片 | **Fixture 通过** | `test_same_page_two_complete_skus_resolve_common_identity_without_second_business_fanout`、`test_linked_discovery_first_new_identity_is_verified_common_dependency`；ExampleSilicon X64 明示虚构 | **未执行**；没有可报告的真实新芯片数量 |
| A09 库外关系依赖 | **Fixture 通过** | `test_missing_model_dependency_preserves_accepted_evidence_without_creating_model`；临时实体/模型依赖、无正式模型创建 | **未执行**；真实关系依赖未抽查 |
| A10 证据/单位/SKU/条件语义 | **Fixture 通过** | 六域正负例、`test_benchmark_missing_required_condition_retains_review_but_no_validated_metric`、`test_optional_unknown_conditions_do_not_invent_required_generation_constraints`、独立复核连接失败与修订上限 | **未执行**；每域最多 5 个真实通过候选及拒绝/条件抽查未做 |
| A11 值/来源两维比较 | **Fixture 通过** | `test_same_value_new_url_source_membership_and_quality_are_independent`、`test_ambiguous_historical_relation_preserves_accepted_metric_without_overwrite`；缺旧证据 quality unknown | **未执行**；真实旧值来源、新来源改善/冲突待核对 |
| A12 五列/历史/实际阶段 | **Fixture Chromium 通过** | `tests/test_domain_history_browser.py` 四项实际浏览器测试及上述截图；五列、拒绝原始形状、旧 failed 阶段不造完成、刷新状态/滚动与 UUID fallback | **未执行**（真实网络运行的 UI/证据关联）；旧历史/受控 UI 已有独立证据 |
| A13 幂等/续跑/关联失败 | **Fixture 通过** | `test_finalize_response_loss_restart_uses_original_receipt_and_jobs_once`、`test_expired_old_generation_cannot_commit_and_resume_keeps_original_freeze_and_limits`、`test_one_revision_limit_persists_unresolved_on_new_resume_request`、`test_main_accepted_linked_model_failure_is_partial_and_main_fact_is_readable`；API/host Worker 来源绑定与预算回归 | **未执行**；实际宿主 generation/挂载恢复待验证 |
| A14 正式库/调度边界 | **Fixture 通过；本机只读诊断摘要通过** | `test_wal_consistent_backup_includes_uncheckpointed_business_rows_and_digest_is_equal`、`test_logical_digest_checks_every_business_table_column_and_row`、`test_external_writer_is_detected_then_check_repeats_after_writer_finishes`；本轮 probe 前后六表摘要一致 | **未执行**（实际 Hermes 前后无写入核验）；本轮未运行 Publisher/heartbeat/服务部署 |

## 本机搜索实测：失败不能由备用来源掩盖

最终默认路线观察起始 `2026-10-09T20:07:20.331859+00:00`，9 次物理搜索调用，总墙钟 7.091 秒，探针退出 2。DATA_AGENT_PROXY 未设置，requests session.trust_env=true；实际 HTTP_PROXY/HTTPS_PROXY（大小写环境）为 `http://127.0.0.1:7897`，各 endpoint 的 ambient/effective proxy 已脱敏保存。TCP 7897 可连不代表目标站点或搜索相关性健康。

| 来源 | 中文开放 `AI 加速芯片 新发布` | 英文开放 `AI accelerator product launch` | 定向 `NVIDIA L40S specifications` |
|---|---|---|---|
| bing-html | HTTP200 / success / 8 URL / 328ms | HTTP200 / success / 8 URL / 390ms | HTTP200 / success / 10 URL / 201ms |
| so360-html | HTTP200 / success / 6 URL / 1200ms | HTTP200 / success / 6 URL / 813ms | HTTP200 / success / 6 URL / 706ms |
| duckduckgo-html | HTTP202 / parse_error / 0 URL / 509ms | HTTP202 / parse_error / 0 URL / 169ms | HTTP202 / parse_error / 0 URL / 151ms |

表中 success 是原始 adapter 的网络/解析结果，不是实际芯片信息发现。360 使用中文站点，en-US header 不代表英文索引质量。DDG HTTP202 未满足可识别结果/健康空结果结构，程序报告 `unrecognized or unparseable search response`；仅凭这份诊断不认定具体挑战页原因。探针没有 fallback/retry；三个来源结果分别保留，不能因 Bing/360 返回 200 把整体写为健康。

Bing 两条开放词返回相同八个泛 AI URL，L40S 词主要返回通用 NVIDIA 页面，相关性退化未解决。追加两次 Bing HTTP 观察在 20:10 UTC 保存传入 params、实际请求/最终 response URL、重定向和选定 cache headers：中文完整 q 保留、无重定向；英文从 www.bing.com HTTP302 到 cn.bing.com，完整 q/setlang=en/mkt=en-US 保留。Cache-Control 为 private,max-age=0，X-Cache 为 CONFIG_NOCACHE。这只证明客户端没有丢失查询词、服务发生上述重定向；无法据此断言代理缓存、服务端改写或根因。没有后台网页访问、候选选择或模型生成词证据。

首轮为显式关闭 trust_env 的 direct 诊断：20:05 UTC，9 调用/96.383 秒，Bing/360 返回 200/可解析；DDG 三个 network_error，各实际约 30 秒。adapter timeout 设置为 15 秒，多地址连接等可能增加墙钟。该路线与生产 adapter 默认不同，保留记录但不作为最终默认环境结论。修正后的生产路线探针保持 trust_env、分开记录显式/环境/effective proxies，并用回归防止再次改写。

忽略目录原始证据：`.tmp/task16-search-probe.json`、`task16-search-probe-execution.json`、`task16-search-probe-production-route.log`、`task16-bing-request-observation.json`、`task16-bing-observation.log`；首轮 `task16-search-probe-initial-direct.json`、`task16-search-probe-execution-initial-direct.json`、`task16-local-diagnostics.log`。网络端点原/实际词、locale、HTTP/network/parse、有效 URL、耗时均可核对，代理认证脱敏；没有导入个人浏览器 cookie、业务 CSV 筛选、RunStore 或模型调用。

## 实际环境预检与未执行清单

本机 Worker 只读检查耗时 0.212 秒，status=failed、exit 2。共享 data 目录/最近祖先访问权限和临时 IPv4 loopback bind 可用；control.sqlite3 不存在、SQLite 实际 journal/锁/附加库事务写能力 deferred。缺 admin token、host/container mounts 配置、Hermes CLI、HERMES_PYTHON 和 `$HOME/.hermes/skills` 完整安装副本。没有实际 Hermes 版本或 SDK import/signature 输出，不把 fixture SDK 返回当作 installed SDK。

读取原根 `.env` 的许可配置名并传入本机诊断进程，未改文件、未输出密钥：后端有效 model 名 kimi-k2.6，temperature=1，base URL `https://api.moonshot.cn`，key 仅记 present=true；首轮预检未执行模型网络连接，随后单次真实传输验证见本文继续验收记录。Hermes 模型/provider 未配置。权威源完整闭包在本地只读校验通过，不证明 installed/frozen resolver 已在实际宿主执行。原始脱敏记录在 `.tmp/task16-local-precheck.json`，规则只读校验在 `.tmp/task16-rules-check.log`。

目标宿主 lxc@81.70.231.92，项目 `/home/lxc/chip-recommend`，预期 CLI `/home/lxc/hermes-agent/venv/bin/hermes`。控制代理在此前恢复回合只读 BatchMode/ConnectTimeout8 的版本检查遭遇 **Host key verification failed**，未执行目标 CLI；本 Task16 没有再次 SSH、绕过主机验证或改变服务。连接别名/可信配置仍待提供。远程 SDK 版本/signature、20 文件真实路径/hash、隔离 cwd/home、插件源/schema、每运行 authenticated `/v1/health/<run_id>`、shared volume、运行目录实际写能力预检均**未执行**。

官方能力参考入口：[CLI commands](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md)、[Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)、[Tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/)。这些文档不能替代实际安装能力探针。新版 safe-mode/ignore-rules 的影响与当前 adapter 的隔离方式见 runbook，不自行追加 flags 改变生产 runner。

实际 Skill 审计的逐域查询与事件回溯方法见 [runbook 的只读 Skill 使用审计](../open_web_test_runbook.md#只读查询实际-skill-使用审计)：`recorded_audit_only` 只表示已保存观察，不升级为真实 SDK 验证；缺 loaded/executed 保留未知。

取得可信连接且经授权核对具体环境后，按 [runbook](../open_web_test_runbook.md) 保存启动键，顺序执行六个无 chip 域；响应丢失同键重试、显式 resume 已有 run_id，不重置来源/预算/规则/修订次数。每域仅 success 或诊断完整的 no_findings 能通过该次网络检查；partial/failed 定位并重验，interrupted 使用原 ID 恢复。真实候选每域最多五个（不足五个全部）、对应拒绝/仅来源变化/新实体/条件未解析及 UI 引用都**未抽查**。真实网络/模型耗时、调用数和覆盖统计为**未测**，不能用候选数量充当准确率。

## 正式库只读核验

使用 `scripts.probe_open_web_search.business_digest`，mode=ro SQLite backup 到内存，包含已提交 WAL，busy/locked backup 默认五秒 deadline；全表/列/行规范化后计算稳定摘要，未读取主文件页 hash 来代替业务内容。本机 worktree 来源库与原根来源库当前一致；最终九探针前后摘要也一致：

`13d142997b4e229825e66441ae4ad9864c0d5923bb75cb4b2c2f192b78a466bf`

| 正式表 | 当前只读行数 |
|---|---:|
| chips | 1093 |
| models | 1370 |
| chip_model_benchmarks | 4237 |
| chip_model_compatibility | 1541 |
| deployment_guides | 46 |
| field_provenance | 41819 |

完整列清单、逐表 hash 和诊断前后摘要保留在上述 JSON。这个“通过”只覆盖本地只读检查/搜索探针区间，没有实际 Hermes 运行，因此实际六域前后 formal_business_writes/formal_provenance_writes=0 仍**未验证**。fixture 测试核验六表全部内容且覆盖 WAL 与外部 writer 干扰；如未来真实窗口出现外部写入，记录干扰并静默重做，不自动停止调度或归咎本链路。

## 自动化验证的实际结果

| 执行范围 | 实际输出/证据 |
|---|---|
| Task16 第一组 TDD RED | `.tmp/task16-probe-red.log`：7 failed/0.37s（模块缺失断言）；前期 import collection 错误不计 RED 完成 |
| Task16 初次 GREEN + 相关搜索回归 | `.tmp/task16-probe-green.log`：85 passed/24.74s，含 probe/domain_search/search_provider_integrity |
| Task16 代理路线 RED | `.tmp/task16-probe-route-red.log`：1 failed/7 passed/0.43s，实际发现 trust_env 被修改 |
| Task16 备份 deadline RED | `.tmp/task16-digest-deadline-red.log`：1 failed/8 passed/0.48s（尚无有界 backup 参数）；添加 progress/monotonic deadline 后 GREEN |
| Task16 首版本窄 GREEN | `.tmp/task16-probe-final.log`：9 passed/1.13s；默认 session 路线不修改、环境/显式代理优先与脱敏、逐源不掩盖失败、健康空/解析/网络、参数拒绝、WAL 全表摘要/无源写入、exclusive lock 的 backup deadline |
| Task16 输出硬链接别名 RED→GREEN | `.tmp/task16-output-alias-red.log`：2 failed/9 passed/1.14s；`.tmp/task16-output-alias-green.log`：11 passed/1.10s。普通源与 symlink 源指向 output.json 同 inode 时，在 provider 调用前拒绝，源 bytes/六表逻辑摘要不变；修复提交 `134920d1c60d5aaeaa7a00fbbb1b3a092f7d49ba` |
| Task16 代理格式脱敏 RED→GREEN | `.tmp/task16-schemeless-proxy-red.log`：9 failed/11 passed/1.48s；`.tmp/task16-schemeless-proxy-green.log`：20 passed/1.46s。显式与 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 的 scheme-less userinfo、错误文本在 stdout/JSON 都脱敏，正常 HTTP 凭证回归通过；仅修改审计副本，实际 transport 参数不变，不改公共业务 redact |
| Task16 QUALITY bypass/非法代理最终窄 GREEN | `.tmp/task16-proxy-bypass-red.log`：2 failed/20 passed/1.61s；`.tmp/task16-proxy-bypass-green.log`：22 passed/1.53s。合法 NO_PROXY IPv6 bypass 列表保留为列表而非 URL；非法代理在 provider 前返回脱敏 failed JSON/exit2，解析错误处理不二次抛异常，原始 URL 与裸凭证安全掩码；无网络/全套重跑，等待 QUALITY 复审 |
| Task15 六域/恢复相关 | 94 combined passed/160.33s + final 9 passed/24.81s，提交 `fb55788df6e6864eb1f41517b5fb4c468648c087`（既有执行交接记录） |
| Task15 唯一全套 pytest | **3 failed / 1304 passed / 662.82s，exit 1**，执行交接记录名 `task15-full.log`；未重复全套，不称 full GREEN |
| 三个全套失败闭环 | 两个旧 orchestrator 测试要求已禁止的全库/单字段子进程伪成功，一个旧 API 测试期待 422 而 Raw JSON 合同明确 400；修改预期并加强 0 runner/0 queue/六表不变：`task15-full-failure-closure.log` 13 passed/7.16s，PDF 2 passed/7.31s |
| Task15 独立 SPEC/QUALITY | SPEC 15 passed/56.53s + 5 passed/10.53s；QUALITY 13 passed/14.55s + 320 形状/跨域/脱敏/不可变 probes；控制代理历史/旧入口/模型家族 61 passed/15.49s（既有交接记录） |

Task15 日志名称与结果来自控制代理执行交接，原始日志不在本 Task16 worktree 的 `.tmp` 内，不声称已独立重新读取或重跑。当前新增探针回归与本机网络/预检日志在上述工作树忽略目录实际存在。用户未提交的温度实现、runbook 末尾五行配置说明和 `tests/test_open_web_temperature.py` 保持原样，未纳入 Task16 提交。

默认 `data/domain_runs/` 已加入 `.gitignore`，`git check-ignore --no-index` 验证 control.sqlite3 与示例 run_id 中的 data.db/manifest.json/source_snapshots/raw.html 都命中该规则。没有忽略整个 data、删除或取消跟踪既有数据；自定义共享根仍由部署侧保护，不整体加入 Git。

Task16 本地交付已通过独立 **SPEC→QUALITY** 及最终集成审查，代码审查绑定 `39761a956a3ca049001109690f66c89a1ff2e95e`。QUALITY 独立窄回归为 **22 passed / 1.44s**，并复验 NO_PROXY、畸形代理、无 scheme 凭据、输出别名和只读边界；最终集成审查静态确认 Task1–15 核心未变。本地批准仅覆盖实现和交付材料，状态仍为 **DONE_WITH_CONCERNS**：真实预检、六域网络执行和真实抽查步骤保持未完成，整体验收未通过。下一步依赖可信宿主连接与实际环境能力；没有正式发布、heartbeat、远程服务启用或业务库写入授权扩展。

## 2026-10-10 继续验收记录

用户同意继续真实搜索和 Hermes 验证后，本机补充八次单变量 HTTP 诊断，时间为北京时间 08:14:29–08:14:43：

- 360 实际摘要位于 `p.res-desc`，原解析器只匹配 `.res-list-summary`，六条结果摘要全部丢失。提交 `d75b81d3d2aa8ed7db46790298b3849a317e13a7` 兼容两个布局；RED 真实布局失败，GREEN 搜索及解析回归 **82 passed / 25.41s**。独立 SPEC 和 QUALITY 通过，QUALITY 四组回归及真实归档离线回放均通过；标题、URL、排名和状态不变，摘要不被当作已验证事实。再执行一个真实 360 请求，HTTP 200、六条结果全部有摘要，耗时 0.452 秒。
- Bing 搜索框、标题和 organic 结果区域都保存完整多词查询；`H100 datasheet` 命中 NVIDIA H100，但 `L40S specifications` 返回不相关的神话/域名页面。`+` 与 `%20` 对照不足以确定编码根因，不能认定客户端丢词、代理缓存或服务器改写。DDG HTTP 202 确认是图片挑战页。相关性和可访问性限制继续保留。
- 生产 `OpenAIModelTransport` 共执行三次小型真实请求，不运行字段提取或 Hermes。前两次温度 1.0 返回 HTTP 400，服务明确要求本模型只能使用 0.6；独立进程临时使用 0.6 后返回 `ready=true` 有效 JSON，31 输入/8 输出 token、耗时 0.838 秒。原 `.env`、用户温度代码和配置说明未改。这个通过仅覆盖后端连接/JSON 传输，完整提取、独立复核和 Hermes 代理模型通路仍未执行。
- SSH 日志确认 TCP 已建立，目标提供 ED25519 指纹 `SHA256:xLhTQ7LZpP5GthOdNO8Zfrw/Fvyk8F8pnDi64G+24g4`，但本机没有匹配的可信记录；这是观测指纹，尚未确认信任。未进入远程认证/版本检查，未修改 known_hosts，未部署。真实宿主机六域运行和候选抽查仍未执行，整体验收仍未通过。

脱敏材料在忽略目录：`.tmp/task16-relevance-diagnosis.json`、`task16-relevance-report.md`、`task17-360-snippet-{red,green,offline}.log`、`task16-360-postfix-live.json`、`runtime-model-preflight.json`、`runtime-model-preflight-temperature06.json`、`ssh-runtime-verbose-preflight.json`。360 单次复验的 UTF-8 JSON 已保存，临时命令打印混合 Unicode 时出现 GBK 编码错误，随后离线读取 JSON 核实结果，没有重复该 HTTP 请求。诊断 HTML hash 已分别标明 Windows 写入前脱敏文本、实际归档字节与解码/换行口径；这些是诊断归档，不是 PageService 不可变原快照证明。


### SSH 恢复后的真实宿主预检

用户提供目标登录信息并授权连接后，已成功以 ubuntu 登录目标；会话校验先前观测的主机指纹，不修改 known_hosts。只读 sudo 可用。安装包元数据为 hermes-agent 0.19.0；hermes-gateway、hermes-manual-run-dispatcher、hermes-webui 三个服务均 active/running。Gateway 使用 /usr/local/lib/hermes-agent/venv/bin/python，工作目录 /root/.hermes，配置模型为 kimi-k2.6、provider 为 custom:moonshot-platform-cn。Skills 位于 /root/.hermes/skills，共 25 个 SKILL.md；目录枚举不能证明实际运行使用了某个 Skill。

现有 chip-recommend 容器仍使用 chip-recommend:hermes-skill-first-73b4c38 镜像，数据挂载 /home/lxc/chip-recommend/data → /app/data，宿主端口 5340。本轮新实现尚未部署到该服务。

真实 SDK 源码签名核验发现硬阻塞：AIAgent.__init__ 不支持当前适配器要求并传入的 skip_background_review、run_budget_seconds、cwd。当前预检会拒绝该 SDK；仅删除预检要求仍会在实例化时失败。必须先审查该版本的等效隔离与预算机制，再适配并回归验证，不能将删除隔离检查当作通过。已经派发独立静态兼容审查。

凭据未写入文件、未输出，未读取或导出远程模型密钥；未修改远程服务、Skills、业务数据库或镜像。证据位于忽略目录 .tmp/inspect-hermes-host.json、.tmp/host-runtime-preflight.json、.tmp/host-sdk-source.json。真实六域运行、候选人工抽查及其业务库零写入证明仍未完成；整体验收未通过。前一段 SSH 未认证记录描述的是提供登录信息之前的状态，当前连接阻塞已解除。

### 2026-10-10 隔离新镜像端到端验收

按用户确认，本次只验收开放发现、字段/来源候选生成链路、界面和手动运行；正式业务表与 `field_provenance` 不写入，heartbeat 和正式服务不启用。使用 Hermes 0.19.0 的隔离容器镜像 `chip-recommend:hermes-acceptance-final6`，仅将独立验收目录挂到容器 `/app/data`，宿主端口仅绑定 loopback `127.0.0.1:18473`；正式 `chip-recommend` 容器、端口 5340 和业务数据库未触碰。镜像构建归档 SHA256：`599128419409CDEB54AD5F9FC216CACA19A4D40BB63CA98DF3ED4D66921D2607`；实际加载 Skill bundle SHA256：`380d76070aa906d33ec9dda8e0666d98b6275144d541f9291774dd2001bfa0d3`。

| 域 / run | 搜索与选择 | 页面 / 抽取 | 结果 |
|---|---|---|---|
| identity / `run-f1293ce6844545d18c4a76b9e4c0d8fe` | 45 个去重候选全部访问并完成模型判定，全部拒绝；其中 19 页访问失败（主要为 403） | 无候选进入提取 | `partial`；没有字段候选、独立复核或来源候选 |
| specs / `run-3b2d4818b74c41c8a02c3ead2a7cac18` | 25 个去重候选全部访问并完成判定；选择 1 个页面，12 个页面成功、13 个失败 | 3 个关联域提取任务完成，事实列表均为空；3 个复核任务因此未执行 | `partial`；没有字段候选、独立复核或来源候选 |

验收目录中的 run artifacts 显示两次实际调用都成功走过 Hermes 搜索词生成、搜索工具、URL 去重/访问、页面判断与 finalization；specs 还走过关联域提取任务。搜索词本身仍出现过固定年份、宽泛词或低相关词；identity 结果包含词典、泛 AI 页面和消费级产品页面，specs 误选消费级 GPU 价格页。新 identity Skill 能拒绝消费级游戏卡，但搜索相关性和页面可访问性仍不足以产出有效事实。UI/API 与人工运行入口可用，然而本轮未得到可供人工核验的字段/来源候选，因此**端到端验收未通过**，不能据此启用六域正式更新或 heartbeat。

正式写入边界核对：隔离容器的数据卷仅指向 `.acceptance/hermes-v3-20261010-426405d/data`；运行结果记录 `formal_database_modified=false`、`formal_business_writes=0`、`formal_provenance_writes=0`。本轮没有调用 Publisher、没有修改正式数据库、没有启用 heartbeat，也没有替换或重启正式服务。隔离镜像继续运行于远程主机 loopback `127.0.0.1:18473` 供验收查看；完成复核前不应提升为正式镜像。
