# Hermes 开放互联网芯片信息测试

该链路由 Hermes 选择一个信息 Skill 执行，系统只提供搜索、网页预览和提交选择三个工具。Skill 必选，芯片可选；省略芯片时，一个父运行覆盖数据库快照中的全部芯片，并增加一个开放发现单元。所有产物写入 `data/test_runs/<run_id>/`；正式数据库仅用于生成隔离副本和前后指纹校验。

## 六类 Skill

| Skill | 更新目标 |
|---|---|
| `chip-identity` | 厂商、型号、系列、发布时间、发布状态 |
| `chip-specs` | 显存、带宽、功耗、制程、形态、互联规格 |
| `chip-compute` | 精度支持、理论峰值、计算单元 |
| `chip-compatibility` | 模型、框架、精度、推理引擎和软件栈兼容关系 |
| `chip-benchmark` | 芯片×模型训练或推理实测数据 |
| `chip-deployment` | 部署后端、软件版本、启动方式和拓扑资料 |

每个 Skill 都完整规定搜索词生成、工具调用、URL 选择、跨 Skill 标记、失败处理和完成条件。每个芯片单元分别生成恰好 10 条动态搜索词，依次调用 `open_web_search`、`open_web_preview`、`open_web_submit_selection`，并对每个候选 URL 给出选择或拒绝理由。不存在“全部芯片共享全局前 10 个 URL”的截断。Hermes 不使用自身搜索功能，网页文本始终作为不可信输入。

当选中页面还包含其他类别时，系统会合并 Hermes 初筛类别与完整快照提取类别，将中文类别规范化为固定 Skill，去除当前 Skill 和重复项，再创建一层关联提取任务。关联任务不会继续派生，避免同一 URL 循环处理或任务数量失控。

## 三个工具

| 工具 | 系统负责的工作 |
|---|---|
| `open_web_search` | 执行 10 条搜索词，检查 URL 安全性、规范化和去重 |
| `open_web_preview` | 逐一访问全部候选，保存完整快照，向 Hermes 返回每页最多 500 字核心文本 |
| `open_web_submit_selection` | 校验所有 URL 的选择结论，调用 Kimi 从选中页面提取字段，并创建跨 Skill 联动任务 |

## 部署

工具服务使用独立容器，端口只绑定服务器回环地址。密钥只放在服务器环境文件中：

```text
AISH_PERF_OPEN_WEB_TOOL_TOKEN=<随机令牌>
DATA_AGENT_LLM_API_KEY=<Moonshot API Key>
```

```bash
bash scripts/deploy_open_web_tool_service.sh /home/lxc/chip-recommend

HERMES_HOME=/home/lxc/.hermes \
HERMES_CLI=/home/lxc/hermes-agent/venv/bin/hermes \
bash scripts/install_hermes_open_web_plugin.sh /home/lxc/chip-recommend
```

Hermes 进程还需配置相同的 `AISH_PERF_OPEN_WEB_TOOL_TOKEN`，以及：

```text
AISH_PERF_OPEN_WEB_TOOL_URL=http://127.0.0.1:5341
```

## 后端手动运行

一次运行只选择一个 Skill。指定芯片时运行一个芯片单元：

```bash
python scripts/run_hermes_open_web_test.py \
  --db data/data.db \
  --chip "NVIDIA H100" \
  --skill chip-specs \
  --field vram_gb \
  --field vram_bw_gb_s
```

省略 `--chip` 时，一次启动全部芯片和开放发现；父运行等待所有单元进入终态：

```bash
python scripts/run_hermes_open_web_test.py \
  --db data/data.db \
  --skill chip-specs \
  --workers 2
```

中断后使用原运行 ID 续跑未完成单元：

```bash
python scripts/run_hermes_open_web_test.py \
  --db data/data.db \
  --skill chip-specs \
  --resume 20260930-120000-abcdef12
```

开放发现单元的新芯片只写入 `new_chip_candidates.jsonl`，不会加入本轮冻结的芯片列表，也不会写正式数据库。

原有 `run_open_web_test.py`、`run_open_web_all_skills.py` 仅保留为旧流程回归工具，不属于新的 Hermes 主链路。

## 运行产物

| 文件 | 内容 |
|---|---|
| `manifest.json` | 父运行范围、芯片快照、单元进度、聚合计数、正式库前后指纹 |
| `units/<unit_id>/manifest.json` | 单颗芯片或开放发现单元的状态、尝试次数和计数 |
| `units/<unit_id>/hermes_search_plan.json` | Hermes 为该单元生成的 10 条搜索词及理由 |
| `units/<unit_id>/*.jsonl` | 搜索结果、预览、决定、字段、关联 Skill、新芯片候选和工具轨迹 |
| `units/<unit_id>/source_snapshots/` | 该单元可访问页面的完整网页快照 |
| `snapshot_cache/` | 父运行内按 URL 复用的页面快照，避免重复访问 |
| `data.db` | 隔离测试数据库 |

状态页只读取这些结果，不提供运行按钮。父卡片展示完成单元数，详情可按芯片名称筛选最终状态，并查看新芯片候选和系统规范化后的关联 Skill。验收时确认 `manifest.json` 的 `formal_database_modified` 为 `false`，并执行：

```bash
python -m pytest -q
```

## 本地模型温度配置

保留本地适配：`DATA_AGENT_LLM_TEMPERATURE` 可设置 0–2 的有限数值，默认 0.6，沿用远程关闭 thinking 的请求参数。本地现有配置为 1。
`DATA_AGENT_LLM_BASE_URL` 填接口根地址，不含 `/v1/chat/completions`。进程启动前需加载环境变量，密钥不得写入版本库。
