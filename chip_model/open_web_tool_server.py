"""Loopback-only tools used by Hermes for isolated open-web chip research."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from chip_model.database import get_db_path
from chip_model.pipeline.data_agent import canonicalize_source_url
from chip_model.pipeline.hermes_open_web_contracts import (
    ExtractionResult,
    HERMES_OPEN_WEB_SKILLS,
    PreviewRequest,
    SearchRequest,
    SelectionRequest,
    SKILL_LABELS,
)
from chip_model.pipeline.hermes_open_web_state import (
    HERMES_OPEN_WEB_SCHEMA,
    aggregate_parent_manifest,
    append_jsonl,
    atomic_json,
    load_manifest,
    load_unit_manifest,
    read_artifact,
    read_jsonl,
    resolve_run_dir,
    resolve_unit_dir,
    save_manifest,
    save_unit_manifest,
    stable_candidate_id,
    trace_tool,
)
from chip_model.pipeline.open_web_test import (
    FallbackSearch,
    OpenAICompatibleExtractor,
    SourceRefresher,
    _insert_test_links,
    _now,
    _source_shape,
    _validate_facts,
    database_fingerprints,
    resolve_test_skill,
)
from chip_model.pipeline.source_refresh import validate_source_url


REACHABLE_OUTCOMES = {"new", "changed", "unchanged"}
CATEGORY_SKILLS = {label: skill for skill, label in SKILL_LABELS.items()}


def _selection_fingerprint(request: SelectionRequest) -> str:
    """Return an order-independent identity for an exact selection submission."""
    payload = {
        "skill": request.skill,
        "decisions": sorted(
            (decision.model_dump(mode="json") for decision in request.decisions),
            key=lambda decision: decision["candidate_id"],
        ),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cached_selection_result(
    folder: Path, *, fingerprint: str
) -> dict[str, Any] | None:
    path = folder / "selection_result.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if saved.get("request_fingerprint") != fingerprint:
        raise HTTPException(status_code=409, detail="本轮选择已由不同内容提交。")
    output = saved.get("output")
    if not isinstance(output, dict):
        return None
    return {**output, "cached": True}


def _bearer(value: str | None) -> str:
    prefix = "Bearer "
    if not value or not value.startswith(prefix):
        return ""
    return value[len(prefix):].strip()


def _core_text(text: str, limit: int = 500) -> str:
    compact = "\n".join(
        line.strip() for line in str(text).splitlines() if line.strip()
    )
    return compact[:limit]


def _safe_snapshot(folder: Path, value: str) -> Path:
    path = Path(value).resolve()
    try:
        path.relative_to((folder / "source_snapshots").resolve())
    except ValueError as exc:
        raise ValueError("网页快照路径越界。") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError("网页快照不存在。")
    return path


def _parent_folder(folder: Path) -> Path:
    return folder.parent.parent if folder.parent.name == "units" else folder


def _workspace_db(folder: Path) -> Path:
    return _parent_folder(folder) / "data.db"


def _existing_link_urls(folder: Path) -> set[str]:
    values: set[str] = set()
    with sqlite3.connect(_workspace_db(folder)) as db:
        for row in db.execute("SELECT url FROM link_library WHERE url IS NOT NULL"):
            try:
                values.add(canonicalize_source_url(str(row[0])))
            except Exception:
                continue
    return values


def _cached_preview(folder: Path, candidate: dict[str, Any]) -> dict[str, Any] | None:
    cache = _parent_folder(folder) / "snapshot_cache"
    metadata = read_artifact(cache, f"{candidate['candidate_id']}.json", default={})
    cached_text = cache / f"{candidate['candidate_id']}.txt"
    if not metadata or not cached_text.is_file() or cached_text.is_symlink():
        return None
    local = folder / "source_snapshots" / f"{candidate['candidate_id']}.txt"
    shutil.copyfile(cached_text, local)
    return {
        "schema_version": HERMES_OPEN_WEB_SCHEMA,
        "candidate_id": candidate["candidate_id"],
        "url": candidate["url"],
        "final_url": metadata.get("final_url") or candidate["url"],
        "title": candidate.get("title") or "",
        "access_status": "reachable",
        "fetch_status": metadata.get("fetch_status") or "unchanged",
        "http_status": metadata.get("http_status"),
        "content_hash": metadata.get("content_hash") or "",
        "snapshot_path": str(local),
        "core_text": _core_text(local.read_text(encoding="utf-8")),
        "error": "",
        "cache_hit": True,
    }


def _cache_preview(folder: Path, row: dict[str, Any]) -> None:
    if row.get("access_status") != "reachable" or not row.get("snapshot_path"):
        return
    cache = _parent_folder(folder) / "snapshot_cache"
    cache.mkdir(parents=True, exist_ok=True)
    source_path = _safe_snapshot(folder, str(row["snapshot_path"]))
    target = cache / f"{row['candidate_id']}.txt"
    temporary = target.with_suffix(".txt.tmp")
    shutil.copyfile(source_path, temporary)
    temporary.replace(target)
    atomic_json(cache / f"{row['candidate_id']}.json", {
        "final_url": row.get("final_url") or row.get("url") or "",
        "fetch_status": row.get("fetch_status") or "",
        "http_status": row.get("http_status"),
        "content_hash": row.get("content_hash") or "",
    })


def _first_mention(candidate: dict[str, Any]) -> dict[str, Any]:
    mentions = candidate.get("mentions") or []
    return mentions[0] if mentions else {}


def _validated_extraction(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("模型提取结果必须是 JSON 对象。")
    metadata = {
        key: value[key]
        for key in ("_model", "_usage")
        if key in value
    }
    semantic = {
        key: item
        for key, item in value.items()
        if key not in metadata
    }
    result = ExtractionResult.model_validate(semantic).model_dump()
    result.update(metadata)
    return result


def _normalized_linked_skills(
    *, current_skill: str, suggested_skills: list[str], categories: list[str]
) -> list[str]:
    """Return the bounded, deduplicated level-one Skill fan-out."""
    ordered = [
        *suggested_skills,
        *[CATEGORY_SKILLS[category] for category in categories if category in CATEGORY_SKILLS],
    ]
    return list(dict.fromkeys(
        skill
        for skill in ordered
        if skill in HERMES_OPEN_WEB_SKILLS and skill != current_skill
    ))


def _write_extraction(
    *,
    folder: Path,
    manifest: dict[str, Any],
    skill_name: str,
    candidate: dict[str, Any],
    preview: dict[str, Any],
    decision: dict[str, Any],
    extractor: Any,
    linked_from_skill: str = "",
) -> tuple[int, int, list[str]]:
    _, skill = resolve_test_skill(skill_name)
    selected_fields = (
        list(manifest.get("target_fields") or [])
        if skill_name == manifest.get("skill")
        else list(skill["fields"])
    )
    active_skill = {
        **skill,
        "fields": {field: skill["fields"][field] for field in selected_fields},
    }
    snapshot = _safe_snapshot(folder, str(preview.get("snapshot_path") or ""))
    source_text = snapshot.read_text(encoding="utf-8")
    extracted = _validated_extraction(extractor.extract(
        text=source_text,
        url=str(candidate["url"]),
        chip_hints=[str(manifest.get("target_chip") or "")] if manifest.get("target_chip") else [],
        skill=active_skill,
    ))
    relevant = extracted["relevant"]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if relevant:
        accepted, rejected = _validate_facts(
            extracted,
            source_text=source_text,
            source_url=str(candidate["url"]),
            skill_name=skill_name,
            target_fields=selected_fields,
        )
    known_categories = set(SKILL_LABELS.values())
    categories = list(dict.fromkeys([
        *[str(value) for value in decision.get("matched_categories") or [] if value in known_categories],
        *[str(value) for value in extracted.get("matched_categories") or [] if value in known_categories],
    ]))
    if relevant and skill["label"] not in categories:
        categories.append(skill["label"])
    mention = _first_mention(candidate)
    source_domain, source_format = _source_shape(str(candidate["url"]))
    extracted_fields = [fact["field_name"] for fact in accepted]
    asset = {
        "schema_version": HERMES_OPEN_WEB_SCHEMA,
        "candidate_id": candidate["candidate_id"],
        "url": candidate["url"],
        "final_url": preview.get("final_url") or candidate["url"],
        "source_domain": source_domain,
        "source_format": source_format,
        "source_type": str(extracted.get("source_type") or source_format),
        "discovery_type": "Hermes 开放互联网搜索",
        "discovery_query": mention.get("query") or "",
        "query_strategy": "Hermes + Skill",
        "search_provider": mention.get("provider") or "",
        "search_rank": mention.get("rank"),
        "title": candidate.get("title") or "",
        "information_category": skill["label"] if relevant else "",
        "information_categories": categories,
        "target_fields": selected_fields,
        "extracted_fields": extracted_fields,
        "skill": skill_name,
        "skill_version": skill["version"],
        "chip_model": str(extracted.get("chip_model") or manifest.get("target_chip") or ""),
        "asset_status": "复核通过" if relevant else "复核未通过",
        "fetch_status": preview.get("fetch_status") or "failed",
        "http_status": preview.get("http_status"),
        "content_hash": preview.get("content_hash") or "",
        "snapshot_path": str(snapshot),
        "precise_relevant": relevant,
        "decision_reason": str(extracted.get("reason") or decision.get("reason") or ""),
        "selection_reason": decision.get("reason") or "",
        "linked_from_skill": linked_from_skill,
        "recorded_at": _now(),
    }
    append_jsonl(folder / "url_assets.jsonl", asset)

    db_path = _workspace_db(folder)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT OR REPLACE INTO test_url_classifications "
            "(schema_version,skill_name,canonical_url,search_query,query_strategy,"
            "search_provider,search_rank,title,snippet,target_fields_json,coarse_score,"
            "coarse_reason,fetch_status,precise_relevant,precise_reason,"
            "matched_categories_json,extracted_fields_json,content_hash,snapshot_path,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                HERMES_OPEN_WEB_SCHEMA, skill_name, candidate["url"],
                mention.get("query") or "", "Hermes + Skill", mention.get("provider") or "",
                mention.get("rank"), candidate.get("title") or "", candidate.get("snippet") or "",
                json.dumps(selected_fields, ensure_ascii=False), 0, decision.get("reason") or "",
                preview.get("fetch_status") or "failed", 1 if relevant else 0,
                str(extracted.get("reason") or decision.get("reason") or ""),
                json.dumps(categories, ensure_ascii=False),
                json.dumps(extracted_fields, ensure_ascii=False),
                preview.get("content_hash") or "", str(snapshot), _now(),
            ),
        )
        model_name = str(extracted.get("_model") or getattr(extractor, "model_name", ""))
        chip_model = str(extracted.get("chip_model") or manifest.get("target_chip") or "")
        for fact in accepted:
            db.execute(
                "INSERT INTO test_extracted_records "
                "(schema_version,skill_name,target_table,chip_model,field_name,proposed_value,"
                "unit,source_url,evidence_text,evidence_location,confidence,validation_status,"
                "extractor_model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    HERMES_OPEN_WEB_SCHEMA, skill_name, skill["target_table"], chip_model,
                    fact["field_name"], fact["proposed_value"], fact["unit"], fact["source_url"],
                    fact["evidence_text"], fact["evidence_location"], fact["confidence"],
                    "validated", model_name, _now(),
                ),
            )
            append_jsonl(folder / "extracted_facts.jsonl", {
                "schema_version": HERMES_OPEN_WEB_SCHEMA,
                "status": "validated", "skill": skill_name, "chip_model": chip_model, **fact,
            })
        for fact in rejected:
            db.execute(
                "INSERT INTO test_extracted_records "
                "(schema_version,skill_name,target_table,chip_model,field_name,proposed_value,"
                "unit,source_url,evidence_text,evidence_location,confidence,validation_status,"
                "rejection_reason,extractor_model,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    HERMES_OPEN_WEB_SCHEMA, skill_name, skill["target_table"], chip_model,
                    fact.get("field_name") or "unknown", fact.get("proposed_value") or "",
                    fact.get("unit") or "", fact.get("source_url") or candidate["url"],
                    fact.get("evidence_text") or "", fact.get("evidence_location") or "正文",
                    fact.get("confidence") or "low", "rejected", fact.get("reason") or "校验未通过",
                    model_name, _now(),
                ),
            )
            append_jsonl(folder / "extracted_facts.jsonl", {
                "schema_version": HERMES_OPEN_WEB_SCHEMA,
                "status": "rejected", "skill": skill_name, "chip_model": chip_model, **fact,
            })
        db.commit()
    return len(accepted), len(rejected), categories


def create_open_web_tool_app(
    *,
    source_db: str | Path | None = None,
    token: str | None = None,
    search_provider_factory: Callable[..., Any] = FallbackSearch,
    refresher_factory: Callable[..., Any] = SourceRefresher,
    extractor_factory: Callable[[], Any] = OpenAICompatibleExtractor,
) -> FastAPI:
    source = Path(source_db or os.getenv("DATA_DB_PATH") or get_db_path()).resolve()
    expected_token = token if token is not None else os.getenv("AISH_PERF_OPEN_WEB_TOOL_TOKEN", "")
    app = FastAPI(title="AISHPerf Hermes Open-Web Tools", docs_url=None, redoc_url=None)

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if not expected_token:
            raise HTTPException(status_code=503, detail="工具服务令牌未配置。")
        if _bearer(authorization) != expected_token:
            raise HTTPException(status_code=401, detail="工具服务认证失败。")

    def checked_run(
        run_id: str, unit_id: str = ""
    ) -> tuple[Path, dict[str, Any], Path, dict[str, Any]]:
        try:
            parent_folder = resolve_run_dir(source, run_id)
            parent = load_manifest(parent_folder)
            if parent.get("schema_version") != HERMES_OPEN_WEB_SCHEMA:
                return parent_folder, parent, parent_folder, parent
            selected_unit = unit_id
            if not selected_unit:
                units = parent.get("units") or []
                if len(units) != 1:
                    raise ValueError("全量运行必须提供单元 ID。")
                selected_unit = str(units[0]["unit_id"])
            folder = resolve_unit_dir(source, run_id, selected_unit)
            return folder, load_unit_manifest(folder), parent_folder, parent
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def save_checked_manifest(
        folder: Path, manifest: dict[str, Any], parent_folder: Path
    ) -> None:
        if folder == parent_folder:
            save_manifest(folder, manifest)
            return
        save_unit_manifest(folder, manifest)
        aggregate_parent_manifest(parent_folder)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "loopback_only": True}

    @app.post("/v1/search", dependencies=[Depends(authorize)])
    def search(request: SearchRequest) -> dict[str, Any]:
        started = _now()
        started_clock = time.monotonic()
        folder, manifest, parent_folder, _ = checked_run(request.run_id, request.unit_id)
        if (
            manifest.get("skill") != request.skill
            or str(manifest.get("target_chip") or "") != request.target_chip
            or str(manifest.get("scope_type") or "chip") != request.scope_type
        ):
            raise HTTPException(status_code=409, detail="请求目标与运行清单不一致。")
        if manifest.get("stage") not in {"waiting_for_hermes", "searching"}:
            raise HTTPException(status_code=409, detail="当前单元状态不允许重复搜索。")
        if read_jsonl(folder, "search_results.jsonl"):
            raise HTTPException(status_code=409, detail="本轮搜索已经执行。")
        provider = search_provider_factory(proxy=os.getenv("DATA_AGENT_PROXY") or None)
        existing_urls = _existing_link_urls(folder)
        candidates: dict[str, dict[str, Any]] = {}
        raw_count = 0
        unsafe_count = 0
        search_errors: list[dict[str, str]] = []
        for query in request.queries:
            try:
                rows = provider.search(query.query, 10)
            except Exception as exc:
                search_errors.append({"query": query.query, "error": str(exc)[:500]})
                continue
            finally:
                for check in getattr(provider, "last_diagnostics", []):
                    append_jsonl(folder / "events.jsonl", {
                        "schema_version": HERMES_OPEN_WEB_SCHEMA,
                        "time": _now(), "stage": "search_provider",
                        "query": query.query, **check,
                        "message": check.get("reason") or "搜索源返回有效结果。",
                    })
            for result in rows:
                raw_count += 1
                try:
                    validate_source_url(result.url)
                    canonical = canonicalize_source_url(result.url)
                except Exception as exc:
                    unsafe_count += 1
                    append_jsonl(folder / "search_results.jsonl", {
                        "schema_version": HERMES_OPEN_WEB_SCHEMA,
                        "status": "unsafe", "url": result.url,
                        "title": result.title, "snippet": result.snippet,
                        "mentions": [{
                            "query": query.query, "reason": query.reason,
                            "rank": result.rank, "provider": result.provider or provider.name,
                        }],
                        "error": str(exc)[:500],
                    })
                    continue
                candidate_id = stable_candidate_id(canonical)
                mention = {
                    "query": query.query,
                    "reason": query.reason,
                    "rank": result.rank,
                    "provider": result.provider or provider.name,
                }
                if candidate_id not in candidates:
                    candidates[candidate_id] = {
                        "schema_version": HERMES_OPEN_WEB_SCHEMA,
                        "candidate_id": candidate_id,
                        "status": "candidate",
                        "url": canonical,
                        "existing_in_link_library": canonical in existing_urls,
                        "asset_origin": "existing" if canonical in existing_urls else "new",
                        "title": result.title,
                        "snippet": result.snippet,
                        "mentions": [mention],
                    }
                else:
                    candidates[candidate_id]["mentions"].append(mention)
        atomic_json(folder / "hermes_search_plan.json", {
            "schema_version": HERMES_OPEN_WEB_SCHEMA,
            "run_id": request.run_id,
            "unit_id": request.unit_id or manifest.get("unit_id") or "",
            "scope_type": request.scope_type,
            "skill": request.skill,
            "target_chip": request.target_chip,
            "queries": [item.model_dump() for item in request.queries],
        })
        for row in candidates.values():
            append_jsonl(folder / "search_results.jsonl", row)
        manifest["stage"] = "awaiting_preview"
        manifest["queries"] = [item.query for item in request.queries]
        manifest["counts"].update({
            "queries": 10,
            "search_results": raw_count,
            "unique_candidates": len(candidates),
            "unsafe_candidates": unsafe_count,
            "search_errors": len(search_errors),
        })
        if not candidates:
            manifest.update({
                "status": "failed" if search_errors else "partial",
                "stage": "completed",
                "finished_at": _now(),
                "error": "搜索工具未返回可用候选 URL。",
            })
        save_checked_manifest(folder, manifest, parent_folder)
        candidate_rows = list(candidates.values())
        candidate_ids = [str(row["candidate_id"]) for row in candidate_rows]
        compact_candidates = [
            {
                "candidate_id": row["candidate_id"],
                "url": row["url"],
                "title": str(row.get("title") or "")[:160],
                "snippet": str(row.get("snippet") or "")[:240],
                "query_hit_count": len(row.get("mentions") or []),
                "asset_origin": row.get("asset_origin") or "new",
            }
            for row in candidate_rows
        ]
        output = {
            "ok": True,
            "run_id": request.run_id,
            "unit_id": request.unit_id or manifest.get("unit_id") or "",
            "skill": request.skill,
            "raw_result_count": raw_count,
            "unique_candidate_count": len(candidates),
            "unsafe_count": unsafe_count,
            "search_errors": search_errors,
            "terminal": not candidates,
            # Keep this explicit list compact and lossless. Hermes must pass it
            # unchanged to open_web_preview; full mentions stay in the artifact.
            "candidate_ids": candidate_ids,
            "candidates": compact_candidates,
        }
        trace_tool(
            folder, tool="open_web_search", status="success", started_at=started,
            finished_at=_now(), input_summary={"queries": 10, "skill": request.skill},
            output_summary={
                "raw_results": raw_count, "unique_candidates": len(candidates),
                "unsafe": unsafe_count, "duration_ms": round((time.monotonic() - started_clock) * 1000),
            },
        )
        return output

    @app.post("/v1/preview", dependencies=[Depends(authorize)])
    def preview(request: PreviewRequest) -> dict[str, Any]:
        started = _now()
        started_clock = time.monotonic()
        folder, manifest, parent_folder, _ = checked_run(request.run_id, request.unit_id)
        if str(manifest.get("scope_type") or "chip") != request.scope_type:
            raise HTTPException(status_code=409, detail="请求范围与运行单元不一致。")
        if manifest.get("stage") != "awaiting_preview":
            raise HTTPException(status_code=409, detail="必须先完成搜索，才能预览候选。")
        candidates = {
            row["candidate_id"]: row
            for row in read_jsonl(folder, "search_results.jsonl")
            if row.get("status") == "candidate" and row.get("candidate_id")
        }
        if not candidates:
            raise HTTPException(status_code=409, detail="本轮尚无搜索候选。")
        if set(request.candidate_ids) != set(candidates):
            raise HTTPException(status_code=409, detail="必须一次预览本轮全部去重候选。")
        existing = read_jsonl(folder, "url_previews.jsonl")
        if existing:
            return {"ok": True, "run_id": request.run_id, "previews": existing, "cached": True}
        ordered = [candidates[candidate_id] for candidate_id in request.candidate_ids]
        preview_by_id: dict[str, dict[str, Any]] = {}
        uncached: list[dict[str, Any]] = []
        for candidate in ordered:
            cached = _cached_preview(folder, candidate)
            if cached is None:
                uncached.append(candidate)
            else:
                preview_by_id[str(candidate["candidate_id"])] = cached
        link_ids = _insert_test_links(
            _workspace_db(folder),
            [{
                "url": row["url"], "title": row.get("title") or "",
                "snippet": row.get("snippet") or "", "query": _first_mention(row).get("query") or "",
                "provider": _first_mention(row).get("provider") or "", "rank": _first_mention(row).get("rank") or 0,
            } for row in uncached],
            skill_name=str(manifest["skill"]),
        ) if uncached else []
        if link_ids:
            refresher = refresher_factory(
                db_path=_workspace_db(folder),
                snapshot_dir=folder / "source_snapshots",
                proxy=os.getenv("DATA_AGENT_PROXY") or None,
                max_bytes=20 * 1024 * 1024,
                max_attempts=2,
            )
            summary = refresher.run(link_ids, apply_state=True, force=True)
            fetched_rows = summary.results
        else:
            fetched_rows = []
        fetched = {
            canonicalize_source_url(str(row.get("requested_url") or "")): row
            for row in fetched_rows
        }
        previews: list[dict[str, Any]] = []
        for candidate in ordered:
            cached = preview_by_id.get(str(candidate["candidate_id"]))
            if cached is not None:
                append_jsonl(folder / "url_previews.jsonl", cached)
                previews.append(cached)
                continue
            item = fetched.get(candidate["url"], {})
            outcome = str(item.get("outcome") or "failed")
            snapshot_value = str(item.get("normalized_snapshot_path") or "")
            core = ""
            if outcome in REACHABLE_OUTCOMES and snapshot_value:
                try:
                    core = _core_text(_safe_snapshot(folder, snapshot_value).read_text(encoding="utf-8"))
                except Exception as exc:
                    outcome = "failed"
                    item = {**item, "error_message": str(exc)}
            row = {
                "schema_version": HERMES_OPEN_WEB_SCHEMA,
                "candidate_id": candidate["candidate_id"],
                "url": candidate["url"],
                "final_url": item.get("final_url") or candidate["url"],
                "title": candidate.get("title") or "",
                "access_status": "reachable" if outcome in REACHABLE_OUTCOMES else "failed",
                "fetch_status": outcome,
                "http_status": item.get("http_status"),
                "content_hash": item.get("content_hash") or "",
                "snapshot_path": snapshot_value,
                "core_text": core,
                "error": str(item.get("error_message") or "")[:1000],
                "cache_hit": False,
            }
            _cache_preview(folder, row)
            append_jsonl(folder / "url_previews.jsonl", row)
            previews.append(row)
        reachable = sum(row["access_status"] == "reachable" for row in previews)
        manifest["stage"] = "awaiting_selection"
        manifest["counts"].update({"previewed": len(previews), "reachable": reachable})
        save_checked_manifest(folder, manifest, parent_folder)
        trace_tool(
            folder, tool="open_web_preview", status="success", started_at=started,
            finished_at=_now(), input_summary={"candidates": len(request.candidate_ids)},
            output_summary={
                "previewed": len(previews), "reachable": reachable,
                "duration_ms": round((time.monotonic() - started_clock) * 1000),
            },
        )
        return {
            "ok": True, "run_id": request.run_id,
            "unit_id": request.unit_id or manifest.get("unit_id") or "",
            "previews": previews, "cached": False,
        }

    @app.post("/v1/submit-selection", dependencies=[Depends(authorize)])
    def submit_selection(request: SelectionRequest) -> dict[str, Any]:
        started = _now()
        started_clock = time.monotonic()
        folder, manifest, parent_folder, parent_manifest = checked_run(
            request.run_id, request.unit_id
        )
        if manifest.get("skill") != request.skill:
            raise HTTPException(status_code=409, detail="提交 Skill 与运行清单不一致。")
        if str(manifest.get("scope_type") or "chip") != request.scope_type:
            raise HTTPException(status_code=409, detail="提交范围与运行单元不一致。")
        request_fingerprint = _selection_fingerprint(request)
        cached = _cached_selection_result(folder, fingerprint=request_fingerprint)
        if cached is not None:
            return cached
        if manifest.get("stage") != "awaiting_selection":
            raise HTTPException(status_code=409, detail="必须先完成全部候选预览，才能提交决定。")
        if read_jsonl(folder, "url_decisions.jsonl"):
            raise HTTPException(
                status_code=409,
                detail="本轮选择正在处理，或旧版本未保存可重放的提交结果。",
            )
        candidates = {
            row["candidate_id"]: row
            for row in read_jsonl(folder, "search_results.jsonl")
            if row.get("status") == "candidate" and row.get("candidate_id")
        }
        previews = {
            row["candidate_id"]: row
            for row in read_jsonl(folder, "url_previews.jsonl")
            if row.get("candidate_id")
        }
        decision_ids = {item.candidate_id for item in request.decisions}
        if not candidates or decision_ids != set(candidates) or decision_ids != set(previews):
            raise HTTPException(status_code=409, detail="必须为本轮每个已预览候选提交一次决定。")
        extractor = extractor_factory()
        selected_count = 0
        rejected_url_count = 0
        validated_count = 0
        rejected_fact_count = 0
        linked_count = 0
        linked_seen: set[tuple[str, str]] = set()
        for decision_model in request.decisions:
            decision = decision_model.model_dump()
            candidate = candidates[decision_model.candidate_id]
            preview_row = previews[decision_model.candidate_id]
            normalized_skills: list[str] = []
            if not decision_model.selected:
                rejected_url_count += 1
                append_jsonl(folder / "url_decisions.jsonl", {
                    "schema_version": HERMES_OPEN_WEB_SCHEMA,
                    **decision,
                    "model_suggested_skills": list(decision_model.suggested_skills),
                    "suggested_skills": normalized_skills,
                })
                continue
            if preview_row.get("access_status") != "reachable":
                raise HTTPException(status_code=409, detail="不可访问候选不能被选中。")
            selected_count += 1
            categories = list(decision_model.matched_categories)
            if request.scope_type == "discovery":
                append_jsonl(folder / "new_chip_candidates.jsonl", {
                    "schema_version": HERMES_OPEN_WEB_SCHEMA,
                    "candidate_id": decision_model.candidate_id,
                    "url": candidate["url"],
                    "title": candidate.get("title") or "",
                    "reason": decision_model.reason,
                    "matched_categories": categories,
                    "recorded_at": _now(),
                })
            try:
                accepted, rejected, categories = _write_extraction(
                    folder=folder, manifest=manifest, skill_name=request.skill,
                    candidate=candidate, preview=preview_row, decision=decision,
                    extractor=extractor,
                )
            except Exception as exc:
                append_jsonl(folder / "events.jsonl", {
                    "schema_version": HERMES_OPEN_WEB_SCHEMA, "time": _now(),
                    "stage": "extract", "status": "failed",
                    "message": "选中网页的完整字段提取失败。",
                    "details": {"candidate_id": decision_model.candidate_id, "error": str(exc)[:1000]},
                })
                accepted, rejected = 0, 0
            validated_count += accepted
            rejected_fact_count += rejected
            normalized_skills = _normalized_linked_skills(
                current_skill=request.skill,
                suggested_skills=list(decision_model.suggested_skills),
                categories=categories,
            )
            append_jsonl(folder / "url_decisions.jsonl", {
                "schema_version": HERMES_OPEN_WEB_SCHEMA,
                **decision,
                "model_suggested_skills": list(decision_model.suggested_skills),
                "suggested_skills": normalized_skills,
            })
            for suggested in normalized_skills:
                key = (decision_model.candidate_id, suggested)
                if key in linked_seen:
                    continue
                linked_seen.add(key)
                linked = {
                    "schema_version": HERMES_OPEN_WEB_SCHEMA,
                    "parent_skill": request.skill,
                    "skill": suggested,
                    "candidate_id": decision_model.candidate_id,
                    "url": candidate["url"],
                    "status": "running",
                    "depth": 1,
                    "created_at": _now(),
                }
                try:
                    child_accepted, child_rejected, _ = _write_extraction(
                        folder=folder, manifest=manifest, skill_name=suggested,
                        candidate=candidate, preview=preview_row, decision=decision,
                        extractor=extractor, linked_from_skill=request.skill,
                    )
                    validated_count += child_accepted
                    rejected_fact_count += child_rejected
                    linked.update({
                        "status": "success" if child_accepted else "partial",
                        "validated": child_accepted, "rejected": child_rejected,
                    })
                except Exception as exc:
                    linked.update({"status": "failed", "error": str(exc)[:1000]})
                append_jsonl(folder / "linked_tasks.jsonl", linked)
                linked_count += 1

        manifest["counts"].update({
            "selected": selected_count,
            "rejected_urls": rejected_url_count,
            "validated": validated_count,
            "rejected": rejected_fact_count,
            "extracted": validated_count + rejected_fact_count,
            "linked_tasks": linked_count,
            "new_chip_candidates": (
                selected_count if request.scope_type == "discovery" else 0
            ),
        })
        manifest["status"] = (
            "success" if validated_count else
            "partial" if manifest["counts"].get("reachable") else "failed"
        )
        manifest["stage"] = "completed"
        manifest["finished_at"] = _now()
        before = parent_manifest.get("formal_database_before") or manifest.get("formal_database_before") or {}
        after = database_fingerprints(source)
        manifest["formal_database_after"] = after
        manifest["formal_database_modified"] = before.get("db") != after.get("db")
        if manifest["formal_database_modified"]:
            manifest["status"] = "failed"
            manifest["audit_warning"] = "检测到正式数据库主体文件发生变化。"
        save_checked_manifest(folder, manifest, parent_folder)
        output = {
            "ok": manifest["status"] in {"success", "partial"},
            "run_id": request.run_id,
            "unit_id": request.unit_id or manifest.get("unit_id") or "",
            "status": manifest["status"],
            "selected_count": selected_count,
            "rejected_url_count": rejected_url_count,
            "validated_fact_count": validated_count,
            "rejected_fact_count": rejected_fact_count,
            "linked_task_count": linked_count,
            "formal_database_modified": manifest["formal_database_modified"],
            "cached": False,
        }
        atomic_json(folder / "selection_result.json", {
            "schema_version": HERMES_OPEN_WEB_SCHEMA,
            "request_fingerprint": request_fingerprint,
            "output": output,
        })
        trace_tool(
            folder, tool="open_web_submit_selection", status=manifest["status"],
            started_at=started, finished_at=_now(),
            input_summary={"decisions": len(request.decisions), "selected": selected_count},
            output_summary={
                **output, "duration_ms": round((time.monotonic() - started_clock) * 1000),
            },
        )
        return output

    return app


app = create_open_web_tool_app()
