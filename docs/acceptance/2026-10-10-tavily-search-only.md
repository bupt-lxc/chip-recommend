# Tavily 仅替换搜索工具：实现与小规模验证

2026-10-10，按用户要求仅替换开放发现流程的搜索执行工具。

## 改动与启用

`chip_model/pipeline/domain_discovery/search.py` 新增 TavilySearchProvider，并接入原有 default_providers 选择入口。隔离 worker 进程设置：

```text
DOMAIN_SEARCH_BACKEND=tavily
TAVILY_API_KEY=<通过服务器受限配置注入>
```

未设置后端时沿用 html；显式指定 Tavily 但缺少 key 会失败，不静默退回旧搜索。切回 `DOMAIN_SEARCH_BACKEND=html` 可恢复旧工具。设置应在新隔离运行启动前完成，避免在同一运行途中切换后端。

每次适配器调用只执行一次 Tavily POST /search，使用 basic、最多原有配置的 5 条候选、禁用自动参数、生成答案和原始正文返回。Hermes 生成的搜索词保持原样；URL/title/content 映射为既有 url/title/snippet/rank。现有 SearchExecutor 继续负责配额、重试、URL 安全检查、去重及结果保存。Tavily 自身返回的 content 仅作搜索摘要，不替代抓取证据。HTTP 错误、网络异常、无结果、无效 JSON 分别记录；不保存含 key 的异常原文。

正文 PageService、Skill、实体比对与字段提取未修改。本次没有修改正式服务器应用环境、运行镜像、业务库或心跳。

## 验证

新增 9 项测试覆盖结果映射、参数边界、空结果与异常、凭据保护、显式选择以及通过既有 SearchExecutor 的 10 查询去重和幂等重放。相关搜索/runtime/worker 测试原 184 项通过，追加的 executor 集成测试也通过（新增测试文件合计 9 项）。

服务器 `/home/ubuntu/utils/tavily-search-only-validation/` 保存独立模块副本和 `results.json`。直接执行实际项目适配器，复用已有受限 Tavily key；4 次请求，无额外重试、无正文提取调用。

| 固定查询 | 结果 | 耗时 |
| --- | --- | --- |
| 国产 AI 芯片 新品 发布 | 5 条；DF1000、华为等发布线索，也混有分析报告 | 2.709 秒 |
| new AI accelerator chip launch | 5 条；NVIDIA Rubin、Intel 官方发布等 | 1.370 秒 |
| 消费级 显卡 模型训练 支持 | 5 条；训练/微调资料，但包含推理文章 | 2.134 秒 |
| NVIDIA L40S official specifications | 5 条；NVIDIA 官方页第 4，另含 Lenovo 与 PNY 规格资料 | 1.146 秒 |

20 条 URL 均通过现有静态 URL 规范化检查；这不代表访问和正文提取成功。服务器 Hermes venv 缺少 bs4，验证依赖安装在独立验证目录 dependencies 中，未修改 Hermes 环境。

结论：仅替换搜索工具即可获得比此前 Bing 泛结果更相关的候选。小样本未验证最新信息完整性、六域全链路或字段准确性；消费级训练仍需筛掉推理资料。下一次应使用不指定芯片的隔离身份域运行，沿用原正文工具，检查实际抓取、模型筛选与证据候选产物。

接口参数依据 [Tavily 官方 SDK](https://github.com/tavily-ai/tavily-python/blob/master/tavily/tavily.py)。本地脱敏日志：`.tmp/tavily-search-only-remote.log`。
