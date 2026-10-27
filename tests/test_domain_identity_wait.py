"""Identity waits use real isolated stores and controlled model adapters."""
import copy
import json
import pytest
from chip_model.pipeline.domain_discovery.models import ModelService, ReviewResponse
from chip_model.pipeline.domain_discovery.runtime import LinkedDomainScheduler
from chip_model.pipeline.domain_discovery.entities import EntityResolver
from tests.test_domain_entities import env, fact, Transport


def clients(extractions, reviews):
    extract, review = Transport(extractions), Transport(reviews)
    extract.model_name, review.model_name = 'extract-kimi-k2.6', 'review-kimi-k3'
    return extract, review


def make(env, extract, review, domain='specs', scope='main'):
    store, gen, budget, bundle, pages, _ = env
    return ModelService(store, 'run-a', gen, budget, pages, bundle, client=extract,
                        review_client=review, domain_id=domain, task_scope=scope, backoff=lambda _: None)


def job(env):
    return {'domain':'specs','main_domain':'specs','snapshot_id':env[-1]['snapshot_id'],
            'url':env[-1]['final_url'],'depth':0,'linked':False}


def test_only_review_uses_separate_model_and_both_are_audited(env):
    e,r = clients([{'facts':[fact(env,'specs')]}], [{'verdict':'accept','reason':'Supported'}])
    m=make(env,e,r);result=m.extract_page(env[-1]['snapshot_id'])
    assert result['candidates'][0]['status']=='validated'
    assert len(e.calls)==len(r.calls)==1
    assert [(c['phase'],c['model']) for c in m.snapshot()['calls']]==[
        ('extract','extract-kimi-k2.6'),('review','review-kimi-k3')]


@pytest.mark.parametrize('verdict',['accept','reject','revise'])
def test_identity_wait_marker_only_valid_with_unresolved(verdict):
    with pytest.raises(ValueError):
        ReviewResponse.model_validate({'verdict':verdict,'reason':'Need identity',
                                       'needs_identity':True,**({'candidate':{}} if verdict=='revise' else {})})


def test_wait_is_scheduled_and_continues_after_verified_identity_without_reextract(env):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Need chip/card identity'},
        {'verdict':'accept','reason':'Explicit identity'},
        {'verdict':'accept','reason':'Same board supported with verified identity'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id'])
    row=first['candidates'][0];assert row['status']=='awaiting_identity'
    assert not first['complete'] and first['outstanding']
    scheduler=LinkedDomainScheduler(env[0],'run-a',env[1]);ids=scheduler.schedule_identity(job(env),first['candidates'])
    assert len(ids)==1
    scheduler.execute_task(ids[0],lambda domain,scope: make(env,e,r,domain,scope))
    second=m.extract_page(env[-1]['snapshot_id'])
    assert second['complete'] and not second['outstanding']
    row=second['candidates'][0];assert row['status']=='validated'
    assert [v['version'] for v in row['versions']]==[0,1]
    assert row['revision_chain']==[]
    assert len(e.calls)==2 and len(r.calls)==3
    before=json.loads(r.calls[0]['messages'][-1]['content'])
    after=json.loads(r.calls[-1]['messages'][-1]['content'])
    assert before['identity_context']=={}
    assert after['identity_context']['confirmed_identity']['identity_proofs']
    assert before['candidate']==after['candidate']
    assert EntityResolver(env[0],'run-a',env[1]).bind_candidate(row['logical_candidate_id'])['validated']
    m.extract_page(env[-1]['snapshot_id']);assert len(r.calls)==3


def test_rejected_or_untyped_unresolved_never_schedules_identity(env):
    e,r=clients([{'facts':[fact(env,'specs'),fact(env,'specs',sku='X80 80GB SXM',value=80)]}], [
        {'verdict':'reject','reason':'Evidence wrong'}, {'verdict':'unresolved','reason':'Missing conditions'}])
    result=make(env,e,r).extract_page(env[-1]['snapshot_id'])
    assert LinkedDomainScheduler(env[0],'run-a',env[1]).schedule_identity(job(env),result['candidates'])==[]


def test_identity_failure_leaves_wait_unvalidated_and_no_followup(env):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Need identity'},
        {'verdict':'reject','reason':'Not an explicit SKU'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id'])
    scheduler=LinkedDomainScheduler(env[0],'run-a',env[1]);ids=scheduler.schedule_identity(job(env),first['candidates'])
    scheduler.execute_task(ids[0],lambda domain,scope:make(env,e,r,domain,scope))
    second=m.extract_page(env[-1]['snapshot_id'])
    assert second['candidates'][0]['status']=='awaiting_identity' and not second['complete']
    assert len(r.calls)==2


def test_identity_dependency_hint_is_frozen_in_its_extraction_context(env):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Need identity'}, {'verdict':'accept','reason':'Identity'}])
    result=make(env,e,r).extract_page(env[-1]['snapshot_id'])
    scheduler=LinkedDomainScheduler(env[0],'run-a',env[1]);ids=scheduler.schedule_identity(job(env),result['candidates'])
    scheduler.execute_task(ids[0],lambda domain,scope:make(env,e,r,domain,scope))
    payload=json.loads(e.calls[-1]['messages'][-1]['content'])
    assert payload['identity_context']['dependency_entity_hint']['full_sku']=='X80 64GB PCIe'
    assert payload['identity_context']['hints_verified'] is False
    # Routing targets narrow extraction; they are not factual review context.
    reviewed=json.loads(r.calls[-1]['messages'][-1]['content'])
    assert reviewed['identity_context']=={}


@pytest.mark.parametrize('dependency_first',[False,True])
def test_ordinary_and_dependency_identity_tasks_share_the_same_bound_review(env,dependency_first):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]},{'facts':[fact(env)]}],[
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity required'},
        {'verdict':'accept','reason':'Concrete identity'},
        {'verdict':'accept','reason':'Spec supported with real identity proof'}])
    main=make(env,e,r);first=main.extract_page(env[-1]['snapshot_id'])
    sch=LinkedDomainScheduler(env[0],'run-a',env[1]);dependency=sch.schedule_identity(job(env),first['candidates'])[0]
    ordinary=env[0].enqueue('run-a',env[1],'ordinary-identity',{
        **job(env),'domain':'identity','linked':True,'depth':1})
    ordered=[dependency,ordinary] if dependency_first else [ordinary,dependency]
    initial=None
    for tid in ordered:
        result=sch.execute_task(tid,lambda d,s:make(env,e,r,d,s))
        row=result['candidates'][0]
        if initial is None:initial=copy.deepcopy(row)
        else:
            assert row['logical_candidate_id']==initial['logical_candidate_id']
            assert row['review_binding_sha256']==initial['review_binding_sha256']
            assert row['versions']==initial['versions'] and row['reviews']==initial['reviews']
            assert row['revision_chain']==[] and row['status']=='validated'
    assert len(e.calls)==3 and len(r.calls)==2  # no second identity review
    contexts=[json.loads(call['messages'][-1]['content'])['identity_context'] for call in e.calls[1:]]
    assert sorted(bool(c.get('dependency_entity_hint')) for c in contexts)==[False,True]
    assert json.loads(r.calls[-1]['messages'][-1]['content'])['identity_context']=={}
    finished=main.extract_page(env[-1]['snapshot_id'])
    assert finished['complete'] and finished['candidates'][0]['status']=='validated'
    assert len(r.calls)==3 and not main.snapshot().get('input_conflicts')


@pytest.mark.parametrize('verdict',['reject','unresolved'])
def test_dependency_target_never_promotes_cached_negative_identity(env,verdict):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]},{'facts':[fact(env)]}],[
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity required'},
        {'verdict':verdict,'reason':'Identity evidence insufficient'}])
    main=make(env,e,r);first=main.extract_page(env[-1]['snapshot_id'])
    sch=LinkedDomainScheduler(env[0],'run-a',env[1]);dependency=sch.schedule_identity(job(env),first['candidates'])[0]
    make(env,e,r,'identity','linked').extract_page(env[-1]['snapshot_id'])
    result=sch.execute_task(dependency,lambda d,s:make(env,e,r,d,s))
    assert result['candidates'][0]['status']=={'reject':'semantic_rejected','unresolved':'unresolved'}[verdict]
    assert len(r.calls)==2 and EntityResolver(env[0],'run-a',env[1]).snapshot()['new_entity_count']==0
    assert main.extract_page(env[-1]['snapshot_id'])['candidates'][0]['status']=='awaiting_identity'


def test_identity_target_is_bound_to_domain_scope_and_preserves_real_review_context(env):
    from chip_model.pipeline.domain_discovery.store import _hash,_json
    resolver=EntityResolver(env[0],'run-a',env[1]);target={'vendor':'Vendor A','full_sku':'X80 64GB PCIe','entity_type':'board_sku'}
    normalized,_=resolver._hint(target);scope='identity:'+_hash(_json(normalized))
    e,r=clients([{'facts':[fact(env)]},{'facts':[fact(env)]}],[{'verdict':'accept','reason':'Identity'}])
    model=make(env,e,r,'identity',scope)
    context={'initial_chip_clue':'original clue'}
    before=copy.deepcopy(context)
    model.extract_page(env[-1]['snapshot_id'],identity_context=context,identity_target=target)
    assert context==before
    assert json.loads(r.calls[-1]['messages'][-1]['content'])['identity_context']==context
    with pytest.raises(ValueError,match='model input conflict'):
        make(env,e,r,'identity','linked').extract_page(env[-1]['snapshot_id'],identity_context={'initial_chip_clue':'changed clue'})
    count=len(e.calls)
    with pytest.raises(ValueError,match='identity target'):
        make(env,e,r,'identity',scope).extract_page(env[-1]['snapshot_id'],identity_target={**target,'full_sku':'X80 80GB SXM'})
    with pytest.raises(ValueError,match='identity target'):
        make(env,e,r,'specs',scope).extract_page(env[-1]['snapshot_id'],identity_target=target)
    assert len(e.calls)==count


def test_review_cache_does_not_silently_strip_identity_routing_keys(env):
    candidate=fact(env)
    e,r=clients([], [{'verdict':'accept','reason':'Identity supported'}])
    model=make(env,e,r,'identity','linked')
    first=model.review_candidate(candidate)
    assert first['status']=='validated'
    with pytest.raises(ValueError,match='model input conflict'):
        model.review_candidate(candidate,identity_context={
            'dependency_entity_hint':{'vendor':'Vendor A','full_sku':'X80 64GB PCIe'},
            'hints_verified':False})
    assert len(r.calls)==1


def test_wait_keeps_one_semantic_revision_after_identity_confirmation(env):
    candidate=fact(env,'specs');revised=copy.deepcopy(candidate);revised['explanation']='Correct explanation'
    e,r=clients([{'facts':[candidate]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity'},
        {'verdict':'accept','reason':'Identity supported'},
        {'verdict':'revise','reason':'Clarify explanation','candidate':revised},
        {'verdict':'accept','reason':'Now supported'}])
    m=make(env,e,r);result=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),result['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    row=m.extract_page(env[-1]['snapshot_id'])['candidates'][0]
    assert row['status']=='validated' and len(row['revision_chain'])==1
    assert [v['version'] for v in row['versions']]==[0,1,2]


def test_repeated_identity_wait_does_not_loop_or_reset_quota(env):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity'},
        {'verdict':'accept','reason':'Identity supported'},
        {'verdict':'unresolved','needs_identity':True,'reason':'Again'}])
    m=make(env,e,r);result=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),result['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    row=m.extract_page(env[-1]['snapshot_id'])['candidates'][0]
    assert row['status']=='unresolved' and row['reason']=='identity_followup_limit'
    m.extract_page(env[-1]['snapshot_id']);assert len(r.calls)==3


def test_two_identity_dependencies_on_one_page_do_not_conflict_or_reextract_other_target(env):
    a,b=fact(env,'specs'),fact(env,'specs',sku='X80 80GB SXM',value=80)
    ia,ib=fact(env),fact(env,sku='X80 80GB SXM')
    e,r=clients([{'facts':[a,b]},{'facts':[ia,ib]},{'facts':[ia,ib]}],[
        {'verdict':'unresolved','needs_identity':True,'reason':'A identity'},
        {'verdict':'unresolved','needs_identity':True,'reason':'B identity'},
        {'verdict':'accept','reason':'A identity'}, {'verdict':'accept','reason':'B identity'},
        {'verdict':'accept','reason':'A value'}, {'verdict':'accept','reason':'B value'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),first['candidates']);assert len(ids)==2
    for tid in ids:sch.execute_task(tid,lambda d,s:make(env,e,r,d,s))
    second=m.extract_page(env[-1]['snapshot_id']);assert second['complete']
    assert all(row['status']=='validated' for row in second['candidates'])
    assert len(e.calls)==3 and len(r.calls)==6


def test_followup_resumes_after_crash_without_overwriting_original_request(env,monkeypatch):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}],[
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity'},
        {'verdict':'accept','reason':'Identity'}, {'verdict':'accept','reason':'Confirmed'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),first['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    original=m.review_candidate
    monkeypatch.setattr(m,'review_candidate',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('controlled crash')))
    with pytest.raises(RuntimeError,match='controlled crash'):m.extract_page(env[-1]['snapshot_id'])
    monkeypatch.setattr(m,'review_candidate',original)
    final=m.extract_page(env[-1]['snapshot_id'])
    assert final['candidates'][0]['status']=='validated'
    assert len(final['candidates'][0]['versions'])==2 and len(r.calls)==3


def test_identity_followup_does_not_restore_already_used_semantic_revision(env):
    original=fact(env,'specs');v1=copy.deepcopy(original);v1['explanation']='First semantic revision'
    v2=copy.deepcopy(v1);v2['explanation']='Forbidden second semantic revision'
    e,r=clients([{'facts':[original]},{'facts':[fact(env)]}],[
        {'verdict':'revise','reason':'One correction','candidate':v1},
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity now needed'},
        {'verdict':'accept','reason':'Identity supported'},
        {'verdict':'revise','reason':'Second correction','candidate':v2}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),first['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    row=m.extract_page(env[-1]['snapshot_id'])['candidates'][0]
    assert row['status']=='unresolved' and row['reason']=='revision_limit'
    assert len(row['revision_chain'])==1 and [v['version'] for v in row['versions']]==[0,1,2]


def test_changed_review_model_cannot_reuse_old_review_binding(env):
    e,r=clients([{'facts':[fact(env,'specs')]}],[{'verdict':'accept','reason':'Supported'}])
    m=make(env,e,r);row=m.extract_page(env[-1]['snapshot_id'])['candidates'][0]
    changed=Transport([]);changed.model_name='different-reviewer'
    other=make(env,e,changed)
    with pytest.raises(ValueError,match='model input conflict'):
        other.review_candidate(row['original_raw'])
    assert changed.calls==[]


def test_duplicate_support_after_identity_followup_uses_frozen_confirmed_context(env):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity'},
        {'verdict':'accept','reason':'Identity'}, {'verdict':'accept','reason':'Confirmed'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),first['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    row=m.extract_page(env[-1]['snapshot_id'])['candidates'][0]
    merged=m._merge_support(row,[row['versions'][-1]['candidate']],env[-1],{})
    assert merged['status']=='validated' and len(r.calls)==3


def test_chip_identity_wait_cannot_be_satisfied_by_board_identity(env):
    candidate=fact(env,'specs',scope='chip',entity_hints={'full_sku':'X80 64GB PCIe','entity_type':'chip'})
    e,r=clients([{'facts':[candidate]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Chip identity needed'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),first['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    result=m.extract_page(env[-1]['snapshot_id'])
    assert not result['complete'] and result['candidates'][0]['status']=='awaiting_identity'
    assert len(r.calls)==1 and len(result['candidates'][0]['versions'])==1


def test_budget_exhausted_after_identity_keeps_range_outstanding(env):
    e,r=clients([{'facts':[fact(env,'specs')]},{'facts':[fact(env)]}], [
        {'verdict':'unresolved','needs_identity':True,'reason':'Identity'},
        {'verdict':'accept','reason':'Identity supported'}])
    m=make(env,e,r);first=m.extract_page(env[-1]['snapshot_id']);sch=LinkedDomainScheduler(env[0],'run-a',env[1])
    ids=sch.schedule_identity(job(env),first['candidates']);sch.execute_task(ids[0],lambda d,s:make(env,e,r,d,s))
    env[2].begin_active();env[2].monotonic=lambda: 3000
    result=m.extract_page(env[-1]['snapshot_id'])
    assert not result['complete'] and result['outstanding'][0]['status']=='budget_stopped'
    assert result['candidates'][0]['status']=='budget_stopped' and len(r.calls)==2
    assert result['chunks'][0]['review_status']=='budget_stopped'
