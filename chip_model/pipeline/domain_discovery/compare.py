"""Immutable value comparison and independent source membership/quality.

Only isolated run metadata and audit events are written. Evidence quality is
based on actual archived reviewed quotes, never official-looking domains,
source_type ranks or model confidence. No fetching or formal publishing.
"""
from __future__ import annotations

import hashlib
import json
import re

from .entities import EntityResolver
from .history import baseline_projection, frozen_baseline, same_value
from .models import ModelService
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
    return record


def _reviewed_proof(store, run_id, identifier, version_number):
    """Read old runs without initializing services, claiming leases or writing."""
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
    return bound, row


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
                if not same_value(candidate['normalized_value'],baseline['old_value']):
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
    def _quality(binding, new, sources):
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
            if not same_value(old['normalized_value'],current['normalized_value']):
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
        candidate = row['versions'][-1]['candidate']
        reason = None
        if not isinstance(candidate,dict):
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
        if binding['model_proof']['accepted']:
            new = _evidence(self.store,self.run_id,binding['candidate'],binding['model_proof'])
        basis = baseline['old_value_basis']
        if not binding['validated']:
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
            value_kind = 'unknown' if binary_unknown else 'unchanged' if same_value(baseline['old_value'],binding['candidate']['normalized_value']) else 'changed'
        else:
            value_kind = 'unknown'
        membership,membership_reason = self._membership(new,old_sources,baseline)
        quality,quality_reason = self._quality(binding,new,old_sources)
        result = {**baseline, **binding, 'old_sources':old_sources, 'new_source':new,
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
        rows = [self.compare_candidate(identifier) for identifier in identifiers]
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
