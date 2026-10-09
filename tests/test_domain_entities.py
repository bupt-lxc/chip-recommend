"""Entity postprocessing against actual frozen model proof and readonly rows."""
import copy
import importlib
import importlib.util
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest

from chip_model.pipeline.domain_discovery.budget import RunBudget
from chip_model.pipeline.domain_discovery.models import ModelService
from chip_model.pipeline.domain_discovery.pages import PageService
from chip_model.pipeline.domain_discovery.rules import freeze_bundle
from chip_model.pipeline.domain_discovery.store import RunStore, _file_hash

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'chip_model.pipeline.domain_discovery.entities'


@pytest.fixture
def entities():
    assert importlib.util.find_spec(MODULE) is not None, 'entity resolution module is missing'
    return importlib.import_module(MODULE)


class Transport:
    model_name = 'frozen-entity-fixture'

    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def call_model(self, **request):
        self.calls.append(request)
        return copy.deepcopy(self.values.pop(0))


class Response:
    status_code = 200
    headers = {'Content-Type': 'text/plain'}
    encoding = 'utf-8'
    url = 'https://vendor.example/products'

    def __init__(self, text):
        self.text = text

    def iter_content(self, **kwargs):
        yield self.text.encode()

    def close(self):
        pass


class Session:
    headers = {}

    def __init__(self, text):
        self.text = text

    def get(self, *args, **kwargs):
        response = Response(self.text)
        response.url = args[0]
        return response


@pytest.fixture
def env(tmp_path, request):
    store = RunStore(tmp_path / 'runs', clock=lambda: 1000)
    store.start('start', {'domain': 'specs'}, 'run-a')
    gen = store.claim('run-a', 'worker', 100000)
    budget = RunBudget(store, 'run-a', gen, monotonic=lambda: 1000, epoch=lambda: 1000)
    bundle = store.run_path('run-a') / 'skill_bundle'
    frozen = freeze_bundle(ROOT / '.hermes-skills', bundle)
    source = tmp_path / 'baseline.db'
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript((ROOT / 'tests/fixtures/domain_discovery/business_schema.sql').read_text(encoding='utf-8'))
        db.execute('CREATE TABLE models (id TEXT)')
        db.execute("INSERT INTO models VALUES ('org/Model-7B')")
        db.execute("INSERT INTO chips (vendor,chip_model,vram_gb,form_factor) VALUES ('Vendor A','X80 80GB SXM','80','SXM')")
        db.execute("INSERT INTO chips (vendor,chip_model,vram_gb,form_factor) VALUES ('Vendor A','D80 64GB PCIe','64','PCIe')")
        options = getattr(request, 'param', {})
        if options.get('incomplete_variant'):
            db.execute("INSERT INTO chips (vendor,chip_model) VALUES ('Vendor A','X80 64GB PCIe')")
        if options.get('duplicate_benchmark') or options.get('single_benchmark'):
            for _ in range(2 if options.get('duplicate_benchmark') else 1):
                db.execute("INSERT INTO chip_model_benchmarks (chip_model,model_id,suite_name,workload_type,chip_count,precision,hardware_config,batch_size,input_seq_length,output_seq_length) VALUES ('X80 64GB PCIe','org/Model-7B','suite-report-A','inference','2','BF16','configuration-A','4','128','256')")
        db.execute("INSERT INTO chips (vendor,chip_model,vram_gb,form_factor) VALUES ('Vendor A','D80 64GB PCIe','64','PCIe')")
    store.ensure_baseline('run-a', gen, source, rule_bundle_hash=frozen['bundle_sha256'])
    text = ('Vendor A X80 64GB PCIe GPU; Vendor A X80 80GB SXM GPU; '
            'Vendor A D80 64GB PCIe GPU; Vendor A X90 series; Vendor A S90 server; '
            'org/Model-7B inference BF16 2 cards suite-report-A configuration-A '
            'batch 4 input 128 output 256 100 tokens/s; PyTorch 2.5 vendor support; '
            'Guide title backend vLLM; training 1000 tokens 2 cards 10 GPU-hours.')
    pages = PageService(store, 'run-a', gen, budget, session=Session(text),
                        resolver=lambda _: ['93.184.216.34'], monotonic=lambda: 1000, backoff=lambda _: None)
    visited = pages.visit({'candidate_id': 'page-a', 'url': Response.url})
    snapshot = pages.read_snapshot(visited['snapshot_id'])
    return store, gen, budget, bundle, pages, snapshot


def fact(env, domain='identity', sku='X80 64GB PCIe', field=None, value=None, **extra):
    snapshot = env[-1]
    if field is None:
        field, default = ('chip_type', 'GPU') if domain == 'identity' else ('vram_gb', 64)
        if value is None:
            value = default
    candidate = {'domain_id': domain, 'target_table': 'chips', 'field_name': field,
                 'entity_key': {'vendor': 'Vendor A', 'chip_model': sku},
                 'entity_hints': {'full_sku': sku, 'entity_type': 'board_sku'},
                 'raw_value': value, 'normalized_value': value,
                 'unit': 'GB' if field == 'vram_gb' else None,
                 'conversion': {'source_unit':'GB','factor':1,'basis':'decimal'} if field == 'vram_gb' else {},
                 'scope': 'board_sku', 'evidence_kind': 'explicit', 'conditions': {},
                 'evidence': {'snapshot_id': snapshot['snapshot_id'], 'body_hash': snapshot['body_hash'],
                              'quote': snapshot['text'], 'start': 0, 'end': len(snapshot['text'])},
                 'context_refs': [], 'explanation': 'Explicit vendor and concrete complete SKU.',
                 'source_type': 'official_datasheet', 'confidence': 'high',
                 'confidence_reason': 'Direct single SKU statement.'}
    candidate.update(extra)
    return candidate


def audit(env, domain, facts, scope='main'):
    store, gen, budget, bundle, pages, snapshot = env
    client = Transport([{'facts': facts}, *[{'verdict': 'accept', 'reason': 'Exact SKU and conditions supported.'} for _ in facts]])
    service = ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id=domain,
                           task_scope=scope, client=client, backoff=lambda _: None)
    return service.extract_page(snapshot['snapshot_id']), service


def resolver(entities, env, **kwargs):
    return entities.EntityResolver(env[0], 'run-a', env[1], **kwargs)


def test_normalization_preserves_complete_sku(entities):
    assert entities.normalized_sku(' X80   64GB PCIe ') == 'x80 64gb pcie'
    assert entities.normalized_sku('X80 64GB PCIe') != entities.normalized_sku('X80 80GB SXM')
    assert entities.temporary_entity_id('Vendor A', 'X80 64GB PCIe') == entities.temporary_entity_id(' vendor   a ', 'x80 64gb pcie')


def test_page_two_skus_keep_independent_vram_and_original_proof(entities, env):
    identity, _ = audit(env, 'identity', [fact(env), fact(env, sku='X80 80GB SXM')])
    specs, model = audit(env, 'specs', [fact(env, 'specs'), fact(env, 'specs', sku='X80 80GB SXM', value=80)])
    before = model.snapshot()
    resolved = resolver(entities, env)
    for row in identity['candidates']:
        resolved.register_identity(row['logical_candidate_id'])
    bound = [resolved.bind_candidate(row['logical_candidate_id']) for row in specs['candidates']]
    assert [r['entity']['status'] for r in bound] == ['new', 'existing']
    assert [r['candidate']['normalized_value'] for r in bound] == [64, 80]
    assert len({r['entity']['canonical_key'] for r in bound}) == 2
    assert model.snapshot() == before
    assert bound[0]['model_proof']['version'] == 0
    assert bound[0]['model_proof']['semantic_review'] == 'accept'
    assert resolved.snapshot()['new_entity_count'] == 1


def test_unverified_hints_do_not_match_or_count_new(entities, env):
    result, _ = audit(env, 'specs', [fact(env, 'specs')])
    resolved = resolver(entities, env)
    row = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert row['entity']['status'] == 'unresolved'
    assert row['dependency_status'] == 'dependency_unresolved'
    assert resolved.snapshot()['new_entity_count'] == 0
    with pytest.raises(ValueError, match='identity'):
        resolved.register_identity(result['candidates'][0]['logical_candidate_id'])


@pytest.mark.parametrize('kind,sku,status', [('series','X90','ambiguous'), ('server','S90','out_of_scope'), ('cluster','S90','out_of_scope')])
def test_series_and_systems_do_not_create_chips(entities, env, kind, sku, status):
    candidate = fact(env, sku=sku, field='chip_series', value=sku,
                     entity_hints={'full_sku': sku, 'entity_type': kind})
    result, _ = audit(env, 'identity', [candidate])
    resolved = resolver(entities, env)
    entity = resolved.register_identity(result['candidates'][0]['logical_candidate_id'])
    assert entity['status'] == status
    assert resolved.snapshot()['new_entity_count'] == 0


def test_duplicate_baseline_is_ambiguous_and_suffix_is_not_removed(entities, env):
    result, _ = audit(env, 'identity', [fact(env, sku='D80 64GB PCIe'), fact(env)])
    resolved = resolver(entities, env)
    assert resolved.register_identity(result['candidates'][0]['logical_candidate_id'])['status'] == 'ambiguous'
    assert resolved.register_identity(result['candidates'][1]['logical_candidate_id'])['status'] == 'new'


def test_vendor_alias_requires_explicit_verified_mapping(entities, env):
    candidate = fact(env, sku='X80 80GB SXM', entity_key={'vendor': 'VA', 'chip_model': 'X80 80GB SXM'})
    result, _ = audit(env, 'identity', [candidate])
    identifier = result['candidates'][0]['logical_candidate_id']
    assert resolver(entities, env).register_identity(identifier)['status'] == 'new'
    # Different policy must not mutate a run's established canonical identities.
    with pytest.raises(ValueError, match='alias'):
        resolver(entities, env, verified_vendor_aliases={'VA': 'Vendor A'})


def test_verified_vendor_alias_matches_exact_baseline(entities, env):
    result, _ = audit(env, 'identity', [fact(env, sku='X80 80GB SXM', entity_key={'vendor': 'VA', 'chip_model': 'X80 80GB SXM'})])
    assert resolver(entities, env, verified_vendor_aliases={'VA': 'Vendor A'}).register_identity(result['candidates'][0]['logical_candidate_id'])['status'] == 'existing'


def test_tampered_baseline_is_rejected(entities, env):
    resolved = resolver(entities, env)
    path = env[0].run_path('run-a') / 'data.db'
    path.chmod(stat.S_IWRITE)
    path.write_bytes(path.read_bytes() + b'tamper')
    with pytest.raises(ValueError, match='baseline'):
        resolved.resolve_entity({'vendor': 'Vendor A', 'full_sku': 'X80 64GB PCIe'})


def test_binding_is_append_only_then_resolves_dependency(entities, env):
    specs, model = audit(env, 'specs', [fact(env, 'specs')])
    resolved = resolver(entities, env)
    identifier = specs['candidates'][0]['logical_candidate_id']
    first = resolved.bind_candidate(identifier)
    original = model.snapshot()['candidates'][identifier]
    identity, _ = audit(env, 'identity', [fact(env)], scope='identity')
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    second = resolved.bind_candidate(identifier)
    assert first['entity']['status'] == 'unresolved' and second['entity']['status'] == 'new'
    assert len(resolved.snapshot()['bindings'][identifier]) == 2
    assert model.snapshot()['candidates'][identifier] == original
    assert resolved.bind_candidate(identifier) == second


def test_invented_or_unaccepted_identity_proof_is_rejected(entities, env):
    resolved = resolver(entities, env)
    with pytest.raises(ValueError, match='proof'):
        resolved.register_identity('invented-fact')


def test_identity_without_optional_type_hint_uses_reviewed_chip_scope(entities, env):
    candidate = fact(env, entity_hints={'full_sku':'X80 64GB PCIe'})
    result, _ = audit(env, 'identity', [candidate])
    assert resolver(entities, env).register_identity(result['candidates'][0]['logical_candidate_id'])['status'] == 'new'


def test_complete_sku_multiple_identity_fields_merge_including_series_scalar(entities, env):
    candidates = [fact(env, field=field,value=value,entity_hints={'full_sku':'X80 64GB PCIe'})
                  for field,value in [('vendor','Vendor A'),('chip_model','X80 64GB PCIe'),
                                      ('chip_series','X80'),('chip_type','GPU')]]
    result, _ = audit(env, 'identity', candidates)
    resolved = resolver(entities, env)
    entities = [resolved.register_identity(row['logical_candidate_id']) for row in result['candidates']]
    assert all(entity['status']=='new' for entity in entities)
    assert resolved.snapshot()['new_entity_count'] == 1
    assert len(entities[-1]['identity_proofs']) == 4


@pytest.mark.parametrize('env', [{'incomplete_variant':True}], indirect=True)
def test_baseline_missing_capacity_or_form_is_ambiguous(entities, env):
    candidate = fact(env, entity_hints={'full_sku':'X80 64GB PCIe','entity_type':'board_sku',
                                      'capacity':'64GB','form_factor':'PCIe'})
    result, _ = audit(env, 'identity', [candidate])
    resolved = resolver(entities, env)
    assert resolved.register_identity(result['candidates'][0]['logical_candidate_id'])['status'] == 'ambiguous'
    assert resolved.snapshot()['new_entity_count'] == 0


def test_entity_id_collision_checks_original_key(entities, env, monkeypatch):
    candidates = [fact(env), fact(env, sku='D90 64GB PCIe')]
    result, _ = audit(env, 'identity', candidates)
    monkeypatch.setattr(entities, 'temporary_entity_id', lambda *args: 'tmp-chip-collision')
    resolved = resolver(entities, env)
    resolved.register_identity(result['candidates'][0]['logical_candidate_id'])
    with pytest.raises(ValueError, match='collision'):
        resolved.register_identity(result['candidates'][1]['logical_candidate_id'])


def test_capacity_and_form_variants_with_same_base_sku_stay_distinct(entities, env):
    candidates = [fact(env, sku='X80', entity_hints={'full_sku':'X80','entity_type':'board_sku',
                                                   'capacity':capacity,'form_factor':form})
                  for capacity,form in [('64GB','PCIe'),('80GB','SXM')]]
    result, _ = audit(env, 'identity', candidates)
    resolved = resolver(entities, env)
    registered = [resolved.register_identity(row['logical_candidate_id']) for row in result['candidates']]
    assert all(row['status']=='new' for row in registered)
    assert len({row['canonical_key'] for row in registered}) == 2
    assert len({row['entity_id'] for row in registered}) == 2
    assert resolved.snapshot()['new_entity_count'] == 2
    assert resolved.resolve_entity({'vendor':'Vendor A','full_sku':'X80'})['status'] == 'ambiguous'


def test_same_existing_sku_with_optional_variant_details_merges_proofs(entities, env):
    candidates = [fact(env, sku='X80 80GB SXM'),
                  fact(env, sku='X80 80GB SXM', entity_hints={'full_sku':'X80 80GB SXM','entity_type':'board_sku',
                                                           'capacity':'80GB','form_factor':'SXM'})]
    result, _ = audit(env, 'identity', candidates)
    resolved = resolver(entities, env)
    first, second = [resolved.register_identity(row['logical_candidate_id']) for row in result['candidates']]
    assert first['canonical_key'] == second['canonical_key']
    assert first['entity_id'] == second['entity_id']
    assert len(second['identity_proofs']) == 2


def test_unreviewed_full_sku_cannot_override_reviewed_entity_key(entities, env):
    candidate = fact(env, entity_hints={'full_sku':'D90 64GB PCIe','entity_type':'board_sku'})
    result, _ = audit(env, 'identity', [candidate])
    resolved = resolver(entities, env)
    entity = resolved.register_identity(result['candidates'][0]['logical_candidate_id'])
    assert entity['status'] == 'ambiguous'
    assert resolved.snapshot()['new_entity_count'] == 0


def test_additional_same_value_evidence_remains_unreviewed_after_binding(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    candidate = fact(env, 'specs')
    extra = copy.deepcopy(candidate)
    quote = 'Vendor A X80 64GB PCIe GPU'
    extra['evidence'].update(quote=quote, start=0, end=len(quote))
    result, model = audit(env, 'specs', [candidate, extra])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    before = model.snapshot()
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert [r['semantic_review'] for r in binding['evidence_outcomes']] == ['accept','not_executed']
    assert binding['model_proof']['semantic_review'] == 'accept'
    assert model.snapshot() == before


@pytest.mark.parametrize('env', [{'duplicate_benchmark':True}], indirect=True)
def test_accepted_benchmark_with_ambiguous_history_is_not_an_update(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    benchmark, _ = audit(env, 'benchmark', [relation(env)])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    bound = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert bound['validated'] and bound['evidence_status'] == 'accepted'
    assert bound['comparison']['status'] == 'comparison_unresolved'


def test_benchmark_no_configuration_cannot_count_validated(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    candidate = relation(env)
    candidate['conditions'].pop('hardware_config')
    benchmark, _ = audit(env, 'benchmark', [candidate])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    bound = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert bound['model_proof']['accepted']
    assert bound['evidence_status'] == 'unresolved' and not bound['validated']


@pytest.mark.parametrize('env', [{'single_benchmark':True}], indirect=True)
def test_unverifiable_historical_configuration_is_not_silently_ignored(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    candidate = relation(env)
    candidate['conditions']['configuration_id'] = 'configuration-A'
    benchmark, _ = audit(env, 'benchmark', [candidate])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert binding['comparison']['status'] == 'comparison_unresolved'


def test_explicit_generic_model_independent_guide_has_no_fake_dependencies(entities, env):
    candidate = relation(env, 'deployment', target_table='deployment_guides', field_name='backend',
                         raw_value='vLLM',normalized_value='vLLM',unit=None,evidence_kind='explicit',entity_hints={},
                         entity_key={'url':'https://vendor.example/guide','title':'Guide title','chip_model':'generic',
                                     'model_id':'not_applicable','source_type':'official'},
                         conditions={'chip_applicability':'generic','model_applicability':'not_applicable'})
    result, _ = audit(env, 'deployment', [candidate])
    bound = resolver(entities, env).bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert bound['validated'] and bound['dependency_status'] == 'resolved'
    assert bound['missing_dependencies'] == []


def test_generation_latency_requires_lengths_even_without_metric_label(env):
    candidate = relation(env, field_name='time_to_first_token_ms', unit='ms', normalized_value=100,
                         conditions={'hardware_config':'configuration-A'})
    assert relation_check(env, candidate)['status'] == 'unresolved'


def relation(env, domain='benchmark', model='org/Model-7B', **updates):
    candidate = fact(env, domain, field='throughput_tok_s', value=100)
    candidate.update(target_table='chip_model_benchmarks', scope='relation', evidence_kind='measured', unit='tokens/s',
                     entity_key={'chip_model': 'X80 64GB PCIe', 'model_id': model, 'suite_name': 'suite-report-A',
                                 'workload_type': 'inference', 'chip_count': 2, 'precision': 'BF16'},
                     entity_hints={'vendor': 'Vendor A', 'full_sku': 'X80 64GB PCIe', 'entity_type': 'board_sku'},
                     conditions={'hardware_config': 'configuration-A', 'metric_class': 'generative_throughput',
                                 'input_seq_length': 128, 'output_seq_length': 256, 'batch_size': 4})
    candidate.update(updates)
    return candidate


def test_missing_model_relation_is_retained_without_model_creation(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    benchmark, model = audit(env, 'benchmark', [relation(env, model='org/model-7b')], scope='linked')
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    path = env[0].run_path('run-a') / 'data.db'
    digest = _file_hash(path)
    row = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert row['dependency_status'] == 'dependency_unresolved'
    assert 'model:org/model-7b' in row['missing_dependencies']
    assert 'chip:' + row['entity']['temporary_entity_id'] in row['missing_dependencies']
    assert row['evidence_status'] == 'accepted'
    assert row['candidate']['entity_key']['model_id'] == 'org/model-7b'
    assert _file_hash(path) == digest


def test_new_chip_relation_identity_and_publication_dependencies_are_distinct(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    benchmark, _ = audit(env, 'benchmark', [relation(env)])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert binding['validated'] and binding['entity']['status'] == 'new'
    assert binding['dependency_status'] == 'dependency_unresolved'
    assert binding['missing_dependencies'] == ['chip:' + binding['entity']['temporary_entity_id']]
    assert not binding['update_eligible']


def relation_check(env, candidate):
    from chip_model.pipeline.domain_discovery.contracts import load_domain, relation_readiness
    return relation_readiness(candidate, load_domain(env[3], candidate['domain_id']))


def test_benchmark_minimum_and_comparison_key_are_separate(entities, env):
    contracts = importlib.import_module('chip_model.pipeline.domain_discovery.contracts')
    complete = relation(env)
    assert relation_check(env, complete)['status'] == 'ready'
    no_config = copy.deepcopy(complete)
    no_config['conditions'].pop('hardware_config')
    assert relation_check(env, no_config)['status'] == 'unresolved'
    no_tokens = copy.deepcopy(complete)
    no_tokens['conditions']['input_seq_length'] = 'not_applicable'
    assert relation_check(env, no_tokens)['status'] == 'unresolved'
    nongenerative = relation(env, field_name='throughput_samples_s', unit='samples/s',
                            conditions={'hardware_config':'configuration-A','metric_class':'nongenerative'})
    assert relation_check(env, nongenerative)['status'] == 'ready'
    other = copy.deepcopy(complete)
    other['conditions']['concurrency'] = 8
    assert contracts.benchmark_comparison_key(complete) != contracts.benchmark_comparison_key(other)


def test_training_minimum_and_compatibility_strength_and_guide_applicability(env):
    contracts = importlib.import_module('chip_model.pipeline.domain_discovery.contracts')
    assert hasattr(contracts, 'relation_readiness'), 'relation minimum gate missing'
    training = relation(env, field_name='gpu_hours', unit='GPU-hours', normalized_value=10, raw_value=10)
    training['entity_key']['workload_type'] = 'training'
    training['conditions'] = {'training_workload':'pretrain', 'training_data':'1000 tokens', 'hardware_configuration':'2 cards'}
    assert relation_check(env, training)['status'] == 'ready'
    training['conditions'].pop('training_data')
    assert relation_check(env, training)['status'] == 'unresolved'
    compat = relation(env, domain='compatibility', target_table='chip_model_compatibility', field_name='compat_status',
                      normalized_value='verified', raw_value='verified', source_type='vendor_claim',
                      entity_key={'chip_model':'X80 64GB PCIe','model_id':'org/Model-7B'})
    assert relation_check(env, compat)['status'] == 'rejected'
    compat.update(normalized_value='vendor_claimed', raw_value='vendor_claimed')
    assert relation_check(env, compat)['status'] == 'ready'
    guide = relation(env, domain='deployment', target_table='deployment_guides', field_name='backend',
                     entity_key={'url':Response.url,'title':'Guide title','chip_model':'X80 64GB PCIe',
                                 'model_id':'org/Model-7B','source_type':'official'}, conditions={})
    assert relation_check(env, guide)['status'] == 'ready'
    guide['entity_key']['chip_model'] = 'generic'
    assert relation_check(env, guide)['status'] == 'unresolved'
    guide['conditions']['chip_applicability'] = 'generic'
    assert relation_check(env, guide)['status'] == 'ready'
