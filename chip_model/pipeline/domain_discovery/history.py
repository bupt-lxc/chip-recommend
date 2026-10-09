"""Read-only historical projections. Never backfill from a current business DB."""
from __future__ import annotations

import copy
import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation

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
