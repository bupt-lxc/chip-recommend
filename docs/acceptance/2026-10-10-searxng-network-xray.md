# SearXNG 网络诊断与 Xray 安装

2026-10-10，按用户授权对腾讯云宿主机与 SearXNG 容器分别执行 IPv4 DNS、TCP、TLS、HTTP 分层检查。

| 目标 | 宿主机 | SearXNG 容器 |
|---|---|---|
| Google 搜索 | TCP 443 连接约 5 秒超时，未进入 TLS | 同样在 TCP 阶段超时 |
| Brave 搜索 | TCP 443 连接约 5 秒超时，未进入 TLS | 同样在 TCP 阶段超时 |
| 百度 | TCP/TLS 成功，HTTP 302 | TCP/TLS 成功，HTTP 302 |
| Tavily API 控制项 | TCP/TLS 成功，HTTP 200 | TCP/TLS 成功，HTTP 200 |

Google、Brave 的两处解析均落在 `104.244.*`，值得进一步做可信 DNS/代理对照，但本轮不据此断言具体 DNS 干扰机制。证据支持特定站点的直连网络/DNS问题，不支持将失败仅归因于 Docker、HTTP 解析或 SearXNG 资源限制。代理是合理的下一步验证；百度此前的 CAPTCHA 是另外的上游限制。没有代理配置前，尚不能证明代理一定恢复搜索。

Xray 安装到 Ubuntu 用户的 `/home/ubuntu/utils/xray/`，目录归属 ubuntu。使用 [XTLS/Xray-core 官方发布](https://github.com/XTLS/Xray-core/releases/tag/v26.3.27) 的 Linux amd64 包，SHA256 与官方发布资产元数据一致：

```text
23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae
```

单独 `.dgst` 文件下载遇到 TLS 超时，改用同一官方发布页的资产 SHA256 校验，没有使用未验证镜像。执行 `xray version` 成功，版本为 `26.3.27`。入口为 `/home/ubuntu/utils/xray/xray`，实际二进制在 `v26.3.27/xray`，同时保留 geoip/geosite 数据与安装记录。

**未启动代理、未注册自启服务、未改系统路由或 SearXNG 出站配置。** 等用户提供 `/home/ubuntu/utils/xray/config.json` 后，再校验配置、限制代理监听范围，确保 Docker 容器能访问代理，并重做小规模直连/代理对照。仅监听宿主 `127.0.0.1` 的代理不能直接供桥接网络容器使用。

远端证据：`/home/ubuntu/aishperf-search/searxng/network-diagnosis-20261010.json`、`/home/ubuntu/utils/xray/installation.json`；脱敏执行日志在工作树 `.tmp/`。

## 2026-10-10：接入用户代理配置后的验证

- 原始配置与服务器适配配置均通过 Xray 校验。原始文件保存为 `/home/ubuntu/utils/xray/config.original.json`，实际配置为 `config.json`，权限 0600；未改动用户本地文件。
- `aishperf-xray` 已启动，SOCKS 监听 Docker 私有网桥 `172.20.0.1:10808`，没有监听公网。服务器适配配置将此入口的流量送到原配置的 `proxy` 出口。
- 仅 SearXNG 配置 `outgoing.proxies` 使用 `socks5h://172.20.0.1:10808`；原设置备份为 `settings.before-xray.yml`。正式应用与 Hermes 配置未修改。
- Xray 当前 active，尚未设置开机自启；服务器重启后需先启动 `aishperf-xray`。SearXNG 容器自身有 restart 策略，但不能据此认定代理随系统启动。

| 验证 | 实测 |
| --- | --- |
| 主机直连 Google / Brave | TCP 阶段超时 |
| 主机通过代理访问百度 / Tavily 首页 | HTTP 200 |
| 主机通过代理访问 Google / Brave 首页 | TLS EOF / connection reset；代理尚不能完整访问这些目标 |
| SearXNG Google，L40S 规格 | 0 条，access denied |
| SearXNG Brave，新加速器发布 | 0 条，certificate validation failed |
| SearXNG 百度，国产新品 | 0 条，CAPTCHA |
| SearXNG Bing，NVIDIA L40S 规格 | 10 条，但前五为厂商首页、驱动页和百科，未出现目标规格页 |
| SearXNG Bing，AMD MI350 规格 | 10 条，但前五为厂商首页、驱动页、百科和商店，未出现目标规格页 |
| SearXNG Bing，L40S / 消费级显卡训练 | 两次均 0 条，未报上游错误 |

这轮共 7 次单引擎搜索，未设置 categories，返回结果中的 engines 验证为 Bing。首轮重启后过早请求造成连接重置，服务就绪后重新测试；连接重置不计作搜索失败样本。

结论：直连网络障碍确实存在，代理接入也确实使部分目标可访问，但不能据此说 SearXNG 已修好。剩余问题包括目标访问拒绝、TLS 校验、验证码及 Bing 结果相关性；目前仍推荐此前验证有效的 Tavily 作为主搜索及普通页面提取来源，Firecrawl 按需补充。未关闭 TLS 校验，也未进行业务数据写入或心跳调度变更。

原始测试结果位于服务器 SearXNG 目录的 `proxy-validation-20261010.json` 和 `proxy-bing-control-20261010.json`；本地脱敏日志位于 `.tmp/searxng-proxy-retry-remote.log` 和 `.tmp/searxng-bing-proxy-remote.log`。正式 `chip-recommend` 启动时间仍为 `2026-09-30T10:30:32.132586456Z`。

代理参数依据 [SearXNG 官方 outgoing 文档](https://docs.searxng.org/admin/settings/settings_outgoing.html)。
