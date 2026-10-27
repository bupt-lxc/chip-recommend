#!/bin/bash
echo "旧 Skill 安装入口已归档，不能覆盖新六域。请使用 scripts/install_hermes_open_web_plugin.sh。" >&2
exit 1

# 历史目录列表仅用于说明旧入口，不再执行。
mkdir -p /root/.hermes/skills/chip-catalog
mkdir -p /root/.hermes/skills/chip-enrich
mkdir -p /root/.hermes/skills/chip-recommend-cli
mkdir -p /root/.hermes/skills/chip-selector-agent
mkdir -p /root/.hermes/skills/source-refresh
mkdir -p /root/.hermes/scripts
echo "dirs created"
