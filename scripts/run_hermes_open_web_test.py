#!/usr/bin/env python3
"""Start one domain discovery test; production v3 wiring arrives in Task 12."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chip_model.database import get_db_path  # noqa: E402
from chip_model.pipeline.domain_discovery.contracts import normalize_start  # noqa: E402


class _RejectField(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.error("--field 已停用，请按数据域运行")


def _timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout 必须为 1–1800 秒的整数") from None
    if not 1 <= timeout <= 1800:
        raise argparse.ArgumentTypeError("timeout 必须为 1–1800 秒的整数")
    return timeout


def _execute_domain(request: dict, dependencies: dict) -> dict:
    module_name = "chip_model.pipeline.domain_discovery.runtime"
    try:
        runtime = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
        return {
            "run_id": None, "request_id": dependencies["request_id"],
            "status": "runtime_unavailable", "artifact_path": None,
            "error": "v3 数据域执行器尚未接线",
        }
    return runtime.run_domain(request, dependencies)


def main(argv: list[str] | None = None, *, executor=None) -> int:
    parser = argparse.ArgumentParser(description="Hermes 数据域开放互联网发现隔离测试", allow_abbrev=False)
    parser.add_argument("--db", default=str(get_db_path()), help="只读来源数据库")
    parser.add_argument("--domain", default=argparse.SUPPRESS, help="数据域：identity/specs/compute/compatibility/benchmark/deployment")
    parser.add_argument("--skill", default=argparse.SUPPRESS, help="兼容别名 chip-<domain>，与 --domain 至少提供一个")
    parser.add_argument("--chip", default=argparse.SUPPRESS, help="可选芯片线索；省略时按数据域执行开放互联网发现")
    parser.add_argument("--field", action=_RejectField, nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=_timeout, default=1800, help="累计时间预算，1–1800 秒（默认 1800）")
    parser.add_argument("--request-id", default=argparse.SUPPRESS, help="请求幂等键；省略时生成")
    parser.add_argument("--resume", default=argparse.SUPPRESS, help="续跑已有 v3 运行 ID")
    args = parser.parse_args(argv)
    values = vars(args)
    payload = {key: values[key] for key in ("domain", "skill", "chip", "request_id") if key in values}
    if "resume" in values:
        payload["resume_run_id"] = values["resume"]
    try:
        request = normalize_start(payload)
    except ValueError as error:
        parser.error(str(error))
    request_id = payload["request_id"].strip() if payload.get("request_id") is not None else uuid.uuid4().hex
    dependencies = {"source_db": Path(args.db), "timeout": args.timeout, "request_id": request_id}
    try:
        result = dict((executor if executor is not None else _execute_domain)(request, dependencies))
    except Exception as error:
        result = {"run_id": None, "request_id": request_id, "status": "error", "artifact_path": None, "error": str(error)}
    result["request_id"] = request_id
    result.setdefault("run_id", None)
    result.setdefault("artifact_path", None)
    result.setdefault("status", "error")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"success", "no_findings"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
