"""Numeric SKU/value scope: real quotes and stores, controlled model responses."""
import copy
import pytest
from tests.test_domain_models import module, environment, page, fact, service, FakeModel
from chip_model.pipeline.domain_discovery.rules import load_phase

POLICY = {'mode': 'local_claim_or_exclusive_header', 'unbound': 'unresolved', 'inherit_parent': False}


def setup_fact(module, environment, text, sku='TPU 8t', quote=None, field='tdp_w', value=1300, unit='W'):
    pages, snapshot = page(environment, text)
    quote = snapshot['text'] if quote is None else quote
    start = snapshot['text'].index(quote)
    candidate = fact(snapshot, entity_key={'vendor':'Vendor','chip_model':sku}, entity_hints={'full_sku':sku},
        field_name=field, raw_value=value, normalized_value=value, unit=unit,
        conversion={'source_unit':unit,'factor':1,'basis':'decimal'},
        evidence={'snapshot_id':snapshot['snapshot_id'],'body_hash':snapshot['body_hash'],
                  'quote':quote,'start':start,'end':start+len(quote)})
    contract = load_phase(environment[-1], 'specs', 'extract')['contract']
    contract['conditions']['sku_value_binding'] = POLICY
    chunk = {'start':0,'end':len(snapshot['text']),'context_refs':[]}
    return pages, snapshot, candidate, contract, chunk


@pytest.mark.parametrize('text,sku', [
    ('TPU v8单芯片功耗达1300W', 'TPU 8t'),
    ('TPU v8单芯片功耗达1300W。TPU v8分为TPU 8t和TPU 8i。', 'TPU 8t'),
    ('TPU 8t用于训练。TPU v8功耗1300W。', 'TPU 8t'),
    ('TPU 8t用于训练,TPU 8i功耗1300W。', 'TPU 8t'),
    ('TPU 8t用于训练. TPU v8 power is 1300W.', 'TPU 8t'),
    ('TPU 8t用于训练\nTPU v8功耗1300W', 'TPU 8t'),
    ('X80-Pro功耗1300W', 'X80'),
    ('X80Plus功耗1300W', 'X80'),
    ('X80 memory 64GB, single card TDP 1300W', 'X80'),
])
def test_numeric_value_cannot_borrow_sku_from_parent_or_other_claim(module, environment, text, sku):
    _, snapshot, candidate, contract, chunk = setup_fact(module, environment, text, sku)
    before = copy.deepcopy(candidate)
    result = module.validate_candidate(candidate, contract, snapshot, chunk)
    assert result['status'] == 'unresolved'
    assert result['reason'] == 'sku_value_binding_unresolved'
    assert result['missing_conditions'] == ['sku_value_evidence_binding']
    assert candidate == before


@pytest.mark.parametrize('text,sku,field,value,unit,mode', [
    ('TPU 8t单芯片功耗达1300W','TPU 8t','tdp_w',1300,'W','direct_claim'),
    ('tpu   8t power: 1300 W.','TPU 8t','tdp_w',1300,'W','direct_claim'),
    ('昇腾910C显存64GB','昇腾910C','vram_gb',64,'GB','direct_claim'),
    ('X80采用2.0nm制程。','X80','process_node_nm',2.0,'nm','direct_claim'),
    ('TPU v8分拆为训练芯片TPU 8t和推理芯片TPU 8i,两款芯片分别合作设计,均锁定台积电2nm制程。',
     'TPU 8t','process_node_nm',2,'nm','explicit_shared_claim'),
    ('TPU 8t and TPU 8i, both use 2nm.','TPU 8i','process_node_nm',2,'nm','explicit_shared_claim'),
])
def test_direct_and_explicit_shared_claims_remain_reviewable(module, environment, text, sku, field, value, unit, mode):
    _, snapshot, candidate, contract, chunk = setup_fact(module, environment, text, sku, field=field, value=value, unit=unit)
    result = module.validate_candidate(candidate, contract, snapshot, chunk)
    assert result['status'] == 'program_valid'
    assert result['sku_value_binding']['mode'] == mode
    binding = result['sku_value_binding']
    assert snapshot['text'][binding['start']:binding['end']] == binding['quote']


@pytest.mark.parametrize('header,quote,expected', [
    ('TPU 8t','功耗1300W','program_valid'),
    ('Vendor TPU 8t','功耗1300W','program_valid'),
    ('TPU 8t / TPU 8i','功耗1300W','unresolved'),
    ('TPU 8t','TPU 8i功耗1300W','unresolved'),
])
def test_only_exclusive_declared_header_can_bind_unnamed_numeric_claim(module, environment, header, quote, expected):
    text = header+'\n'+quote
    _, snapshot, candidate, contract, chunk = setup_fact(module, environment, text, quote=quote)
    ref = {'kind':'heading','start':0,'end':len(header)}
    candidate['context_refs'] = [{**ref,'snapshot_id':snapshot['snapshot_id'],
                                 'body_hash':snapshot['body_hash'],'quote':header}]
    chunk['context_refs'] = [ref]
    result = module.validate_candidate(candidate, contract, snapshot, chunk)
    assert result['status'] == expected
    if expected == 'program_valid': assert result['sku_value_binding']['mode'] == 'exclusive_header'


def test_old_frozen_contract_keeps_historical_program_semantics(module, environment):
    _, snapshot, candidate, contract, chunk = setup_fact(module, environment, 'TPU v8单芯片功耗达1300W')
    contract['conditions'].pop('sku_value_binding')
    assert module.validate_candidate(candidate, contract, snapshot, chunk)['status'] == 'program_valid'


def test_unbound_value_never_reaches_accepting_reviewer_or_identity_wait(module, environment):
    pages, _, candidate, _, _ = setup_fact(module, environment, 'TPU v8单芯片功耗达1300W')
    client = FakeModel([{'verdict':'accept','reason':'This intentionally permissive model must not run.'}])
    models = service(module, environment, pages, client)
    row = models.review_candidate(candidate)
    assert row['status'] == 'unresolved' and row['reason'] == 'sku_value_binding_unresolved'
    assert not client.requests and not row['reviews']
    assert not models.snapshot()['calls']
    assert not row.get('identity_wait_context')


def test_specs_authority_enables_binding_policy(environment):
    assert load_phase(environment[-1], 'specs', 'extract')['contract']['conditions']['sku_value_binding'] == POLICY


@pytest.mark.parametrize('text', ['X80 has 64 cores, X90 has 64GB.', 'X80 bandwidth 64GB/s.'])
def test_value_and_unit_cannot_come_from_different_claims(module, environment, text):
    _, snapshot, candidate, contract, chunk = setup_fact(module, environment, text, sku='X80',
                                                       field='vram_gb', value=64, unit='GB')
    result = module.validate_candidate(candidate, contract, snapshot, chunk)
    assert result['status'] == 'unresolved'


def test_omitting_intervening_header_cannot_reassign_another_sku_value(module, environment):
    text = 'TPU 8t\nTPU 8i\n功耗1300W'
    _, snapshot, candidate, contract, chunk = setup_fact(module, environment, text, quote='功耗1300W')
    first = {'kind':'heading','start':0,'end':len('TPU 8t')}
    second_start = snapshot['text'].index('TPU 8i')
    second = {'kind':'heading','start':second_start,'end':second_start+len('TPU 8i')}
    candidate['context_refs'] = [{**first,'snapshot_id':snapshot['snapshot_id'],
        'body_hash':snapshot['body_hash'],'quote':'TPU 8t'}]
    chunk['context_refs'] = [first, second]
    assert module.validate_candidate(candidate, contract, snapshot, chunk)['status'] == 'unresolved'


def test_unbound_fact_cannot_become_validated_in_final_comparison(module, environment):
    from chip_model.pipeline.domain_discovery.compare import ComparisonService
    pages, _, candidate, _, _ = setup_fact(module, environment, 'X90 has 64GB memory', sku='X80',
                                          field='vram_gb',value=64,unit='GB')
    client = FakeModel([{'verdict':'accept','reason':'Must not be called'}])
    row = service(module, environment, pages, client).review_candidate(candidate)
    comparison = ComparisonService(environment[0], 'run-a', environment[1], pages=pages)
    final = comparison.compare_candidate(row['logical_candidate_id'])
    assert not final['validated'] and comparison.statistics()['validated_candidates'] == 0
    assert not client.requests
