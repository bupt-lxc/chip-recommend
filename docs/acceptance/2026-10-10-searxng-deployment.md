# SearXNG 隔离部署记录

2026-10-10，按用户授权部署到腾讯云 Ubuntu 服务器；未修改项目代码、Hermes 配置或正式数据更新流程。

| 项目 | 实际状态 |
|---|---|
| 容器 | `aishperf-searxng`，运行中 |
| 版本 | `2026.10.9+f4822b3fc` |
| 镜像 | `searxng/searxng@sha256:85492ba800a861e3e8ff6014c430e81369fe6ca766a9990595f616d1ce95b272` |
| 服务器地址 | `http://127.0.0.1:18888`，只绑定 loopback |
| Docker 网络 | `aishperf-search`；同网络客户端可使用 `http://searxng:8080` |
| 部署目录 | `/home/ubuntu/aishperf-search/searxng` |
| 配置与记录 | `config/settings.yml`、`deployment.json`、`compose.yaml`、`validation-20261010.json` |
| 资源限制 | 256MiB 内存、0.5 CPU、单 Worker；验证后约 104.6MiB，无 OOM、无重启 |

JSON 配置接口 HTTP 200；配置了 Bing、Brave、Google、百度，并启用 JSON 搜索。镜像按 digest 固定；Docker Compose 插件未安装，因此实际通过 Docker run 启动，同时保存了可复用的 Compose 文件。没有安装系统依赖或修改现有容器。

**搜索验证未形成可判读的质量样本。** 4 条查询覆盖中文开放发现、英文开放发现、消费级训练显卡和 L40S 型号追查，均返回 HTTP 200 / 0 结果。实际诊断为百度 CAPTCHA、Google/Brave timeout；后续请求被上游故障暂停机制阻断，因此不能当作四次独立健康搜索。请求的 engines 列表与实际诊断集合存在差异，应在后续单源测试时核对。补充的 Bing 单源诊断也返回 0 结果，但没有报告上游错误；尚未判定其空结果原因。GPT-6 Luna 独立审阅确认：部署通过，搜索可用性和相关性均尚未验证。

正式 `chip-recommend` 镜像和启动时间保持不变；没有正式数据库写入、没有 heartbeat 启用，也没有把 SearXNG 配置为 Hermes 主搜索来源。当前仅证明服务部署成功，不证明服务器出口下的搜索可用。

本机查看页面可建立 SSH 转发：

```bash
ssh -N -L 18888:127.0.0.1:18888 ubuntu@81.70.231.92
```

然后访问 `http://127.0.0.1:18888`。停止或重启只针对这个容器：`sudo docker stop aishperf-searxng` / `sudo docker restart aishperf-searxng`。

下一步等待 Tavily/Firecrawl 测试 key，以相同小样本对照搜索与访问结果；在找到有效来源前不接入正式更新流程。脱敏部署日志在工作树 `.tmp/searxng-*-remote.log`。
