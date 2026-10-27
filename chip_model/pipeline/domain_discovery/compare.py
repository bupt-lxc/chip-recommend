"""Immutable value comparison and independent source membership/quality.

Only isolated run metadata and audit events are written. Evidence quality is
based on actual archived reviewed quotes, never official-looking domains,
source_type ranks or model confidence. No fetching or formal publishing.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing

from .entities import EntityResolver
from .history import baseline_projection, frozen_baseline, same_value
from .models import ModelService, ReviewResponse, FactCandidate, phase_messages, decode_content, validate_candidate
from .pages import PageService
from .search import canonical_url
from .rules import load_phase
from .store import _hash, _json


def source_summary(membership, quality):
    if membership not in {'added','unchanged','unknown'} or quality not in {'upgraded','conflict','unchanged','unknown'}:
        raise ValueError('invalid independent source dimensions')
    if quality in {'conflict','upgraded'}:
        return quality
    if membership == 'added':
        return 'added'
    return 'unknown' if 'unknown' in {membership,quality} else 'unchanged'


def _snapshot(store, run_id, evidence):
    """Verify saved capture metadata and exact quote bytes, with no refetch."""
    record = (store.metadata(run_id, PageService.KEY) or {}).get('snapshots', {}).get(evidence['snapshot_id'])
    if record is None or record['body_hash'] != evidence['body_hash']:
        raise ValueError('saved evidence snapshot/hash missing')
    root = store.run_path(run_id)
    directory = root / 'source_snapshots'
    if directory.resolve() != directory:
        raise ValueError('unsafe archived snapshot directory')
    payload = {}
    for kind in ('raw','text'):
        name = record[kind + '_ref']
        if not isinstance(name,str) or not re.fullmatch(r'[0-9a-f]{64}\.(raw|txt)',name):
            raise ValueError('unsafe archived snapshot reference')
        path = directory / name
        if path.resolve() != path or not path.is_relative_to(root):
            raise ValueError('unsafe archived snapshot path')
        body = path.read_bytes()
        digest = record['raw_hash' if kind=='raw' else 'body_hash']
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError('archived snapshot hash mismatch')
        payload[kind] = body
    text = payload['text'].decode('utf-8')
    start,end = evidence['start'],evidence['end']
    if (isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int)
            or not 0 <= start < end <= len(text) or text[start:end] != evidence['quote']):
        raise ValueError('archived exact quote mismatch')
    return {**record,'text':text}


def _saved_review(store, run_id, identifier, version_number):
    """Read a specific saved accepted review; never initialize a service."""
    state = store.metadata(run_id, ModelService.KEY) or {}
    row = state.get('candidates', {}).get(identifier)
    if row is None or not row.get('versions'):
        raise ValueError('archived model proof missing')
    if type(version_number) is not int or version_number < 0:
        raise ValueError('archived original model version missing')
    version = next((v for v in row['versions'] if v['version']==version_number),None)
    if version is None:
        raise ValueError('archived original model version unavailable')
    review = next((r for r in row.get('reviews', []) if r['version']==version['version']),None)
    logical = 'review-' + _hash(_json([identifier,version['version']]))
    call = next((c for c in state.get('calls', []) if c['logical_id']==logical and c['phase']=='review'
                 and c['status']=='completed' and c.get('response',{}).get('verdict')=='accept'),None)
    if version.get('program',{}).get('status')!='program_valid' or review is None or review['verdict']!='accept' or call is None:
        raise ValueError('archived accepted review missing')
    return row,version,review,call


def _reviewed_proof(store, run_id, identifier, version_number):
    """Read old runs without initializing services, claiming leases or writing."""
    row,version,review,call = _saved_review(store,run_id,identifier,version_number)
    entities = store.metadata(run_id, EntityResolver.KEY) or {}
    bindings = [b for b in entities.get('bindings',{}).get(identifier,[])
                if b['model_proof']['version']==version_number and b['validated']]
    if not bindings:
        raise ValueError('archived final binding missing')
    bound = bindings[-1]
    proof = bound['model_proof']
    if (not bound['validated'] or proof['version']!=version['version']
            or proof['candidate_sha256']!=_hash(_json(version['candidate']))
            or proof['execution_id']!=call['execution_id'] or bound['candidate']!=version['candidate']):
        raise ValueError('archived binding/review mismatch')
    current = next((e for e in entities.get('entities',{}).values()
                    if e['canonical_key']==bound['entity'].get('canonical_key')),None)
    if (bound['entity']['status'] not in {'existing','new','not_applicable'}
            or bound['entity']['status']!='not_applicable' and (current is None or current['status'] not in {'existing','new'})):
        raise ValueError('archived identity is no longer resolved')
    _,baseline = frozen_baseline(store,run_id)
    if call['bundle_sha256'] != baseline['rule_bundle_hash']:
        raise ValueError('archived review rule bundle mismatch')
    _verify_review_input(store,run_id,row,version,review,call,proof)
    return bound, row



def _archive_selected_baseline(path, metadata, candidate, contract):
    """Replay the V3 model's read-only baseline query; never initialize old runs."""
    result = {'baseline_sha256':metadata['baseline_sha256'], 'created_at':metadata['created_at'],
              'chips':[], 'relations':[], 'field_provenance':[]}
    key,table = candidate['entity_key'],candidate['target_table']
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        columns = {r[1] for r in db.execute('PRAGMA table_info(chips)')}
        names = [name for name in ('vendor','chip_model') if name in columns and key.get(name)]
        if 'chip_model' in names:
            sql = 'SELECT * FROM chips WHERE '+' AND '.join('"'+name+'"=?' for name in names)
            result['chips'] = [dict(r) for r in db.execute(sql,[key[name] for name in names])]
        if table!='chips' and table in contract['fields']:
            columns = {r[1] for r in db.execute('PRAGMA table_info("'+table+'")')}
            names = [name for name in key if name in columns]
            if names:
                sql = 'SELECT * FROM "'+table+'" WHERE '+' AND '.join('"'+name+'"=?' for name in names)
                result['relations'] = [dict(r) for r in db.execute(sql,[key[name] for name in names])]
        columns = {r[1] for r in db.execute('PRAGMA table_info(field_provenance)')}
        if {'table_name','row_id'} <= columns:
            for target,rows in [('chips',result['chips']),(table,result['relations'])]:
                for row in rows:
                    if row.get('id') is not None:
                        result['field_provenance'].extend(dict(r) for r in db.execute(
                            'SELECT * FROM field_provenance WHERE table_name=? AND row_id=?',(target,str(row['id']))))
    return result



class _UnverifiableReview(ValueError):
    """Saved review audit cannot authenticate the claimed semantic input."""

def _verify_review_input(store, run_id, row, version, review, call, proof):
    """Authenticate actual review input via its original immutable operation hash.

    Audited requests can be redacted. When their safe bytes cannot verify the
    original hash, this archive is unverifiable; never reconstruct secrets or
    assert that a redacted request was the original model input.
    """
    path,baseline = frozen_baseline(store,run_id)
    frozen = load_phase(store.run_path(run_id)/'skill_bundle',row['domain_id'],'review')
    if frozen['bundle_sha256'] != baseline['rule_bundle_hash']:
        raise ValueError('archived frozen review rules mismatch')
    messages = call.get('raw_request')
    if (not isinstance(messages,list) or len(messages)!=2
            or any(not isinstance(message,dict) for message in messages)
            or messages[0].get('role')!='system' or messages[1].get('role')!='user'):
        raise _UnverifiableReview('archived actual review request missing')
    operation = next((r for r in store.list_operations(run_id) if r['request_id']==call['operation_id']),None)
    if (operation is None or operation['state']!='completed'
            or operation['payload'].get('logical_id')!=call['logical_id']
            or operation['payload'].get('stage')!='reviewing'
            or _hash(_json(operation['payload']))!=operation['payload_hash']
            or operation['result']!=call.get('business_response',call['response'])):
        raise _UnverifiableReview('archived actual review operation binding unavailable')
    if _hash(_json(messages)) != operation['payload'].get('messages_sha256'):
        raise _UnverifiableReview('archived request original hash cannot be verified; audit may be redacted or modified')
    events = [event['payload'] for event in store.events(run_id)]
    if (not any(e.get('kind')=='model_call_started' and e.get('execution_id')==call['execution_id']
                and e.get('domain_id')==row['domain_id'] and e.get('phase')=='review' for e in events)
            or not any(e.get('kind')=='model_call_finished' and e.get('execution_id')==call['execution_id']
                       and e.get('domain_id')==row['domain_id'] and e.get('phase')=='review'
                       and e.get('status')=='completed' and e.get('bundle_sha256')==frozen['bundle_sha256'] for e in events)):
        raise _UnverifiableReview('archived actual review execution audit missing')
    if (call.get('request_ref')!=ModelService.KEY+'/calls/'+call['execution_id']+'/raw_request'
            or call.get('response_ref')!=ModelService.KEY+'/calls/'+call['execution_id']+'/response'):
        raise _UnverifiableReview('archived actual review request/response references mismatch')
    payload = decode_content(messages[1]['content'],allow_fences=False)
    digest = _hash(_json(payload))
    if version.get('input_sha256')!=digest or proof.get('input_sha256')!=digest:
        raise _UnverifiableReview('archived review input hash mismatch')
    candidate = version['candidate']
    if (proof.get('version')!=version['version'] or proof.get('execution_id')!=call['execution_id']
            or proof.get('candidate_sha256')!=_hash(_json(candidate)) or proof.get('semantic_review')!='accept'):
        raise _UnverifiableReview('archived candidate/version/execution proof mismatch')
    if (payload.get('candidate')!=candidate or payload.get('domain_id')!=row['domain_id']
            or payload.get('phase')!='review' or payload.get('scope_type')!=call['scope']
            or payload.get('bundle_sha256')!=frozen['bundle_sha256']
            or payload.get('contract_sha256')!=frozen['contract_sha256']
            or payload.get('reference_hashes')!=frozen['reference_hashes']
            or payload.get('required_schema')!=ReviewResponse.model_json_schema()
            or payload.get('candidate_schema')!=FactCandidate.model_json_schema()
            or call['domain_id']!=row['domain_id'] or call['phase']!='review'):
        raise _UnverifiableReview('archived review candidate/schema/frozen closure mismatch')
    expected = phase_messages('review',frozen['skill_text'],frozen['references'],frozen['contract'],payload)
    if messages!=expected:
        raise _UnverifiableReview('archived trusted review instructions mismatch')
    snapshot = _snapshot(store,run_id,candidate['evidence'])
    chunk = row['review_chunk']
    page = ModelService._page_payload(None,snapshot,chunk)
    if (payload.get('page')!=page or row.get('source')!=page
            or call['input_range']!={'snapshot_id':page['snapshot_id'],'start':page['start'],'end':page['end']}):
        raise _UnverifiableReview('archived review source/chunk binding mismatch')
    if validate_candidate(candidate,frozen['contract'],snapshot,chunk)['status']!='program_valid':
        raise _UnverifiableReview('archived actual candidate program proof invalid')
    if payload.get('baseline')!=_archive_selected_baseline(path,baseline,candidate,frozen['contract']):
        raise _UnverifiableReview('archived review selected frozen baseline mismatch')
    binding = {name:payload[name] for name in ('phase','scope_type','bundle_sha256','contract_sha256',
               'reference_hashes','page','identity_context','required_schema','candidate_schema')}
    binding['baseline'] = {'baseline_sha256':baseline['baseline_sha256'],'created_at':baseline['created_at'],
                           'chips':[],'relations':[],'field_provenance':[]}
    digest = _hash(_json(binding))
    if row.get('review_binding_sha256')!=digest or proof.get('review_binding_sha256')!=digest:
        raise _UnverifiableReview('archived candidate-independent review binding hash mismatch')
    expected_review = ReviewResponse.model_validate({k:v for k,v in call.get('business_response',call['response']).items() if not k.startswith('_')}).model_dump()
    if {k:v for k,v in review.items() if k!='version'}!=expected_review or not proof.get('accepted'):
        raise _UnverifiableReview('archived accepted review result binding mismatch')


def _verify_current_review(store, run_id, row, version, review, call, proof):
    """Fresh factual authority uses the real saved producer/review/binding.

    Original source evidence remains verifiable even when audit diagnostics
    intentionally redact public example commands. No archive-grade request
    reconstruction or recovery of a private identity-context value is needed.
    """
    _,baseline = frozen_baseline(store,run_id)
    frozen = load_phase(store.run_path(run_id)/'skill_bundle',row['domain_id'],'review')
    if frozen['bundle_sha256'] != baseline['rule_bundle_hash']:
        raise ValueError('current frozen review rules mismatch')
    if (proof.get('version')!=version['version'] or proof.get('execution_id')!=call['execution_id']
            or proof.get('candidate_sha256')!=_hash(_json(version['candidate']))
            or proof.get('semantic_review')!='accept' or not proof.get('accepted')
            or proof.get('input_sha256')!=version.get('input_sha256')
            or proof.get('review_binding_sha256')!=row.get('review_binding_sha256')):
        raise _UnverifiableReview('current candidate/version/review hash association mismatch')
    for value in (version.get('input_sha256'),row.get('review_binding_sha256')):
        if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
            raise _UnverifiableReview('current saved review input hash missing')
    messages = call.get('raw_request')
    if not isinstance(messages,list) or len(messages)!=2 or any(not isinstance(m,dict) for m in messages):
        raise _UnverifiableReview('current saved review audit request missing')
    operation = next((r for r in store.list_operations(run_id) if r['request_id']==call['operation_id']),None)
    if (operation is None or operation['state']!='completed'
            or operation['payload'].get('logical_id')!=call['logical_id']
            or operation['payload'].get('stage')!='reviewing'
            or _hash(_json(operation['payload']))!=operation['payload_hash']
            or operation['result']!=call.get('business_response',call['response'])
            or call['phase']!='review' or call['domain_id']!=row['domain_id']
            or call['bundle_sha256']!=frozen['bundle_sha256'] or call['contract_sha256']!=frozen['contract_sha256']
            or call['reference_hashes']!=frozen['reference_hashes']):
        raise _UnverifiableReview('current actual producer review operation mismatch')
    events = [event['payload'] for event in store.events(run_id)]
    started = next((e for e in events if e.get('kind')=='model_call_started'
                    and e.get('execution_id')==call['execution_id'] and e.get('domain_id')==row['domain_id']
                    and e.get('phase')=='review'),None)
    if started is None or not any(e.get('kind')=='model_call_finished' and e.get('execution_id')==call['execution_id']
                and e.get('domain_id')==row['domain_id'] and e.get('phase')=='review' and e.get('status')=='completed' for e in events):
        raise _UnverifiableReview('current actual review execution audit missing')
    # Hash-only producer fields leave the operation/replay protocol intact.
    # Older missing candidate bindings stay unverifiable, never backfilled.
    if call.get('input_sha256') is not None:
        if (call.get('candidate_sha256')!=_hash(_json(version['candidate']))
                or call['input_sha256']!=version['input_sha256']
                or call['review_binding_sha256']!=row['review_binding_sha256']
                or call['audited_request_sha256']!=_hash(_json(messages))
                or any(started.get(name)!=call.get(name) for name in
                       ('candidate_sha256','input_sha256','review_binding_sha256','audited_request_sha256'))):
            raise _UnverifiableReview('current original producer review hash association mismatch')
    else:
        raise _UnverifiableReview('current original producer candidate binding unavailable; old audit was not migrated')

def _evidence(store, run_id, candidate, proof):
    snapshot = _snapshot(store,run_id,candidate['evidence'])
    for quote in candidate.get('context_refs',[]):
        if quote['snapshot_id']!=candidate['evidence']['snapshot_id']:
            raise ValueError('context evidence snapshot mismatch')
        _snapshot(store,run_id,quote)
    return {'run_id':run_id, 'snapshot_id':snapshot['snapshot_id'], 'body_hash':snapshot['body_hash'],
            'raw_hash':snapshot['raw_hash'], 'url':canonical_url(snapshot['final_url']),
            'evidence':candidate['evidence'], 'context_refs':candidate.get('context_refs',[]),
            'model_proof':proof}


def _basis(candidate):
    conversion = candidate.get('conversion',{})
    # Unit conversion changes magnitudes, but not binary/decimal, direction,
    # aggregation, sparsity, precision, conditions or the applicable scope.
    dimensions = {'unit':candidate.get('unit'), 'scope':candidate.get('scope'),
                  **{'condition:'+name:value for name,value in candidate.get('conditions',{}).items()},
                  **{name:conversion.get(name) for name in
                     ('basis','direction','aggregation','dense_or_sparse','precision','calculation_basis')}}
    return {name:None if value in (None,'','unknown') else value for name,value in dimensions.items()}


def _support(candidate, entity):
    """Conservative explicit support dimensions in already reviewed quotes.

    A more complete literal full SKU/condition statement can improve support;
    domain/source_type/confidence never establish first-hand authorship.
    """
    text = ' '.join('\n'.join(q['quote'] for q in [candidate['evidence'],*candidate.get('context_refs',[])]).split()).casefold()
    hint = entity.get('hint',{})
    dimensions = set()
    for name in ('vendor','full_sku'):
        if hint.get(name) and re.search(r'(?<!\w)'+re.escape(hint[name])+r'(?!\w)',text):
            dimensions.add(name)
    for name,value in candidate.get('conditions',{}).items():
        if value not in (None,'','unknown','not_applicable'):
            labels = [name.casefold(),name.replace('_',' ').casefold()]
            pattern = r'(?<!\w)(?:'+'|'.join(re.escape(label) for label in labels)+r')\s*[:=]?\s*'+re.escape(str(value).casefold())+r'(?!\w)'
            if re.search(pattern,text):
                dimensions.add('condition:'+name)
    return dimensions


class ComparisonService:
    KEY = 'domain_compare.v1'

    def __init__(self, store, run_id, generation, *, pages=None):
        self.store,self.run_id,self.generation = store,run_id,generation
        if pages is not None and (pages.store is not store or pages.run_id != run_id):
            raise ValueError('page service must belong to this run')
        self.resolver = EntityResolver(store,run_id,generation)

    def snapshot(self):
        return self.store.metadata(self.run_id,self.KEY) or {'baselines':{},'comparisons':{},'decisions':[]}

    @staticmethod
    def _field_key(binding):
        candidate,entity = binding['candidate'],binding['entity']
        if candidate['target_table']=='chips':
            identity = entity.get('canonical_key')
        elif binding.get('comparison_key'):
            identity = binding['comparison_key']
        else:
            key = {name:value for name,value in candidate['entity_key'].items()
                   if name!='chip_model' or entity.get('canonical_key') is None}
            if key.get('url'):
                key['url'] = canonical_url(key['url'])
            identity = {'entity':entity.get('canonical_key'), 'entity_key':key,
                        'conditions':candidate.get('conditions',{})}
        return _hash(_json([candidate['target_table'],identity,candidate['field_name']]))

    def _old_evidence(self, baseline, binding):
        archived = []
        metadata = self.store.metadata(self.run_id,'baseline')
        manifest = metadata.get('manifest') or {}
        refs = manifest.get('historical_evidence_refs',{})
        for source in baseline['old_sources']:
            item = dict(source)
            ref = refs.get(str(source.get('id')))
            if ref is None and source.get('evidence_ref_json'):
                try:
                    ref = json.loads(source['evidence_ref_json'])
                except (ValueError,TypeError):
                    pass
            try:
                if not isinstance(ref,dict):
                    raise ValueError('archived evidence reference unavailable')
                bound,_ = _reviewed_proof(self.store,ref['run_id'],ref['logical_candidate_id'],ref.get('version'))
                candidate = bound['candidate']
                if any(candidate[name] != binding['candidate'][name] for name in ('target_table','field_name')):
                    raise ValueError('archived evidence field mismatch')
                if bound['entity'].get('canonical_key') != binding['entity'].get('canonical_key'):
                    raise ValueError('archived evidence full entity mismatch')
                if candidate['target_table']!='chips' and self._field_key(bound)!=self._field_key(binding):
                    raise ValueError('archived relation comparison scope mismatch')
                if not same_value(candidate['normalized_value'],baseline['old_value'],policy=baseline['normalization_policy']):
                    raise ValueError('archived evidence does not support frozen old value')
                evidence = _evidence(self.store,ref['run_id'],candidate,bound['model_proof'])
                if evidence['url'] != canonical_url(source['source_url']):
                    raise ValueError('archived evidence URL mismatch')
                item.update(verified_evidence=evidence, archived_candidate=candidate)
            except (KeyError,ValueError,TypeError,OSError,UnicodeError) as exc:
                item['evidence_unavailable_reason'] = str(exc) if isinstance(exc,ValueError) else 'archived evidence unavailable'
            archived.append(item)
        return archived

    @staticmethod
    def _membership(new, old_sources, baseline):
        if new is None:
            return 'unknown','new supported evidence unavailable'
        if baseline['old_value_basis'] in {'entity_absent','relation_absent','field_empty'}:
            return 'added','baseline has no source for this entity/field'
        old_urls = set()
        for source in old_sources:
            try:
                old_urls.add(canonical_url(source['source_url']))
            except (KeyError,TypeError,ValueError):
                pass
        if new['url'] not in old_urls:
            return ('added','normalized URL is absent from frozen supporting sources') if old_urls or baseline['source_membership_recorded'] else ('unknown','frozen source membership unavailable')
        same = [source for source in old_sources if source.get('verified_evidence',{}).get('url')==new['url']]
        if any(source['verified_evidence']['body_hash']==new['body_hash'] for source in same):
            return 'unchanged','same normalized URL and actual supported evidence version'
        if same:
            return 'added','same URL has a new actual supported evidence version'
        return 'unchanged','normalized URL already present; old document version is unavailable'

    @staticmethod
    def _quality(binding, new, sources, normalization_policy):
        if new is None:
            return 'unknown','new archived supported evidence unavailable'
        verified = [source for source in sources if source.get('verified_evidence')]
        if not verified:
            return 'unknown','old archived evidence unavailable or not verifiable for the frozen value'
        current = binding['candidate']
        upgraded,unknown = False,False
        for source in verified:
            old = source['archived_candidate']
            old_basis,new_basis = _basis(old),_basis(current)
            different = [name for name in old_basis.keys()|new_basis.keys()
                         if not same_value(old_basis.get(name),new_basis.get(name))]
            if different:
                # Unknown on only one side is a missing comparison basis, not
                # an explicit disagreement. Preserve both original contexts.
                if any(old_basis.get(name) is not None and new_basis.get(name) is not None for name in different):
                    return 'conflict','verified old and new evidence have incompatible explicit measurement bases'
                unknown = True
                continue
            if not same_value(old['normalized_value'],current['normalized_value'],policy=normalization_policy):
                return 'conflict','comparable verified evidence supports different values; neither side was overwritten'
            old_support,new_support = _support(old,binding['entity']),_support(current,binding['entity'])
            if new_support > old_support:
                upgraded = True
            elif not new_support >= old_support:
                unknown = True
        if upgraded:
            return 'upgraded','verified new quotes provide more complete literal full-SKU/condition support'
        if unknown or len(verified)!=len(sources):
            return 'unknown','some archived support is missing or not directly comparable'
        return 'unchanged','verified evidence supports the same value and measurement basis without demonstrated improvement'

    def _saved_scopes(self, domain, snapshot_id, start, end):
        scopes = (self.store.metadata(self.run_id,PageService.KEY) or {}).get('coverage_scopes',{}).get(snapshot_id,{})
        if type(start) is not int or type(end) is not int:
            return []
        return [scope for scope in ('main','linked','identity')
                if any(r['start'] <= start < end <= r['end']
                       for r in scopes.get(domain+'/'+scope,[]))]

    def _task_scopes(self, binding):
        candidate = binding['candidate']
        evidence = candidate['evidence']
        return self._saved_scopes(candidate['domain_id'],evidence['snapshot_id'],evidence['start'],evidence['end'])

    def comparison_input(self, candidate_id):
        """Typed skip for retained invalid raw facts; infrastructure errors raise.

        Runtime callers must not broadly catch ValueError: missing saved proof,
        frozen rules/baseline/hash failures and fencing failures are fatal to
        that operation. Only actual saved shape/ownership outcomes are skips.
        """
        row = (self.store.metadata(self.run_id,ModelService.KEY) or {}).get('candidates',{}).get(candidate_id)
        if row is None or not row.get('versions'):
            raise ValueError('saved model proof missing')
        _,baseline = frozen_baseline(self.store,self.run_id)
        frozen = load_phase(self.store.run_path(self.run_id)/'skill_bundle',row['domain_id'],'review')
        if frozen['bundle_sha256'] != baseline['rule_bundle_hash']:
            raise ValueError('frozen comparison rules and baseline mismatch')
        version = row['versions'][-1]
        candidate = version['candidate']
        program = version.get('program',{})
        reason = None
        if row['status']=='program_rejected' or program.get('status')=='rejected':
            reason = 'saved_program_rejected'
        elif not isinstance(candidate,dict):
            reason = 'raw_fact_not_an_object'
        elif (any(name not in candidate for name in ('domain_id','target_table','field_name','entity_key','evidence'))
              or any(not isinstance(candidate.get(name),str) for name in ('domain_id','target_table','field_name'))
              or any(not isinstance(candidate.get(name),dict) for name in ('entity_key','entity_hints','conditions','evidence'))
              or any(name not in candidate.get('evidence',{}) for name in ('snapshot_id','body_hash','start','end','quote'))
              or any(value is not None and not isinstance(value,str) for value in
                     (candidate.get('entity_key',{}).get('vendor'),candidate.get('entity_key',{}).get('chip_model'),
                      candidate.get('entity_hints',{}).get('vendor'),candidate.get('entity_hints',{}).get('full_sku')))):
            reason = 'raw_fact_missing_binding_shape'
        elif (candidate['domain_id'] != row['domain_id']
              or candidate['field_name'] not in frozen['contract']['fields'].get(candidate['target_table'],[])):
            reason = 'field_outside_frozen_domain_ownership'
        return {'logical_candidate_id':candidate_id, 'is_comparable':reason is None,
                'status':'comparable' if reason is None else 'skipped',
                'reason':reason or 'saved_structured_owned_field', 'candidate_status':row['status'],
                **({'validated':False,'value_change_kind':'unknown','new_source':None,
                    'program_rejection_reason':program.get('reason',row.get('reason'))} if reason else {}),
                'source_ref':ModelService.KEY+'/candidates/'+candidate_id}

    def compare_candidate(self, candidate_id):
        outcome = self.comparison_input(candidate_id)
        if not outcome['is_comparable']:
            def skip(state):
                state = state or {'baselines':{},'comparisons':{},'decisions':[]}
                saved = state.setdefault('skipped_inputs',{})
                if candidate_id in saved and saved[candidate_id]==outcome:
                    return state,outcome,None
                saved[candidate_id] = outcome
                return state,outcome,{'kind':'field_comparison_skipped','stage':'comparing',**outcome}
            return self.store.update_metadata(self.run_id,self.generation,self.KEY,skip)
        # Rebinding after all identity proofs is essential: completed task
        # receipts can contain an older, now ambiguous entity decision.
        binding = self.resolver.bind_candidate(candidate_id)
        baseline = baseline_projection(self.store,self.run_id,binding)
        baseline_key = _hash(_json([binding['candidate']['target_table'],binding['entity'].get('canonical_key'),
                                   binding.get('comparison_key'),binding['comparison'],binding['candidate']['field_name']]))
        old_sources = self._old_evidence(baseline,binding)
        new = None
        verification_status,verification_reason = 'not_executed','saved semantic proof was not accepted'
        if binding['model_proof']['accepted']:
            # Current snapshot/baseline/rule integrity failures still propagate.
            # Only a typed unverifiable review audit is a conservative outcome.
            supported = _evidence(self.store,self.run_id,binding['candidate'],binding['model_proof'])
            try:
                row,version,review,call = _saved_review(self.store,self.run_id,candidate_id,binding['model_proof']['version'])
                _verify_current_review(self.store,self.run_id,row,version,review,call,binding['model_proof'])
            except _UnverifiableReview as exc:
                verification_status,verification_reason = 'unverifiable',str(exc)
            else:
                new = supported
                verification_status,verification_reason = 'verified','current saved producer, independent review and final evidence binding verified; audit redaction is separate'
        effective_validated = binding['validated'] and verification_status=='verified'
        basis = baseline['old_value_basis']
        if not effective_validated:
            value_kind = 'unknown'
        elif basis == 'entity_absent':
            value_kind = 'new_entity'
        elif basis in {'field_empty','relation_absent'}:
            value_kind = 'added'
        elif basis == 'frozen_value':
            # Binary memory must not become equivalent to an unlabeled decimal
            # GB baseline merely because normalized magnitudes are identical.
            conversion = binding['candidate'].get('conversion',{})
            binary_unknown = conversion.get('basis')=='binary' and not any(s.get('archived_candidate',{}).get('conversion',{}).get('basis')=='binary' for s in old_sources)
            value_kind = 'unknown' if binary_unknown else 'unchanged' if same_value(baseline['old_value'],binding['candidate']['normalized_value'],policy=baseline['normalization_policy']) else 'changed'
        else:
            value_kind = 'unknown'
        membership,membership_reason = self._membership(new,old_sources,baseline)
        quality,quality_reason = self._quality(binding,new,old_sources,baseline['normalization_policy'])
        result = {**baseline, **binding, 'old_sources':old_sources, 'new_source':new,
                  'validated':effective_validated, 'update_eligible':binding['update_eligible'] and effective_validated,
                  'evidence_verification_status':verification_status,'evidence_verification_reason':verification_reason,
                  'field_key':self._field_key(binding), 'task_scopes':self._task_scopes(binding),
                  'value_change_kind':value_kind, 'source_membership_kind':membership,
                  'source_membership_reason':membership_reason, 'source_quality_kind':quality,
                  'source_quality_reason':quality_reason, 'source_change_kind':source_summary(membership,quality),
                  'formal_business_writes':0, 'formal_provenance_writes':0, 'publish_eligible':False,
                  'value_change_reason':'candidate differs only; frozen baseline is not asserted wrong and no value was published' if value_kind=='changed' else 'comparison against immutable baseline',
                  'baseline_key':baseline_key}
        result['comparison_id'] = 'comparison-' + _hash(_json(result))
        def save(state):
            state = state or {'baselines':{},'comparisons':{},'decisions':[]}
            if baseline_key in state['baselines'] and state['baselines'][baseline_key]!=baseline:
                raise ValueError('immutable comparison baseline conflict')
            state['baselines'].setdefault(baseline_key,baseline)
            versions = state['comparisons'].setdefault(candidate_id,[])
            prior = next((v for v in versions if v['comparison_id']==result['comparison_id']),None)
            if prior is not None:
                return state,prior,None
            versions.append(result)
            state['decisions'].append({'comparison_id':result['comparison_id'],'logical_candidate_id':candidate_id})
            return state,result,{'kind':'immutable_field_comparison','stage':'comparing','domain_id':binding['candidate']['domain_id'],
                                 'comparison_id':result['comparison_id'],'logical_candidate_id':candidate_id,
                                 'value_change_kind':value_kind,'source_membership_kind':membership,'source_quality_kind':quality}
        return self.store.update_metadata(self.run_id,self.generation,self.KEY,save)

    def statistics(self):
        """Latest decisions; fields dedupe entity/table/full relation key/field.

        Source summary dedupes normalized URL + actual supported body_hash.
        Per-field source associations remain available in comparison records.
        Rebind all compared candidates before reporting final validated counts.
        """
        identifiers = list(self.snapshot()['comparisons'])
        outcomes = [self.compare_candidate(identifier) for identifier in identifiers]
        # A previously saved rejected comparison may now be a typed skip.
        # Keep its original audit versions; raw rejections are counted below.
        rows = [row for row in outcomes if row.get('status')!='skipped']
        models = (self.store.metadata(self.run_id,ModelService.KEY) or {}).get('candidates',{})
        rejected = {}
        for identifier,row in models.items():
            if row['status'] in {'program_rejected','rejected','semantic_rejected'}:
                source = row.get('source',{})
                rejected[identifier] = self._saved_scopes(row['domain_id'],source.get('snapshot_id'),source.get('start'),source.get('end'))
        for row in rows:
            if row['evidence_status'] in {'rejected','program_rejected','semantic_rejected'} or row['entity']['status']=='out_of_scope':
                rejected[row['logical_candidate_id']] = row['task_scopes']
        def counts(items,scope=None):
            valid = [r for r in items if r['validated']]
            sources = {(r['new_source']['url'],r['new_source']['body_hash']) for r in valid if r['new_source']}
            return {'independent_fields':len({r['field_key'] for r in valid}), 'source_candidates':len(sources),
                    'urls':len({url for url,_ in sources}), 'validated_candidates':len(valid),
                    'rejected_candidates':sum(scope is None or scope in scopes for scopes in rejected.values()),
                    'value_changes':len({r['field_key'] for r in valid if r['value_change_kind'] in {'new_entity','added','changed'}}),
                    'unverifiable_evidence_candidates':sum(r['evidence_verification_status']=='unverifiable' for r in items),
                    'condition_unresolved_candidates':sum(r['relation_readiness']['status']=='unresolved' for r in items),
                    'dependency_unresolved_candidates':sum(r['dependency_status']=='dependency_unresolved' for r in items),
                    'comparison_unresolved_candidates':sum(r['comparison']['status']=='comparison_unresolved' for r in items),
                    'ambiguous_entity_candidates':sum(r['entity']['status']=='ambiguous' for r in items)}
        entities = self.resolver.snapshot()['entities'].values()
        return {**counts(rows), 'new_entities':len({r['canonical_key'] for r in entities if r['status']=='new'}),
                'main':counts([r for r in rows if 'main' in r['task_scopes']],'main'),
                'linked':counts([r for r in rows if 'linked' in r['task_scopes']],'linked'),
                'formal_business_writes':0,'formal_provenance_writes':0,
                'source_summary_basis':'normalized URL + actual supported body_hash; per-field associations retained'}
