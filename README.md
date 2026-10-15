# AISHPerf — AI 芯片 × 模型知识图谱

AISHPerf 将芯片规格、模型元数据、兼容关系、训练与推理实测及字段来源汇集到 SQLite，提供 Web 界面、FastAPI 接口和命令行工具，支持芯片查询、模型画像与算力选型。

## 主要能力

- **芯片与模型检索**：按厂商、地区、用途、显存等条件筛选芯片，查询模型参数、架构及兼容芯片；支持单条和批量画像。
- **算力推荐**：根据模型和训练、推理等场景估算显存与卡数，结合生态、实测、算力和性价比评分，支持国产偏好、厂商偏好及卡数、价格约束。
- **实测与来源追溯**：查询芯片 × 模型的测试条件、吞吐和延迟，通过 `field_provenance` 查看字段来源、置信度及新旧值。
- **数据维护**：提供来源链接库、抓取与增量更新流水线、候选校验和发布，以及 Hugging Face 模型元数据同步。
- **开放互联网隔离测试**：由 Hermes 执行六个数据域的 Skill，保留搜索、网页快照、提取、独立审核与比较记录；管理员提交运行请求，由宿主机 Worker 执行。

## 快速开始

需要 **Python 3.11 或以上版本**。以下命令均在仓库根目录执行。

### 1. 安装依赖

```bash
git clone https://github.com/bupt-lxc/chip-recommend.git
cd chip-recommend
python -m venv .venv
```

激活虚拟环境：

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

```bash
python -m pip install -r requirements.txt
python -m pip install pytest jsonschema
```

使用 uv 时，也可通过 `uv venv` 创建环境，再执行 `uv pip install -r requirements.txt pytest jsonschema`。`requirements.txt` 包含工作簿导入与 PDF 提取所需的附加依赖，`jsonschema` 用于六域合同验证测试。

### 2. 准备数据库

默认路径为 `data/data.db`。该运行数据库被 Git 忽略，**克隆仓库不会获得当前全量数据**。已有数据库时，将其放到默认路径，或通过下文的 `DATA_DB_PATH` 指向它。

首次体验可先创建空库，验证服务和 CLI：

```bash
python -c "from chip_model.database import init_db; init_db()"
python scripts/run_cli.py db status
```

空库的搜索结果为空。需要初始数据时，在空库上执行播种脚本；这一步会访问互联网，耗时和结果取决于来源可用性：

```bash
python scripts/run_seed.py
```

播种脚本使用仓库内的来源 CSV 和 Hugging Face API，并固定写入仓库下的 `data/data.db`；它不读取 `DATA_DB_PATH`。脚本目前使用本机代理 `http://127.0.0.1:7897`，运行前需确保代理可用；其他网络环境需调整脚本中的代理配置。播种只提供初始数据，不等同于恢复全量数据库。重复播种前请检查已有记录；`--reset` 会清空五张核心表的数据。

### 3. 启动 Web 服务

```bash
python scripts/run_server.py
```

- Web 界面：[http://localhost:8000](http://localhost:8000)
- Scalar API 文档：[http://localhost:8000/docs](http://localhost:8000/docs)
- OpenAPI 定义：[http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
- 健康检查：[http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)

前端包含芯片搜索、模型搜索、算力推荐、推荐方法、显存计算说明、兼容性和系统状态等页面。无需模型 API 密钥即可使用基础查询；对话选型需另外配置 DeepSeek。

## 配置

| 配置项 | 用途 | 默认值 |
|---|---|---|
| `DATA_DB_PATH` | Web 服务和 CLI 使用的 SQLite 路径 | 仓库下的 `data/data.db` |
| `HOST` | `scripts/run_server.py` 监听地址 | `0.0.0.0` |
| `PORT` | `scripts/run_server.py` 监听端口 | `8000` |
| CLI `--db-path` | 为本次 CLI 调用指定数据库，放在子命令之前 | 使用默认数据库路径 |
| `DEEPSEEK_API_KEY` | 可选，对话选型 API 密钥 | 无 |
| `DEEPSEEK_BASE_URL` | 可选，对话 API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 可选，对话模型 | `deepseek-chat` |

例如，在 PowerShell 中指定数据库和端口：

```powershell
$env:DATA_DB_PATH = "D:\aishperf-data\data.db"
$env:HOST = "127.0.0.1"
$env:PORT = "8080"
python scripts/run_server.py
```

Linux / macOS：

```bash
DATA_DB_PATH=/path/to/data.db HOST=127.0.0.1 PORT=8080 python scripts/run_server.py
```

对话模块会读取项目根目录的 `.env`；服务监听地址、端口及独立脚本所需变量应在启动前设置。密钥保存在本地环境中，`.env` 已被 Git 忽略。

## 命令行使用

CLI 通过 `scripts/run_cli.py` 运行，查询结果输出 JSON，支持中文内容。使用 `--help` 查看当前命令和参数：

```bash
python scripts/run_cli.py --help
python scripts/run_cli.py chip recommend --help
```

```bash
# 芯片筛选
python scripts/run_cli.py chip search --search H100 --vram-min 64
python scripts/run_cli.py chip search --region domestic --usage inference

# 芯片画像：名称或 ID，支持一次传入多个
python scripts/run_cli.py chip profile "H100 SXM5 80 GB"

# 模型查询与推理推荐（需要库中已有匹配模型）
python scripts/run_cli.py model search --search Qwen
python scripts/run_cli.py chip recommend --model Qwen2.5-7B --scenario inference --max-cards 8

# 实测、兼容性与字段来源
python scripts/run_cli.py benchmark search --chip H100 --workload inference
python scripts/run_cli.py compat search --chip H100
python scripts/run_cli.py provenance show --table chips --field vram_gb
python scripts/run_cli.py provenance stats

# 来源链接与数据库概览
python scripts/run_cli.py link search --vendor NVIDIA
python scripts/run_cli.py --db-path data/data.db db status
```

| 命令组 | 主要子命令 |
|---|---|
| `chip` | `search`、`profile`、`recommend`、`add`、`update`、`delete` |
| `model` | `search`、`profile`、`add`、`update` |
| `benchmark` / `compat` | `search`、`add` |
| `provenance` | `show`、`stats` |
| `link` | `search`、`add`、`import-csv`、`export-csv`、`auto-discover` |
| `db` | `status` |
| `config` | `show`、`set` |

CLI 配置保存在用户目录的 `.parse1/config.yaml`。数据写入应使用项目数据库接口或带来源参数的 CLI 命令，保留字段级溯源。

## API 与推荐逻辑

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/v1/chips` | 芯片搜索 |
| GET | `/api/v1/chips/recommend` | 算力推荐 |
| GET | `/api/v1/chips/{identifier}` | 芯片画像 |
| POST | `/api/v1/chips/batch` | 批量芯片画像 |
| GET | `/api/v1/models`、`/api/v1/models/{identifier}` | 模型搜索与画像 |
| POST | `/api/v1/models/batch` | 批量模型画像 |
| GET | `/api/v1/benchmarks`、`/api/v1/compat` | 实测与兼容性查询 |
| GET | `/api/v1/provenance`、`/api/v1/provenance/stats` | 字段来源与统计 |
| GET | `/api/v1/db/status`、`/api/v1/methodology` | 数据概览与评分方法 |
| GET | `/api/v1/data-agent/open-web-runs` | 开放互联网测试历史 |
| GET | `/api/v1/data-agent/open-web-runs/{session_id}` | 单次测试详情 |
| POST | `/api/v1/data-agent/manual-run` | 受管理员认证保护的后端手动运行 |
| POST | `/api/v1/chat` | 对话选型，流式响应 |

完整参数、请求体与返回结构以运行中的 `/docs` 为准。

服务端推荐使用 **4 大类、8 个子维度**，通用大类权重为生态成熟度 40%、实测验证度 30%、算力性能 20%、性价比 10%。先根据场景估算显存和卡数，再应用约束并排序。部分模型存在专用的可比实测排序规则；例如 DeepSeek-V4-Flash 使用固定输入、输出和并发条件下的实测，不能推广为任意场景的性能结论。

推荐中的理论估算、外推结果和真实实测需分别理解；参数缺失时可能使用默认值或中性分。选型依据及适用条件见[推荐逻辑](docs/recommendation_logic.md)、[工作簿实测导入说明](docs/benchmark_workbook_import.md)和 `/api/v1/methodology`。

## 数据与溯源

五张核心业务表为：

| 表 | 内容 |
|---|---|
| `chips` | 芯片规格、算力、价格、生命周期与生态 |
| `models` | Hugging Face 元数据、模型配置及原始 API 响应 |
| `chip_model_benchmarks` | 芯片 × 模型的训练或推理测试记录 |
| `chip_model_compatibility` | 模型、框架和精度等兼容关系 |
| `field_provenance` | 字段来源 URL、置信度、新旧值和更新时间 |

实际 schema 还包含来源链接、部署指南、工作簿原始行、运行日志、候选和发布等辅助表及索引，完整定义见 [schema.sql](schema.sql)。当前数据量应通过 `db status` 或 `/api/v1/db/status` 查询。

芯片来源以 `data/信息来源链接库_final.csv` 等资料为入口；模型通过 Hugging Face API 获取；实测来自 MLPerf、社区资料与导入的测试工作簿。数据维护须遵守：

- 只收录算力芯片，区分 GPU、NPU、DCU、TPU 等类型，排除整机和集群。
- 核心硬件参数须有实际证据，记录精度支持情况，区分实测、厂商声明和估算。
- 业务字段写入或更新时记录 `field_provenance`，保留来源与置信度。
- 未公开发布芯片使用 `production_status="未公开发布"`、`is_released="0"` 标记。

## 开放互联网测试

当前主链路为 `hermes-open-web-v3`，需另行安装兼容的 Hermes SDK、项目插件与宿主机 Worker，并分别配置 Hermes 和后端提取/审核模型。Worker 为每次运行创建临时回环工具服务，完整安装、预检、变量与产物说明见[操作手册](docs/open_web_test_runbook.md)。

通过环境预检后，启动一个数据域运行；可选芯片名称仅作为线索，例如：

```bash
python scripts/run_hermes_open_web_test.py --db data/data.db --domain specs --chip "NVIDIA H100"
```

支持 `identity`、`specs`、`compute`、`compatibility`、`benchmark`、`deployment` 六个数据域，也接受对应的 `--skill chip-<domain>` 别名。省略 `--chip` 时启动一个数据域开放发现运行，任务数不随库中芯片数增加。旧 `--field`、`--workers` 参数已停用；使用 `--resume <run_id>` 续跑，响应丢失时用同一 `--request-id` 和参数重试。

运行状态持久保存在 `data/domain_runs/control.sqlite3` 和 `<run_id>/run.sqlite3`，并导出 manifest、工作清单与事件记录。每个运行保留冻结的业务基线、完整 Skill 包、网页快照和模型审核证据；自定义数据库路径时，`domain_runs/` 位于该数据库同级。正式业务表与 `field_provenance` 不写入，候选仅供复核；正式 Publisher 和持续更新 heartbeat 尚未启用。旧版运行记录和回归脚本仍保留。

当前真实端到端验收尚未通过：已有搜索、访问和筛选证据，但小规模运行未产生有效字段或新实体，模型超时、候选相关性与发现范围仍需处理。实现与受控测试通过不代表正式更新可用，详见[验收记录](docs/acceptance/2026-10-08-hermes-domain-discovery.md)、[当前工作状态](docs/acceptance/2026-10-10-work-status.md)和[小规模对照结果](docs/acceptance/2026-10-10-tavily-small-run-tuning.md)。

## 开发与验证

```bash
python -m pytest -q
```

部分 API、CLI 和数据库查询测试依赖 `data/data.db` 中已有芯片、模型及实测记录；只有空库时不能期待完整测试集全部通过。请准备有数据的本地测试副本，避免直接对生产数据库执行测试。新增功能覆盖正常流程、输入边界和失败路径，缺陷修复补充回归测试。

Docker 构建前也需先准备 `data/data.db`；当前 Dockerfile 会将 `data/` 复制到镜像中：

```bash
docker build -t chip-recommend .
docker run --rm -p 8000:8000 chip-recommend
```

持久化数据、Hermes 工具服务及线上运维见[部署记录](docs/deployment.md)。

## 目录与文档

```text
chip_model/          数据库接口、CLI、FastAPI、评分和对话模块
chip_model/pipeline/ 抓取、同步、候选校验、发布与隔离测试流水线
scripts/             服务、CLI、数据维护与测试入口
static/              Web 前端及运行历史页面资源
data/                来源资料与本地运行数据库
config/              数据更新等配置文件
tests/               pytest 测试
.agents/skills/      Codex 查询/选型技能及六域技能的生成副本
.hermes-skills/      六域信息采集技能的权威规则
.hermes-plugins/     Hermes 开放互联网工具插件
docs/                设计、验证与部署文档
schema.sql           SQLite 建表与索引定义
```

- [数据库字段设计](docs/database_design.md)：核心表与字段来源约定；完整表结构以 `schema.sql` 为准。
- [推荐逻辑](docs/recommendation_logic.md)：显存、卡数与评分规则。
- [开放互联网测试操作手册](docs/open_web_test_runbook.md)：Hermes 配置、运行和审计产物。
- [来源巡检说明](docs/source_refresh.md)：来源检查、快照与差异记录。
- [实测工作簿导入](docs/benchmark_workbook_import.md)：原始行保留、结构化测试条件与参考场景。
- [部署记录](docs/deployment.md)：发布、验证与回滚记录。
- [项目进度](docs/progress.md)：历史进展。

部分历史文档保留旧目录和命令示例，执行时以本 README、脚本 `--help` 与运行中的 API 文档为准。
