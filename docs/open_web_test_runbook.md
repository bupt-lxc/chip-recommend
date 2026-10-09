# Hermes 六域开放互联网发现：隔离运行操作手册（V3）

本轮运行 `hermes-open-web-v3`，只保存可复核的隔离候选。省略芯片时启动一个数据域发现运行，搜索范围不受现有芯片目录限制，任务数不随库内芯片数增加。提供已有或库外型号只作为可选线索。正式 chips、models、实测、兼容、部署和 field_provenance 不写入；不启用 Publisher、旧 heartbeat、周期服务或 HF 模型更新。

当前实现与受控测试证据见 [验收记录](acceptance/2026-10-08-hermes-domain-discovery.md)。实际六域 Hermes 网络验收尚未执行，不能用 fixture 或 CLI 退出记录代替。

## 数据域、规则和字段

| domain | Skill 别名 | 业务范围 |
|---|---|---|
| identity | chip-identity | 厂商、完整型号、系列与发布生命周期 |
| specs | chip-specs | 芯片/单卡硬件与互联规格 |
| compute | chip-compute | 精度、稠密/稀疏理论峰值和计算单元 |
| compatibility | chip-compatibility | 芯片×模型及软件栈支持关系与证据级别 |
| benchmark | chip-benchmark | 芯片×模型实测、单位、SKU 和测试条件 |
| deployment | chip-deployment | 文档、适用范围、后端、版本与启动方法 |

唯一权威规则根为 `.hermes-skills/`。六域包包含 `SKILL.md` 的 plan/select/extract/review/complete 阶段、`domain.json`、域参考和共享参考。显式闭包为 20 个文件；`.agents/skills/` 是生成副本。四个独立非更新 Skill（model-catalog、model-activity、chip-recommend-cli、chip-selector-agent）保留，16 个旧更新包位于 `docs/archive/hermes-skills/`，空旧目录不构成可加载 Skill。

以下为当前六个 `domain.json.fields` 的完整清单。实体键、条件、单位、最低识别条件与参考清单以各合同的 `entity_keys/conditions/units/references` 为准。新增字段先修改权威合同和规则，校验实际 SQLite 列、同步生成副本与插件合同；不能通过启动参数绕过合同。关系的模型、卡数和精度是带条件实体关系的一部分。

```json
{
  "identity": {
    "chips": [
      "vendor",
      "vendor_display",
      "vendor_region",
      "chip_series",
      "chip_model",
      "chip_type",
      "usage",
      "tier",
      "release_date",
      "production_status",
      "eol_date",
      "target_market",
      "is_released",
      "expected_release_date"
    ]
  },
  "specs": {
    "chips": [
      "architecture",
      "arch_codename",
      "generation",
      "process_node_nm",
      "foundry",
      "die_size_mm2",
      "transistors_b",
      "package_type",
      "is_chiplet",
      "vram_gb",
      "vram_type",
      "vram_bus_bit",
      "vram_bw_gb_s",
      "vram_clock_mhz",
      "l1_cache_kb",
      "l2_cache_mb",
      "on_chip_sram_mb",
      "base_clock_mhz",
      "boost_clock_mhz",
      "tdp_w",
      "max_power_w",
      "psu_w",
      "power_connector",
      "board_length_mm",
      "board_width_mm",
      "slot_width",
      "form_factor",
      "bus_interface",
      "interconnect_bw_gb_s",
      "interconnect_tech",
      "network_interface"
    ]
  },
  "compute": {
    "chips": [
      "precision_support",
      "precision_perf",
      "compute_units",
      "tensor_cores",
      "rt_cores",
      "shading_units",
      "sm_count"
    ]
  },
  "compatibility": {
    "chips": [
      "software_stack",
      "compatible_frameworks",
      "framework_compat",
      "sw_stack"
    ],
    "chip_model_compatibility": [
      "compat_status",
      "framework",
      "precision",
      "verified_at",
      "notes"
    ]
  },
  "benchmark": {
    "chip_model_benchmarks": [
      "scenario",
      "task",
      "hardware_config",
      "framework",
      "batch_size",
      "input_seq_length",
      "output_seq_length",
      "concurrency",
      "prefill_throughput",
      "decode_throughput",
      "time_to_first_token_ms",
      "inter_token_latency_ms",
      "memory_peak_mb",
      "throughput_tok_s",
      "throughput_samples_s",
      "tpot_ms",
      "mfu_pct",
      "gpu_hours",
      "training_tokens_T",
      "training_gpu_count",
      "training_workload_type",
      "test_date",
      "notes",
      "citation",
      "evidence_level",
      "methodology",
      "reference_url"
    ]
  },
  "deployment": {
    "deployment_guides": [
      "backend",
      "notes"
    ]
  }
}
```

只读核对权威源、生成副本、插件版本/Schema 和逐文件哈希：

```bash
python scripts/sync_domain_skills.py --check
```

安装脚本 `scripts/install_hermes_open_web_plugin.sh` 会备份旧安装并复制完整闭包，属于另行授权的宿主机变更；本轮未运行安装/远程部署。安装路径应显式匹配 `AISH_PERF_HERMES_SKILL_ROOT`。Hermes 的 `HERMES_HOME` 规则根优先级及最近 `.git` 祖先的项目规则解析须由实际 SDK 的 resolver 证明；安装成功不等于加载成功。运行时在专用临时 home 和项目树之外的 cwd 中只加载当前运行冻结的完整包，禁用其他工具/插件与项目上下文；不要自行加 `--safe-mode`（会关闭插件）或 `--ignore-rules`（也会忽略 skills）代替该隔离实现。

## 一致的来源库与运行根

V3 的默认布局是 `data/domain_runs/control.sqlite3` 和 `data/domain_runs/<run_id>/`。其他共享 data root 使用同一布局；`data/test_runs/` 仅保存旧 v1/v2，只读展示，不能用 v3 resume。

CLI 的 `--db` 指定只读来源库，其父目录成为 data root；CLI 没有 `--data-root`。省略 `--db` 时使用 `DATA_DB_PATH` 或 `chip_model.database.get_db_path()` 默认值。API 同样使用 get_db_path 的父目录。宿主机 Worker 使用 `--data-root`，从控制库绑定的共享根内相对 `source_locator` 找来源库，不自行改读默认库。例如宿主机 `/home/lxc/chip-recommend/data/data.db` 与容器 `/app/data/data.db` 必须来自同一 RW volume，控制库与 run.sqlite3 也在同一文件系统。容器路径只是映射示例，需用实际 Docker mounts 验证。

新启动时 request_id、规范化 payload、累计 timeout、source_locator 持久绑定；同键相同 payload 重试返回同一 run_id，同键更改域/线索/来源返回冲突。保存响应中的 request_id 与 run_id；使用新键重提启动会创建新运行。显式 resume 保留原基线、规则包、来源库、额度、修订计数与收据，不重新冻结当前库。续跑时省略域和线索即可继承原范围；若显式给出则必须匹配。

## CLI：启动、响应丢失重试与续跑

预生成并保存启动键，再启动一个不绑定芯片的域。以下 PowerShell 命令在已通过实际环境预检的宿主机运行；是待执行步骤，不是当前成功证据：

```powershell
New-Item -ItemType Directory -Force .tmp | Out-Null
$startKey = [guid]::NewGuid().ToString('N')
$startKey | Set-Content -Encoding utf8 .tmp/identity-start-key.txt
python scripts/run_hermes_open_web_test.py --db data/data.db --domain identity --request-id $startKey --timeout 1800
# 响应丢失：保持原键、域、来源与 timeout
python scripts/run_hermes_open_web_test.py --db data/data.db --domain identity --request-id $startKey --timeout 1800
# 填入第一次响应的 run_id；为这次显式续跑保存新的请求键
$resumeKey = [guid]::NewGuid().ToString('N')
python scripts/run_hermes_open_web_test.py --db data/data.db --resume <已有运行ID> --request-id $resumeKey
```

先创建 `.tmp`（本地诊断目录）再保存键。尖括号表示需替换的值，不能直接粘贴执行。每域顺序运行并保存自己的键，domains 为 identity/specs/compute/compatibility/benchmark/deployment。每次检查最终 status、阶段、完整诊断及 pending 清单，再决定是否显式续跑已有 ID；不要循环重建六个失败运行。

Linux 宿主可先保存六个域的键，再逐项执行。下列准备代码只创建缺失键，不覆盖重试所需的已有键；在可信宿主通过预检后执行：

```bash
python - <<'PY'
from pathlib import Path
import uuid
root = Path('.tmp')
root.mkdir(exist_ok=True)
for domain in ('identity', 'specs', 'compute', 'compatibility', 'benchmark', 'deployment'):
    try:
        with (root / (domain + '-start-key.txt')).open('x', encoding='utf-8') as file:
            file.write(uuid.uuid4().hex + '\n')
    except FileExistsError:
        pass
PY
python scripts/run_hermes_open_web_test.py --db data/data.db --domain identity --request-id "$(cat .tmp/identity-start-key.txt)"
python scripts/run_hermes_open_web_test.py --db data/data.db --domain specs --request-id "$(cat .tmp/specs-start-key.txt)"
python scripts/run_hermes_open_web_test.py --db data/data.db --domain compute --request-id "$(cat .tmp/compute-start-key.txt)"
python scripts/run_hermes_open_web_test.py --db data/data.db --domain compatibility --request-id "$(cat .tmp/compatibility-start-key.txt)"
python scripts/run_hermes_open_web_test.py --db data/data.db --domain benchmark --request-id "$(cat .tmp/benchmark-start-key.txt)"
python scripts/run_hermes_open_web_test.py --db data/data.db --domain deployment --request-id "$(cat .tmp/deployment-start-key.txt)"
```

每条独立执行、保存 run_id 并确认状态后再进行下一域；不要在响应丢失后覆盖已有键文件。

可选 `--chip "NVIDIA L40S"` 或 `--chip "库外具体型号"`；`--skill chip-specs` 是 `--domain specs` 的兼容别名，两者一起提供时必须一致。旧 `--field` 明确拒绝，`--workers`、用户搜索词或单字段参数不受支持。入口不接受 target_fields、search_queries/queries、全库芯片列表或配额覆盖。API 的兼容 `chips` 至多一条线索，并须与 chip 一致。超时为 1–1800 秒整数；CLI 退出码 0 只用于 success/no_findings，partial/failed/interrupted 返回 2。

## API：同一持久队列与管理员边界

`POST /api/v1/data-agent/manual-run` 接收原始 JSON，验证 HTTPS 或真实 loopback 传输以及 `X-Data-Agent-Admin`，并使用 `Idempotency-Key` 或 body request_id。二者同时出现必须一致。不要把口令放 body、URL 或共享日志，也不要放宽远程 HTTP/转发头校验。

```json
{
  "pipeline": "open_web",
  "mode": "test",
  "domain": "specs",
  "chip": null,
  "request_id": "调用方预先保存的唯一键"
}
```

本机 loopback API 示例使用受保护口令文件与已保存的启动键；无需在 body 或命令参数填写口令：

```python
from pathlib import Path
import requests
request_id = Path('.tmp/specs-start-key.txt').read_text(encoding='utf-8').strip()
token = Path('data/manual_control/admin_token').read_text(encoding='utf-8').strip()
response = requests.post('http://127.0.0.1:8000/api/v1/data-agent/manual-run',
    headers={'X-Data-Agent-Admin': token, 'Idempotency-Key': request_id},
    json={'pipeline': 'open_web', 'mode': 'test', 'domain': 'specs',
          'chip': None, 'request_id': request_id}, timeout=20)
response.raise_for_status()
receipt = response.json()
print({key: receipt[key] for key in ('request_id', 'run_id', 'status')})
```

远程 API 必须换为已核验的 HTTPS endpoint。客户端不记录请求 headers/完整私密响应；响应丢失重用同一键与 payload。202 返回 queue receipt，不代表完成。API 不在进程内启动 Hermes，宿主机 Worker 领取同一个控制库。查询 `GET /api/v1/data-agent/open-web-runs` 和 `GET /api/v1/data-agent/open-web-runs/<run_id>` 只读获取状态与历史。无权限返回 401，错误域/字段/模式/原始 JSON 返回 400，启动键冲突返回 409；storage 不可用不能伪造已启动。

## 三个工具的真实合同

权威工具 schema 是 `_shared/tool-contract.json`，与 Python `chip_model.domain_tool_api.tool_contract()`、插件闭包逐字节核对。全部调用绑定 contract_version=3、pipeline=hermes-open-web-v3、run_id、domain、scope_type、chip；写操作另带 request_id，同键同 payload 重放，不同 payload 冲突。

| Hermes 工具 | 操作与限制 |
|---|---|
| open_web_search | action=execute 带 plan；action=results 带 list_version/cursor；action=complete_intents 带 failed_query_id/replacement_query_id/reason 完成恢复意图 |
| open_web_preview | mode=initial 带 list_version/cursor/limit(1–10)；mode=detail 带 snapshot_id/offset/length(1–4000) |
| open_web_submit_selection | action=append 带 list_version/decisions(1–10)；action=finalize 带当前 list_version |

初始 plan.round_number=0，恰好十条真实 Hermes 生成的短词（每条 <=120 Unicode 字符，至少 3 zh、3 en）；每条有 query_id/text/language/reason/page_type。round_number=1 和 2 各至多五条，不能用更多轮或换 ID 放宽额度；replaces_query_id 必须引用原失败意图。程序执行、URL 安全校验和去重，不替模型生成词，也不把诊断三条固定词作为生产模板。

结果按每页最多十项、完整响应最多 12000 字符分页，cursor 包含 list_version/offset。必须分页至 has_more=false，不只看第一页。主要 URL 全部实际访问，后端保存不可变全文快照/hash/正文范围，initial 每页每个 URL 返回最多 500 字主体核心预览及位置。detail 从保存正文读，不再访问网络，每个 snapshot 最多三次、每次最多 4000 字。后端全文分块提取另记录覆盖与未处理范围，不受 Hermes 核心预览长度限制，也不静默截断。

每个 major candidate 的 append 决定包含 candidate_id、decision(select/reject)、reason、expected_info、linked_domains，可附 entity_clues 与 matched_category。select 必须给预期信息；未真实完成访问的 URL 不能提交决定，选择还须有可读完整快照。每项决定均可追溯。finalize 只在当前列表全部访问与决定、搜索意图完整结束时接受，持久提交 jobs；退出码或 stdout 文本不能替代 finalize。

工具 HTTP 路径为 POST `/v1/search`、`/v1/preview`、`/v1/selection`。V3 不接受旧 `/v1/submit-selection`。读取进度通过已认证 GET `/v1/runs/<run_id>`（read_run / selection_progress）检查 pending_candidates/selection_finalization，当前 schema 没有第四个 Hermes 工具或 selection action=read_progress。健康检查为已认证 GET `/v1/health/<run_id>`。

后端 extract/review 加载当前域冻结的完整 Skill、合同和全部参考。程序校验通过后独立模型语义复核；每个逻辑候选至多一次修订，不能换 ID 重置。一层关联域复用快照，不搜索、不进一步派生业务域；必要身份判定由主/关联域共用，身份证据不充分不能统计新芯片。库外模型只保存未解析关系依赖，不自动创建正式模型。

## 只读查询实际 Skill 使用审计

在结果页或直接 GET `/api/v1/data-agent/open-web-runs/<run_id>` 获取同一历史投影。先确认返回 `schema_version=hermes-open-web-v3`，再检查 `actual_skill_usage`；这段读取代码可用于已存在的真实或 fixture run_id，不会补写历史或启动任务：

```python
import requests
run_id = '替换为已有运行ID'
domain = 'specs'
response = requests.get(
    'http://127.0.0.1:8000/api/v1/data-agent/open-web-runs/' + run_id,
    timeout=20)
response.raise_for_status()
detail = response.json()
assert detail.get('schema_version') == 'hermes-open-web-v3'
events = {event['sequence']: event for event in detail.get('raw_records', {}).get('events', [])}
for observation in detail.get('actual_skill_usage', []):
    usage = observation.get('domain_usage', {}).get(domain, {})
    print({'executor': observation['executor'], 'domain': domain,
           'loaded': usage.get('loaded'), 'executed': usage.get('executed'),
           'scope': observation.get('scope'),
           'verification': observation.get('verification')})
    for load in observation.get('loads', []):
        if load.get('domain_id') not in (None, domain):
            continue
        ref = load['evidence_ref']  # events/<sequence>
        original = events.get(int(ref.split('/')[1]))
        print({key: load.get(key) for key in
               ('phase', 'domain_id', 'bundle_sha256', 'input_sha256', 'files', 'evidence_ref')})
        print({'raw_event_sequence': original.get('sequence') if original else None})
```

逐 executor（hermes、backend_extract、backend_review）核对 `domain_usage[domain].loaded/executed`，值只有 true 或 null；缺该 domain、缺该 executor 或 null 表示未记录/未知，不能当 false，也不能当完成。`actual_skill_usage[].loaded/executed` 是 `scope="executor_observations"` 的总体观察，可能来自不同域或不同事件，不能跨域合并后判定当前域两阶段完成。`verification="recorded_audit_only"` 表示保存的审计观察，不能当实际 installed SDK 的独立验证，尤其不能把 fixture 轨迹冒充真实环境。

`loads[]` 保留 phase、domain_id、bundle_sha256、input_sha256、files、evidence_ref。`loads[].evidence_ref`、每行及逐域 `evidence_refs` 的 `events/<sequence>` 都应在 `raw_records.events` 按 sequence 找对应原始事件，核对 payload.executor/kind/domain_id/phase 及当次引用；查不到或缺 phase/domain_id 就保留未知，不从 Skill 名、当前主域或别的记录补推。executed 观察仅证明执行调用已记录，最终是否完成还须核对 model_calls、真实阶段状态、覆盖/失败和独立复核结果，不能用 loaded 代替 executed 或 success。

Hermes 的 loaded 事件 `resolver.files` 是完整 20 文件闭包，各项 `{path, sha256}` 应逐一匹配本运行 skill_bundle manifest 的绝对冻结路径与 hash；同时核对原始预检来源和实际 SDK/插件/工具 schema 证明。backend_extract 与 backend_review 是两次独立阶段调用，其 loads.files 来自对应 loaded 事件的 reference_hashes，不能要求或冒充 Hermes 的完整 resolver 路径表；分别检查 phase/domain_id、bundle_sha256、对应输入摘要、引用 hash 和 model_calls 的执行/响应/独立复核绑定。输入 hash 以原记录的计算范围为准（例如 executor loaded 的 input_sha256 是完整 messages 摘要，model_calls 的 input_sha256 是 payload 摘要），不同命名空间不能直接强行对等。

该 API 从原始事件生成只读投影，不打开当前正式库补齐旧值，不补写缺失的规则加载/执行证据。不要在共享日志输出整个 raw_records 或模型私密请求；按上述引用在受保护结果中定位即可。

## 宿主机预检与运行边界

API 容器只共享 data root。HermesRunner 在宿主机每次建立临时 IPv4 loopback 工具服务、临时令牌和独立 SDK cwd/home，与活动 generation、工具 schema、冻结包 hash 和绝对路径绑定。旧固定 5341 服务不是 V3 的前提；不用公网工具服务绕过容器回环限制。

只读预检命令：

```bash
python scripts/hermes_domain_worker.py --data-root /home/lxc/chip-recommend/data --check
python scripts/hermes_domain_worker.py --help
python scripts/run_hermes_open_web_test.py --help
```

实际目标为 lxc@81.70.231.92；Hermes CLI 预期路径 `/home/lxc/hermes-agent/venv/bin/hermes`，项目 `/home/lxc/chip-recommend`，实际路径/版本未验证。当前 SSH 因 Host key verification failed 未执行远程步骤；等待已信任连接信息，不修改信任配置或绕过 host key 检查。

配置 HERMES_CLI_COMMAND、HERMES_PYTHON（真实 SDK Python）以及必要 HERMES_SDK_ROOT，从 allowlist 提供 HERMES_MODEL/HERMES_PROVIDER 与对应凭证；后端 extract/review 使用 DATA_AGENT_LLM_BASE_URL/MODEL/API_KEY/TEMPERATURE。两个模型通路的配置和可用性分别验证，不因 key 存在标记模型服务可用。`--check` 只读检查目录权限、control.sqlite3 schema/唯一键、安装规则和 Docker mounts；即使没有已知失败也只报告 preflight_required，SQLite journal/锁/磁盘/跨附加库原子提交能力仍需实际 runtime 预检。正式开启 Worker 前必须核对实际 installed SDK 版本/signature、完整 20 文件 resolver 路径/hash、唯一插件和三个 schema、实际 authenticated health、共享 volume 和运行目录写能力。

累计 ACTIVE 预算默认 30 分钟，技术请求至多三次，语义修订一次。恢复不能重置预算，旧 Worker generation 被 fencing 拒绝；不会自动重领未知外部执行或重开已失效租约，需要显式 resume。Worker `--once` 是已有队列处理能力，本轮未启动周期服务；不要把 `--check` 的成功权限检查当成运行 READY。

## 搜索来源探针

```bash
python scripts/probe_open_web_search.py --db data/data.db --provider all --output .tmp/search-probe.json
python scripts/probe_open_web_search.py --provider bing-html --proxy http://127.0.0.1:7897 --output .tmp/bing-probe.json
```

provider 可为 all/bing-html/so360-html/duckduckgo-html。每个源独立执行“AI 加速芯片 新发布”“AI accelerator product launch”“NVIDIA L40S specifications”，不调用生产 RunStore、业务更新或已知芯片 CSV 过滤。记录原词/实际词、locale、endpoint、HTTP、network/parse 状态、有效 URL、耗时、代理路由和失败；没有自动 fallback，其他源成功不覆盖本源故障。HTTP 可达与搜索相关性分别判断，200 且有通用 AI 结果不能当作发现芯片成功。

显式代理默认 DATA_AGENT_PROXY；requests 的 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY（含小写）仍按实际 session.trust_env 生效。输出分别显示 explicit_proxy、ambient_proxy_environment、ambient_proxies、effective_proxies，凭证脱敏，不导入浏览器个人 cookies。不要仅因 DATA_AGENT_PROXY 为空声称直接连接。当前运行时 provider 可从 dependencies.proxy 或 requests 环境读取代理，诊断 DATA_AGENT_PROXY 配置不自动证明宿主 Worker 也使用该显式值；逐端点核对实际路线。探针不会自动加载 `.env`，在受保护进程中先加载配置，不把文件内容或密钥打印到共享日志。

每网络请求 adapter timeout 最多 15 秒；DNS/多地址连接可能增加实际墙钟，elapsed_ms 原样记录。parse_error/http_error/network_error 不伪装 healthy_empty。退出 0 表示所有来源探针健康，2 表示有故障或数据库摘要改变，不代表六域验收结论。

## 产物、状态与抽查

| V3 路径 | 内容 |
|---|---|
| domain_runs/control.sqlite3 | 持久启动键、payload、source_locator、timeout 与 lease generation |
| domain_runs/<run_id>/run.sqlite3 | 权威 metadata、事件、预算、任务和请求收据 |
| <run_id>/data.db | WAL-aware 冻结业务基线，只读用于实体匹配与比较 |
| <run_id>/skill_bundle/ | 不可变完整规则闭包、manifest 和逐文件 hash |
| <run_id>/source_snapshots/ | 请求/final URL、正文与 hash、引文位置可追溯的完整快照 |
| <run_id>/manifest.json、work.json、events.jsonl | 从权威 SQLite 快照生成的状态/工作/审计导出 |

额外记录保留在 run.sqlite3 metadata：搜索计划和原始来源诊断、预览/完整覆盖、决定、模型调用、候选版本、独立复核、关联/身份任务、实体和比较。导出是只读投影，不能改 manifest 或手写 JSONL 来授权重试。全文、大数据库、令牌、网页/模型秘密和 `.tmp` 诊断不入 Git，运行数据不要整体 git add。

success/no_findings 都要求全部计划访问、决定及必要后端步骤完整；success 至少一个同时通过程序和独立复核的候选，no_findings 为零。健康零结果与正常拒绝可完成；真实执行故障、未覆盖正文、未 finalize 或关联失败不能被零候选隐藏。致命规则/隔离/基础设施问题为 failed；其他有可审计阶段但有未完成/预算停止为 partial；进程/人工中断为 interrupted。备用源或明确完成替代意图可以恢复查询，但原始失败诊断保留。候选有效与依赖/比较可更新性分别显示。

UI 默认展开五列“实体/字段、原值与原来源、候选新值与新来源、原文证据、变更与状态”。原值只能来自本轮冻结基线；缺旧历史显示旧值未记录，新实体显示本轮基线无此实体。值变化、来源成员与质量分别保存，缺旧证据不凭官方域名自动提升。页面显示“已保存到隔离区，未发布”，历史阶段按实际证据渲染。

每域最多抽查五个通过候选，少于五个全部检查，另查产生的拒绝、来源变化、新实体和未解析条件。核对 snapshot hash/start:end 与逐字引文、表头/脚注、单位换算、完整 SKU/容量/形态、实测最低条件、来源与冻结基线、规则 hash/executor/model execution ID、独立复核/修订、比较和 UI 引用。候选数量不是准确率；报告实际模型/网络耗时、调用数与未覆盖范围。当前真实抽查未执行，fixture Chromium 检查单列记录。

## 正式业务库一致性与验证

正式六表为 chips、models、chip_model_benchmarks、chip_model_compatibility、deployment_guides、field_provenance。`scripts.probe_open_web_search.business_digest(path)` 使用 mode=ro 连接、SQLite backup 到内存（包含已提交 WAL），对全部列及按稳定键/完整行规范排序的全部行计算摘要；缺表报错。备份 busy/locked 的 progress 回调默认五秒 deadline，超时明确失败，不会因 SQLite backup 内部重试无限等待。probe 的 --db 在诊断前后调用它，并记录逐表列/行数/hash 与总摘要。

真实六域验收应在没有外部写入的窗口，先调用同一函数保存 before，运行后再保存 after，核对完整摘要和表结构/行数；源码库不改 journal mode，不仅比较主文件或 SQLite 页 hash。外部写入导致摘要改变时，记录干扰并在静默窗口重做，不擅自停 cron。当前摘要一致只覆盖本地探针区间，尚无真实 Hermes 运行前后无写入证明。

Task16 窄回归：`python -m pytest tests/test_open_web_search_probe.py -q`。项目完整回归要求 `python -m pytest -q`，唯一已执行全套为 1304 passed/3 failed；后续针对三处旧合同预期的修复与相关测试通过，但未重跑全套，不能称 full GREEN。更完整的测试与限制见验收记录。
