#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
HERMES_CLI="${HERMES_CLI:-hermes}"
PYTHON="${PYTHON:-python3}"
PLUGIN_NAME="aishperf-open-web"

if [[ ! -f "${PROJECT_ROOT}/.hermes-plugins/${PLUGIN_NAME}/plugin.yaml" ]]; then
  echo "找不到 Hermes 插件：${PROJECT_ROOT}/.hermes-plugins/${PLUGIN_NAME}" >&2
  exit 1
fi

skills=(
  chip-identity
  chip-specs
  chip-compute
  chip-compatibility
  chip-benchmark
  chip-deployment
)

# 先只读核验权威包与生成副本；缺合同/参考/哈希漂移时不写安装目录。
"${PYTHON}" "${PROJECT_ROOT}/scripts/sync_domain_skills.py" --check
for skill in "${skills[@]}"; do
  source_dir="${PROJECT_ROOT}/.hermes-skills/${skill}"
  if [[ ! -f "${source_dir}/SKILL.md" || ! -f "${source_dir}/domain.json" ]]; then
    echo "缺少权威 Skill：${source_dir}" >&2
    exit 1
  fi
done

mkdir -p "${HERMES_HOME}/plugins" "${HERMES_HOME}/skills" "${HERMES_HOME}/backups"
backup_dir="${HERMES_HOME}/backups/aishperf-open-web-$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "${backup_dir}/plugins" "${backup_dir}/skills"
if [[ -d "${HERMES_HOME}/plugins/${PLUGIN_NAME}" ]]; then
  cp -R "${HERMES_HOME}/plugins/${PLUGIN_NAME}" "${backup_dir}/plugins/${PLUGIN_NAME}"
fi
rm -rf "${HERMES_HOME}/plugins/${PLUGIN_NAME}"
cp -R "${PROJECT_ROOT}/.hermes-plugins/${PLUGIN_NAME}" "${HERMES_HOME}/plugins/${PLUGIN_NAME}"

for skill in "${skills[@]}"; do
  if [[ -d "${HERMES_HOME}/skills/${skill}" ]]; then
    cp -R "${HERMES_HOME}/skills/${skill}" "${backup_dir}/skills/${skill}"
  fi
done
for shared in _shared installed.manifest.json; do
  if [[ -e "${HERMES_HOME}/skills/${shared}" ]]; then
    cp -R "${HERMES_HOME}/skills/${shared}" "${backup_dir}/skills/${shared}"
  fi
done

# 同步器只从 .hermes-skills 复制显式合同/SKILL/参考并逐文件验证；保留其他工具。
"${PYTHON}" "${PROJECT_ROOT}/scripts/sync_domain_skills.py" --write --install-root "${HERMES_HOME}/skills"

"${HERMES_CLI}" plugins enable "${PLUGIN_NAME}" --no-allow-tool-override
echo "已复制 ${PLUGIN_NAME} 插件和六域 V3 规则；旧版本备份在 ${backup_dir}。实际冻结加载隔离能力仍须运行预检，安装成功不等于加载证明。"
