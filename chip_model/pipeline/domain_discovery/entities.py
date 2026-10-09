"""Deterministic entity/dependency postprocessing; never rewrite model evidence."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing

from .contracts import DomainContract, benchmark_comparison_key, benchmark_metric_kind, relation_readiness
from .models import ModelService, _capacity_identity
from .rules import load_phase
from .store import _file_hash, _hash, _json


def normalized_sku(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise ValueError('vendor/full SKU must be nonblank text without NUL')
    return ' '.join(value.split()).casefold()


def temporary_entity_id(vendor: str, full_sku: str) -> str:
    key = normalized_sku(vendor) + '\0' + normalized_sku(full_sku)
    return 'tmp-chip-' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]


def identity_variants(hint: dict) -> dict:
    """Extra dimensions only; exact literal SKU dimensions are not duplicated.

    This never removes or rewrites the original SKU suffix, capacity or form.
    Base names without those tokens retain every explicit variant dimension.
    """
    sku = hint['full_sku']
    variants = {name:hint[name] for name in ('capacity','form_factor') if name in hint}
    if 'capacity' in variants:
        capacities = [{'value':float(amount), 'unit':unit} for amount,unit in
                      re.findall(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(gb|gib|mb|mib)(?!\w)', sku)]
        if capacities == [variants['capacity']]:
            variants.pop('capacity')
    if 'form_factor' in variants:
        form = re.escape(variants['form_factor'])
        if re.search(r'(?<!\w)' + form + r'(?!\w)', sku):
            variants.pop('form_factor')
    return variants


def _known_comparison_value(value):
    return value is not None and (not isinstance(value, str) or value.strip().casefold() not in {'','unknown','not_applicable'})


def _entity_kind(value):
    if value is None:
        return None
    if not isinstance(value, str):
        return 'unknown'
    value = value.strip().casefold()
    if value in {'chip','die'}:
        return 'chip'
    if value in {'board_sku','single_card'}:
        return 'board_sku'
    return value if value in {'series','server','cluster','non_compute'} else 'unknown'


def candidate_entity_hint(candidate: dict) -> dict:
    """Structured model hints remain unverified until identity proof is registered."""
    key, hints = candidate.get('entity_key', {}), candidate.get('entity_hints', {})
    mismatch = (bool(key.get('chip_model') and hints.get('full_sku'))
                and normalized_sku(key['chip_model']) != normalized_sku(hints['full_sku']))
    kind = hints.get('entity_type')
    if candidate.get('domain_id') == 'identity' and kind is None:
        kind = candidate.get('scope')
    scope_kind = _entity_kind(candidate.get('scope'))
    kind_conflict = (candidate.get('domain_id')=='identity' and scope_kind in {'chip','board_sku'}
                     and _entity_kind(kind) in {'chip','board_sku'} and _entity_kind(kind)!=scope_kind)
    return {'vendor': key.get('vendor') or hints.get('vendor'),
            'full_sku': key.get('chip_model') or hints.get('full_sku'),
            **({'entity_type':kind} if kind is not None else {}),
            **({'entity_kind_scope_conflict':True} if kind_conflict else {}),
            **({'identity_hint_conflict':True, 'claimed_full_sku':hints['full_sku']} if mismatch else {}),
            **{name: hints[name] for name in ('capacity', 'form_factor') if name in hints}}


class EntityResolver:
    KEY = 'domain_entities.v1'
    POLICY_VERSION = 2

    def __init__(self, store, run_id, generation, *, verified_vendor_aliases=None):
        self.store, self.run_id, self.generation = store, run_id, generation
        prior = store.metadata(run_id, self.KEY)
        if prior and prior.get('entities') and (prior.get('entity_policy_version')!=self.POLICY_VERSION
                or any(not row.get('entity_kind') for row in prior['entities'].values())):
            raise ValueError('entity normalization policy version incompatible; original artifacts retained')
        aliases = (prior['verified_vendor_aliases'] if prior else {}) if verified_vendor_aliases is None else verified_vendor_aliases
        if not isinstance(aliases, dict):
            raise ValueError('verified vendor alias map must be explicit')
        self.aliases = {normalized_sku(name): normalized_sku(target) for name, target in aliases.items()}
        if any(target in self.aliases and self.aliases[target] != target for target in self.aliases.values()):
            raise ValueError('verified aliases must map directly to canonical vendors')
        def initialize(state):
            if state is not None and state['verified_vendor_aliases'] != self.aliases:
                raise ValueError('verified vendor alias policy conflict')
            state = state or {'verified_vendor_aliases':self.aliases, 'entities':{}, 'bindings':{}, 'new_entity_count':0}
            state['entity_policy_version'] = self.POLICY_VERSION
            state.setdefault('entity_decisions', [])
            return state, None, None
        self._update(initialize)

    def _update(self, callback):
        return self.store.update_metadata(self.run_id, self.generation, self.KEY, callback)

    def snapshot(self):
        return self.store.metadata(self.run_id, self.KEY)

    def _baseline(self):
        metadata = self.store.metadata(self.run_id, 'baseline')
        path = self.store.run_path(self.run_id) / 'data.db'
        if not metadata or not path.is_file() or _file_hash(path) != metadata['baseline_sha256']:
            raise ValueError('frozen baseline missing or hash mismatch')
        return path, metadata

    def _rows(self, table):
        if table not in {'chips', 'models', 'chip_model_benchmarks', 'chip_model_compatibility', 'deployment_guides'}:
            raise ValueError('unsupported entity table')
        path, _ = self._baseline()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            if not list(db.execute(f'PRAGMA table_info("{table}")')):
                raise ValueError('frozen baseline table missing: ' + table)
            return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]

    def _hint(self, hint):
        vendor = normalized_sku(hint['vendor'])
        vendor = self.aliases.get(vendor, vendor)
        sku = normalized_sku(hint['full_sku'])
        normalized = {'vendor':vendor, 'full_sku':sku}
        kind = _entity_kind(hint.get('entity_type', hint.get('entity_kind')))
        if kind is not None:
            normalized['entity_kind'] = kind
        if 'capacity' in hint:
            normalized['capacity'] = _capacity_identity(hint['capacity'])
        if 'form_factor' in hint:
            normalized['form_factor'] = normalized_sku(hint['form_factor'])
        variants = identity_variants(normalized)
        kind_key = '\0kind=' + kind if kind and kind!='board_sku' else ''
        return normalized, vendor + '\0' + sku + kind_key + ('\0' + _json(variants) if variants else '')

    def _proof(self, candidate_id, *, require_accepted=False):
        state = self.store.metadata(self.run_id, ModelService.KEY) or {}
        row = state.get('candidates', {}).get(candidate_id)
        if row is None or not row.get('versions'):
            raise ValueError('saved model proof missing')
        version = row['versions'][-1]
        review = next((r for r in row.get('reviews', []) if r['version'] == version['version']), None)
        logical = 'review-' + _hash(_json([candidate_id, version['version']]))
        call = next((c for c in state.get('calls', []) if c['logical_id'] == logical
                     and c['phase'] == 'review' and c['status'] == 'completed'
                     and c.get('response', {}).get('verdict') == 'accept'), None)
        accepted = (row['status'] == 'validated' and version.get('program', {}).get('status') == 'program_valid'
                    and review is not None and review['verdict'] == 'accept' and call is not None)
        if require_accepted and not accepted:
            raise ValueError('accepted program and semantic model proof required')
        proof = {'logical_candidate_id':candidate_id, 'version':version['version'],
                 'candidate_sha256':_hash(_json(version['candidate'])),
                 'input_sha256':version.get('input_sha256'),
                 'review_binding_sha256':row.get('review_binding_sha256'),
                 'semantic_review':review['verdict'] if review else 'not_executed',
                 'execution_id':call['execution_id'] if call else None,
                 'accepted':accepted}
        return row, version['candidate'], proof

    def _match(self, hint, *, verified=False, entity_type=None):
        self._baseline()
        if entity_type is not None:
            hint = {**hint, 'entity_type':entity_type}
        try:
            normalized, key = self._hint(hint)
        except (ValueError, KeyError, TypeError):
            return {'status':'unresolved', 'entity_id':None, 'canonical_key':None, 'reason':'incomplete_entity_hint', 'hint':hint}
        variants = identity_variants(normalized)
        if normalized.get('entity_kind') and normalized['entity_kind']!='board_sku':
            variants = {**variants,'entity_kind':normalized['entity_kind']}
        complete_sku = normalized['full_sku'] + (' ' + _json(variants) if variants else '')
        temporary = temporary_entity_id(normalized['vendor'], complete_sku)
        result = {'status':'unresolved', 'entity_id':temporary, 'temporary_entity_id':temporary,
                  'entity_kind':normalized.get('entity_kind','unknown'),
                  'canonical_key':key, 'hint':normalized, 'reason':'identity_dependency_unresolved', 'possible_matches':[]}
        if hint.get('entity_kind_scope_conflict'):
            return {**result, 'status':'ambiguous', 'reason':'entity_kind_scope_mismatch'}
        if hint.get('identity_hint_conflict'):
            return {**result, 'status':'ambiguous', 'reason':'full_sku_disagrees_with_reviewed_entity_key',
                    'claimed_full_sku':hint['claimed_full_sku']}
        if not verified:
            return result
        entity_type = normalized.get('entity_kind')
        if entity_type in {'server', 'cluster', 'non_compute'}:
            return {**result, 'status':'out_of_scope', 'reason':'identity_out_of_scope'}
        if entity_type == 'series':
            return {**result, 'status':'ambiguous', 'reason':'series_is_not_specific_sku'}
        if entity_type not in {'chip', 'board_sku'}:
            return {**result, 'reason':'concrete_entity_type_unconfirmed'}
        matches, uncertain = [], []
        chip_rows = self._rows('chips')
        for row in chip_rows:
            try:
                vendor = normalized_sku(row['vendor'])
                vendor = self.aliases.get(vendor, vendor)
                if vendor != normalized['vendor'] or normalized_sku(row['chip_model']) != normalized['full_sku']:
                    continue
            except (ValueError, KeyError):
                continue
            unknown, mismatch = False, False
            for name, column in [('capacity','vram_gb'), ('form_factor','form_factor')]:
                if name not in normalized:
                    continue
                if not row.get(column):
                    unknown = True
                    continue
                if name == 'capacity':
                    capacity = normalized[name]
                    # A numeric baseline GB column has no binary-basis proof.
                    try:
                        same = capacity['unit'] == 'gb' and float(row[column]) == capacity['value']
                    except (ValueError, TypeError):
                        same = False
                    if capacity['unit'] != 'gb':
                        unknown = True
                    else:
                        mismatch |= not same
                else:
                    mismatch |= normalized_sku(row[column]) != normalized[name]
            if not mismatch:
                (uncertain if unknown else matches).append(row['id'])
        possible = matches + uncertain
        if uncertain or len(matches) > 1:
            return {**result, 'status':'ambiguous', 'reason':'baseline_identity_ambiguous', 'possible_matches':possible}
        if matches:
            row = next(row for row in chip_rows if row['id'] == matches[0])
            # A uniquely matched immutable row supplies a stable canonical
            # key, whether a particular page restates its variant columns.
            complete_hint = dict(normalized)
            if row.get('vram_gb'):
                try:
                    capacity = _capacity_identity(row['vram_gb'])
                    if capacity['unit'] in {'unknown','gb'}:
                        complete_hint['capacity'] = {**capacity, 'unit':'gb'}
                except ValueError:
                    pass
            if row.get('form_factor'):
                complete_hint['form_factor'] = normalized_sku(row['form_factor'])
            complete_hint, canonical_key = self._hint(complete_hint)
            return {**result, 'status':'existing', 'entity_id':str(matches[0]), 'row_id':matches[0],
                    'canonical_key':canonical_key, 'hint':complete_hint, 'identity_key_source':'frozen_baseline_row',
                    'reason':'exact_verified_identity', 'possible_matches':matches}
        return {**result, 'status':'new', 'reason':'verified_identity_absent_from_baseline'}

    def resolve_entity(self, hint):
        """Only registered identity-domain model proof can release this dependency."""
        preliminary = self._match(hint)
        key = preliminary['canonical_key']
        if key is None:
            return preliminary
        if hint.get('identity_hint_conflict'):
            return preliminary
        if hint.get('entity_kind_scope_conflict'):
            return preliminary
        requested = preliminary['hint']
        records = []
        for record in self.snapshot()['entities'].values():
            if record['reason']=='entity_kind_scope_mismatch':
                continue
            if any(record['hint'].get(name) != requested.get(name) for name in ('vendor','full_sku')):
                continue
            if requested.get('entity_kind') and requested['entity_kind']!=record['entity_kind']:
                continue
            if record['entity_kind']=='unknown' and requested.get('entity_kind')!='unknown':
                continue
            if record['canonical_key']==key or all(record['hint'].get(name)==value
                    for name,value in identity_variants(requested).items()):
                records.append(record)
        exact = [record for record in records if record['canonical_key']==key]
        if requested.get('entity_kind') and exact:
            return exact[0]
        if len(records) == 1:
            return records[0]
        if records or any(r['hint']['vendor'] == requested['vendor'] and r['hint']['full_sku'] == requested['full_sku']
                          for r in self.snapshot()['entities'].values()):
            return {**preliminary, 'status':'ambiguous', 'reason':'variant_identity_unconfirmed'}
        return preliminary

    @staticmethod
    def _reconcile_board_variants(state):
        groups = {}
        for row in state['entities'].values():
            if row['entity_kind']=='board_sku' and row['reason']!='entity_kind_scope_mismatch':
                group = row['hint']['vendor'],row['hint']['full_sku']
                groups.setdefault(group, []).append(row)
        for boards in groups.values():
            dimensions = {row['entity_id']:identity_variants(row['hint']) for row in boards}
            for record in boards:
                own = dimensions[record['entity_id']]
                more_specific = [other for other in boards if other['status'] in {'new','existing'}
                    and all(dimensions[other['entity_id']].get(name)==value for name,value in own.items())
                    and set(dimensions[other['entity_id']])>set(own)]
                incompatible = any(any(name in dimensions[a['entity_id']] and name in dimensions[b['entity_id']]
                                       and dimensions[a['entity_id']][name]!=dimensions[b['entity_id']][name]
                                       for name in ('capacity','form_factor'))
                                   for index,a in enumerate(more_specific) for b in more_specific[index+1:])
                if incompatible:
                    record.update(status='ambiguous', reason='board_variant_unspecified',
                                  possible_matches=sorted(row['entity_id'] for row in more_specific))

    def register_identity(self, candidate_id):
        row, candidate, proof = self._proof(candidate_id)
        if candidate['domain_id'] != 'identity' or candidate['target_table'] != 'chips':
            raise ValueError('identity domain model proof required')
        if not proof['accepted']:
            raise ValueError('accepted identity program and semantic model proof required')
        phase = load_phase(self.store.run_path(self.run_id) / 'skill_bundle', 'identity', 'review')
        if self.store.metadata(self.run_id, 'baseline')['rule_bundle_hash'] != phase['bundle_sha256']:
            raise ValueError('identity rule bundle and baseline mismatch')
        hint = candidate_entity_hint(candidate)
        kind = candidate.get('entity_hints', {}).get('entity_type') or candidate['scope']
        if candidate['scope'] in {'server','cluster'}:
            kind = candidate['scope']
        entity = self._match(hint, verified=True, entity_type=kind)
        if hint.get('entity_kind_scope_conflict'):
            entity['entity_id'] = 'ambiguous-kind-' + _hash(candidate_id)[:24]
        entity['identity_proofs'] = [proof]
        def save(state):
            before = {identifier:_json(row) for identifier,row in state['entities'].items()}
            identifier = entity['entity_id']
            if identifier is None:
                raise ValueError('identity full vendor and SKU proof missing')
            prior = state['entities'].get(identifier)
            if prior and prior['canonical_key'] != entity['canonical_key']:
                raise ValueError('entity ID collision: original canonical key differs')
            if prior:
                incompatible = any(name in prior['hint'] and prior['hint'][name] != value
                                   for name,value in entity['hint'].items())
                derived_ambiguity = prior['reason']=='board_variant_unspecified'
                if incompatible or (prior['status'] != entity['status'] and not derived_ambiguity):
                    prior.update(status='ambiguous', reason='conflicting_identity_proofs')
                else:
                    prior['hint'].update(entity['hint'])
                if proof not in prior['identity_proofs']:
                    prior['identity_proofs'].append(proof)
                result = prior
            else:
                state['entities'][identifier] = entity
                result = entity
            self._reconcile_board_variants(state)
            changes = []
            for identifier,current in state['entities'].items():
                if before.get(identifier)==_json(current):
                    continue
                current['decision_version'] = current.get('decision_version',0)+1
                decision = {name:current[name] for name in ('entity_id','canonical_key','entity_kind','status','reason','identity_proofs','decision_version')}
                state['entity_decisions'].append(decision)
                changes.append(decision)
            state['new_entity_count'] = sum(r['status'] == 'new' for r in state['entities'].values())
            event = {'kind':'identity_resolution', 'stage':'comparing', 'domain_id':'identity', 'entity':result,
                     'entity_decisions':changes} if changes else None
            return state, result, event
        return self._update(save)

    def _relation_comparison(self, candidate, entity, contract):
        table, key = candidate['target_table'], candidate['entity_key']
        rows = self._rows(table)
        dimensions = dict(key)
        if table in {'chip_model_benchmarks','chip_model_compatibility'}:
            dimensions.update({name:value for name,value in candidate['conditions'].items()
                               if name not in {'metric_class','verification_kind'}})
        # Semantic comparison dimensions are restricted by the frozen field
        # ownership, not every metadata/metric column in a historical record.
        condition_fields = {'scenario','task','hardware_config','framework','batch_size',
                            'input_seq_length','output_seq_length','concurrency','test_date',
                            'training_gpu_count','training_workload_type','training_tokens_T','precision'}
        historical_dimensions = condition_fields & set(contract.fields[table])
        matches, uncertain = [], []
        variants = identity_variants(entity.get('hint', {'full_sku':''}))
        for row in rows:
            if any(_known_comparison_value(row.get(name)) and _known_comparison_value(value)
                   and str(row[name]) != str(value) for name,value in key.items()):
                continue
            unknown, different = False, False
            for name in set(dimensions) | historical_dimensions:
                old, new = row.get(name), dimensions.get(name)
                old_known, new_known = _known_comparison_value(old), _known_comparison_value(new)
                if old_known != new_known:
                    unknown = True
                elif old_known:
                    different |= str(old) != str(new)
            # A base-name relationship row cannot prove which board variant
            # produced its measurement. Do not infer it from current catalog
            # membership or remove capacity/form from the candidate identity.
            for name,value in variants.items():
                column = 'vram_gb' if name == 'capacity' else name
                if row.get(column) in (None, ''):
                    unknown = True
                elif name == 'capacity':
                    try:
                        if value['unit'] != 'gb':
                            unknown = True
                        else:
                            different |= float(row[column]) != value['value']
                    except (ValueError, TypeError):
                        unknown = True
                else:
                    different |= normalized_sku(row[column]) != value
            if not different:
                (uncertain if unknown else matches).append(row['id'])
        if uncertain or len(matches) > 1:
            return {'status':'comparison_unresolved', 'possible_matches':matches+uncertain}
        return {'status':'matched' if matches else 'new_relation', 'row_id':matches[0] if matches else None}

    def _audited_metrics(self, candidate, entity, contract):
        """Retrieve accepted absolute metrics from this exact frozen test scope."""
        if candidate['domain_id'] != 'benchmark' or benchmark_metric_kind(candidate['field_name'], contract):
            return []
        key = benchmark_comparison_key(candidate, entity)
        accepted = []
        state = self.store.metadata(self.run_id, ModelService.KEY)
        for identifier,row in state['candidates'].items():
            if row.get('domain_id') != 'benchmark' or row['versions'][-1].get('program', {}).get('status') != 'program_valid':
                continue
            _, metric, proof = self._proof(identifier)
            if not proof['accepted'] or not benchmark_metric_kind(metric['field_name'], contract):
                continue
            if metric['evidence']['snapshot_id'] != candidate['evidence']['snapshot_id']:
                continue
            metric_entity = self.resolve_entity(candidate_entity_hint(metric))
            if benchmark_comparison_key(metric, metric_entity) != key:
                continue
            if relation_readiness(metric, contract)['status'] == 'ready':
                accepted.append({'candidate':metric, 'proof':proof})
        return accepted

    def bind_candidate(self, candidate_id):
        row, candidate, proof = self._proof(candidate_id)
        entity = self.resolve_entity(candidate_entity_hint(candidate))
        frozen = load_phase(self.store.run_path(self.run_id) / 'skill_bundle', candidate['domain_id'], 'review')
        if self.store.metadata(self.run_id, 'baseline')['rule_bundle_hash'] != frozen['bundle_sha256']:
            raise ValueError('frozen baseline and relation rules mismatch')
        contract = DomainContract.model_validate(frozen['contract'])
        metrics = self._audited_metrics(candidate, entity, contract)
        readiness = relation_readiness(candidate, contract, metric_candidates=[row['candidate'] for row in metrics])
        missing = []
        key = candidate['entity_key']
        generic_chip = (candidate['domain_id'] == 'deployment' and key.get('chip_model') in {'generic','not_applicable'}
                        and candidate['conditions'].get('chip_applicability') == key['chip_model'])
        if generic_chip:
            entity = {'status':'not_applicable', 'entity_id':None, 'canonical_key':None,
                      'reason':'explicit_generic_guide_applicability'}
        elif entity['status'] not in {'new','existing'}:
            missing.append('chip_identity')
        elif entity['status'] == 'new' and candidate['target_table'] != 'chips':
            missing.append('chip:' + entity['temporary_entity_id'])
        model_id = candidate['entity_key'].get('model_id')
        if model_id and model_id not in {'generic','not_applicable'}:
            if not any(r.get('id') == model_id or r.get('model_id') == model_id for r in self._rows('models')):
                missing.append('model:' + str(model_id))
        evidence_accepted = proof['accepted'] and readiness['status'] == 'ready'
        comparison = (self._relation_comparison(candidate, entity, contract) if candidate['target_table'] != 'chips'
                      and evidence_accepted and entity['status'] in {'new','existing','not_applicable'} else
                      {'status':'dependency_unresolved'} if missing else
                      {'status':'not_applicable'})
        binding = {'logical_candidate_id':candidate_id, 'candidate':candidate, 'entity':entity,
                   'model_proof':proof, 'evidence_status':'accepted' if evidence_accepted else readiness['status'] if proof['accepted'] else row['status'],
                   'metric_proofs':[metric['proof'] for metric in metrics],
                   'relation_readiness':readiness, 'dependency_status':'dependency_unresolved' if missing else 'resolved',
                   'missing_dependencies':missing, 'comparison':comparison,
                   'validated':evidence_accepted and entity['status'] in {'new','existing','not_applicable'},
                   'update_eligible':evidence_accepted and not missing and comparison['status'] not in {'comparison_unresolved','dependency_unresolved'},
                   'evidence_outcomes':row.get('evidence_outcomes', []),
                   'evidence_versions':row.get('evidence_versions', []),
                   'comparison_key':benchmark_comparison_key(candidate, entity) if candidate['domain_id']=='benchmark' else None}
        binding['postprocessing_id'] = 'binding-' + _hash(_json(binding))
        def save(state):
            versions = state['bindings'].setdefault(candidate_id, [])
            prior = next((v for v in versions if v['postprocessing_id'] == binding['postprocessing_id']), None)
            if prior is not None:
                return state, prior, None
            versions.append(binding)
            return state, binding, {'kind':'candidate_dependency_resolution', 'stage':'comparing',
                                   'domain_id':candidate['domain_id'], 'postprocessing_id':binding['postprocessing_id'],
                                   'logical_candidate_id':candidate_id, 'model_proof':proof}
        return self._update(save)
