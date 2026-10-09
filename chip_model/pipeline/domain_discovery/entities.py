"""Deterministic entity/dependency postprocessing; never rewrite model evidence."""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

from .contracts import benchmark_comparison_key, relation_readiness
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


def candidate_entity_hint(candidate: dict) -> dict:
    """Structured model hints remain unverified until identity proof is registered."""
    key, hints = candidate.get('entity_key', {}), candidate.get('entity_hints', {})
    mismatch = (bool(key.get('chip_model') and hints.get('full_sku'))
                and normalized_sku(key['chip_model']) != normalized_sku(hints['full_sku']))
    return {'vendor': key.get('vendor') or hints.get('vendor'),
            'full_sku': key.get('chip_model') or hints.get('full_sku'),
            **({'identity_hint_conflict':True, 'claimed_full_sku':hints['full_sku']} if mismatch else {}),
            **{name: hints[name] for name in ('capacity', 'form_factor', 'entity_type') if name in hints}}


class EntityResolver:
    KEY = 'domain_entities.v1'

    def __init__(self, store, run_id, generation, *, verified_vendor_aliases=None):
        self.store, self.run_id, self.generation = store, run_id, generation
        prior = store.metadata(run_id, self.KEY)
        aliases = (prior['verified_vendor_aliases'] if prior else {}) if verified_vendor_aliases is None else verified_vendor_aliases
        if not isinstance(aliases, dict):
            raise ValueError('verified vendor alias map must be explicit')
        self.aliases = {normalized_sku(name): normalized_sku(target) for name, target in aliases.items()}
        if any(target in self.aliases and self.aliases[target] != target for target in self.aliases.values()):
            raise ValueError('verified aliases must map directly to canonical vendors')
        def initialize(state):
            if state is not None and state['verified_vendor_aliases'] != self.aliases:
                raise ValueError('verified vendor alias policy conflict')
            return state or {'verified_vendor_aliases':self.aliases, 'entities':{}, 'bindings':{}, 'new_entity_count':0}, None, None
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
        if 'capacity' in hint:
            normalized['capacity'] = _capacity_identity(hint['capacity'])
        if 'form_factor' in hint:
            normalized['form_factor'] = normalized_sku(hint['form_factor'])
        variants = {name:normalized[name] for name in ('capacity','form_factor') if name in normalized}
        return normalized, vendor + '\0' + sku + ('\0' + _json(variants) if variants else '')

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
        try:
            normalized, key = self._hint(hint)
        except (ValueError, KeyError, TypeError):
            return {'status':'unresolved', 'entity_id':None, 'canonical_key':None, 'reason':'incomplete_entity_hint', 'hint':hint}
        variants = {name:normalized[name] for name in ('capacity','form_factor') if name in normalized}
        complete_sku = normalized['full_sku'] + (' ' + _json(variants) if variants else '')
        temporary = temporary_entity_id(normalized['vendor'], complete_sku)
        result = {'status':'unresolved', 'entity_id':temporary, 'temporary_entity_id':temporary,
                  'canonical_key':key, 'hint':normalized, 'reason':'identity_dependency_unresolved', 'possible_matches':[]}
        if hint.get('identity_hint_conflict'):
            return {**result, 'status':'ambiguous', 'reason':'full_sku_disagrees_with_reviewed_entity_key',
                    'claimed_full_sku':hint['claimed_full_sku']}
        if not verified:
            return result
        if entity_type in {'server', 'cluster', 'non_compute'}:
            return {**result, 'status':'out_of_scope', 'reason':'identity_out_of_scope'}
        if entity_type == 'series':
            return {**result, 'status':'ambiguous', 'reason':'series_is_not_specific_sku'}
        if entity_type not in {'chip', 'board_sku', 'single_card'}:
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
        requested = preliminary['hint']
        records = []
        for record in self.snapshot()['entities'].values():
            if any(record['hint'].get(name) != requested.get(name) for name in ('vendor','full_sku')):
                continue
            if record['canonical_key'] == key:
                return record
            if all(name not in requested or requested[name] == record['hint'].get(name)
                   for name in ('capacity','form_factor')):
                records.append(record)
        if len(records) == 1:
            return records[0]
        if records or any(r['hint']['vendor'] == requested['vendor'] and r['hint']['full_sku'] == requested['full_sku']
                          for r in self.snapshot()['entities'].values()):
            return {**preliminary, 'status':'ambiguous', 'reason':'variant_identity_unconfirmed'}
        return preliminary

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
        entity['identity_proofs'] = [proof]
        def save(state):
            identifier = entity['entity_id']
            if identifier is None:
                raise ValueError('identity full vendor and SKU proof missing')
            prior = state['entities'].get(identifier)
            if prior and prior['canonical_key'] != entity['canonical_key']:
                raise ValueError('entity ID collision: original canonical key differs')
            if prior:
                if prior['hint'] != entity['hint'] or prior['status'] != entity['status']:
                    prior.update(status='ambiguous', reason='conflicting_identity_proofs')
                if proof not in prior['identity_proofs']:
                    prior['identity_proofs'].append(proof)
                result = prior
            else:
                state['entities'][identifier] = entity
                result = entity
            state['new_entity_count'] = sum(r['status'] == 'new' for r in state['entities'].values())
            return state, result, {'kind':'identity_resolution', 'stage':'comparing', 'domain_id':'identity', 'entity':result}
        return self._update(save)

    def _relation_comparison(self, candidate):
        table, key = candidate['target_table'], candidate['entity_key']
        rows = self._rows(table)
        dimensions = dict(key)
        if table in {'chip_model_benchmarks','chip_model_compatibility'}:
            dimensions.update({name:value for name,value in candidate['conditions'].items()
                               if name not in {'metric_class','verification_kind'}})
        matches, uncertain = [], []
        for row in rows:
            if any(str(row.get(name)) != str(value) for name,value in key.items()):
                continue
            unknown = any(row.get(name) in (None, '') for name in dimensions if name not in key)
            different = any(row.get(name) not in (None, '') and str(row[name]) != str(value)
                            for name,value in dimensions.items() if name not in key)
            if not different:
                (uncertain if unknown else matches).append(row['id'])
        if uncertain or len(matches) > 1:
            return {'status':'comparison_unresolved', 'possible_matches':matches+uncertain}
        return {'status':'matched' if matches else 'new_relation', 'row_id':matches[0] if matches else None}

    def bind_candidate(self, candidate_id):
        row, candidate, proof = self._proof(candidate_id)
        entity = self.resolve_entity(candidate_entity_hint(candidate))
        frozen = load_phase(self.store.run_path(self.run_id) / 'skill_bundle', candidate['domain_id'], 'review')
        if self.store.metadata(self.run_id, 'baseline')['rule_bundle_hash'] != frozen['bundle_sha256']:
            raise ValueError('frozen baseline and relation rules mismatch')
        from .contracts import DomainContract
        readiness = relation_readiness(candidate, DomainContract.model_validate(frozen['contract']))
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
        comparison = (self._relation_comparison(candidate) if candidate['target_table'] != 'chips'
                      and evidence_accepted and entity['status'] in {'new','existing','not_applicable'} else
                      {'status':'dependency_unresolved'} if missing else
                      {'status':'not_applicable'})
        binding = {'logical_candidate_id':candidate_id, 'candidate':candidate, 'entity':entity,
                   'model_proof':proof, 'evidence_status':'accepted' if evidence_accepted else readiness['status'] if proof['accepted'] else row['status'],
                   'relation_readiness':readiness, 'dependency_status':'dependency_unresolved' if missing else 'resolved',
                   'missing_dependencies':missing, 'comparison':comparison,
                   'validated':evidence_accepted and entity['status'] in {'new','existing','not_applicable'},
                   'update_eligible':evidence_accepted and not missing and comparison['status'] not in {'comparison_unresolved','dependency_unresolved'},
                   'evidence_outcomes':row.get('evidence_outcomes', []),
                   'evidence_versions':row.get('evidence_versions', []),
                   'comparison_key':benchmark_comparison_key(candidate) if candidate['domain_id']=='benchmark' else None}
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
