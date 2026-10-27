#!/usr/bin/env bash
set -euo pipefail

echo "旧心跳 Skill 安装入口已归档，不能覆盖新六域。请使用 scripts/install_hermes_open_web_plugin.sh；心跳启用须另行验收。" >&2
exit 1

# Run on the current primary host, where Hermes Gateway and the primary app
# container share the same host. This keeps the existing Cron job ID and schedule.
PROJECT_ROOT=${1:-$(cd "$(dirname "$0")/.." && pwd)}
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
readonly CONTAINER_NAME="chip-recommend"
readonly BACKUP_DIR="$HERMES_ROOT/backups/primary-migration-$(date +%Y%m%d-%H%M%S)"

test -f "$PROJECT_ROOT/config/hermes_primary_gate.json"
test -f "$PROJECT_ROOT/config/hermes_primary_worker.json"
test -f "$PROJECT_ROOT/config/hermes_legacy_disabled.json"
test -f "$PROJECT_ROOT/.hermes-skills/data-update-orchestrator/SKILL.md"
test -f "$PROJECT_ROOT/scripts/hermes_data_agent_gate.py"
test -f "$PROJECT_ROOT/scripts/hermes_data_agent_worker.py"
test "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" = "true"
DATA_DIR=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER_NAME")
test -n "$DATA_DIR"
test -f "$DATA_DIR/data.db"

mkdir -p "$BACKUP_DIR" "$HERMES_ROOT/scripts" "$HERMES_ROOT/skills"
for target in \
  "$HERMES_ROOT/scripts/data_agent_gate.json" \
  "$HERMES_ROOT/scripts/data_agent_gate_61.json" \
  "$HERMES_ROOT/scripts/local_docker_worker.json" \
  "$HERMES_ROOT/scripts/source_refresh_gate.json" \
  "$HERMES_ROOT/scripts/hermes_data_agent_gate.py" \
  "$HERMES_ROOT/scripts/hermes_data_agent_worker.py" \
  "$HERMES_ROOT/skills/data-update-orchestrator/SKILL.md" \
  "$HERMES_ROOT/cron/jobs.json"; do
  if test -f "$target"; then
    cp -a "$target" "$BACKUP_DIR/$(basename "$target")"
  fi
done

cp "$PROJECT_ROOT/config/hermes_primary_gate.json" "$HERMES_ROOT/scripts/data_agent_gate.json"
cp "$PROJECT_ROOT/config/hermes_primary_gate.json" "$HERMES_ROOT/scripts/data_agent_gate_61.json"
cp "$PROJECT_ROOT/config/hermes_primary_worker.json" "$HERMES_ROOT/scripts/local_docker_worker.json"
cp "$PROJECT_ROOT/config/hermes_legacy_disabled.json" "$HERMES_ROOT/scripts/source_refresh_gate.json"
cp "$PROJECT_ROOT/scripts/hermes_data_agent_gate.py" "$HERMES_ROOT/scripts/hermes_data_agent_gate.py"
cp "$PROJECT_ROOT/scripts/hermes_data_agent_worker.py" "$HERMES_ROOT/scripts/hermes_data_agent_worker.py"
for skill_dir in \
  "$PROJECT_ROOT"/.hermes-skills/url-* \
  "$PROJECT_ROOT"/.hermes-skills/data-* \
  "$PROJECT_ROOT"/.hermes-skills/chip-* \
  "$PROJECT_ROOT"/.hermes-skills/model-* \
  "$PROJECT_ROOT"/.hermes-skills/benchmark-* \
  "$PROJECT_ROOT"/.hermes-skills/deployment-*; do
  [[ -d "$skill_dir" ]] || continue
  skill_name=$(basename "$skill_dir")
  target="$HERMES_ROOT/skills/$skill_name"
  if [[ -d "$target" ]]; then
    mkdir -p "$BACKUP_DIR/skills/$skill_name"
    cp -a "$target/." "$BACKUP_DIR/skills/$skill_name/"
  fi
  mkdir -p "$target"
  cp -R "$skill_dir/." "$target/"
done
chmod 600 "$HERMES_ROOT/scripts/"{data_agent_gate.json,data_agent_gate_61.json,local_docker_worker.json,source_refresh_gate.json}
chmod 700 "$HERMES_ROOT/scripts/"{hermes_data_agent_gate.py,hermes_data_agent_worker.py}

echo "Hermes primary target: local Docker container $CONTAINER_NAME"
echo "Previous configuration: $BACKUP_DIR"
echo "Existing Hermes Cron job and schedule were not changed."
