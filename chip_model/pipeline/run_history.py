"""Read-only, session-scoped run views and immutable candidate baselines."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from chip_model.database import get_db

TABLES = {'chips', 'models', 'chip_model_benchmarks', 'chip_model_compatibility', 'deployment_guides'}


def list_open_web_runs(base, *, final_only=False):
    """V3 persistent queue and legacy artifacts share one read-only history."""
    from .domain_discovery.history import list_v3_runs
    from .open_web_test import list_open_web_test_runs
    legacy = list_open_web_test_runs(base, final_only=final_only)
    # final_only was the old latest-success shortcut. V3 never hides failure,
    # no-findings or a preparing/running receipt that needs inspection.
    runs = list_v3_runs(Path(base).resolve().parent / 'domain_runs') + legacy
    return sorted(runs, key=lambda r:r.get('started_at') or '', reverse=True)


def read_open_web_run(base, session_id):
    from .domain_discovery.history import read_v3_run, _readonly
    from .domain_discovery.contracts import validate_run_id
    from .open_web_test import read_open_web_test_run
    validate_run_id(session_id)
    root = Path(base).resolve().parent / 'domain_runs'
    if (root / session_id).exists():
        return read_v3_run(root, session_id)
    if (root / 'control.sqlite3').is_file():
        with _readonly(root / 'control.sqlite3') as db:
            if db.execute('SELECT 1 FROM runs WHERE run_id=?',(session_id,)).fetchone():
                return read_v3_run(root, session_id)
    legacy = read_open_web_test_run(base, session_id)
    legacy['independent_review_status'] = 'not_recorded'
    legacy['evidence_quality'] = 'unknown'
    return legacy


def decode(value, default=None):
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def has_table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def entity_baseline(db, candidate):
    table = candidate['target_table']
    field = candidate['field_name']
    key = decode(candidate['entity_key_json'], {})
    result = {'old_value': None, 'entity': key, 'old_sources': [], 'found': False}
    if table not in TABLES or not isinstance(key, dict) or not key:
        return result
    columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    if field not in columns or not set(key).issubset(columns):
        return result
    lookup = {'id': key['id']} if key.get('id') else key
    where = ' AND '.join(f'"{k}"=?' for k in lookup)
    rows = db.execute(f'SELECT * FROM "{table}" WHERE {where} LIMIT 2', tuple(lookup.values())).fetchall()
    if len(rows) != 1:
        return result
    row = dict(rows[0])
    result.update(old_value=row[field], found=True,
                  entity={k: row[k] for k in ('id', 'chip_model', 'vendor', 'model_name', 'name') if k in row})
    if has_table(db, 'field_provenance'):
        provenance = db.execute(
            'SELECT source_url,source_type,confidence,updated_at,new_value FROM field_provenance '
            'WHERE table_name=? AND row_id=? AND field_name=? ORDER BY id DESC',
            (table, str(row['id']), field)).fetchall()
        # Only cite provenance for this value, not an unrelated historic write.
        result['old_sources'] = [dict(p) for p in provenance if p['new_value'] == row[field]][:5]
    return result


def capture_candidate_baseline(db, candidate_id):
    # Existing isolated sessions may predate the additive schema migration.
    # Keep this in the caller's transaction; never executescript/commit here.
    db.execute('CREATE TABLE IF NOT EXISTS candidate_baselines ('
               'candidate_id INTEGER PRIMARY KEY REFERENCES extraction_candidates(id) ON DELETE CASCADE,'
               'baseline_json TEXT NOT NULL,captured_at TEXT NOT NULL)')
    row = db.execute('SELECT * FROM extraction_candidates WHERE id=?', (candidate_id,)).fetchone()
    if row:
        baseline = entity_baseline(db, dict(row))
        db.execute('INSERT OR IGNORE INTO candidate_baselines (candidate_id,baseline_json,captured_at) VALUES (?,?,?)',
                   (candidate_id, json.dumps(baseline, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))


def resolve_session(base, session):
    base = Path(base).resolve()
    if session == 'formal':
        return base
    if not re.fullmatch(r'[A-Za-z0-9_-]+', session):
        raise ValueError('无效会话标识')
    root = (base.parent / 'test_runs').resolve()
    folder = root / session
    path = (folder / 'data.db').resolve()
    if folder.is_symlink() or path.parent != folder or not path.is_file():
        raise ValueError('测试会话不存在或路径无效')
    try:
        marker = json.loads((folder / 'test_mode.json').read_text(encoding='utf-8'))
        if marker.get('mode') != 'test' or Path(marker.get('db_path', '')).resolve() != path:
            raise ValueError('测试会话标记无效')
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('测试会话标记无效') from exc
    return path


def run_summary(db, row, session):
    item = dict(row)
    counts = dict(db.execute('SELECT status,COUNT(*) FROM data_agent_jobs WHERE cycle_run_id=? GROUP BY status', (row['id'],)).fetchall())
    first = db.execute("SELECT input_json FROM data_agent_jobs WHERE cycle_run_id=? AND job_type='source_refresh' ORDER BY id LIMIT 1", (row['id'],)).fetchone()
    item.update(session=session, key=f'{session}:{row["id"]}', statuses=counts,
                title=decode(first[0], {}).get('url', '数据抓取运行') if first else '数据抓取运行',
                candidate_count=db.execute('SELECT COUNT(*) FROM extraction_candidates WHERE cycle_run_id=?', (row['id'],)).fetchone()[0])
    item.pop('counts_json', None)
    active = any(counts.get(s, 0) for s in ('running', 'queued', 'awaiting_agent'))
    item['active'] = active
    # Old run_cycle summaries sometimes mark finished_at after deterministic fetch only.
    if active:
        item['finished_at'] = None
    return item


def list_runs(base, mode='test', limit=10, offset=0):
    root = Path(base).resolve().parent / 'test_runs'
    sessions = ['formal'] if mode == 'formal' else sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    results, warnings = [], []
    for session in sessions:
        try:
            path = resolve_session(base, session)
            with get_db(path, readonly=True) as db:
                if not has_table(db, 'data_agent_jobs'):
                    continue
                sql = "SELECT * FROM update_runs WHERE run_type='data_agent_cycle'"
                sql += " AND mode='test'" if session != 'formal' else " AND mode!='test'"
                for row in db.execute(sql):
                    results.append(run_summary(db, row, session))
        except (ValueError, OSError, sqlite3.Error) as exc:
            warnings.append({'session': session, 'message': str(exc)})
    results.sort(key=lambda r: (r['started_at'] or '', r['key']), reverse=True)
    return {'runs': results[offset:offset+limit], 'total': len(results), 'offset': offset, 'limit': limit, 'warnings': warnings}


def run_detail(base, session, run_id):
    from chip_model.pipeline.data_agent import get_data_agent_status, SKILL_FIELD_TARGETS
    path = resolve_session(base, session)
    data = get_data_agent_status(db_path=path, run_id=run_id, limit=10000)
    cycle = data.get('latest_cycle')
    if not cycle or (session != 'formal' and cycle['mode'] != 'test'):
        raise ValueError('运行不存在')
    jobs = data['jobs']
    with get_db(path, readonly=True) as db:
        summary = run_summary(db, cycle, session)
        source_runs = set()
        first_run = cycle.get('counts', {}).get('source_run_id')
        if first_run:
            source_runs.add(int(first_run))
        for job in jobs:
            inp, out = job['input'], job['output']
            if inp.get('source_run_id'):
                source_runs.add(int(inp['source_run_id']))
            # Also include failed source visits that created no semantic tasks.
            if out.get('checked_at') and job.get('link_id'):
                source_runs.update(r[0] for r in db.execute('SELECT run_id FROM source_checks WHERE link_id=? AND checked_at=?', (job['link_id'], out['checked_at'])))
        sources, events = [], list(cycle.get('events', []))
        for rid in sorted(source_runs):
            sources.extend(dict(r) for r in db.execute('SELECT * FROM source_checks WHERE run_id=? ORDER BY id', (rid,)))
            events.extend({**dict(r), 'details': decode(r['details_json'], {})} for r in db.execute('SELECT * FROM update_run_events WHERE run_id=?', (rid,)))
        changes = []
        for candidate in data['candidates']:
            baseline = entity_baseline(db, candidate)
            basis = 'current_reference' if baseline['found'] else 'unknown'
            if has_table(db, 'candidate_baselines'):
                snap = db.execute('SELECT baseline_json FROM candidate_baselines WHERE candidate_id=?', (candidate['id'],)).fetchone()
                if snap:
                    baseline, basis = decode(snap[0], baseline), 'candidate_snapshot'
            published = False
            for publication in data['publications']:
                if candidate['id'] in publication['candidate_ids'] and publication['status'] == 'success':
                    baseline['old_value'] = publication['old_values'].get(candidate['field_name'])
                    basis, published = 'publication_record', True
                    # Current provenance is not evidence for the pre-publication value.
                    if not has_table(db, 'candidate_baselines') or not db.execute('SELECT 1 FROM candidate_baselines WHERE candidate_id=?', (candidate['id'],)).fetchone():
                        baseline['old_sources'] = []
            changes.append({**candidate, **baseline, 'old_value_basis': basis, 'published': published,
                            'change_kind': 'unknown' if basis == 'unknown' else 'unchanged' if baseline['old_value'] == candidate['proposed_value'] else 'added' if baseline['old_value'] in (None, '') else 'changed'})
    parameters = {}
    for event in events:
        if event['stage'] == 'run_parameters':
            parameters.update(event.get('details') or {})
    runtime = {'models': [], 'total_tokens': 0, 'recorded_responses': 0}
    # Local diagnostic evidence is session-owned. Read only bounded, known files;
    # never return request headers, prompts or arbitrary credential-bearing keys.
    if session != 'formal':
        for job in jobs:
            response_path = path.parent / f'job-{job["id"]}-response.json'
            if response_path.is_file() and not response_path.is_symlink() and response_path.stat().st_size < 1_000_000:
                try:
                    response = json.loads(response_path.read_text(encoding='utf-8'))
                    model = response.get('model')
                    if isinstance(model, str) and model not in runtime['models']:
                        runtime['models'].append(model)
                    usage = response.get('usage') or {}
                    runtime['total_tokens'] += int(usage.get('total_tokens') or 0)
                    runtime['recorded_responses'] += 1
                except (ValueError, OSError, TypeError, AttributeError):
                    continue
        if runtime['models'] and 'model' not in parameters:
            parameters['model'] = ', '.join(runtime['models'])
        trace_path = path.parent / 'trace.jsonl'
        if trace_path.is_file() and not trace_path.is_symlink() and trace_path.stat().st_size < 2_000_000:
            job_ids = {j['id'] for j in jobs}
            for line in trace_path.read_text(encoding='utf-8').splitlines():
                record = decode(line, {})
                if isinstance(record, dict) and record.get('job_id') in job_ids and record.get('stage') in {'model_request', 'model_response', 'extraction_rejected'}:
                    detail = {k: record[k] for k in ('job_id', 'http_status', 'finish_reason') if k in record}
                    events.append({'id': 0, 'created_at': record.get('time'), 'actor': 'model-worker',
                                   'stage': record['stage'], 'status': 'failed' if record['stage'] == 'extraction_rejected' else 'recorded',
                                   'message': f"模型调用记录 · 子任务 #{record['job_id']}", 'details': detail})
    planned = [{'skill': skill, 'fields': sorted(SKILL_FIELD_TARGETS.get(skill, {}).get('chips', []))}
               for skill in sorted({j['skill_name'] for j in jobs if j['job_type'] == 'agent_extract'})]
    stages = []
    for label, types, explanation in [
        ('检查已有来源', ['source_refresh'], '访问本次选定来源并保存网页内容。'),
        ('查找新资料', ['agent_url_discovery', 'agent_discovery'], '从网页中寻找可能有用的新链接。'),
        ('访问候选网页', ['source_target_refresh'], '访问新发现的详情页并保存结果。'),
        ('提取信息', ['agent_extract'], '提取候选信息，并核对格式与原文依据。')]:
        subset = [j for j in jobs if j['job_type'] in types]
        counts = {s: sum(j['status'] == s for j in subset) for s in {j['status'] for j in subset}}
        status = 'not_run' if not subset else 'running' if any(counts.get(s) for s in ('queued', 'running', 'awaiting_agent')) else 'failed' if counts.get('failed') == len(subset) else 'partial' if counts.get('failed') else 'success'
        stages.append({'label': label, 'status': status, 'counts': counts, 'description': explanation})
    stages.append({'label': '保存测试结果', 'status': 'success' if changes else 'not_run',
                   'counts': {}, 'description': '结果只保存在本次隔离测试区，不会修改正式数据库。'})
    events.sort(key=lambda e: (e.get('created_at') or '', e.get('id', 0)))
    return {**data, **summary, 'run': summary, 'parameters': parameters, 'planned_fields': planned,
            'sources': sources, 'timeline': events, 'field_changes': changes, 'stages': stages, 'runtime': runtime}
