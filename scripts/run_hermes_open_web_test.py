#!/usr/bin/env python3
"""Start and execute one persistent V3 domain discovery test."""

from __future__ import annotations

import argparse
import json
import sys
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


def main(argv: list[str] | None = None, *, executor=None) -> int:
    parser = argparse.ArgumentParser(description="Hermes 数据域开放互联网发现隔离测试", allow_abbrev=False)
    parser.add_argument("--db", default=argparse.SUPPRESS, help="只读来源 SQLite；其父目录作为 API/host 共享 data-root，续跑省略时继承原来源")
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
        normalize_start(payload)
    except ValueError as error:
        parser.error(str(error))
    from chip_model.pipeline.manual_control import queue_domain_run
    from chip_model.pipeline.domain_discovery.store import RunStore
    from scripts.hermes_domain_worker import work_once
    receipt = None
    try:
        source = Path(values.get('db', get_db_path())).resolve()
        explicit = source if 'db' in values or not payload.get('resume_run_id') else None
        receipt = queue_domain_run(payload, data_root=source.parent, source_db=explicit, timeout=args.timeout)
        results = work_once(source.parent, run_id=receipt['run_id'], executor=executor,
            dependencies={'source_db': source} if explicit is not None else {})
        if results:
            result = dict(results[0])
        else:
            store = RunStore(source.parent / 'domain_runs')
            current = store.get_run(receipt['run_id'])
            if current['status'] in {'success','no_findings','partial','failed','interrupted'}:
                result = store.metadata(receipt['run_id'], 'domain_runtime_result.v1') or receipt
            else:
                result = {**receipt, 'status': current['status'], 'stage': current['stage']}
        result = {**receipt, **result, 'request_id': receipt['request_id'], 'run_id': receipt['run_id']}
        result.setdefault('artifact_path', str(source.parent / 'domain_runs' / receipt['run_id']))
    except Exception as error:
        result = {**(receipt or {}), 'request_id': payload.get('request_id'), 'status': 'failed', 'error': str(error)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"success", "no_findings"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
