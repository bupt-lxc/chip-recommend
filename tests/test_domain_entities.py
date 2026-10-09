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
            if options.get('historical_existing'):
                db.execute("UPDATE chip_model_benchmarks SET chip_model='X80 80GB SXM'")
            if options.get('historical_extras'):
                db.execute("UPDATE chip_model_benchmarks SET concurrency='8',framework='PyTorch 2.5',test_date='2026-10-01'")
        db.execute("INSERT INTO chips (vendor,chip_model,vram_gb,form_factor) VALUES ('Vendor A','D80 64GB PCIe','64','PCIe')")
    store.ensure_baseline('run-a', gen, source, rule_bundle_hash=frozen['bundle_sha256'])
    text = ('Vendor A X80 64GB PCIe GPU; Vendor A X80 80GB SXM GPU; '
            'Vendor A D80 64GB PCIe GPU; Vendor A X90 series; Vendor A S90 server; '
            'org/Model-7B inference BF16 2 cards suite-report-A configuration-A '
            'batch 4 input 128 output 256 100 tokens/s; PyTorch 2.5 vendor support; '
            'Guide title backend vLLM; training 1000 tokens 2 cards 10 GPU-hours.')
    text += ' org/Model-7B non-generation inference BF16 2 cards suite-report-A configuration-A: 200 samples/s.'
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


def test_same_new_complete_sku_redundant_variants_count_once(entities, env):
    candidates = [fact(env), fact(env, entity_hints={'full_sku':'X80 64GB PCIe','entity_type':'board_sku',
                                                  'capacity':'64GB','form_factor':'PCIe'})]
    result, _ = audit(env, 'identity', candidates)
    resolved = resolver(entities, env)
    first, second = [resolved.register_identity(row['logical_candidate_id']) for row in result['candidates']]
    assert first['canonical_key'] == second['canonical_key']
    assert first['entity_id'] == second['entity_id']
    assert resolved.snapshot()['new_entity_count'] == 1
    assert len(second['identity_proofs']) == 2


def test_condition_scalar_is_not_an_absolute_performance_metric(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    candidate = relation(env, field_name='batch_size', raw_value=4, normalized_value=4, unit='samples_per_batch',
                         conditions={'hardware_config':'configuration-A','metric_class':'nongenerative'})
    benchmark, _ = audit(env, 'benchmark', [candidate])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    bound = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert bound['model_proof']['accepted']
    assert not bound['validated'] and bound['relation_readiness']['status']=='unresolved'
    assert 'absolute_metric_and_unit' in bound['relation_readiness']['missing_conditions']


def test_token_metric_cannot_bypass_generation_conditions_with_label(entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    candidate = relation(env, conditions={'hardware_config':'configuration-A','metric_class':'nongenerative'})
    benchmark, _ = audit(env, 'benchmark', [candidate])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    bound = resolved.bind_candidate(benchmark['candidates'][0]['logical_candidate_id'])
    assert bound['model_proof']['accepted']
    assert not bound['validated']
    assert set(bound['relation_readiness']['missing_conditions']) >= {'input_seq_length','output_seq_length','batch_size_or_concurrency'}


@pytest.mark.parametrize('field,value,unit', [('batch_size',4,'samples_per_batch'),('notes','configuration-A',None)])
def test_nonmetric_fields_use_only_same_configuration_audited_metric_proof(entities, env, field, value, unit):
    identity,_ = audit(env,'identity',[fact(env)])
    metadata = relation(env,field_name=field,normalized_value=value,raw_value=value,unit=unit)
    metric = relation(env)
    result,model = audit(env,'benchmark',[metadata,metric])
    before = model.snapshot()
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert binding['model_proof']['accepted'] and binding['validated']
    assert binding['metric_proofs'][0]['logical_candidate_id'] == result['candidates'][1]['logical_candidate_id']
    assert binding['metric_proofs'][0]['semantic_review'] == 'accept'
    assert model.snapshot() == before


def test_metadata_cannot_borrow_metric_from_other_suite_or_variant(entities, env):
    identity,_ = audit(env,'identity',[fact(env)])
    metadata = relation(env,field_name='batch_size',normalized_value=4,raw_value=4,unit='samples_per_batch')
    other = relation(env,entity_key={**metadata['entity_key'],'suite_name':'suite-report-B'})
    result,_ = audit(env,'benchmark',[metadata,other])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert not binding['validated'] and binding['metric_proofs'] == []


def test_metadata_cannot_borrow_rejected_metric_proof(entities, env):
    identity,_ = audit(env,'identity',[fact(env)])
    metadata = relation(env,field_name='batch_size',normalized_value=4,raw_value=4,unit='samples_per_batch')
    client = Transport([{'facts':[metadata,relation(env)]}, {'verdict':'accept','reason':'Configuration condition supported.'},
                        {'verdict':'reject','reason':'Metric unsupported.'}])
    model = ModelService(env[0],'run-a',env[1],env[2],env[4],env[3],domain_id='benchmark',client=client,backoff=lambda _:None)
    result = model.extract_page(env[-1]['snapshot_id'])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert not binding['validated'] and binding['metric_proofs'] == []


def test_metadata_cannot_borrow_metric_across_snapshots(entities, env):
    identity,_ = audit(env,'identity',[fact(env)])
    metadata = relation(env,field_name='notes',normalized_value='configuration-A',raw_value='configuration-A',unit=None)
    result,_ = audit(env,'benchmark',[metadata])
    visited = env[4].visit({'candidate_id':'other-report','url':'https://vendor.example/other-report'})
    other_env = (*env[:-1],env[4].read_snapshot(visited['snapshot_id']))
    audit(other_env,'benchmark',[relation(other_env)])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert binding['model_proof']['accepted'] and not binding['validated']
    assert binding['metric_proofs'] == []


def test_metadata_cannot_borrow_metric_across_physical_variants(entities, env):
    hints = [{'full_sku':'X80','entity_type':'board_sku','capacity':capacity,'form_factor':form}
             for capacity,form in [('64GB','PCIe'),('80GB','SXM')]]
    identity,_ = audit(env,'identity',[fact(env,sku='X80',entity_hints=h) for h in hints])
    key = {**relation(env)['entity_key'],'chip_model':'X80'}
    metadata = relation(env,field_name='batch_size',normalized_value=4,raw_value=4,unit='samples_per_batch',
                        entity_key=key,entity_hints={'vendor':'Vendor A',**hints[0]})
    metric = relation(env,entity_key=key,entity_hints={'vendor':'Vendor A',**hints[1]})
    result,_ = audit(env,'benchmark',[metadata,metric])
    resolved = resolver(entities,env)
    for row in identity['candidates']:
        resolved.register_identity(row['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert not binding['validated'] and binding['metric_proofs'] == []


def test_actual_nongeneration_metric_does_not_require_token_conditions(entities, env):
    identity,_ = audit(env,'identity',[fact(env)])
    candidate = relation(env,field_name='throughput_samples_s',unit='samples/s',raw_value=200,normalized_value=200,
                         conditions={'hardware_config':'configuration-A','metric_class':'nongenerative'})
    result,_ = audit(env,'benchmark',[candidate])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert binding['validated'] and binding['relation_readiness']['status']=='ready'


@pytest.mark.parametrize('env', [{'single_benchmark':True}], indirect=True)
def test_relation_comparison_preserves_resolved_variants_and_unknown_history(entities, env):
    # Build the actual ambiguous historical relation as part of a new frozen
    # fixture baseline; the original immutable run baseline is never edited.
    source = env[0].run_path('run-a').parent / 'variant-baseline.db'
    with closing(sqlite3.connect(source)) as db, db:
        db.executescript((ROOT / 'tests/fixtures/domain_discovery/business_schema.sql').read_text(encoding='utf-8'))
        db.execute('CREATE TABLE models (id TEXT)')
        db.execute("INSERT INTO models VALUES ('org/Model-7B')")
        db.execute("INSERT INTO chip_model_benchmarks (chip_model,model_id,suite_name,workload_type,chip_count,precision,hardware_config,batch_size,input_seq_length,output_seq_length) VALUES ('X80','org/Model-7B','suite-report-A','inference','2','BF16','configuration-A','4','128','256')")
    store = RunStore(env[0].root / 'variant-runs', clock=lambda:1000)
    store.start('start', {'domain':'specs'}, 'run-a')
    gen = store.claim('run-a','worker',100000)
    budget = RunBudget(store,'run-a',gen,monotonic=lambda:1000,epoch=lambda:1000)
    bundle = store.run_path('run-a') / 'skill_bundle'
    frozen = freeze_bundle(ROOT / '.hermes-skills',bundle)
    store.ensure_baseline('run-a',gen,source,rule_bundle_hash=frozen['bundle_sha256'])
    pages = PageService(store,'run-a',gen,budget,session=Session(env[-1]['text']),resolver=lambda _:['93.184.216.34'],monotonic=lambda:1000,backoff=lambda _:None)
    visited = pages.visit({'candidate_id':'variant-page','url':Response.url})
    variant_env = store,gen,budget,bundle,pages,pages.read_snapshot(visited['snapshot_id'])
    hints = [{'full_sku':'X80','entity_type':'board_sku','capacity':capacity,'form_factor':form}
             for capacity,form in [('64GB','PCIe'),('80GB','SXM')]]
    identity,_ = audit(variant_env,'identity',[fact(variant_env,sku='X80',entity_hints=h) for h in hints])
    benchmarks = [relation(variant_env,entity_hints={'vendor':'Vendor A',**h},
                          entity_key={**relation(variant_env)['entity_key'],'chip_model':'X80'}) for h in hints]
    result,_ = audit(variant_env,'benchmark',benchmarks)
    resolved = resolver(entities,variant_env)
    for row in identity['candidates']:
        resolved.register_identity(row['logical_candidate_id'])
    bound = [resolved.bind_candidate(row['logical_candidate_id']) for row in result['candidates']]
    assert len({row['entity']['entity_id'] for row in bound}) == 2
    assert len({row['comparison_key'] for row in bound}) == 2
    assert all(row['comparison']['status']=='comparison_unresolved' for row in bound)


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


@pytest.mark.parametrize('env', [{'single_benchmark':True,'historical_existing':True,'historical_extras':True}], indirect=True)
def test_history_only_known_conditions_require_unresolved_comparison(entities, env):
    identity,_ = audit(env,'identity',[fact(env,sku='X80 80GB SXM')])
    candidate = relation(env,entity_key={**relation(env)['entity_key'],'chip_model':'X80 80GB SXM'},
                         entity_hints={'vendor':'Vendor A','full_sku':'X80 80GB SXM','entity_type':'board_sku'})
    result,_ = audit(env,'benchmark',[candidate])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert binding['validated'] and binding['evidence_status']=='accepted'
    assert binding['entity']['status']=='existing' and binding['dependency_status']=='resolved'
    assert binding['comparison']['status']=='comparison_unresolved'
    assert not binding['update_eligible']


@pytest.mark.parametrize('env', [{'single_benchmark':True,'historical_existing':True}], indirect=True)
def test_optional_conditions_unknown_on_both_sides_do_not_block_comparison(entities, env):
    identity,_ = audit(env,'identity',[fact(env,sku='X80 80GB SXM')])
    candidate = relation(env,entity_key={**relation(env)['entity_key'],'chip_model':'X80 80GB SXM'},
                         entity_hints={'vendor':'Vendor A','full_sku':'X80 80GB SXM','entity_type':'board_sku'},
                         conditions={**relation(env)['conditions'],'concurrency':None,'framework':'unknown','test_date':None})
    result,_ = audit(env,'benchmark',[candidate])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(result['candidates'][0]['logical_candidate_id'])
    assert binding['validated'] and binding['comparison']['status']=='matched'
    assert binding['update_eligible']


@pytest.mark.parametrize('order', [('bare','a','b'),('a','b','bare'),('a','bare','b')])
def test_bare_board_identity_becomes_ambiguous_without_counting_third_entity(entities, env, order):
    hints = {'bare':{'full_sku':'X80','entity_type':'board_sku'},
             'a':{'full_sku':'X80','entity_type':'board_sku','capacity':'64GB','form_factor':'PCIe'},
             'b':{'full_sku':'X80','entity_type':'board_sku','capacity':'80GB','form_factor':'SXM'}}
    result,model = audit(env,'identity',[fact(env,sku='X80',entity_hints=hints[name]) for name in ('bare','a','b')])
    rows = dict(zip(('bare','a','b'), result['candidates']))
    before = model.snapshot()
    resolved = resolver(entities,env)
    early = None
    for name in order:
        resolved.register_identity(rows[name]['logical_candidate_id'])
        if name=='bare':
            early = resolved.bind_candidate(rows[name]['logical_candidate_id'])
    bare = resolved.resolve_entity({'vendor':'Vendor A',**hints['bare']})
    assert bare['status']=='ambiguous'
    assert resolved.snapshot()['new_entity_count']==2
    assert resolved.resolve_entity({'vendor':'Vendor A',**hints['a']})['status']=='new'
    assert resolved.resolve_entity({'vendor':'Vendor A',**hints['b']})['status']=='new'
    late = resolved.bind_candidate(rows['bare']['logical_candidate_id'])
    assert not late['validated'] and late['entity']['status']=='ambiguous'
    assert early is not None and model.snapshot()==before
    decisions = resolved.snapshot()['entity_decisions']
    assert any(d['entity_id']==bare['entity_id'] and d['status']=='ambiguous' for d in decisions)
    count = len(decisions)
    for name in order:
        resolved.register_identity(rows[name]['logical_candidate_id'])
    assert resolved.snapshot()['new_entity_count']==2
    assert len(resolved.snapshot()['entity_decisions'])==count


def test_explicit_chip_die_identity_survives_ambiguous_bare_board(entities, env):
    candidates = [fact(env,sku='X80',scope='chip',entity_hints={'full_sku':'X80','entity_type':'chip'}),
                  fact(env,sku='X80',scope='board_sku',entity_hints={'full_sku':'X80','entity_type':'board_sku'}),
                  *[fact(env,sku='X80',entity_hints={'full_sku':'X80','entity_type':'board_sku','capacity':capacity,'form_factor':form})
                    for capacity,form in [('64GB','PCIe'),('80GB','SXM')]]]
    result,model = audit(env,'identity',candidates)
    assert len(result['candidates'])==4
    before = model.snapshot()
    resolved = resolver(entities,env)
    for row in result['candidates']:
        resolved.register_identity(row['logical_candidate_id'])
    chip = resolved.resolve_entity({'vendor':'Vendor A','full_sku':'X80','entity_type':'chip'})
    board = resolved.resolve_entity({'vendor':'Vendor A','full_sku':'X80','entity_type':'board_sku'})
    assert chip['status']=='new' and chip['entity_kind']=='chip'
    assert board['status']=='ambiguous' and board['entity_kind']=='board_sku'
    assert chip['entity_id']!=board['entity_id']
    assert resolved.snapshot()['new_entity_count']==3
    assert model.snapshot()==before


def test_untyped_specs_resolves_unique_reviewed_chip_without_inference_from_scope(entities, env):
    identity,_ = audit(env,'identity',[fact(env,scope='chip',entity_hints={'full_sku':'X80 64GB PCIe','entity_type':'chip'})])
    specs,_ = audit(env,'specs',[fact(env,'specs',scope='chip',entity_hints={'full_sku':'X80 64GB PCIe'})])
    resolved = resolver(entities,env)
    entity = resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    binding = resolved.bind_candidate(specs['candidates'][0]['logical_candidate_id'])
    assert binding['validated'] and binding['entity']['entity_kind']=='chip'
    assert binding['entity']['entity_id']==entity['entity_id']


def test_untyped_nonidentity_measurement_scope_does_not_choose_between_kinds(entities, env):
    identity,_ = audit(env,'identity',[fact(env,scope=scope,entity_hints={'full_sku':'X80 64GB PCIe','entity_type':scope})
                                     for scope in ('chip','board_sku')])
    specs,_ = audit(env,'specs',[fact(env,'specs',scope='chip',entity_hints={'full_sku':'X80 64GB PCIe'})])
    resolved = resolver(entities,env)
    for row in identity['candidates']:
        resolved.register_identity(row['logical_candidate_id'])
    binding = resolved.bind_candidate(specs['candidates'][0]['logical_candidate_id'])
    assert not binding['validated'] and binding['entity']['status']=='ambiguous'


def test_old_nonempty_entity_policy_fails_closed_without_modifying_artifacts(entities, env):
    identity,model = audit(env,'identity',[fact(env)])
    resolved = resolver(entities,env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    def legacy(state):
        state.pop('entity_policy_version')
        for row in state['entities'].values():
            row.pop('entity_kind')
        return state,None,None
    env[0].update_metadata('run-a',env[1],resolved.KEY,legacy)
    before = resolved.snapshot()
    proof = model.snapshot()
    with pytest.raises(ValueError,match='policy version incompatible'):
        resolver(entities,env)
    assert resolved.snapshot()==before and model.snapshot()==proof


def test_identity_revision_cannot_change_entity_kind_and_reset_revision(entities, env):
    initial = fact(env)
    revised = fact(env,scope='chip',entity_hints={'full_sku':'X80 64GB PCIe','entity_type':'chip'})
    client = Transport([{'facts':[initial]}, {'verdict':'revise','reason':'Change physical entity type','candidate':revised}])
    model = ModelService(env[0],'run-a',env[1],env[2],env[4],env[3],domain_id='identity',client=client,backoff=lambda _:None)
    result = model.extract_page(env[-1]['snapshot_id'])
    assert result['candidates'][0]['status']=='unresolved'
    assert len(client.calls)==2 and len(result['candidates'][0]['versions'])==1


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
