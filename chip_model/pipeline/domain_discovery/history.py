"""Read-only historical projections. Never backfill from a current business DB."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .store import _file_hash
from .rules import load_phase


def normalized_stored_value(value, *, policy='opaque'):
    """Preserve opaque text/types; normalize only explicit field semantics."""
    if policy not in {'opaque','numeric_quantity','sku_identity'}:
        raise ValueError('unsupported value normalization policy')
    if value is None:
        return None
    if isinstance(value, bool):
        return ('boolean', value)
    if policy == 'numeric_quantity' and isinstance(value, (str,int,float)):
        try:
            number = Decimal(str(value).strip())
            if number.is_finite():
                return ('number', number)
        except InvalidOperation:
            pass
    if isinstance(value,str):
        return ('text', ' '.join(value.split()).casefold() if policy=='sku_identity' else value)
    if isinstance(value,dict):
        return ('object', {key:normalized_stored_value(item) for key,item in value.items()})
    if isinstance(value,list):
        return ('array', [normalized_stored_value(item) for item in value])
    return (type(value).__name__, value)


def same_value(left, right, *, policy='opaque'):
    return normalized_stored_value(left,policy=policy) == normalized_stored_value(right,policy=policy)


def field_normalization(candidate, contract):
    """Quantity ownership comes from frozen unit rules, not a second registry.

    Only the explicit vendor/full-SKU identity policy folds case and spacing.
    Units, URL paths, commands, notes and opaque configuration labels do not.
    """
    table,field = candidate['target_table'],candidate['field_name']
    if field not in contract['fields'].get(table,[]):
        raise ValueError('normalization field outside frozen ownership')
    if table=='chips' and field in {'vendor','chip_model'}:
        return 'sku_identity'
    rule = contract['units'].get(field)
    unit = rule if isinstance(rule,str) else rule.get('unit') if isinstance(rule,dict) else None
    if unit and unit not in {'source_date','explicit_source_format'}:
        return 'numeric_quantity'
    return 'opaque'


def frozen_baseline(store, run_id):
    metadata = store.metadata(run_id, 'baseline')
    path = store.run_path(run_id) / 'data.db'
    if not metadata or not path.is_file() or _file_hash(path) != metadata['baseline_sha256']:
        raise ValueError('frozen baseline missing or hash mismatch')
    return path, metadata


def baseline_projection(store, run_id, binding):
    """Capture the actual frozen value plus ALL corresponding provenance rows.

    Old writes are references only unless their new_value supports the actual
    frozen stored value. No source-count cap and no DDL on the immutable DB.
    """
    path, metadata = frozen_baseline(store, run_id)
    candidate, entity = binding['candidate'], binding['entity']
    frozen = load_phase(store.run_path(run_id)/'skill_bundle',candidate['domain_id'],'review')
    if frozen['bundle_sha256'] != metadata['rule_bundle_hash']:
        raise ValueError('frozen normalization rules and baseline mismatch')
    policy = field_normalization(candidate,frozen['contract'])
    table, field = candidate['target_table'], candidate['field_name']
    result = {'old_value':None, 'old_sources':[], 'old_source_references':[], 'source_membership_recorded':False,
              'old_value_basis':'entity_unresolved', 'normalization_policy':policy, 'baseline_time':metadata['created_at'],
              'baseline_sha256':metadata['baseline_sha256'], 'baseline_ref':'data.db',
              'baseline_row_id':None}
    if table == 'chips':
        if entity['status'] == 'new':
            return {**result, 'old_value_basis':'entity_absent'}
        if entity['status'] != 'existing':
            return result
        row_id = entity['row_id']
    else:
        comparison = binding['comparison']
        if comparison['status'] == 'new_relation':
            return {**result, 'old_value_basis':'relation_absent'}
        if comparison['status'] != 'matched':
            return {**result, 'old_value_basis':'comparison_unresolved'}
        row_id = comparison['row_id']
    # Table/field ownership was verified against the frozen JSON contract by
    # bind_candidate; columns are nevertheless checked before quoting SQL.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        columns = {row['name'] for row in db.execute('PRAGMA table_info("' + table.replace('"','""') + '")')}
        if field not in columns:
            return {**result, 'old_value_basis':'field_not_recorded', 'baseline_row_id':row_id}
        row = db.execute('SELECT * FROM "' + table.replace('"','""') + '" WHERE id=?', (row_id,)).fetchone()
        if row is None:
            raise ValueError('matched frozen baseline row missing')
        old = row[field]
        result.update(old_value=old, old_value_basis='field_empty' if old in (None,'') else 'frozen_value', baseline_row_id=row_id)
        provenance_columns = {row['name'] for row in db.execute('PRAGMA table_info(field_provenance)')}
        result['source_membership_recorded'] = {'table_name','row_id','field_name','new_value'} <= provenance_columns
        if {'table_name','row_id','field_name'} <= provenance_columns:
            references = [dict(p) for p in db.execute('SELECT * FROM field_provenance WHERE table_name=? AND CAST(row_id AS TEXT)=? AND field_name=?', (table,str(row_id),field))]
            result['old_source_references'] = references
            if old not in (None,''):
                result['old_sources'] = [p for p in references if 'new_value' in p and same_value(p['new_value'], old,policy=policy)]
    return result


def project_comparison(record):
    """Small adapter seam for Task14. Saved baseline stays authoritative."""
    result = copy.deepcopy(record)
    if 'old_value' not in result or 'old_value_basis' not in result:
        result.update(old_value=None, old_sources=[], old_value_basis='old_value_not_recorded',
                      baseline_time=None, baseline_sha256=None, value_change_kind='unknown')
    return result


BASELINE_LABELS = {
    'entity_absent':'本轮基线无此实体', 'new_entity':'本轮基线无此实体',
    'relation_absent':'本轮基线无此关系', 'field_empty':'本轮基线字段为空',
    'frozen_value':'本轮冻结基线', 'snapshot':'本轮冻结基线',
    'candidate_snapshot':'候选生成时快照', 'publication_record':'发布前记录',
    'current_reference':'当前库参考值 · 历史旧值未记录',
    'entity_unresolved':'实体未确认，无法比较',
    'comparison_unresolved':'关系条件未确认，无法比较',
    'field_not_recorded':'本轮基线未记录此字段',
    'old_value_not_recorded':'旧值未记录', 'unknown':'旧值未记录',
}
DOMAIN_LABELS = {'identity':'芯片型号','specs':'基础参数','compute':'算力指标',
                 'compatibility':'兼容信息','benchmark':'实测数据','deployment':'部署资料'}


def _time(value):
    if type(value) in (int,float):
        try:
            return datetime.fromtimestamp(value,timezone.utc).isoformat()
        except (ValueError,OverflowError,OSError):
            return None
    return value or None


def display_baseline(record):
    """Display aliases only. Never rewrite stored baseline contracts or values."""
    old = record.get('old_value')
    value = ('未记录' if old is None else '空值' if old == '' else
             json.dumps(old,ensure_ascii=False) if isinstance(old,(dict,list)) else str(old))
    return {'basis_label':BASELINE_LABELS.get(record.get('old_value_basis'),'旧值未记录'),
            'value':value,'time':_time(record.get('baseline_time')) or '未记录'}


def _run_folder(root, run_id, *, allow_missing=False):
    from .contracts import validate_run_id
    validate_run_id(run_id)
    root = Path(root).resolve()
    folder = root / run_id
    if folder.resolve() != folder or (not folder.is_dir() and not allow_missing):
        raise ValueError('运行不存在或路径无效')
    return folder


def _readonly(path):
    """No RunStore constructor, DDL, journal settings or implicit DB creation."""
    if not path.is_file() or path.resolve() != path:
        raise ValueError('运行数据库不存在或路径无效')
    db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return closing(db)


def _json_file(folder,name,default):
    path = folder / name
    if not path.is_file() or path.resolve()!=path:
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (ValueError,OSError):
        return default


def _jsonl_file(folder,name):
    path=folder/name
    if not path.is_file() or path.resolve()!=path:
        return []
    rows=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row=json.loads(line)
            if isinstance(row,dict): rows.append(row)
        except ValueError:
            continue
    return rows


def _receipt(row):
    result=dict(row)
    for key in ('payload','result'):
        raw=result.pop(key+'_json',None)
        if raw is not None: result[key]=json.loads(raw)
    return result


def _control_run(root,run_id):
    from .store import PIPELINE, CONTRACT_VERSION
    with _readonly(Path(root).resolve()/'control.sqlite3') as db:
        row=db.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone()
        if row is None or row['pipeline']!=PIPELINE or row['contract_version']!=CONTRACT_VERSION:
            raise ValueError('不是 V3 隔离运行')
        starts=db.execute('SELECT * FROM starts WHERE run_id=? ORDER BY created_at LIMIT 1',(run_id,)).fetchone()
        return dict(row), _receipt(starts) if starts else {}


def _diagnostic_call(call):
    from .search import redact
    result=redact(call)
    # Typed backend business responses were credential-checked at production.
    # Audit request/response strings are a separate, already redacted protocol.
    if 'business_response' in call:
        result['business_response']=copy.deepcopy(call['business_response'])
    return result


def _diagnostic_operation(operation):
    from .search import redact
    result=redact(operation)
    stage=(operation.get('payload') or {}).get('stage',operation.get('stage'))
    if stage in {'extracting','reviewing'} and isinstance(operation.get('result'),dict):
        result['result']=copy.deepcopy(operation['result'])
    return result


def _safe_metadata(metadata):
    from .search import redact
    result=copy.deepcopy(metadata)
    if 'domain_models.v1' in result:
        result['domain_models.v1']['calls']=[_diagnostic_call(c) for c in result['domain_models.v1'].get('calls',[])]
    for name in ('domain_hermes_diagnostic.v1','domain_hermes_preflight.v1','domain_executor_audit.v1'):
        if name in result: result[name]=redact(result[name])
    return result


def _project_field(identifier,model,saved,entities):
    """Adapt business names while retaining all evidence and condition objects."""
    versions=model.get('versions') or []
    version=versions[-1] if versions else {}
    # Raw program rejections are arbitrary JSON, including falsey scalars and
    # malformed nested objects. Keep the original separate from display access.
    raw_candidate=(saved['candidate'] if 'candidate' in saved else version['candidate']
                   if 'candidate' in version else model.get('original_raw'))
    candidate=copy.deepcopy(raw_candidate)
    if not isinstance(candidate,dict):
        candidate={'raw_value':candidate}
    mapping=lambda item:item if isinstance(item,dict) else {}
    row=copy.deepcopy(saved)
    entity=copy.deepcopy(mapping(row.get('entity')))
    # An identity-domain candidate can have registered, independently reviewed
    # identity proof before a value comparison exists. Show that actual gate.
    identity=next((e for e in entities.values() if any(p.get('logical_candidate_id')==identifier
                  for p in e.get('identity_proofs',[]))),None)
    if not entity and identity is not None:
        entity=copy.deepcopy(identity)
        row['model_proof']=copy.deepcopy(next(p for p in identity['identity_proofs'] if p.get('logical_candidate_id')==identifier))
    key=mapping(candidate.get('entity_key'))
    current=next((e for e in entities.values() if e.get('canonical_key') is not None
                  and e.get('canonical_key')==entity.get('canonical_key')),None)
    if current is not None:
        entity.update(copy.deepcopy(current))
        if current.get('status') not in {'existing','new','not_applicable'}:
            row.update(validated=False,update_eligible=False,value_change_kind='unknown')
    entity.update({k:v for k,v in key.items() if k not in entity})
    hint=mapping(entity.get('hint'))
    if not entity.get('chip_model') and hint.get('full_sku'):
        entity['chip_model']=hint['full_sku']
    evidence=mapping(candidate.get('evidence'))
    source=mapping(row.get('new_source'))
    pagesource=mapping(model.get('source'))
    reviews=model.get('reviews') or []
    review=next((r for r in reversed(reviews) if r.get('version')==version.get('version')), {})
    program=mapping(version.get('program'))
    row.update(id=identifier,logical_candidate_id=identifier,candidate=candidate,entity=entity,
        raw_candidate=copy.deepcopy(raw_candidate),candidate_source_ref=copy.deepcopy(model.get('source')),
        domain_id=candidate.get('domain_id',model.get('domain_id')),
        target_table=candidate.get('target_table'),field_name=candidate.get('field_name'),
        owner_skill='chip-'+str(candidate.get('domain_id',model.get('domain_id') or 'unknown')),
        proposed_value=candidate.get('normalized_value',candidate.get('proposed_value',candidate.get('raw_value'))),
        source_url=source.get('url') or candidate.get('source_url') or pagesource.get('url') or pagesource.get('final_url'),
        source_type=candidate.get('source_type'),confidence=candidate.get('confidence'),
        evidence_text=evidence.get('quote',candidate.get('evidence_text')),
        conditions=copy.deepcopy(candidate.get('conditions') or {}),
        review_status=review.get('verdict',model.get('final_verdict','not_executed')),
        review_reason=review.get('reason'),
        program_status=program.get('status','not_recorded'),
        program_reason=program.get('reason'),
        status=model.get('status',row.get('evidence_status','not_recorded')),
        rejection_reason=(model.get('reason') if model.get('status') in {'program_rejected','semantic_rejected','rejected','unresolved','review_failed','extract_failed'} else None) or row.get('program_rejection_reason'),published=False,
        domain_contract_ref=model.get('domain_contract_ref') or row.get('domain_contract_ref'),
        revision_chain=copy.deepcopy(model.get('revision_chain') or row.get('revision_chain') or []))
    if not row.get('old_value_basis'):
        row.update(old_value=None,old_sources=[],old_value_basis='old_value_not_recorded',baseline_time=None)
    for dimension in ('value_change_kind','source_membership_kind','source_quality_kind','source_change_kind'):
        row.setdefault(dimension,'unknown')
    row.setdefault('evidence_verification_status','not_recorded')
    row.setdefault('update_eligible',False)
    row['baseline_display']=display_baseline(row)
    return row


def read_v3_run(root,run_id):
    """Read one SQLite snapshot plus original optional exports without mutation.

    Only diagnostic model/operation/event traces are redacted. Typed business
    candidates and archived exact public evidence are never generically redacted.
    The current formal DB is neither opened nor used to fill missing history.
    """
    from .store import PIPELINE
    from .search import redact
    from .contracts import validate_run_id
    validate_run_id(run_id)
    control,start=_control_run(root,run_id)
    folder=_run_folder(root,run_id,allow_missing=not control.get('initialized'))
    metadata,operations,tasks,events={ },[],[],[]
    if control.get('initialized'):
        with _readonly(folder/'run.sqlite3') as db:
            db.execute('BEGIN')
            metadata={r['key']:json.loads(r['value_json']) for r in db.execute('SELECT * FROM metadata')}
            if metadata.get('identity')!={'run_id':run_id,'pipeline':PIPELINE,'contract_version':3}:
                raise ValueError('运行身份或版本标记不匹配')
            operations=[_receipt(r) for r in db.execute('SELECT * FROM operations ORDER BY rowid')]
            tasks=[_receipt(r) for r in db.execute('SELECT * FROM tasks ORDER BY rowid')]
            events=[_receipt(r) for r in db.execute('SELECT * FROM events ORDER BY sequence')]
    manifest=_json_file(folder,'manifest.json',{})
    result=metadata.get('domain_runtime_result.v1') or manifest
    models=metadata.get('domain_models.v1') or {}
    entities=(metadata.get('domain_entities.v1') or {}).get('entities',{})
    comparisons=(metadata.get('domain_compare.v1') or {}).get('comparisons',{})
    bindings=(metadata.get('domain_entities.v1') or {}).get('bindings',{})
    skipped=(metadata.get('domain_compare.v1') or {}).get('skipped_inputs',{})
    identifiers=list(dict.fromkeys([*comparisons,*models.get('candidates',{})]))
    fields=[]
    for identifier in identifiers:
        compared=comparisons.get(identifier) or []
        saved=copy.deepcopy((compared or bindings.get(identifier) or [{}])[-1])
        if not compared:
            saved.update(binding_update_eligible=saved.get('update_eligible'),update_eligible=False,
                         evidence_verification_status='not_recorded',value_change_kind='unknown')
        if identifier in skipped:
            saved={**saved,**skipped[identifier],'validated':False,'update_eligible':False}
        fields.append(_project_field(identifier,models.get('candidates',{}).get(identifier,{}),saved,entities))
    # Current entity reconciliation overrides stale task receipts in the view.
    def recorded_identity(e):
        from .store import _hash, _json
        for proof in e.get('identity_proofs',[]):
            if not proof.get('accepted') or proof.get('semantic_review')!='accept': continue
            model=models.get('candidates',{}).get(proof.get('logical_candidate_id'),{})
            version=next((v for v in model.get('versions',[]) if v.get('version')==proof.get('version')),None)
            review=next((r for r in model.get('reviews',[]) if r.get('version')==proof.get('version')),None)
            call=next((c for c in models.get('calls',[]) if c.get('execution_id')==proof.get('execution_id')),None)
            if (model.get('domain_id')=='identity' and model.get('status')=='validated' and version
                    and (version.get('program') or {}).get('status')=='program_valid'
                    and _hash(_json(version.get('candidate')))==proof.get('candidate_sha256')
                    and review and review.get('verdict')=='accept' and call and call.get('phase')=='review'
                    and call.get('status')=='completed' and (call.get('response') or {}).get('verdict')=='accept'):
                return True
        return False
    verified=[copy.deepcopy(e) for e in entities.values() if e.get('status') in {'existing','new'} and recorded_identity(e)]
    search=metadata.get('domain_search.v1') or {}
    pages=metadata.get('domain_pages.v1') or {}
    safe_metadata=_safe_metadata(metadata)
    work=_json_file(folder,'work.json',{})
    if work:
        work['metadata']=_safe_metadata(work.get('metadata',{}))
        for item in work.get('work_items',[]):
            if item.get('kind')=='model_call': item['record']=_diagnostic_call(item['record'])
            elif item.get('kind')=='operation': item['record']=_diagnostic_operation(item['record'])
    payload=start.get('payload') or {}
    started=_time(start.get('created_at'))
    finished=_time(next((e.get('created_at') for e in reversed(events) if e.get('payload',{}).get('kind')=='run_terminal'),None))
    return {**result,'schema_version':PIPELINE,'pipeline':PIPELINE,'contract_version':3,
        'run_id':run_id,'session_id':run_id,'status':control['status'], 'current_stage':control['stage'],
        'domain':payload.get('domain',result.get('domain')), 'chip':payload.get('chip',result.get('chip')),
        'scope_type':payload.get('scope_type',result.get('scope_type')),
        'started_at':started,'finished_at':finished,'active':control['status'] in {'preparing','running','in_progress'},
        'skill_label':DOMAIN_LABELS.get(payload.get('domain'),'开放发现'),
        'field_changes':fields,'verified_entities':verified,
        'stages':(metadata.get('domain_budget.v1') or {}).get('stages',{}),
        'search':search,'pages':pages,'selection':metadata.get('domain_selection.v1') or {},
        'model_calls':[_diagnostic_call(c) for c in models.get('calls',[])], 'linked_tasks':tasks,
        'executor_proof':redact(metadata.get('domain_executor_audit.v1') or {}),
        'baseline':copy.deepcopy(metadata.get('baseline')),
        'formal_database_modified':False,'formal_business_writes':0,'formal_provenance_writes':0,
        'raw_records':{'manifest':redact(manifest),'metadata':safe_metadata,'work':work,
            'operations':[_diagnostic_operation(o) for o in operations],'tasks':tasks,'events':redact(events),
            'export_events':redact(_jsonl_file(folder,'events.jsonl')),
            'export_records':_jsonl_file(folder,'records.jsonl')}}


def list_v3_runs(root):
    """Control index includes preparing/failed runs even before export exists."""
    root=Path(root).resolve()
    if not (root/'control.sqlite3').is_file(): return []
    with _readonly(root/'control.sqlite3') as db:
        ids=[r[0] for r in db.execute("SELECT run_id FROM runs WHERE pipeline='hermes-open-web-v3' AND contract_version=3")]
    results=[]
    for identifier in ids:
        try:
            d=read_v3_run(root,identifier)
            results.append({k:d.get(k) for k in ('schema_version','session_id','run_id','status','started_at',
                'finished_at','active','domain','chip','scope_type','skill_label','statistics','formal_database_modified')})
        except (ValueError,OSError,sqlite3.Error):
            continue
    return sorted(results,key=lambda r:(r.get('started_at') or '',r['run_id']),reverse=True)
