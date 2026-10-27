"""Controlled model/HTTP fixtures; these are not live model or internet proof."""

import copy
import importlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
import requests

from chip_model.pipeline.domain_discovery.budget import RunBudget
from chip_model.pipeline.domain_discovery.pages import PageService
from chip_model.pipeline.domain_discovery.rules import freeze_bundle, load_phase
from chip_model.pipeline.domain_discovery.store import RunStore

ROOT = Path(__file__).resolve().parents[1]


def test_models_module_exists():
    assert importlib.util.find_spec('chip_model.pipeline.domain_discovery.models') is not None


@pytest.mark.parametrize('text,field,value,unit', [
    ('X80单芯片功耗达1300W','tdp_w',1300,'W'),
    ('X80功耗约1300 W','tdp_w',1300,'W'),
    ('X80功耗为+1300W','tdp_w',1300,'W'),
    ('X80显存容量为64GB','vram_gb',64,'GB'),
])
def test_chinese_adjacent_quantity_with_explicit_unit_is_valid(module,environment,text,field,value,unit):
    _,snapshot=page(environment,text)
    candidate=fact(snapshot,field_name=field,raw_value=value,normalized_value=value,unit=unit,
                   conversion={'source_unit':unit,'factor':1,'basis':'decimal'})
    before=copy.deepcopy(candidate)
    program=module.validate_candidate(candidate,load_phase(environment[-1],'specs','extract')['contract'],
                                      snapshot,{'start':0,'end':len(snapshot['text']),'context_refs':[]})
    assert program['status']=='program_valid' and candidate==before


@pytest.mark.parametrize('text,sku,field,value,unit', [
    ('H1300W型号,功耗300W','H1300W','tdp_w',1300,'W'),
    ('X-1300W型号,功耗300W','X-1300W','tdp_w',1300,'W'),
    ('昇腾1300W型号,功耗300W','昇腾1300W','tdp_w',1300,'W'),
    ('X80功耗-1300W','X80','tdp_w',1300,'W'),
    ('X80功耗13 W和00 W','X80','tdp_w',1300,'W'),
    ('X80功耗1300.5W','X80','tdp_w',300,'W'),
    ('X80功耗1300Watt','X80','tdp_w',1300,'W'),
    ('X80带宽64GB/s','X80','vram_gb',64,'GB'),
])
def test_chinese_numeric_relaxation_never_uses_sku_or_spliced_values(module,environment,text,sku,field,value,unit):
    _,snapshot=page(environment,text)
    candidate=fact(snapshot,entity_key={'vendor':'Vendor','chip_model':sku},entity_hints={'full_sku':sku},
                   field_name=field,raw_value=value,normalized_value=value,unit=unit,
                   conversion={'source_unit':unit,'factor':1,'basis':'decimal'})
    with pytest.raises(ValueError,match='raw numeric value'):
        module.validate_candidate(candidate,load_phase(environment[-1],'specs','extract')['contract'],
                                  snapshot,{'start':0,'end':len(snapshot['text']),'context_refs':[]})


@pytest.fixture
def module():
    assert importlib.util.find_spec('chip_model.pipeline.domain_discovery.models') is not None
    return importlib.import_module('chip_model.pipeline.domain_discovery.models')


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class PageResponse:
    status_code = 200
    headers = {'Content-Type': 'text/plain'}
    encoding = 'utf-8'
    url = 'https://vendor.example/spec'

    def __init__(self, text, mime='text/plain'):
        self.text = text
        self.headers = {'Content-Type': mime}

    def iter_content(self, chunk_size=65536):
        yield self.text.encode()

    def close(self):
        pass


class PageSession:
    headers = {}

    def __init__(self, text, mime='text/plain'):
        self.text, self.mime = text, mime

    def get(self, url, **kwargs):
        return PageResponse(self.text, self.mime)


class FakeModel:
    model_name = 'controlled-fixture-model'

    def __init__(self, results):
        self.results, self.requests = list(results), []

    def call_model(self, *, messages, timeout):
        self.requests.append({'messages': messages, 'timeout': timeout})
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)


@pytest.fixture
def environment(tmp_path):
    clock = Clock()
    store = RunStore(tmp_path / 'runs', clock=clock)
    store.start('start', {'domain': 'specs'}, 'run-a')
    generation = store.claim('run-a', 'worker', 100000)
    budget = RunBudget(store, 'run-a', generation, monotonic=clock, epoch=clock)
    bundle = store.run_path('run-a') / 'skill_bundle'
    frozen = freeze_bundle(ROOT / '.hermes-skills', bundle)
    source = tmp_path / 'fixture.db'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE chips (id TEXT,vendor TEXT,chip_model TEXT,vram_gb TEXT);'
                         "INSERT INTO chips VALUES ('1','Vendor','X80','32');"
                         'CREATE TABLE field_provenance (id TEXT,table_name TEXT,row_id TEXT,field_name TEXT,source_url TEXT);')
    store.ensure_baseline('run-a', generation, source, rule_bundle_hash=frozen['bundle_sha256'])
    return store, generation, budget, clock, bundle


def page(environment, text='X80: 64GB HBM3', mime='text/plain'):
    store, gen, budget, clock, _ = environment
    pages = PageService(store, 'run-a', gen, budget, session=PageSession(text, mime),
                        resolver=lambda _: ['93.184.216.34'], monotonic=clock, backoff=lambda _: None)
    item = pages.visit({'candidate_id': 'page-a', 'url': PageResponse.url})
    return pages, pages.read_snapshot(item['snapshot_id'])


def fact(snapshot, **changes):
    text = snapshot['text']
    value = {'domain_id': 'specs', 'target_table': 'chips', 'field_name': 'vram_gb',
             'entity_key': {'vendor': 'Vendor', 'chip_model': 'X80'},
             'entity_hints': {'full_sku': 'X80'}, 'raw_value': 64, 'normalized_value': 64,
             'unit': 'GB', 'conversion': {'source_unit': 'GB', 'factor': 1, 'basis': 'decimal'},
             'scope': 'chip', 'evidence_kind': 'explicit', 'conditions': {},
             'evidence': {'snapshot_id': snapshot['snapshot_id'], 'body_hash': snapshot['body_hash'],
                          'quote': text, 'start': 0, 'end': len(text)},
             'context_refs': [], 'explanation': 'Exact single SKU capacity.',
             'source_type': 'official_datasheet', 'confidence': 'high',
             'confidence_reason': 'Explicit specification.'}
    value.update(changes)
    return value


def service(module, environment, pages, client):
    store, gen, budget, clock, bundle = environment
    return module.ModelService(store, 'run-a', gen, budget, pages, bundle,
                               client=client, monotonic=clock, backoff=lambda _: None)


def test_exact_continuous_quote_and_strict_positions(module):
    assert module.validate_quote('X80: 64GB HBM3', {'quote': '64GB', 'start': 5, 'end': 9})
    for wrong in [{'quote': '80GB', 'start': 5, 'end': 9},
                  {'quote': '64GB', 'start': True, 'end': 9},
                  {'quote': '', 'start': 5, 'end': 5},
                  {'quote': '64 HBM3', 'start': 5, 'end': 13}]:
        with pytest.raises(ValueError):
            module.validate_quote('X80: 64GB HBM3', wrong)


def test_extract_and_independent_review_receive_complete_same_frozen_bundle(module, environment):
    pages, snapshot = page(environment)
    candidate = fact(snapshot)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'accept', 'reason': 'The quote supports X80.'}])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'], identity_context={'X80': {'status': 'existing'}})
    assert result['candidates'][0]['status'] == 'validated'
    assert result['complete'] and len(client.requests) == 2
    for index, phase in enumerate(('extract', 'review')):
        frozen = load_phase(environment[-1], 'specs', phase)
        system, user = client.requests[index]['messages']
        assert frozen['skill_text'] in system['content']
        assert all(text in system['content'] for text in frozen['references'].values())
        assert 'UNTRUSTED' in system['content'] and phase in system['content']
        payload = json.loads(user['content'])
        assert payload['bundle_sha256'] == frozen['bundle_sha256']
        assert payload['identity_context']['X80']['status'] == 'existing'
        assert payload['required_schema'] and payload['page']['text'] == snapshot['text']
    reviewed = json.loads(client.requests[1]['messages'][1]['content'])
    assert reviewed['candidate'] == candidate
    assert reviewed['baseline']['chips'][0]['vram_gb'] == '32'
    assert models.snapshot()['calls'][0]['phase'] == 'extract'
    assert pages.chunks(snapshot['snapshot_id'], domain_id='specs')['coverage']['complete']


@pytest.mark.parametrize('reason,text,changes,status,calls', [
    ('wrong SKU', 'X80-A: 64GB; X80-B: 80GB', {'entity_key': {'vendor': 'Vendor', 'chip_model': 'X80-A'}, 'raw_value': 80, 'normalized_value': 80}, 'unresolved', 1),
    ('system power', 'X80 server total power 2400 W', {'field_name': 'tdp_w', 'unit': 'W', 'raw_value': 2400, 'normalized_value': 2400, 'conversion': {'source_unit': 'W', 'factor': 1}}, 'semantic_rejected', 2),
    ('unrelated quote', 'X80 ships in 64 countries; capacity unit GB is listed separately', {}, 'unresolved', 1),
])
def test_semantic_false_facts_are_never_program_only_accepted(module, environment, reason, text, changes, status, calls):
    pages, snapshot = page(environment, text)
    candidate = fact(snapshot, **changes)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'reject', 'reason': reason}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == status
    assert len(client.requests) == calls and result['complete']
    if status == 'unresolved':
        assert result['candidates'][0]['reason'] == 'sku_value_binding_unresolved'


@pytest.mark.parametrize('change', [
    {'field_name': 'price_usd'}, {'raw_value': 640, 'normalized_value': 640},
    {'raw_value': True, 'normalized_value': True}, {'normalized_value': 'inf'},
    {'unit': 'GiB'}, {'entity_key': {}}, {'scope': 'cluster'},
    {'evidence': {'quote': '64GB', 'start': 5, 'end': 9, 'snapshot_id': 'other', 'body_hash': 'x'}},
    {'context_refs': [{'start': 0, 'end': 3, 'kind': 'table_header', 'quote': 'X80'}]},
])
def test_program_rejections_are_audited_without_semantic_calls(module, environment, change):
    pages, snapshot = page(environment)
    client = FakeModel([{'facts': [fact(snapshot, **change)]}])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'program_rejected'
    assert len(client.requests) == 1 and result['complete']
    assert models.snapshot()['raw_facts']


def test_one_revision_revalidated_and_independently_reviewed(module, environment):
    pages, snapshot = page(environment, 'X80: 64GB HBM3 or 80GB alternate configuration')
    first = fact(snapshot)
    revised = fact(snapshot, raw_value=80, normalized_value=80)
    client = FakeModel([{'facts': [first]}, {'verdict': 'revise', 'reason': 'Use alternate configuration.', 'candidate': revised},
                        {'verdict': 'accept', 'reason': 'Explicit alternate capacity.'}])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    row = result['candidates'][0]
    assert row['status'] == 'validated' and len(row['versions']) == 2
    assert row['versions'][1]['candidate']['normalized_value'] == 80
    assert len(client.requests) == 3
    models.review_candidate(first, request_id='changed-client-id', candidate_version=900)
    assert len(client.requests) == 3
    assert list(environment[2].snapshot()['candidate_revisions'].values()) == [1]


def test_second_revision_never_resets_stable_candidate_budget(module, environment):
    pages, snapshot = page(environment)
    candidate = fact(snapshot)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'revise', 'reason': 'Recheck.', 'candidate': candidate},
                        {'verdict': 'revise', 'reason': 'Again.', 'candidate': candidate}])
    models = service(module, environment, pages, client)
    row = models.extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'unresolved' and row['reason'] == 'revision_limit'
    assert len(client.requests) == 3
    assert module.logical_candidate_id(candidate) == module.logical_candidate_id({**candidate, 'normalized_value': 999, 'raw_value': 999})


def test_revision_cannot_change_entity_or_bypass_program_check(module, environment):
    pages, snapshot = page(environment)
    candidate = fact(snapshot)
    revised = fact(snapshot, entity_key={'vendor': 'Vendor', 'chip_model': 'X80-B'})
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'revise', 'reason': 'Change entity.', 'candidate': revised}])
    row = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'unresolved' and len(client.requests) == 2


def test_review_unavailable_stays_failed_range_and_three_total_physical_calls(module, environment):
    pages, snapshot = page(environment)
    client = FakeModel([{'facts': [fact(snapshot)]}, requests.Timeout('Bearer secret'), requests.Timeout(), requests.Timeout()])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'review_failed' and not result['complete']
    assert pages.chunks(snapshot['snapshot_id'], domain_id='specs')['coverage']['failed']
    assert len(client.requests) == 4
    assert 'secret' not in json.dumps(models.snapshot())
    assert models.extract_page(snapshot['snapshot_id']) == result
    assert len(client.requests) == 4


def test_duplicate_same_fact_merges_evidence_and_conflicting_values_unresolved(module, environment):
    pages, snapshot = page(environment, 'X80: 64GB or 80GB')
    a = fact(snapshot)
    b = fact(snapshot, raw_value=80, normalized_value=80)
    client = FakeModel([{'facts': [a, copy.deepcopy(a), b]}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['status'] == 'unresolved'
    assert len(result['candidates'][0]['evidence_versions']) == 2
    assert len(client.requests) == 1


@pytest.mark.parametrize('domain,field,text,raw,unit,conversion,keys,conditions,reason', [
    ('identity', 'release_date', 'X80 Copyright 2026', '2026', None, {},
     {'vendor': 'Vendor', 'chip_model': 'X80'}, {}, 'copyright date is not a release'),
    ('compute', 'precision_perf', 'X80 FP16 sparse peak 100 TFLOPS', 100, 'TFLOPS',
     {'source_unit': 'TFLOPS', 'factor': 1, 'dense_or_sparse': 'dense', 'precision': 'FP16'},
     {'vendor': 'Vendor', 'chip_model': 'X80'}, {}, 'sparse peak misrepresented as dense'),
    ('benchmark', 'throughput_tok_s', 'X80 theoretical 100 tokens/s Qwen/Qwen3-8B; Report A 1 card FP16 input 128 output 128 batch 1', 100, 'tokens/s',
     {'source_unit': 'tokens/s', 'factor': 1},
     {'chip_model': 'X80', 'model_id': 'Qwen/Qwen3-8B', 'suite_name': 'Report A',
      'workload_type': 'inference', 'chip_count': 1, 'precision': 'FP16'},
     {'metric_class': 'generative_throughput', 'input_seq_length': 128, 'output_seq_length': 128, 'batch_size': 1},
     'theoretical mislabeled measured'),
])
def test_domain_semantic_rejections_use_independent_model(module, environment, domain, field, text, raw, unit,
                                                        conversion, keys, conditions, reason):
    pages, snapshot = page(environment, text)
    candidate = fact(snapshot, domain_id=domain, field_name=field, raw_value=raw, normalized_value=raw,
                     target_table='chip_model_benchmarks' if domain == 'benchmark' else 'chips',
                     unit=unit, conversion=conversion, entity_key=keys, conditions=conditions)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'reject', 'reason': reason}])
    store, gen, budget, clock, bundle = environment
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle,
                                client=client, domain_id=domain, monotonic=clock, backoff=lambda _: None)
    result = models.extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'semantic_rejected' and len(client.requests) == 2


def test_benchmark_missing_required_generative_conditions_is_typed_unresolved(module, environment):
    pages, snapshot = page(environment, 'X80 Qwen/Qwen3-8B measured 100 tokens/s')
    candidate = fact(snapshot, domain_id='benchmark', target_table='chip_model_benchmarks',
                     field_name='throughput_tok_s', raw_value=100, normalized_value=100, unit='tokens/s',
                     conversion={'source_unit': 'tokens/s', 'factor': 1},
                     entity_key={'chip_model': 'X80', 'model_id': 'Qwen/Qwen3-8B', 'suite_name': 'Report A',
                                 'workload_type': 'inference', 'chip_count': 1, 'precision': 'FP16'},
                     conditions={'metric_class': 'generative_throughput'}, scope='relation', evidence_kind='measured')
    client = FakeModel([{'facts': [candidate]}])
    store, gen, budget, clock, bundle = environment
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='benchmark',
                                client=client, monotonic=clock, backoff=lambda _: None)
    result = models.extract_page(snapshot['snapshot_id'])
    row = result['candidates'][0]
    assert row['status'] == 'unresolved' and row['reason'] == 'incomplete_evidence'
    assert 'input_seq_length' in row['missing_conditions'] and len(client.requests) == 1


@pytest.mark.parametrize('text,field,raw,normalized,unit,conversion', [
    ('X80 memory 64 GiB', 'vram_gb', 64, 64, 'GB', {'source_unit': 'GiB', 'factor': 1, 'basis': 'binary'}),
    ('X80 bandwidth 2.0 TB/s', 'vram_bw_gb_s', 2.0, 2000, 'GB/s', {'source_unit': 'TB/s', 'factor': 1000, 'direction': 'bidirectional', 'aggregation': 'aggregate'}),
    ('X80 link 800 Gb/s', 'interconnect_bw_gb_s', 800, 100, 'GB/s', {'source_unit': 'Gb/s', 'factor': 0.125, 'direction': 'unidirectional', 'aggregation': 'per_link'}),
])
def test_explicit_conversions_preserve_lossless_basis_and_scope(module, environment, text, field, raw,
                                                             normalized, unit, conversion):
    pages, snapshot = page(environment, text)
    candidate = fact(snapshot, field_name=field, raw_value=raw, normalized_value=normalized, unit=unit, conversion=conversion)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'accept', 'reason': 'Explicit value and scope.'}])
    row = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'validated'
    assert row['versions'][0]['candidate']['conversion'] == conversion


def test_invalid_revised_value_is_program_rejected_before_second_review(module, environment):
    pages, snapshot = page(environment)
    candidate = fact(snapshot)
    changed = fact(snapshot, raw_value=999, normalized_value=999)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'revise', 'reason': 'Unsupported change.', 'candidate': changed}])
    row = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'program_rejected' and len(client.requests) == 2
    assert row['versions'][1]['program']['status'] == 'rejected'


def test_nonfinite_provider_response_is_audited_as_parser_failure(module, environment):
    pages, snapshot = page(environment)
    bad = {'facts': [fact(snapshot, normalized_value=float('inf'))]}
    client = FakeModel([bad, bad, bad])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert not result['complete'] and result['chunks'][0]['reason'] == 'parser_rejected'
    assert len(client.requests) == 3
    assert models.snapshot()['calls'][-1]['response']['facts'][0]['normalized_value'] == {'invalid_number': 'inf'}


def test_known_response_reconciles_after_crash_without_new_physical_model_call(module, environment, monkeypatch):
    pages, snapshot = page(environment)
    client = FakeModel([{'facts': [fact(snapshot)]}, {'verdict': 'accept', 'reason': 'Exact source.'}])
    models = service(module, environment, pages, client)
    store, gen, budget, _, bundle = environment
    original = models._persist

    def crash_after_committed_result(callback):
        result = original(callback)
        if any(row['status'] == 'completed' for row in models.snapshot()['calls']):
            raise KeyboardInterrupt('simulated crash after model outcome before receipt')
        return result

    monkeypatch.setattr(models, '_persist', crash_after_committed_result)
    with pytest.raises(KeyboardInterrupt):
        models.extract_page(snapshot['snapshot_id'])
    assert len(client.requests) == 1
    store.release('run-a', gen)
    new_gen = store.claim('run-a', 'replacement', 100000)
    store.recover_operations('run-a', new_gen)
    new_budget = RunBudget(store, 'run-a', new_gen, monotonic=environment[3], epoch=environment[3])
    new_pages = PageService(store, 'run-a', new_gen, new_budget, session=PageSession('unused'),
                            resolver=lambda _: ['93.184.216.34'], monotonic=environment[3])
    restarted = module.ModelService(store, 'run-a', new_gen, new_budget, new_pages, bundle, client=client)
    result = restarted.extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'validated' and len(client.requests) == 2
    assert [r['execution_count'] for key, r in new_budget.snapshot()['logical_operations'].items()
            if key.startswith(('extract-', 'review-'))] == [1, 1]
    assert any(e['payload']['kind'] == 'model_receipt_reconciliation' for e in store.events('run-a'))


def test_tampered_bundle_and_baseline_fail_closed_before_model_call(module, environment):
    pages, snapshot = page(environment)
    client = FakeModel([])
    models = service(module, environment, pages, client)
    (environment[-1] / 'chip-specs/references/specs-rules.md').write_text('wrong bytes')
    with pytest.raises(ValueError, match='hash'):
        models.extract_page(snapshot['snapshot_id'])
    assert client.requests == []


def test_model_deadline_uses_same_active_budget_and_timeout_cap(module, environment):
    pages, snapshot = page(environment)
    budget, clock = environment[2:4]
    budget.begin_active()
    clock.now += 1795
    client = FakeModel([{'facts': []}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert result['complete'] and client.requests[0]['timeout'] == 5


def test_conflicting_fact_in_later_chunk_never_silently_replaces_evidence(module, environment):
    text = 'X80 64GB\n' + 'body text\n' * 900 + 'X80 80GB'
    pages, snapshot = page(environment, text)
    chunks = pages.chunks(snapshot['snapshot_id'], max_chars=8000)['chunks']
    first = fact(snapshot, evidence={**fact(snapshot)['evidence'], 'quote': 'X80 64GB', 'start': 0, 'end': 8})
    offset = snapshot['text'].index('X80 80GB')
    second = fact(snapshot, raw_value=80, normalized_value=80,
                  evidence={**fact(snapshot)['evidence'], 'quote': 'X80 80GB', 'start': offset, 'end': offset + 8})
    client = FakeModel([{'facts': [first]}, {'verdict': 'accept', 'reason': 'Exact capacity.'},
                        *[{'facts': []}] * (len(chunks) - 2), {'facts': [second]}])
    row = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'unresolved' and len(row['evidence_versions']) == 2


def test_expired_active_budget_leaves_explicit_outstanding_ranges(module, environment):
    pages, snapshot = page(environment)
    budget, clock = environment[2:4]
    budget.begin_active()
    clock.now += 1800
    client = FakeModel([])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert not result['complete'] and result['outstanding'][0]['status'] == 'budget_stopped'
    assert client.requests == [] and pages.chunks(snapshot['snapshot_id'], domain_id='specs')['coverage']['pending']


def test_latest_unknown_recovery_is_bounded_across_generations(module, environment):
    pages, snapshot = page(environment)
    client = FakeModel([KeyboardInterrupt('unknown provider effect')] * 3)
    models = service(module, environment, pages, client)
    store, generation, budget, clock, bundle = environment
    for index in range(3):
        with pytest.raises(KeyboardInterrupt):
            models.extract_page(snapshot['snapshot_id'])
        store.release('run-a', generation)
        generation = store.claim('run-a', 'replacement-' + str(index), 100000)
        store.recover_operations('run-a', generation)
        budget = RunBudget(store, 'run-a', generation, monotonic=clock, epoch=clock)
        pages = PageService(store, 'run-a', generation, budget, session=PageSession('unused'),
                            resolver=lambda _: ['93.184.216.34'], monotonic=clock)
        models = module.ModelService(store, 'run-a', generation, budget, pages, bundle, client=client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert not result['complete'] and len(client.requests) == 3
    assert result['outstanding'][0]['reason'] == 'technical_retry_budget_exhausted'


def test_storage_fence_failure_is_not_a_model_retry(module, environment, monkeypatch):
    pages, snapshot = page(environment)
    client = FakeModel([{'facts': []}])
    models = service(module, environment, pages, client)
    original = models._update

    def fail_finished_commit(callback):
        state = models.snapshot()
        changed, result, event = callback(copy.deepcopy(state))
        if event and event['kind'] == 'model_call_finished':
            raise ValueError('lease fence rejected')
        return original(callback)

    monkeypatch.setattr(models, '_update', fail_finished_commit)
    with pytest.raises(ValueError, match='fence'):
        models.extract_page(snapshot['snapshot_id'])
    assert len(client.requests) == 1
    assert environment[0].list_operations('run-a')[-1]['state'] == 'running'


@pytest.mark.parametrize('kind', ['response', 'http_error'])
def test_known_transport_credential_echo_is_masked_everywhere(module, environment, monkeypatch, kind):
    pages, snapshot = page(environment)
    private = 'sk-actual-private-credential'

    class Response:
        def raise_for_status(self):
            if kind == 'http_error':
                raise requests.HTTPError('Provider echoed ' + private)

        @property
        def text(self):
            return json.dumps(self.json())

        def json(self):
            return {'id': private, 'model': 'keep-model-' + private, 'usage': {'total_tokens': 3},
                    'choices': [{'message': {'content': '{"facts":[],"linked_domains":[]}'}}]}

        def close(self):
            pass

    monkeypatch.setattr('requests.post', lambda *a, **kw: Response())
    client = module.OpenAIModelTransport(base_url='https://model.example', api_key=private)
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    audit = json.dumps({'models': models.snapshot(), 'events': environment[0].events('run-a'),
                       'operations': environment[0].list_operations('run-a'), 'result': result})
    assert private not in audit
    if kind == 'response':
        assert 'keep-model-' in audit and 'total_tokens' in audit


def test_complete_headers_footnotes_and_multiple_skus_stay_separate(module, environment):
    html = ('<main><table><tr><th>SKU</th><th>Memory (GB)</th></tr>'
            '<tr><td>X80-A</td><td>64*</td></tr><tr><td>X80-B</td><td>80*</td></tr></table></main>'
            '<footer><p id="footnote">* Memory is per accelerator.</p></footer>')
    pages, snapshot = page(environment, html, mime='text/html')
    chunks = pages.chunks(snapshot['snapshot_id'])['chunks']
    context = [{**ref, 'snapshot_id': snapshot['snapshot_id'], 'body_hash': snapshot['body_hash'],
                'quote': snapshot['text'][ref['start']:ref['end']]} for ref in chunks[0]['context_refs']]
    facts = []
    for sku, value in [('X80-A', 64), ('X80-B', 80)]:
        quote = f'{sku} | {value}*'
        start = snapshot['text'].index(quote)
        facts.append(fact(snapshot, entity_key={'vendor': 'Vendor', 'chip_model': sku},
                          entity_hints={'full_sku': sku}, raw_value=value, normalized_value=value,
                          evidence={'snapshot_id': snapshot['snapshot_id'], 'body_hash': snapshot['body_hash'],
                                    'quote': quote, 'start': start, 'end': start + len(quote)}, context_refs=context))
    client = FakeModel([{'facts': facts}, {'verdict': 'accept', 'reason': 'Exact A row and GB header.'},
                        {'verdict': 'accept', 'reason': 'Exact B row and GB header.'}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert [row['versions'][0]['candidate']['normalized_value'] for row in result['candidates']] == [64, 80]
    assert len({row['logical_candidate_id'] for row in result['candidates']}) == 2
    for request in client.requests:
        payload = json.loads(request['messages'][1]['content'])
        assert any('Memory (GB)' in ref['quote'] for ref in payload['page']['context_refs'])
        assert any('per accelerator' in ref['quote'] for ref in payload['page']['context_refs'])


def test_invalid_review_json_never_downgrades_to_program_accept(module, environment):
    pages, snapshot = page(environment)
    client = FakeModel([{'facts': [fact(snapshot)]}, {'verdict': 'accept'}, {'verdict': 'accept'}, {'verdict': 'accept'}])
    models = service(module, environment, pages, client)
    row = models.extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'review_failed' and row['reason'] == 'parser_rejected'
    assert len(client.requests) == 4


def test_immutable_chunk_plan_blocks_rechunking_to_reset_model_operations(module, environment):
    pages, snapshot = page(environment, 'Long X80 text.\n' * 700)
    client = FakeModel([{'facts': []}] * len(pages.chunks(snapshot['snapshot_id'])['chunks']))
    models = service(module, environment, pages, client)
    models.extract_page(snapshot['snapshot_id'])
    count = len(client.requests)
    with pytest.raises(ValueError, match='chunk plan is immutable'):
        models.extract_page(snapshot['snapshot_id'], max_chars=7999)
    assert len(client.requests) == count


def test_provider_invalid_content_keeps_safe_raw_response_for_audit(module, environment, monkeypatch):
    pages, snapshot = page(environment)
    private = 'actual-api-secret'

    class Response:
        def raise_for_status(self):
            pass

        @property
        def text(self):
            return json.dumps(self.json())

        def json(self):
            return {'id': 'call-a', 'usage': {'total_tokens': 5},
                    'choices': [{'message': {'content': 'bad JSON echoed ' + private}}]}

        def close(self):
            pass

    monkeypatch.setattr('requests.post', lambda *a, **kw: Response())
    models = service(module, environment, pages, module.OpenAIModelTransport(base_url='https://model.example', api_key=private))
    result = models.extract_page(snapshot['snapshot_id'])
    calls = models.snapshot()['calls']
    assert calls[0]['response']['_raw_response']['choices'][0]['message']['content'] == 'bad JSON echoed [REDACTED]'
    assert not result['complete'] and len(calls) == 3


def test_model_coverage_is_authoritative_and_isolated_per_domain_scope(module, environment):
    pages, snapshot = page(environment, 'X80 64GB memory. FP16 peak 100 TFLOPS.')
    failed_client = FakeModel([{'facts': [fact(snapshot)]}, requests.Timeout(), requests.Timeout(), requests.Timeout()])
    specs = service(module, environment, pages, failed_client)
    specs_result = specs.extract_page(snapshot['snapshot_id'])
    compute_fact = fact(snapshot, domain_id='compute', field_name='precision_perf', raw_value=100,
                        normalized_value=100, unit='TFLOPS', conversion={'source_unit': 'TFLOPS', 'factor': 1,
                        'precision': 'FP16', 'dense_or_sparse': 'dense'}, evidence_kind='theoretical')
    store, gen, budget, clock, bundle = environment
    compute = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='compute', task_scope='linked',
                                  client=FakeModel([{'facts': [compute_fact]}, {'verdict': 'accept', 'reason': 'Explicit peak.'}]),
                                  monotonic=clock, backoff=lambda _: None)
    compute_result = compute.extract_page(snapshot['snapshot_id'])
    assert not specs_result['complete'] and compute_result['complete']
    assert specs_result['coverage'] == pages.chunks(snapshot['snapshot_id'], domain_id='specs')['coverage']
    assert compute_result['coverage'] == pages.chunks(snapshot['snapshot_id'], domain_id='compute', scope='linked')['coverage']
    assert specs_result['coverage']['failed'] and compute_result['coverage']['complete']
    assert not pages.chunks(snapshot['snapshot_id'], domain_id='compute')['coverage']['complete']
    assert not pages.chunks(snapshot['snapshot_id'])['coverage']['complete']
    with pytest.raises(ValueError, match='scope'):
        pages.chunks(snapshot['snapshot_id'], domain_id='compute', scope='unknown')
    with pytest.raises(ValueError, match='domain'):
        pages.mark_coverage('bad-mark', snapshot['snapshot_id'], 0, len(snapshot['text']), 'processed', domain_id='prices')


def test_successful_sibling_review_cannot_hide_failed_fact_in_same_chunk(module, environment):
    pages, snapshot = page(environment, 'X80 memory 64 GB, X80 single card TDP 300 W')
    vram = fact(snapshot)
    power = fact(snapshot, field_name='tdp_w', raw_value=300, normalized_value=300, unit='W',
                 conversion={'source_unit': 'W', 'factor': 1})
    client = FakeModel([{'facts': [vram, power]}, requests.Timeout(), requests.Timeout(), requests.Timeout(),
                        {'verdict': 'accept', 'reason': 'Explicit single card TDP.'}])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert [row['status'] for row in result['candidates']] == ['review_failed', 'validated']
    assert not result['complete'] and result['coverage']['failed']
    assert result['coverage'] == pages.chunks(snapshot['snapshot_id'], domain_id='specs')['coverage']


def test_pending_review_then_true_completion_clears_range_only_after_all_facts_finished(module, environment, monkeypatch):
    pages, snapshot = page(environment, 'X80 memory 64 GB, X80 single card TDP 300 W')
    vram = fact(snapshot)
    power = fact(snapshot, field_name='tdp_w', raw_value=300, normalized_value=300, unit='W',
                 conversion={'source_unit': 'W', 'factor': 1})
    client = FakeModel([{'facts': [vram, power]}, requests.Timeout(),
                        {'verdict': 'accept', 'reason': 'Explicit TDP.'},
                        {'verdict': 'accept', 'reason': 'Explicit capacity.'}])
    models = service(module, environment, pages, client)
    store = environment[0]
    original = store.operation

    def pending_backoff(run_id, generation, request_id, payload, execute, **kwargs):
        if request_id.startswith('model-backoff-'):
            return {'state': 'running', 'result': None}
        return original(run_id, generation, request_id, payload, execute, **kwargs)

    monkeypatch.setattr(store, 'operation', pending_backoff)
    first = models.extract_page(snapshot['snapshot_id'])
    assert [row['status'] for row in first['candidates']] == ['pending', 'validated']
    assert first['coverage']['pending'] and not first['complete']
    monkeypatch.setattr(store, 'operation', original)
    second = models.extract_page(snapshot['snapshot_id'])
    assert second['complete'] and all(row['status'] == 'validated' for row in second['candidates'])
    assert len(client.requests) == 4


def test_program_rejection_never_claims_a_semantic_review_stage(module, environment):
    pages, snapshot = page(environment)
    models = service(module, environment, pages, FakeModel([{'facts': [fact(snapshot, field_name='price_usd')]}]))
    models.extract_page(snapshot['snapshot_id'])
    stages = environment[2].stages()
    assert 'reviewing' not in stages or all(row['status'] == 'not_executed' for row in stages['reviewing'].values())


def test_no_existing_row_is_required_and_frozen_baseline_is_read_only(module, environment):
    pages, snapshot = page(environment, 'X80-B new SKU: 64GB')
    candidate = fact(snapshot, entity_key={'vendor': 'NewVendor', 'chip_model': 'X80-B'}, entity_hints={'full_sku': 'X80-B'})
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'accept', 'reason': 'Explicit new SKU evidence.'}])
    baseline = environment[0].run_path('run-a') / 'data.db'
    before = baseline.read_bytes()
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'validated'
    assert json.loads(client.requests[1]['messages'][1]['content'])['baseline']['chips'] == []
    assert baseline.read_bytes() == before and 'new_chip_count' not in result


def test_structured_compute_values_cannot_invent_or_splice_numeric_raw_evidence(module, environment):
    pages, snapshot = page(environment, 'X80 FP16 sparse peak 1 | 00 TFLOPS')
    candidate = fact(snapshot, domain_id='compute', field_name='precision_perf', raw_value={'FP16': 100},
                     normalized_value={'FP16': 100}, unit='TFLOPS',
                     conversion={'source_unit': 'TFLOPS', 'precision': 'FP16', 'dense_or_sparse': 'sparse'},
                     evidence_kind='theoretical')
    store, gen, budget, clock, bundle = environment
    client = FakeModel([{'facts': [candidate]}])
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='compute',
                                client=client, monotonic=clock, backoff=lambda _: None)
    result = models.extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'program_rejected' and len(client.requests) == 1


def test_malformed_candidate_does_not_erase_valid_siblings(module, environment):
    pages, snapshot = page(environment)
    broken = fact(snapshot, evidence=None)
    client = FakeModel([{'facts': [broken, fact(snapshot)]}, {'verdict': 'accept', 'reason': 'Valid sibling evidence.'}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert [row['status'] for row in result['candidates']] == ['program_rejected', 'validated']
    assert result['complete']


def test_full_long_snapshot_every_chunk_is_submitted_without_cut(module, environment):
    pages, snapshot = page(environment, 'X80 specification text.\n' * 3500 + 'LATE 64GB')
    chunks = pages.chunks(snapshot['snapshot_id'], max_chars=8000)['chunks']
    client = FakeModel([{'facts': []}] * len(chunks))
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    submitted = [json.loads(r['messages'][1]['content'])['page']['text'] for r in client.requests]
    assert ''.join(submitted) == snapshot['text'] and len(snapshot['text']) > 60000
    assert result['complete'] and result['candidates'] == []


@pytest.mark.parametrize('content', ['{"facts":[],"facts":[]}', '{"facts":NaN}', '[]'])
def test_strict_model_json_rejects_duplicates_nonfinite_and_nonobject(module, content):
    with pytest.raises(ValueError):
        module.decode_content(content)


@pytest.mark.parametrize('temperature', ['0.6', '1.7'])
def test_transport_reuses_configuration_thinking_timeout_and_closes_response(module, monkeypatch, temperature):
    monkeypatch.setenv('DATA_AGENT_LLM_TEMPERATURE', temperature)
    seen, closed = [], []

    class Response:
        def raise_for_status(self):
            pass

        @property
        def text(self):
            return json.dumps(self.json())

        def json(self):
            return {'id': 'provider-call', 'model': 'provider-model', 'usage': {'total_tokens': 9},
                    'choices': [{'message': {'content': '{"facts":[]}'}}]}

        def close(self):
            closed.append(True)

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return Response()

    monkeypatch.setattr('requests.post', post)
    client = module.OpenAIModelTransport(base_url='https://model.example', api_key='private-secret', model='configured-model')
    result = client.call_model(messages=[{'role': 'system', 'content': 'full trusted rules'}], timeout=7)
    assert seen[0][1]['json']['temperature'] == float(temperature)
    assert seen[0][1]['json']['thinking'] == {'type': 'disabled'}
    assert seen[0][1]['timeout'] == 7 and closed == [True]
    assert result['_usage']['total_tokens'] == 9 and result['_call_id'] == 'provider-call'


def test_incidental_entity_hints_cannot_split_same_fact_or_reset_revision(module, environment):
    pages, snapshot = page(environment, 'X80 64GB or 80GB')
    first = fact(snapshot, entity_hints={'full_sku': 'X80', 'note': 'nonce-1'})
    second = fact(snapshot, raw_value=80, normalized_value=80, entity_hints={'full_sku': 'X80', 'note': 'nonce-2'})
    assert module.logical_candidate_id(first) == module.logical_candidate_id(second)
    different_sku = fact(snapshot, entity_key={'vendor': 'Vendor', 'chip_model': 'X80-B'})
    assert module.logical_candidate_id(first) != module.logical_candidate_id(different_sku)
    client = FakeModel([{'facts': [first, second]}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert len(result['candidates']) == 1 and result['candidates'][0]['status'] == 'unresolved'
    assert len(result['candidates'][0]['evidence_versions']) == 2 and len(client.requests) == 1


@pytest.mark.parametrize('domain,table,field,keys', [
    ('compatibility', 'chip_model_compatibility', 'framework', {'chip_model': 'X80'}),
    ('deployment', 'deployment_guides', 'backend', {}),
])
def test_missing_relation_or_guide_identity_never_validated(module, environment, domain, table, field, keys):
    pages, snapshot = page(environment, 'X80 PyTorch deployment guide')
    candidate = fact(snapshot, domain_id=domain, target_table=table, field_name=field, entity_key=keys,
                     raw_value='PyTorch', normalized_value='PyTorch', unit=None, conversion={}, scope='relation')
    store, gen, budget, clock, bundle = environment
    client = FakeModel([{'facts': [candidate]}])
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id=domain, client=client,
                                monotonic=clock, backoff=lambda _: None)
    row = models.extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] == 'unresolved' and row['reason'] == 'incomplete_evidence'
    assert row['missing_conditions'] and len(client.requests) == 1


def test_explicit_generic_deployment_allows_model_not_applicable(module, environment):
    pages, snapshot = page(environment, 'Generic X80 PyTorch runtime setup, model independent')
    candidate = fact(snapshot, domain_id='deployment', target_table='deployment_guides', field_name='backend',
                     entity_key={'url': snapshot['final_url'], 'title': 'Generic X80 runtime setup', 'chip_model': 'X80',
                                 'model_id': 'not_applicable', 'source_type': 'official_doc'},
                     raw_value='PyTorch', normalized_value='PyTorch', unit=None, conversion={}, scope='relation',
                     conditions={'model_applicability': 'not_applicable'})
    store, gen, budget, clock, bundle = environment
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'accept', 'reason': 'Explicit model independent guide.'}])
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='deployment', client=client,
                                monotonic=clock, backoff=lambda _: None)
    assert models.extract_page(snapshot['snapshot_id'])['candidates'][0]['status'] == 'validated'


def benchmark_fact(snapshot, **changes):
    value = fact(snapshot, domain_id='benchmark', target_table='chip_model_benchmarks', field_name='throughput_tok_s',
                 entity_key={'chip_model': 'X80', 'model_id': 'Qwen/Qwen3-8B', 'suite_name': 'Report A',
                             'workload_type': 'inference', 'chip_count': 1, 'precision': 'FP16'},
                 raw_value=100, normalized_value=100, unit='tokens/s', conversion={'source_unit': 'tokens/s'},
                 conditions={'metric_class': 'generative_throughput', 'input_seq_length': 128,
                             'output_seq_length': 128, 'batch_size': 1}, scope='relation', evidence_kind='measured')
    value.update(changes)
    return value


@pytest.mark.parametrize('name,bad', [('chip_count', 0), ('chip_count', 1.5), ('chip_count', False),
                                     ('input_seq_length', False), ('output_seq_length', {}), ('batch_size', True),
                                     ('input_seq_length', -1), ('concurrency', '1.5')])
def test_benchmark_required_counts_are_typed_integral_and_positive(module, environment, name, bad):
    pages, snapshot = page(environment, 'X80 Qwen/Qwen3-8B Report A 1 card FP16, input 128 output 128 batch 1, 100 tokens/s')
    candidate = benchmark_fact(snapshot)
    namespace = candidate['entity_key'] if name == 'chip_count' else candidate['conditions']
    namespace[name] = bad
    client = FakeModel([{'facts': [candidate]}])
    store, gen, budget, clock, bundle = environment
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='benchmark', client=client,
                                monotonic=clock, backoff=lambda _: None)
    row = models.extract_page(snapshot['snapshot_id'])['candidates'][0]
    assert row['status'] in {'unresolved', 'program_rejected'} and len(client.requests) == 1


@pytest.mark.parametrize('workload,conditions', [
    ('inference', {'metric_class': 'nongenerative', 'input_seq_length': 'not_applicable', 'output_seq_length': 'unknown'}),
    ('training', {'training_workload': 'pretrain', 'training_data': '1 billion tokens', 'hardware_configuration': '1 card X80'}),
])
def test_benchmark_non_generation_and_training_do_not_require_generation_lengths(module, environment, workload, conditions):
    pages, snapshot = page(environment, 'X80 Qwen/Qwen3-8B Report A 1 card FP16 100 tokens/s; pretrain 1 billion tokens')
    candidate = benchmark_fact(snapshot, conditions=conditions)
    candidate['entity_key']['workload_type'] = workload
    result = module.validate_candidate(candidate, load_phase(environment[-1], 'benchmark', 'extract')['contract'],
                                       snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0])
    assert result['status'] == 'program_valid'


@pytest.mark.parametrize('body', [
    '{"id":"one","id":"two","choices":[{"message":{"content":"{\\"facts\\":[]}"}}]}',
    '{"model":42,"choices":[{"message":{"content":"{\\"facts\\":[]}"}}]}',
    '{"usage":"a string","choices":[{"message":{"content":"{\\"facts\\":[]}"}}]}',
    '{"usage":{"total_tokens":NaN},"choices":[{"message":{"content":"{\\"facts\\":[]}"}}]}',
])
def test_http_envelope_is_strict_json_and_typed_metadata(module, monkeypatch, body):
    closed = []

    class Response:
        text = body

        def raise_for_status(self):
            pass

        def json(self):
            return json.loads(self.text)

        def close(self):
            closed.append(True)

    monkeypatch.setattr('requests.post', lambda *args, **kwargs: Response())
    client = module.OpenAIModelTransport(base_url='https://model.example', api_key='private')
    with pytest.raises(ValueError):
        client.call_model(messages=[], timeout=1)
    assert closed == [True]


def test_failed_raw_envelope_text_is_saved_without_known_secret(module, environment, monkeypatch):
    pages, snapshot = page(environment)
    private = 'secret-raw-credential'
    body = '{"id":"' + private + '","id":"two","choices":[]}'

    class Response:
        text = body

        def raise_for_status(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr('requests.post', lambda *args, **kwargs: Response())
    models = service(module, environment, pages, module.OpenAIModelTransport(base_url='https://model.example', api_key=private))
    result = models.extract_page(snapshot['snapshot_id'])
    assert not result['complete'] and result['chunks'][0]['reason'] == 'parser_rejected'
    assert models.snapshot()['calls'][0]['response']['_raw_envelope_text'] == body.replace(private, '[REDACTED]')
    assert private not in json.dumps(models.snapshot())


def test_benchmark_counts_outside_submitted_source_are_incomplete(module, environment):
    pages, snapshot = page(environment, 'X80 Qwen/Qwen3-8B measured 100 tokens/s')
    candidate = benchmark_fact(snapshot)
    program = module.validate_candidate(candidate, load_phase(environment[-1], 'benchmark', 'extract')['contract'],
                                        snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0])
    assert program['status'] == 'unresolved' and 'chip_count' in program['missing_conditions']


@pytest.mark.parametrize('domain,table,field,raw,unit,scope', [
    ('benchmark', 'chip_model_benchmarks', 'throughput_tok_s', 100, 'tokens/s', 'configuration'),
    ('deployment', 'deployment_guides', 'backend', 'PyTorch', None, 'configuration'),
])
def test_relation_configuration_scope_preserves_chip_identity(module, environment, domain, table, field, raw, unit, scope):
    pages, snapshot = page(environment, 'X80 Qwen/Qwen3-8B Report A 2 cards FP16 input 128 output 128 batch 1, 100 tokens/s PyTorch')
    if domain == 'benchmark':
        candidate = benchmark_fact(snapshot, scope=scope)
        candidate['entity_key']['chip_count'] = 2
    else:
        candidate = fact(snapshot, domain_id=domain, target_table=table, field_name=field, raw_value=raw, normalized_value=raw,
                         unit=unit, conversion={}, scope=scope,
                         entity_key={'url': snapshot['final_url'], 'title': 'X80 runtime guide', 'chip_model': 'X80',
                                     'model_id': 'Qwen/Qwen3-8B', 'source_type': 'official_doc'})
    program = module.validate_candidate(candidate, load_phase(environment[-1], domain, 'extract')['contract'],
                                        snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0])
    assert program['status'] == 'program_valid'
    assert candidate['entity_key']['chip_model'] == 'X80'


def test_normalized_identity_dimensions_share_ids_but_preserve_real_variants(module, environment):
    pages, snapshot = page(environment)
    first = benchmark_fact(snapshot, entity_hints={'capacity': '64 GB', 'form_factor': 'PCIe', 'note': 'A'})
    second = copy.deepcopy(first)
    second['entity_hints'].update(capacity='64.0GB', form_factor=' pcie ', note='B')
    second['entity_key']['chip_count'] = '1'
    assert module.logical_candidate_id(first) == module.logical_candidate_id(second)
    second['entity_hints']['capacity'] = '80GB'
    assert module.logical_candidate_id(first) != module.logical_candidate_id(second)


def test_generic_guide_requires_explicit_applicability_declaration(module, environment):
    pages, snapshot = page(environment, 'PyTorch runtime guide')
    candidate = fact(snapshot, domain_id='deployment', target_table='deployment_guides', field_name='backend',
                     entity_key={'url': snapshot['final_url'], 'title': 'Runtime setup', 'chip_model': 'generic',
                                 'model_id': 'generic', 'source_type': 'official_doc'},
                     raw_value='PyTorch', normalized_value='PyTorch', unit=None, conversion={}, scope='configuration')
    program = module.validate_candidate(candidate, load_phase(environment[-1], 'deployment', 'extract')['contract'],
                                        snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0])
    assert program['status'] == 'unresolved'
    assert {'chip_model', 'model_id'}.issubset(program['missing_conditions'])


@pytest.mark.parametrize('first_url,second_url', [
    ('https://vendor.example/guides/A', 'https://vendor.example/guides/a'),
    ('https://vendor.example/guide?variant=A', 'https://vendor.example/guide?variant=a'),
])
def test_distinct_case_sensitive_guide_urls_remain_independently_reviewed(module, environment, first_url, second_url):
    pages, snapshot = page(environment, f'X80 PyTorch guides: {first_url} and {second_url}')
    keys = {'url': first_url, 'title': 'X80 runtime guide', 'chip_model': 'X80',
            'model_id': 'Qwen/Qwen3-8B', 'source_type': 'official_doc'}
    first = fact(snapshot, domain_id='deployment', target_table='deployment_guides', field_name='backend',
                 entity_key=keys, raw_value='PyTorch', normalized_value='PyTorch', unit=None, conversion={}, scope='relation')
    second = copy.deepcopy(first)
    second['entity_key']['url'] = second_url
    assert module.logical_candidate_id(first) != module.logical_candidate_id(second)
    store, gen, budget, clock, bundle = environment
    client = FakeModel([{'facts': [first, second]}, {'verdict': 'accept', 'reason': 'First explicit guide.'},
                        {'verdict': 'accept', 'reason': 'Second explicit guide.'}])
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='deployment', client=client,
                                monotonic=clock, backoff=lambda _: None)
    result = models.extract_page(snapshot['snapshot_id'])
    assert len(result['candidates']) == 2 and all(row['status'] == 'validated' for row in result['candidates'])
    assert len(client.requests) == 3 and result['complete']


def test_guide_url_host_case_is_equivalent_but_model_ids_are_opaque(module, environment):
    pages, snapshot = page(environment, 'X80 PyTorch guide https://VENDOR.EXAMPLE/guides/A?variant=B for Qwen/ModelA')
    first = fact(snapshot, domain_id='deployment', target_table='deployment_guides', field_name='backend',
                 raw_value='PyTorch', normalized_value='PyTorch', unit=None, conversion={}, scope='relation',
                 entity_key={'url': 'https://VENDOR.EXAMPLE/guides/A?variant=B', 'title': 'Runtime guide',
                             'source_type': 'official_doc', 'chip_model': 'X80', 'model_id': 'Qwen/ModelA'})
    second = copy.deepcopy(first)
    second['entity_key']['url'] = 'https://vendor.example/guides/A?variant=B'
    assert module.logical_candidate_id(first) == module.logical_candidate_id(second)
    store, gen, budget, clock, bundle = environment
    client = FakeModel([{'facts': [first]}, {'verdict': 'accept', 'reason': 'Explicit guide.'}])
    models = module.ModelService(store, 'run-a', gen, budget, pages, bundle, domain_id='deployment', client=client,
                                monotonic=clock, backoff=lambda _: None)
    result = models.extract_page(snapshot['snapshot_id'])
    cached = models.review_candidate(second)
    assert cached == result['candidates'][0] and len(client.requests) == 2
    second['entity_key']['model_id'] = 'Qwen/modela'
    assert module.logical_candidate_id(first) != module.logical_candidate_id(second)


@pytest.mark.parametrize('entrypoint', ['extract', 'review'])
def test_cached_verdict_rejects_changed_identity_context_without_new_calls(module, environment, entrypoint):
    pages, snapshot = page(environment)
    candidate = fact(snapshot)
    original_context = {'X80': {'status': 'existing', 'row_id': '1'}}
    changed_context = {'X80': {'status': 'ambiguous', 'row_id': '2'}}
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'accept', 'reason': 'Supports frozen row one.'}])
    models = service(module, environment, pages, client)
    models.extract_page(snapshot['snapshot_id'], identity_context=original_context)
    with pytest.raises(ValueError, match='model input conflict'):
        if entrypoint == 'extract':
            models.extract_page(snapshot['snapshot_id'], identity_context=changed_context)
        else:
            models.review_candidate(candidate, identity_context=changed_context)
    assert len(client.requests) == 2
    assert models.snapshot()['input_conflicts']


def test_cached_review_rejects_unapproved_values_but_authorized_revision_replays(module, environment):
    pages, snapshot = page(environment, 'X80 64GB or 80GB')
    original = fact(snapshot)
    revised = fact(snapshot, raw_value=80, normalized_value=80)
    client = FakeModel([{'facts': [original]}, {'verdict': 'revise', 'reason': 'Use alternate capacity.', 'candidate': revised},
                        {'verdict': 'accept', 'reason': 'Exact alternate capacity.'}])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert models.review_candidate(original, request_id='new-nonce', candidate_version=999) == result['candidates'][0]
    assert models.review_candidate(revised) == result['candidates'][0]
    changed = fact(snapshot, raw_value=999, normalized_value=999)
    with pytest.raises(ValueError, match='model input conflict'):
        models.review_candidate(changed)
    assert len(client.requests) == 3
    assert list(environment[2].snapshot()['candidate_revisions'].values()) == [1]


def test_cached_snapshot_never_skips_frozen_baseline_verification(module, environment):
    pages, snapshot = page(environment)
    client = FakeModel([{'facts': []}])
    models = service(module, environment, pages, client)
    models.extract_page(snapshot['snapshot_id'])
    baseline = environment[0].run_path('run-a') / 'data.db'
    baseline.chmod(0o600)  # controlled run-local fixture, never formal data.db
    baseline.write_bytes(baseline.read_bytes() + b'tampered')
    with pytest.raises(ValueError, match='baseline hash mismatch'):
        models.extract_page(snapshot['snapshot_id'])
    assert len(client.requests) == 1


@pytest.mark.parametrize('fail', [False, True])
def test_baseline_sqlite_connections_close_on_success_and_error(module, environment, monkeypatch, fail):
    pages, snapshot = page(environment)
    models = service(module, environment, pages, FakeModel([]))
    baseline_uri = (environment[0].run_path('run-a') / 'data.db').as_uri() + '?mode=ro'
    opened = []
    original_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'PRAGMA query_only=ON':
                raise sqlite3.OperationalError('controlled read failure')
            return super().execute(sql, *args, **kwargs)

    def connect(path, *args, **kwargs):
        if path == baseline_uri:
            if fail:
                kwargs['factory'] = FailingConnection
            connection = original_connect(path, *args, **kwargs)
            opened.append(connection)
            return connection
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', connect)
    if fail:
        with pytest.raises(sqlite3.OperationalError, match='controlled read failure'):
            models._baseline(fact(snapshot))
    else:
        assert models._baseline(fact(snapshot))['chips'][0]['vram_gb'] == '32'
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


@pytest.mark.parametrize('first_review', ['accept', 'failed'])
def test_equal_fact_across_chunks_merges_unreviewed_support_without_changing_proof(module, environment, first_review):
    pages, snapshot = page(environment, 'X80 64GB\n' + 'body text\n' * 900 + 'X80 64GB')
    chunks = pages.chunks(snapshot['snapshot_id'])['chunks']
    first = fact(snapshot, evidence={**fact(snapshot)['evidence'], 'quote': 'X80 64GB', 'start': 0, 'end': 8})
    offset = snapshot['text'].rindex('X80 64GB')
    second = fact(snapshot, evidence={**fact(snapshot)['evidence'], 'quote': 'X80 64GB', 'start': offset, 'end': offset + 8})
    responses = [{'facts': [first]}]
    responses += [{'verdict': 'accept', 'reason': 'Exact first capacity.'}] if first_review == 'accept' else [requests.Timeout()] * 3
    responses += [{'facts': []}] * (len(chunks) - 2) + [{'facts': [second]}]
    client = FakeModel(responses)
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    row = result['candidates'][0]
    assert len(result['chunks']) == len(chunks) and len(row['evidence_versions']) == 2
    assert row['versions'][0]['candidate']['evidence'] == first['evidence']
    assert row['source']['start'] == 0 and row['review_chunk'] == chunks[0]
    assert row['evidence_outcomes'][-1]['semantic_review'] == 'not_executed'
    assert result['chunks'][-1]['review_status'] == 'not_executed'
    assert result['chunks'][-1]['review_reused'] is True
    if first_review == 'accept':
        assert row['status'] == 'validated' and result['complete']
    else:
        assert row['status'] == 'review_failed' and not result['complete'] and result['coverage']['failed']
    count = len(client.requests)
    assert models.extract_page(snapshot['snapshot_id']) == result
    assert len(client.requests) == count


def test_equal_duplicate_with_changed_semantic_conditions_is_unresolved_without_aborting(module, environment):
    pages, snapshot = page(environment, 'X80 64GB\n' + 'body text\n' * 900 + 'X80 64GB')
    first = fact(snapshot, evidence={**fact(snapshot)['evidence'], 'quote': 'X80 64GB', 'start': 0, 'end': 8})
    offset = snapshot['text'].rindex('X80 64GB')
    second = fact(snapshot, conditions={'capacity_scope': 'alternate configuration'},
                  evidence={**fact(snapshot)['evidence'], 'quote': 'X80 64GB', 'start': offset, 'end': offset + 8})
    client = FakeModel([{'facts': [first]}, {'verdict': 'accept', 'reason': 'Exact first capacity.'}, {'facts': [second]}])
    result = service(module, environment, pages, client).extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'unresolved'
    assert result['candidates'][0]['reason'] == 'duplicate_semantic_inputs_changed'
    assert len(result['chunks']) == 2 and len(client.requests) == 3

@pytest.mark.parametrize('base_value,configured,expected', [(None,None,0.6),(None,'1.7',1.7),(0.4,'1.7',0.4)])
def test_transport_compatibility_with_legacy_base_temperature(module,monkeypatch,base_value,configured,expected):
    # Emulate the tracked legacy base, which has no temperature attribute.
    def legacy_init(self,**kwargs):
        self.base_url='https://fixture.invalid'
        self.api_key='fixture-secret'
        self.model_name='fixture-model'
        self.timeout=60
        if base_value is not None:
            self.temperature=base_value
    monkeypatch.setattr(module.OpenAICompatibleExtractor,'__init__',legacy_init)
    if configured is None:
        monkeypatch.delenv('DATA_AGENT_LLM_TEMPERATURE',raising=False)
    else:
        monkeypatch.setenv('DATA_AGENT_LLM_TEMPERATURE',configured)
    seen=[]
    class Response:
        text='{"choices":[{"message":{"content":"{\\"facts\\":[]}"}}]}'
        def raise_for_status(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr('requests.post',lambda url,**kwargs:(seen.append(kwargs) or Response()))
    client=module.OpenAIModelTransport()
    client.call_model(messages=[],timeout=7)
    assert seen[0]['json']['temperature']==expected
    assert seen[0]['json']['thinking']=={'type':'disabled'}
    assert seen[0]['timeout']==7


@pytest.mark.parametrize('configured',['NaN','inf','-1','2.1','not-a-number'])
def test_legacy_transport_rejects_invalid_temperature_before_request(module,monkeypatch,configured):
    def legacy_init(self,**kwargs):
        self.base_url='https://fixture.invalid'
        self.api_key='fixture-secret'
        self.model_name='fixture-model'
        self.timeout=60
    monkeypatch.setattr(module.OpenAICompatibleExtractor,'__init__',legacy_init)
    monkeypatch.setenv('DATA_AGENT_LLM_TEMPERATURE',configured)
    monkeypatch.setattr('requests.post',lambda *args,**kwargs:pytest.fail('invalid configuration must not send request'))
    with pytest.raises(ValueError,match='TEMPERATURE'):
        module.OpenAIModelTransport().call_model(messages=[],timeout=7)


def test_extract_prompt_scopes_backend_and_explains_candidate_contract(module, environment):
    frozen = load_phase(environment[-1], 'specs', 'extract')
    messages = module.phase_messages('extract', frozen['skill_text'], frozen['references'], frozen['contract'], {})
    instruction = messages[0]['content']
    assert 'BACKEND PHASE TASK: extract' in instruction
    assert 'empty baseline' in instruction
    assert 'entity_hints' in instruction and 'Omit unknown optional hints' in instruction
    assert 'numeric raw_value' in instruction
    assert 'continuous substring' in instruction
    for reference in frozen['references'].values():
        assert reference in instruction


@pytest.mark.parametrize('text,quote,start,end,expected', [
    ('Vendor X80 64GB', '64GB', 1, 5, (11, 15)),
    ('64GB / 64GB', '64GB', 7, 11, (7, 11)),
    ('64GB / 64GB', '64GB', 1, 5, (1, 5)),
    ('64 GB', '64GB', 0, 4, (0, 4)),
])
def test_quote_location_only_repairs_unique_verbatim_evidence(module, text, quote, start, end, expected):
    snapshot = {'snapshot_id': 's', 'body_hash': 'h', 'text': text}
    candidate = {'evidence': {'snapshot_id': 's', 'body_hash': 'h', 'quote': quote, 'start': start, 'end': end}}
    located, audit = module.locate_candidate_quotes(candidate, snapshot, {'start': 0, 'end': len(text), 'context_refs': []})
    assert (located['evidence']['start'], located['evidence']['end']) == expected
    assert candidate['evidence']['start'] == start
    assert bool(audit) == (expected != (start, end))


def test_quote_location_rejects_wrong_snapshot_and_outside_chunk(module):
    snap = {'snapshot_id': 's', 'body_hash': 'h', 'text': '64GB outside'}
    for sid, chunk in [('other', {'start': 0, 'end': 12, 'context_refs': []}),
                       ('s', {'start': 5, 'end': 12, 'context_refs': []})]:
        candidate = {'evidence': {'snapshot_id': sid, 'body_hash': 'h', 'quote': '64GB', 'start': 1, 'end': 5}}
        located, audit = module.locate_candidate_quotes(candidate, snap, chunk)
        assert located == candidate and not audit


def test_extract_locates_quote_before_validation_and_retains_raw_audit(module, environment):
    pages, snapshot = page(environment)
    candidate = fact(snapshot)
    candidate['evidence'].update(start=1, end=2)
    client = FakeModel([{'facts': [candidate]}, {'verdict': 'accept', 'reason': 'Exact source supports this SKU capacity.'}])
    models = service(module, environment, pages, client)
    result = models.extract_page(snapshot['snapshot_id'])
    assert result['candidates'][0]['status'] == 'validated'
    assert models.snapshot()['raw_facts'][0]['facts'][0]['evidence']['start'] == 1
    assert models.snapshot()['quote_locations'][0]['changes'][0]['method'] == 'unique_exact_substring'


def test_quote_location_requires_integer_offsets_and_keeps_validation_strict(module):
    snap = {'snapshot_id': 's', 'body_hash': 'h', 'text': 'prefix GPU 64GB'}
    chunk = {'start': 0, 'end': len(snap['text']), 'context_refs': []}
    candidate = {'evidence': {'snapshot_id': 's', 'body_hash': 'h', 'quote': '64GB', 'start': True, 'end': 4}}
    located, changes = module.locate_candidate_quotes(candidate, snap, chunk)
    assert located == candidate and not changes
    with pytest.raises(ValueError):
        module.validate_quote(snap['text'], located['evidence'])


def test_quote_location_unicode_nonzero_chunk_and_context_deduplication(module):
    snap = {'snapshot_id': 's', 'body_hash': 'h', 'text': '标题\n芯片 X80：64GB'}
    chunk = {'start': 3, 'end': len(snap['text']), 'context_refs': [{'start': 3, 'end': len(snap['text'])}]}
    candidate = {'evidence': {'snapshot_id': 's', 'body_hash': 'h', 'quote': '64GB', 'start': 0, 'end': 4}}
    located, changes = module.locate_candidate_quotes(candidate, snap, chunk)
    assert located['evidence']['start'] == snap['text'].index('64GB')
    assert len(changes) == 1
    assert module.validate_quote(snap['text'], located['evidence'])


def test_extract_payload_binds_backend_protocol_version(module, environment):
    pages, snapshot = page(environment)
    models = service(module, environment, pages, FakeModel([]))
    chunk = pages.chunks(snapshot['snapshot_id'])['chunks'][0]
    payload = models._payload(models._phase('extract'), snapshot, chunk, {})
    assert payload['backend_protocol_version'] == 8


def test_extract_required_schema_describes_facts_not_unconstrained_json(module, environment):
    pages, snapshot = page(environment)
    models = service(module, environment, pages, FakeModel([]))
    payload = models._payload(models._phase('extract'), snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0], {})
    item = payload['required_schema']['properties']['facts']['items']
    assert item['additionalProperties'] is False
    assert 'entity_key' in item['properties'] and 'evidence' in item['required']
    assert 'confidence_reason_detail' not in item['properties']
    messages = module.phase_messages('extract', '', {}, {}, payload)
    assert 'conversion.description' in messages[0]['content']
    assert 'no extra properties' in messages[0]['content']


@pytest.mark.parametrize('raw,normalized,valid', [
    (2, 2, True), (2.5, 2.5, True), ('2nm', 2, False),
    ('2', 2, False), (2, '2', False), (True, 2, False),
])
def test_extract_schema_enforces_frozen_numeric_field_types(module, environment, raw, normalized, valid):
    import jsonschema
    pages, snapshot = page(environment, 'X80 uses 2nm')
    models = service(module, environment, pages, FakeModel([]))
    frozen = models._phase('extract')
    before = copy.deepcopy(frozen)
    payload = models._payload(frozen, snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0], {})
    candidate = fact(snapshot, field_name='process_node_nm', raw_value=raw, normalized_value=normalized,
                     unit='nm', conversion={'source_unit': 'nm', 'factor': 1})
    response = {'facts': [candidate], 'linked_domains': []}
    if valid:
        jsonschema.validate(response, payload['required_schema'])
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(response, payload['required_schema'])
    assert frozen == before
    # Wire models and independent reviewer stay compatible with archived proofs.
    assert payload['candidate_schema'] == module.FactCandidate.model_json_schema()
    review = models._payload(models._phase('review'), snapshot,
                             pages.chunks(snapshot['snapshot_id'])['chunks'][0], {}, candidate)
    assert review['required_schema'] == module.ReviewResponse.model_json_schema()


def test_extract_schema_keeps_prose_and_source_format_fields(module, environment):
    import jsonschema
    pages, snapshot = page(environment, 'X80 uses HBM3')
    models = service(module, environment, pages, FakeModel([]))
    frozen = models._phase('extract')
    # No numeric assumption for source-native formats or unowned unit metadata.
    frozen['contract']['units'].update(vram_type='explicit_source_format', not_owned='W')
    payload = models._payload(frozen, snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0], {})
    candidate = fact(snapshot, field_name='vram_type', raw_value='HBM3', normalized_value='HBM3',
                     unit=None, conversion={})
    jsonschema.validate({'facts': [candidate], 'linked_domains': []}, payload['required_schema'])
    assert 'not_owned' not in json.dumps(payload['required_schema'])


def test_extract_prompt_requires_separate_concrete_variants_and_complete_claim(module):
    legacy = module.phase_messages('extract', '', {}, {}, {'backend_protocol_version': 6})[0]['content']
    current = module.phase_messages('extract', '', {}, {}, {'backend_protocol_version': 7})[0]['content']
    assert 'EXTRACTION FIELD TYPES AND SUBJECTS' not in legacy
    assert 'EXTRACTION FIELD TYPES AND SUBJECTS' in current
    assert 'separate fact for each concrete named SKU' in current
    assert 'include the named subjects and shared predicate' in current


@pytest.mark.parametrize('phase,expected', [
    ('extract', 'b7e6ecab1017fbaaddffafc3d3eed8930fa1fbd22387ca99be11e74ff26a45e0'),
    ('review', '64826633da4ee95d690dfcb5086f288e6ae0377c24b75ef5a88098d0a4340766'),
])
def test_protocol_six_archived_prompt_bytes_remain_unchanged(module, phase, expected):
    import hashlib
    system = module.phase_messages(phase, 'frozen skill', {'rules': 'frozen ref'},
        {'domain_id': 'specs', 'fields': {'chips': ['tdp_w']}},
        {'backend_protocol_version': 6, 'candidate': {'target_table': 'chips', 'field_name': 'tdp_w'}})[0]['content']
    assert hashlib.sha256(system.encode()).hexdigest() == expected


def test_review_json_schema_expresses_revision_only_candidate(module):
    import jsonschema
    schema = module.ReviewResponse.model_json_schema()
    jsonschema.validate({'verdict': 'accept', 'reason': 'Supported', 'candidate': None}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'verdict': 'accept', 'reason': 'Supported', 'candidate': {}}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'verdict': 'revise', 'reason': 'Correct source', 'candidate': None}, schema)


def test_source_schema_distinguishes_first_party_release_from_media(module):
    description = module.FactCandidate.model_json_schema()['properties']['source_type'].get('description', '')
    assert 'first-party' in description and 'third-party' in description
    for phase in ['extract', 'review']:
        prompt = module.phase_messages(phase, '', {}, {}, {})[0]['content']
        assert 'third-party media' in prompt and 'actual page publisher' in prompt


def test_compute_skill_metadata_names_resolve_to_candidate_schema(module):
    contract = json.loads((ROOT / '.hermes-skills/chip-compute/domain.json').read_text(encoding='utf-8'))
    paths = contract['conditions']['business_format']['metadata']
    assert set(paths) == {'conversion.precision', 'conversion.source_unit', 'raw_value',
                          'conversion.dense_or_sparse', 'conversion.calculation_basis'}
    for path in paths:
        parts = path.split('.')
        assert parts[0] in module.FactCandidate.model_fields
        if len(parts) == 2:
            assert parts[0] == 'conversion' and parts[1] in module.Conversion.model_fields


@pytest.mark.parametrize('domain', ['identity', 'specs', 'compute', 'compatibility', 'benchmark', 'deployment'])
def test_frozen_skill_schema_examples_reach_extract_and_review(module, environment, domain):
    import re
    frozen = load_phase(environment[-1], domain, 'extract')
    shared = frozen['references']['_shared/rules.md']
    marker = '## 候选 JSON 输出与 Schema 对齐'
    assert marker in shared
    section = shared.split(marker, 1)[1].split('\n## ', 1)[0]
    examples = re.findall(r'```json\s*\n(.*?)\n```', section, re.DOTALL)
    assert len(examples) == 1
    examples = json.loads(examples[0])
    assert len(examples) == 4
    for example in examples:
        candidate = fact({'text': 'Fictional example', 'snapshot_id': 'example', 'body_hash': 'example'})
        candidate.update(example)
        module.FactCandidate.model_validate(candidate)
    assert any(e['conversion'] == {} for e in examples)
    assert any(e['raw_value'] == 2 and e['unit'] == 'nm' for e in examples)
    assert any(e['conversion'].get('precision') == 'INT8' for e in examples)
    for phase in ['extract', 'review']:
        prompt = module.phase_messages(phase, frozen['skill_text'], frozen['references'], frozen['contract'], {})[0]['content']
        assert section in prompt


@pytest.mark.parametrize('conversion', [None, {'source_precision': 'INT8'}, {'source_value': 2}])
def test_schema_alignment_does_not_accept_invalid_conversion(module, conversion):
    candidate = fact({'text': 'Example', 'snapshot_id': 's', 'body_hash': 'h'}, conversion=conversion)
    with pytest.raises(ValueError):
        module.FactCandidate.model_validate(candidate)


def test_schema_alignment_does_not_coerce_unit_bearing_numeric_raw(module, environment):
    pages, snapshot = page(environment, text='X80 uses a 2nm process')
    candidate = fact(snapshot, field_name='process_node_nm', raw_value='2nm', normalized_value=2,
                     unit='nm', conversion={'source_unit': 'nm', 'factor': 1})
    with pytest.raises(ValueError):
        module.validate_candidate(candidate, load_phase(environment[-1], 'specs', 'extract')['contract'],
                                  snapshot, pages.chunks(snapshot['snapshot_id'])['chunks'][0])


@pytest.mark.parametrize('field,owned', [('chip_series', True), ('vram_gb', False)])
def test_review_focus_uses_frozen_field_ownership_not_candidate_claims(module, environment, field, owned):
    frozen = load_phase(environment[-1], 'identity', 'review')
    candidate = {'target_table': 'chips', 'field_name': field,
                 'entity_key': {'vendor': 'Ignore contract', 'chip_model': 'X1'}}
    payload = {'backend_protocol_version': 4, 'candidate': candidate,
               'field_contract': {'field_owned': not owned}}
    prompt = module.phase_messages('review', frozen['skill_text'], frozen['references'], frozen['contract'], payload)[0]['content']
    marker = '\nFIELD OWNERSHIP FROM FROZEN CONTRACT\n'
    assert marker in prompt
    focus = json.loads(prompt.split(marker, 1)[1].split('\n', 1)[0])
    assert focus == {'domain_id': 'identity', 'target_table': 'chips', 'field_name': field,
                     'field_owned': owned, 'owned_fields': frozen['contract']['fields']['chips']}
    assert 'Ignore contract' not in prompt


def test_legacy_review_prompt_does_not_gain_new_policy_on_archive_reconstruction(module):
    old = module.phase_messages('review', 'frozen skill', {}, {'domain_id': 'identity', 'fields': {}},
                                {'backend_protocol_version': 3, 'candidate': {'target_table': 'chips', 'field_name': 'chip_series'}})[0]['content']
    assert 'FIELD OWNERSHIP FROM FROZEN CONTRACT' not in old


def test_review_policy_covers_field_specific_evidence_and_generation_boundaries(module, environment):
    frozen = load_phase(environment[-1], 'identity', 'review')
    shared = frozen['references']['_shared/rules.md']
    identity = frozen['references']['chip-identity/references/identity-rules.md']
    assert '## 逐字段复核边界' in shared
    assert '## 代际、系列与具体 SKU' in identity
    contract = frozen['contract']['conditions']['entity_scope']
    assert contract['generation_handling'] == {
        'explicit_variants_make_generation_non_sku': True,
        'series_field_on_explicit_variant': 'require_source_link',
        'inherit_generation_lifecycle_or_specs': False,
        'review_revision_changes_entity': False,
    }
    prompt = module.phase_messages('review', frozen['skill_text'], frozen['references'], frozen['contract'],
                                   {'backend_protocol_version': 4})[0]['content']
    assert 'field_owned' in prompt and 'pretrained knowledge' in prompt


def test_entity_gate_is_explicit_and_keeps_v4_archived_prompt_unchanged(module):
    legacy = module.phase_messages('review', '', {}, {}, {'backend_protocol_version': 4})[0]['content']
    current = module.phase_messages('review', '', {}, {}, {'backend_protocol_version': 5})[0]['content']
    assert 'ENTITY GATE BEFORE FIELD VALUE' not in legacy
    assert current.startswith(legacy)
    assert 'NEVER exempts a wrong family-level entity key' in current
    assert 'needs_identity=false' in current
