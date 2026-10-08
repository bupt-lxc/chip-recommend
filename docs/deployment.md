# 当前线上部署

更新时间：2026-10-08

## 2026-10-08 搜索源异常回退修复

- 主站容器仍使用 `chip-recommend:hermes-skill-first-73b4c38`；工具容器 `chip-recommend-open-web-tools` 更新为 `chip-recommend:search-fix-20261008`，继续绑定 `127.0.0.1:5341`。
- 搜索工具识别“非空但整批不包含明确目标型号”的异常，继续备用源，并遵守搜索词的 `site:` / `-site:` 限定。开放发现和 Hermes 页面价值判断保持原合同。
- 发布目录：`/home/lxc/chip-recommend/releases/20261008-search-relevance-fix`。工具回滚容器：`chip-recommend-open-web-tools-previous-20261008-search-fix`（已停止）。
- PR 提交前完整回归 325 项通过；追加的连字符型号匹配修复尚未重新发布到工具容器。实际已部署版本的 Hermes/Kimi 会话 `20261008-124757-74b34351` 完成 10 条搜索词、4 个候选访问、3 个官方页面选择，并取得 3 条显存 80 GB 的目标字段来源记录。
- 含关联 Skill，共 21 条候选记录通过现有校验；这不是 21 项独立参数，语义质量仍有待核验。9 条搜索词未获有效批次，搜索覆盖仍有限。
- 正式数据库主体哈希前后一致；详情、运行环境修正和后续问题见 [搜索修复记录](search_provider_fix_20261008.md)。

## 2026-09-30 Hermes Skill-first 全芯片编排 v2

- 当前发布目录：`/home/lxc/chip-recommend/releases/20260930-hermes-skill-first-73b4c38`
- 当前镜像：`chip-recommend:hermes-skill-first-73b4c38`
- 主站回滚容器：`chip-recommend-previous-20260930-1035`（已停止）
- 工具服务回滚容器：`chip-recommend-open-web-tools-previous-20260930-1035`（已停止）
- 数据库快照：`/home/lxc/chip-recommend/backups/pre-hermes-skill-first-73b4c38.db`
- 六类中文 Skill 与 `aishperf-open-web` Hermes 插件升级到 `2.0.0`。Skill 规定完整业务步骤、判定标准和 JSON 合同；插件工具只负责搜索、访问和结果落盘。
- 后端支持“指定单芯片”和“全部芯片 + 开放发现”两种范围。全量运行先冻结正式库芯片清单，再逐芯片建立独立单元；新发现芯片只进入候选资产，不动态扩大本轮队列。
- 每个运行单元严格执行：10 组搜索词 → 搜索与去重 → 全候选网页预览 → Hermes 选择 → 字段提取与分类联动；失败只重试当前单元，并支持按父运行 ID 续跑。
- 前端状态页可查看父运行总体进度、逐芯片单元、搜索/访问/选择计数、新芯片候选和系统规范化后的关联 Skill；网页仍不提供启动入口。
- 本地完整回归 `317 passed`；写 Skill 时先执行未读新 Skill 的 RED 基线，再用正式 Skill 完成 GREEN 场景，验证了可选芯片、全量范围、精确工具合同、零候选和禁止动态扩容等规则。
- 线上冒烟会话 `20260930-103344-228dae98` 已实际跑通 Hermes → `chip-specs` → 搜索工具 → 80 个候选预览 → 模型复核 → JSON 提交。Bing 在当前服务器返回无关内容，模型正确拒绝全部候选，因此本次为 `partial`，未提取字段；这是搜索提供器质量问题，不是 Skill/工具调用失败。
- 部署前后正式数据库主体 SHA-256 均为 `b77e2af7244469dcc38bf08b9effb5d473bd57796fe569521d91a8b9c542534f`，正式业务数据未变化。

## 2026-09-28 开放互联网分类 Skill v1.2

- 当前发布目录：`/home/lxc/chip-recommend/releases/20260928-222335-skill-v1.2`
- 当前镜像：`chip-recommend:skill-v1.2-20260928-222335`
- 回滚容器：`chip-recommend-previous-20260928-222335`（已停止）
- 数据库快照：`/home/lxc/chip-recommend/data/backups/20260928-222335-skill-v1.2/data.db`（`quick_check=ok`）
- 六类开放互联网 Skill 已升级到 `1.2.0`：芯片型号、硬件规格、算力指标、兼容信息、实测数据、部署资料。
- Skill 现已明确输入、字段范围、粗筛与正文复核、网页快照与逐字证据、输出留痕、常见错误和完成标准；同一网页可记录多个信息类别。
- 发布仅覆盖六个 Skill 及 `open_web_test.py` 中的版本注册表，没有带入工作区其他未完成改动，也没有启动抓取任务。
- 本地专项测试 `18 passed`、完整回归 `244 passed`；隔离 canary、运行历史 API、双 Skill 目录、线上 SQLite `quick_check` 和公网入口均通过。
- 部署前后正式数据库 SHA-256 均为 `b77e2af7244469dcc38bf08b9effb5d473bd57796fe569521d91a8b9c542534f`，正式数据未变化。

## 2026-09-23 开放互联网测试链路 v2

- 当前发布目录：`/home/lxc/chip-recommend/releases/20260923-165351-open-web-v2`
- 当前镜像：`chip-recommend:open-web-v2-20260923-165351`
- 回滚容器：`chip-recommend-previous-20260923-165351`（已停止）
- 数据库快照：`/home/lxc/chip-recommend/data/backups/20260923-165351/data.db`（`quick_check=ok`）
- 六类 Skill 更新至 `1.1.0`：先确定目标字段，再按类别关键词、来源类型和目标字段生成可审计的搜索计划。
- `url_assets.jsonl` 现在记录所有实际访问 URL；包含发现搜索词、搜索策略、提供商、排名、域名、网页形态、访问状态、网页镜像、复核结论、一个或多个信息类别及提取字段。
- 后端脚本和受保护接口支持传入 `target_fields`，并拒绝跨类别字段；状态页仍无启动入口，只在历史详情中展示 URL 资产结果。
- 本地完整回归 `177 passed`，六个 Skill 规范校验、服务器 canary、SQLite `quick_check`、80 端口公网健康检查和运行历史接口均通过。
- 本次部署没有启动真实互联网抓取任务，也没有写入新的正式业务数据。

## 2026-09-23 测试统一由后端启动

- 当前发布目录：`/home/lxc/chip-recommend/releases/20260923-162923-status-readonly`
- 当前镜像：`chip-recommend:status-readonly-20260923-162923`
- 回滚容器：`chip-recommend-previous-20260923-162923`（已停止）
- 数据库快照：`/home/lxc/chip-recommend/data/backups/20260923-162923/data.db`
- 状态页删除信息类别、芯片、搜索词、口令和“开始测试”等启动控件，只保留运行结果、历史记录和“刷新记录”。
- 测试脚本与后端接口继续保留；开发测试只在收到明确测试请求后从服务器后端执行，页面访问不会触发任务。
- 本地完整回归 `173 passed`，服务器 canary、正式健康检查、运行记录接口及 80 端口公网状态页均验证通过。

## 2026-09-22 开放互联网分类 Skill

- 当前发布目录：`/home/lxc/chip-recommend/releases/20260922-200621-open-web-skills`
- 当前镜像：`chip-recommend:open-web-skills-20260922-200621`
- 回滚容器：`chip-recommend-previous-20260922-200621`（已停止）
- 数据库快照：`/home/lxc/chip-recommend/data/backups/20260922-200621/data.db`（`quick_check=ok`）
- 页面可以选择芯片型号、基础参数、算力指标、兼容信息、实测数据和部署资料；每类使用独立搜索词、粗筛线索、字段白名单和目标数据表。
- 新增六个中文 Skill，并同时放入镜像内 `.agents/skills` 与 `.claude/skills`。
- 粗筛不再因为只提到芯片名称就通过；指定芯片时还必须出现当前信息类别的线索。
- 精确复核通过的页面写入本次运行的 `url_assets.jsonl`，记录搜索词、信息类别、芯片、内容哈希和判定原因。
- 本地全量测试 `173 passed`；隔离 canary、公网健康检查、状态页分类选择和正式数据库 `quick_check` 均通过。
- 线上 `chip-benchmark` 小规模测试搜索 15 条、粗筛并访问 1 条，正式数据库主体哈希未变化；模型网关仍返回 HTTP 401，因此尚未完成语义复核和字段提取。

## 2026-09-22 数据更新页面精简

- 发布目录：`/home/lxc/chip-recommend/releases/20260922-161708-ui`
- 镜像：`chip-recommend:ui-compact-20260922-161708`
- 当时的回滚容器：`chip-recommend-previous-20260922-161708`（已停止）
- 删除页面中重复出现的测试隔离提示，不再占用首屏空间。
- 单次运行默认只展示“字段提取与校验”和“详细记录”两个层级；运行阶段、参数、URL、网页访问、Agent 任务和时间线统一收进“详细记录”。
- 开放互联网运行摘要由四项统计缩减为“网页访问、有效字段”两项；旧任务统一收进“历史运行”。
- 本地全量测试 `168 passed`，服务器 canary、正式健康检查、公网状态页和 SQLite `quick_check` 均通过。

## 2026-09-22 开放互联网隔离测试

- 上一发布目录：`/home/lxc/chip-recommend/releases/20260922-131053`
- 上一镜像：`chip-recommend:open-web-test-20260922-131053-final`
- 发布前数据库快照：`/home/lxc/chip-recommend/backups/20260922-131053/data.db`（`quick_check=ok`）
- 新增“基础参数”开放互联网测试：Bing 与 360 搜索 → 粗筛 → 安全访问 → 网页快照 → 大模型复核与字段提取。
- 全部结果只写 `data/test_runs/<session_id>/` 与副本中的 `test_*` 表；不会调用正式候选发布器，也不会更新正式业务表。
- 页面字段已简化为“要查的芯片、补充搜索词、开始测试、找到的信息、访问过的网页、执行记录”。
- 手动开放互联网测试由 API 后台任务直接执行，不依赖 Hermes 心跳或旧的主机调度脚本；正式巡检仍保持关闭。
- 线上实测会话 `20260922-053543-37cce7ca` 已完成搜索和官方 H100 页面访问，正式数据库主体哈希前后一致。
- 当前 GLM 网关凭据调用 `glm-5.3` 返回 HTTP 401；替换服务器私密文件 `config/data-agent.env` 中的有效凭据后即可继续模型提取。密钥不写入代码、镜像和文档。
- 当前公网仅提供 HTTP。管理接口继续要求 HTTPS 或本机/SSH 隧道，因此公网页面会禁用“开始测试”按钮，避免管理员口令明文传输。

2026-09-17 已部署独立运行卡片、运行历史和字段对比，详见 `docs/deploy_run_cards_20260917.md`。用户确认使用 HTTP。

## 访问入口

- 主要平台首页：http://81.70.231.92/
- 算力推荐：http://81.70.231.92/recommend
- 芯片搜索：http://81.70.231.92/chips
- 模型搜索：http://81.70.231.92/models
- 系统状态：http://81.70.231.92/status
- 健康检查：http://81.70.231.92/api/v1/health

主要平台通过现有 Nginx 的 80 端口转发到本机容器；Hermes WebUI 在服务器内部监听 `8787`，但公网安全组尚未开放该端口。

## 81.70.231.92 主服务器

- SSH：`lxc@81.70.231.92`
- 项目根目录：`/home/lxc/chip-recommend`
- 当前发布目录：`/home/lxc/chip-recommend/releases/20260930-hermes-skill-first-73b4c38`
- 持久化数据库：`/home/lxc/chip-recommend/data/data.db`
- Docker 容器：`chip-recommend`
- Docker 镜像：`chip-recommend:hermes-skill-first-73b4c38`
- 容器端口：`0.0.0.0:5340 -> 8000/tcp`
- 重启策略：`unless-stopped`
- Hermes：`hermes-gateway`、`hermes-webui`、`hermes-manual-run-dispatcher` 均在本机运行并开机自启

2026-09-15 已录入评测表中可核对模型、芯片、物理卡数与 1024 输入/1024 输出/并发 1 的三条 DeepSeek-V4-Flash 推理记录：沐曦 C550、昆仑芯 P800 (OAM)、海光 BW1000。每条记录写入字段级来源，并保留工作表、单元格、量化精度和部署方式；未能核实映射的其他表格行暂不导入。Flash 推理实测项权重为 60%，已实测芯片优先；其他模型继续使用常规 40% 生态、30% 实测验证、20% 算力、10% 性价比权重。17:32 先发布到镜像服务器；17:59 又同步到主要服务器 `61.172.167.201`，两台服务器公网接口均验证三条实测生效、其他模型维持常规排序。

## 自动数据自检

- 当前模式：正式巡检与开发测试 Cron 均禁用；网页不提供任务启动入口，隔离测试只从服务器后端按需执行
- 恢复正式巡检后的计划：北京时间 `02:00` 开启当天首轮；未完成周期每 30 分钟继续检查并续跑，完成后当天静默
- 调度方：`81.70.231.92` 上的 Hermes Gateway（开机自启）
- Hermes 任务：`AISHPerf 数据抓取智能体`（ID `268fc87564c3`）
- 调用方式：Hermes 预检查与 Worker 在 `81.70.231.92` 本机通过 `docker exec chip-recommend` 调用固定数据智能体入口，不依赖旧服务器
- 重叠保护：未完成周期优先续跑；北京时间 02:00 前及当天已开过周期时不新建周期；上次中断超过 30 分钟的第二轮抓取会重新排队
- 运行记录：主要服务器持久化数据库的 `update_runs`、`update_run_events` 和任务表
- 自动发布备份：主要服务器容器持久化目录 `data/backups`
- 页面入口：http://81.70.231.92/status

无变化时门控保持安静；发现页面变化或来源连续失败达到阈值时才唤醒 Agent。对于能够从官方新增原文中唯一确认的芯片字段变化，Agent 会调用受限发布器，在影子库完成溯源和现有功能测试、备份正式库后自动更新；证据不足、型号不唯一或字段不在白名单时直接拒绝。执行时间、抓取链接、新旧值、备份及业务影响可在“系统状态 & 数据更新”页面和 Hermes 运行记录中查看。

### 完整数据抓取智能体

完整调度器和以下受限入口已经部署；当前 Hermes 使用主服务器本机 Docker 桥接，远程入口只作为明确配置的镜像或回滚工具：

- `run_data_agent.sh`：执行一次确定性周期；
- `run_data_agent_claim.sh`：领取一批语义任务；
- `run_data_agent_snapshot.sh`：分页读取受限目录内的证据快照；
- `run_data_agent_finish.sh`：通过标准输入返回 Agent 结果；
- `run_candidate_publish.sh`：通过标准输入发布已经校验的候选事实。

远程入口由 `run_hermes_remote_command.sh` 的 ForceCommand 白名单限制；本机桥接只允许 `chip-recommend` 容器和固定脚本。种子页/列表页先提取实际链接，`url-discovery` 只从该清单中筛选目标 URL；服务端完成规范化、去重、安全校验后，在同一周期执行详情页第二轮访问，再生成领域提取任务。状态页会展示父 URL、目标 URL、深度和二轮抓取结果。

语义任务每批最多 5 条；重复 claim 会幂等返回原批次，快照每次最多读取 64KB。正式巡检恢复后，未完成周期会在下一次心跳优先续跑并阻止重叠周期。

## 已退出调度的旧服务器（61.172.167.201，历史记录）

- 平台入口：http://61.172.167.201:5340/
- 发布目录：`/root/chip-recommend/releases/20260915-224150`
- 持久化数据库：`/root/chip-recommend/data/data.db`
- Docker 镜像：`chip-recommend:codex-20260915-224150`
- 本次数据库备份：`/root/chip-recommend/backups/20260915-224150/data.db`（SQLite 在线快照，`quick_check=ok`）
- 回滚容器：`chip-recommend-previous-20260915-224150`（已停止；上一镜像为 `chip-recommend:codex-20260915-223614`）
- 数据更新迁移前数据库快照：`/root/chip-recommend/backups/20260915-182048/data.db`（`quick_check=ok`）
- Hermes 配置回滚目录：`/root/.hermes/backups/primary-migration-20260915-182342`
- Hermes 正式任务：`268fc87564c3`，每 30 分钟检查；门控确保每天北京时间 02:00 后只开启一个新周期，未完成则续跑；门控和 Worker 均指向本机 `chip-recommend` 容器
- Hermes Skill 备份：`/root/.hermes/backups/20260909-190238`

两台平台使用各自的持久化数据库，Flash 实测记录分别幂等导入并有字段级来源。2026-09-15 18:23 起，Hermes 每日任务改为在主要服务器本机调用数据智能体；镜像服务器历史数据不自动合并，旧来源直写通道已禁用。迁移后预检查验证续跑主要服务器现有周期 `run_id=1`，只读 Worker 验证能读取主要服务器的真实快照。主要服务器的未完成任务会先处理完，之后才开启新周期。

## Nginx

项目使用服务器原有的 `awards-nginx` 容器，通过 IP 专属虚拟主机转发；原域名站点配置保持不变。

- 配置源文件：`/home/lxc/awards/backend/nginx.conf`
- 上线前备份：`/home/lxc/awards/backend/nginx.conf.codex-backup-20260828-082611`
- 转发目标：`http://172.18.0.1:5340`

## 常用检查

```bash
docker ps --filter name=chip-recommend
docker logs --tail 100 chip-recommend
curl -fsS http://127.0.0.1:5340/api/v1/health
```

文档不保存 SSH 私钥内容或服务器密码。
