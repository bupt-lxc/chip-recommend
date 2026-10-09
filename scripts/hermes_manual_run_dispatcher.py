#!/usr/bin/env python3
"""Host-side dispatcher for token-protected manual requests in the shared data volume."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from chip_model.pipeline.domain_discovery.markers import is_domain_request


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_request(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def dispatch(path: Path, *, container: str, formal_job_id: str, test_job_id: str,
             hermes_python: str, docker_bin: str = "/usr/bin/docker",
             test_job_active: bool = True) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if is_domain_request(value):
        return value
    if value.get("status") != "queued" or value.get("mode") not in {"formal", "test"}:
        return value
    if container != "chip-recommend":
        raise ValueError("手动执行器只允许 chip-recommend 容器。")
    limit = int(value["limit"])
    if not 1 <= limit <= 10:
        raise ValueError("请求数量必须在 1–10 之间。")
    mode = value["mode"]
    if mode == "formal":
        value.update(status="failed", finished_at=stamp(),
                     error="正式巡检已暂停；只允许手动芯片隔离测试。")
        write_request(path, value)
        return value
    value.update(status="running", started_at=stamp())
    write_request(path, value)
    try:
        pipeline = str(value.get("pipeline") or "known_sources")
        if pipeline == "open_web":
            command = [docker_bin, "exec", container, "python", "scripts/run_open_web_test.py",
                       "--search-limit", str(limit), "--visit-limit", str(limit),
                       "--skill", str(value.get("skill") or "chip-specs")]
            for chip in value.get("chips") or []:
                command.extend(("--chip", str(chip)))
            for query in value.get("queries") or []:
                command.extend(("--query", str(query)))
            for field in value.get("target_fields") or []:
                command.extend(("--field", str(field)))
        else:
            command = [docker_bin, "exec", container, "python", "scripts/run_data_agent.py",
                       "test" if mode == "test" else "run", "--limit", str(limit)]
        if mode == "formal":
            command.append("--resume-stale-targets")
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "数据智能体启动失败")[:1400])
        summary = json.loads(result.stdout)
        if not isinstance(summary, dict):
            raise ValueError("执行结果不是 JSON 对象。")
        value["result"] = {key: summary.get(key) for key in (
            "run_id", "status", "planned_jobs", "awaiting_agent_jobs", "db_path", "session_id"
        ) if key in summary}
        if pipeline == "open_web":
            value.update(status="completed", finished_at=stamp(),
                         message="开放互联网测试已完成；结果只保存在隔离测试区。")
            write_request(path, value)
            return value
        if mode == "test" and not test_job_active:
            value.update(status="partial", finished_at=stamp(),
                         message="隔离抓取已完成；测试 Hermes Agent 暂停，语义提取尚未执行。正式数据未改动。")
            write_request(path, value)
            return value
        job_id = test_job_id if mode == "test" else formal_job_id
        if not job_id:
            raise RuntimeError("Hermes 心跳任务尚未配置，已执行确定性阶段但未唤醒语义 Agent。")
        subprocess.Popen(
            [hermes_python, "-m", "hermes_cli.main", "cron", "run", job_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        value.update(status="completed", finished_at=stamp(),
                     message="确定性抓取阶段已执行，Hermes Agent 已收到手动唤醒；后续进度见页面日志。")
    except Exception as exc:
        value.update(status="failed", finished_at=stamp(), error=str(exc)[:1500])
    write_request(path, value)
    return value


def cron_job_active(job_id: str, jobs_path: Path) -> bool:
    try:
        jobs = json.loads(jobs_path.read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        return False
    return any(job.get("id") == job_id and job.get("enabled") is True for job in jobs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("/root/chip-recommend/data"))
    parser.add_argument("--container", default="chip-recommend")
    parser.add_argument("--formal-job-id", required=True)
    parser.add_argument("--test-job-id", required=True)
    parser.add_argument("--hermes-python", default="/usr/local/lib/hermes-agent/venv/bin/python")
    parser.add_argument("--hermes-jobs", type=Path, default=Path("/root/.hermes/cron/jobs.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    requests = args.data_root.resolve() / "manual_control" / "requests"
    while True:
        if requests.is_dir():
            for path in sorted(requests.glob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if is_domain_request(value):
                        continue
                    if value.get("status") == "queued":
                        dispatch(path, container=args.container, formal_job_id=args.formal_job_id,
                                 test_job_id=args.test_job_id, hermes_python=args.hermes_python,
                                 test_job_active=cron_job_active(args.test_job_id, args.hermes_jobs))
                    elif value.get("status") == "running" and value.get("started_at"):
                        started = datetime.fromisoformat(str(value["started_at"]))
                        if datetime.now(timezone.utc) - started > timedelta(hours=1):
                            value.update(status="failed", finished_at=stamp(),
                                         error="执行器中断或超时；请查看 Hermes 日志后重新提交。")
                            write_request(path, value)
                except (OSError, ValueError) as exc:
                    print(f"跳过无效手动请求 {path.name}: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(3)


if __name__ == "__main__":
    raise SystemExit(main())
