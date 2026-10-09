"""Admin-only manual data-agent controls, separate from unauthenticated read APIs."""

from __future__ import annotations

import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chip_model.database import get_db, get_db_path
from chip_model.pipeline.data_agent import canonicalize_source_url
from chip_model.pipeline.test_workspace import read_test_seeds, save_test_seed


def control_root() -> Path:
    return (get_db_path().resolve().parent / "manual_control").resolve()


def admin_token_valid(provided: str | None) -> bool:
    path = control_root() / "admin_token"
    if not path.is_file() or not provided:
        return False
    expected = path.read_text(encoding="utf-8").strip()
    return bool(expected) and hmac.compare_digest(provided, expected)


def list_seed_urls(mode: str) -> list[dict[str, Any]]:
    if mode == "test":
        return read_test_seeds()
    if mode != "formal":
        raise ValueError("运行模式仅支持 formal 或 test。")
    with get_db(readonly=True) as db:
        rows = db.execute(
            "SELECT id,url,description,category,vendor,access_method,accessible "
            "FROM link_library WHERE COALESCE(url,'')!='' ORDER BY id DESC LIMIT 1000"
        ).fetchall()
    return [dict(row) for row in rows]


def save_seed_url(
    mode: str, url: str, description: str, category: str, vendor: str = "",
    *, enabled: bool = True,
) -> dict[str, Any]:
    canonical = canonicalize_source_url(url)
    description = description.strip()
    category = category.strip()
    vendor = vendor.strip()
    if not description or not category:
        raise ValueError("请填写来源描述和数据类别。")
    if len(description) > 300 or len(category) > 80 or len(vendor) > 80:
        raise ValueError("来源描述、类别或厂商字段过长。")
    if mode == "test":
        return save_test_seed(canonical, description, category, vendor, enabled=enabled)
    if mode != "formal":
        raise ValueError("运行模式仅支持 formal 或 test。")
    raise ValueError("正式来源维护已暂停；请在芯片隔离测试中配置来源。")


def queue_manual_run(
    mode: str,
    limit: int = 5,
    *,
    pipeline: str = "known_sources",
    skill: str = "chip-specs",
    chips: list[str] | None = None,
    queries: list[str] | None = None,
    target_fields: list[str] | None = None,
) -> dict[str, Any]:
    if mode not in {"formal", "test"}:
        raise ValueError("运行模式仅支持 formal 或 test。")
    if mode == "formal":
        raise ValueError("正式巡检已暂停；目前只允许手动芯片隔离测试。")
    if not 1 <= limit <= 10:
        raise ValueError("手动执行的测试种子数必须在 1–10 之间。")
    if pipeline not in {"known_sources", "open_web"}:
        raise ValueError("测试类型仅支持 known_sources 或 open_web。")
    if pipeline == "open_web":
        from chip_model.pipeline.open_web_test import (
            _resolve_target_fields,
            resolve_test_skill,
        )

        skill, skill_config = resolve_test_skill(skill)
        clean_target_fields = list(dict.fromkeys(
            str(value).strip() for value in (target_fields or [])
            if str(value).strip()
        ))
        if clean_target_fields:
            _resolve_target_fields(skill_config, clean_target_fields)
    else:
        clean_target_fields = []
    clean_chips = [str(value).strip() for value in (chips or []) if str(value).strip()]
    clean_queries = [str(value).strip() for value in (queries or []) if str(value).strip()]
    if pipeline == "open_web" and not clean_chips and not clean_queries:
        raise ValueError("开放互联网测试至少填写一个芯片名称或搜索词。")
    if len(clean_chips) > 5 or len(clean_queries) > 5:
        raise ValueError("单次测试最多填写 5 个芯片和 5 个补充搜索词。")
    if any(len(value) > 160 for value in clean_chips + clean_queries):
        raise ValueError("芯片名称或搜索词过长。")
    root = control_root()
    pending = root / "requests"
    pending.mkdir(parents=True, exist_ok=True)
    for path in pending.glob("*.json"):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if old.get("mode") == mode and old.get("status") in {"queued", "running"}:
            raise RuntimeError(f"已有 {mode} 手动执行请求正在排队或运行。")
    in_process = pipeline == "open_web"
    request = {"id": uuid.uuid4().hex, "mode": mode, "limit": limit,
               "pipeline": pipeline, "skill": skill,
               "chips": clean_chips, "queries": clean_queries,
               "target_fields": clean_target_fields,
               "status": "running" if in_process else "queued",
               "created_at": datetime.now(timezone.utc).isoformat()}
    if in_process:
        request["started_at"] = request["created_at"]
    target = pending / (request["id"] + ".json")
    temporary = pending / (request["id"] + ".tmp")
    temporary.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)
    return request


def execute_open_web_manual_request(request_id: str) -> dict[str, Any]:
    """Execute one claimed open-web request inside the API background worker."""
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("无效手动测试标识。")
    path = control_root() / "requests" / f"{request_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("pipeline") != "open_web" or value.get("status") != "running":
        return value
    try:
        from chip_model.pipeline.open_web_test import run_open_web_test

        result = run_open_web_test(
            source_db=get_db_path(),
            chips=list(value.get("chips") or []),
            skill_name=str(value.get("skill") or "chip-specs"),
            extra_queries=list(value.get("queries") or []),
            target_fields=list(value.get("target_fields") or []),
            search_limit=int(value["limit"]),
            visit_limit=int(value["limit"]),
        )
        value["result"] = {
            key: result.get(key) for key in (
                "session_id", "status", "db_path", "counts",
                "formal_database_modified",
            )
        }
        value.update(
            status="completed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            message="开放互联网测试已完成；结果只保存在隔离测试区。",
        )
    except Exception as exc:
        value.update(
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=str(exc)[:1500],
        )
    temporary = path.with_suffix(".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return value


def latest_manual_result(mode: str) -> dict[str, Any] | None:
    root = control_root() / "requests"
    if not root.is_dir():
        return None
    results = []
    for path in root.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if item.get("mode") == mode:
            results.append(item)
    return max(results, key=lambda item: item.get("created_at", "")) if results else None


class DomainStartConflict(ValueError):
    """The request key already binds a different canonical request."""


def queue_domain_run(payload, *, idempotency_key=None, data_root=None, timeout=1800):
    """Shared CLI/API V3 entry; never starts an in-process background task."""
    from chip_model.pipeline.domain_discovery.contracts import normalize_start, _start_request_id
    from chip_model.pipeline.domain_discovery.store import RunStore
    canonical = normalize_start(payload, idempotency_key)
    key = _start_request_id(payload.get('request_id')) or _start_request_id(idempotency_key)
    generated = key is None
    key = key or uuid.uuid4().hex
    root = Path(data_root) if data_root is not None else get_db_path().resolve().parent
    store = RunStore(root / 'domain_runs')
    try:
        if canonical['resume_run_id']:
            binding = store.queue_resume(key, canonical)
        else:
            binding = store.start(key, canonical, 'run-' + uuid.uuid4().hex, timeout=timeout)
    except ValueError as exc:
        if '幂等键 payload 冲突' in str(exc):
            raise DomainStartConflict(str(exc)) from exc
        raise
    receipt = {'schema_version': 'hermes-open-web-v3', 'request_id': binding['request_key'],
        'run_id': binding['run_id'], 'session_id': binding['run_id'],
        'status': binding['status'], 'stage': binding['stage'], 'idempotency_key_generated': generated}
    if generated:
        receipt['idempotency_notice'] = 'Save request_id for retry; retry after a lost response cannot guarantee deduplication without the original key.'
    return receipt
