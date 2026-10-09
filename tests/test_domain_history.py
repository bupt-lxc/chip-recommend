"""V3 history reads controlled run artifacts only; no live SDK or business DB."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from chip_model.pipeline.domain_discovery import history
from chip_model.pipeline.domain_discovery.store import RunStore


@pytest.mark.parametrize('basis,label', [
    (None,'旧值未记录'), ('new_entity','本轮基线无此实体'),
    ('entity_absent','本轮基线无此实体'), ('relation_absent','本轮基线无此关系'),
    ('field_empty','本轮基线字段为空'), ('frozen_value','本轮冻结基线'),
    ('entity_unresolved','实体未确认，无法比较'), ('comparison_unresolved','关系条件未确认，无法比较'),
    ('field_not_recorded','本轮基线未记录此字段'), ('old_value_not_recorded','旧值未记录'),
])
def test_display_baseline_keeps_missing_and_canonical_bases(basis,label):
    raw={'old_value_basis':basis,'old_value':None}
    before=copy.deepcopy(raw)
    result=history.display_baseline(raw)
    assert result['basis_label']==label
    assert result['value']=='未记录'
    assert result['time']=='未记录'
    assert raw==before


def test_display_baseline_keeps_saved_value_and_time():
    assert history.display_baseline({'old_value_basis':'snapshot','old_value':64,'baseline_time':1000}) == {
        'basis_label':'本轮冻结基线','value':'64','time':'1970-01-01T00:16:40+00:00'}


def fixture_run(tmp_path, status='partial', run_id='fixture-history'):
    root=tmp_path/'domain_runs'
    store=RunStore(root,clock=lambda:1000)
    store.start('fixture-key-'+run_id, {'domain':'specs','chip':None,'scope_type':'discovery','resume_run_id':None},run_id)
    gen=store.claim(run_id,'fixture-owner',10000)
    fact={'domain_id':'specs','target_table':'chips','field_name':'vram_gb',
        'entity_key':{'vendor':'Fixture Vendor','chip_model':'Fixture X80 80GB SXM'},
        'normalized_value':80,'raw_value':'80 GB','unit':'GB',
        'source_type':'official_datasheet','confidence':'high','conditions':{'form_factor':'SXM'},
        'evidence':{'snapshot_id':'fixture-snapshot','body_hash':'hash','start':0,'end':40,
                    'quote':'Public API_KEY=example; 80 GB exact quote'}}
    entity={'status':'existing','canonical_key':'fixture-key','entity_id':'1',
            'hint':{'vendor':'Fixture Vendor','full_sku':'Fixture X80 80GB SXM'},'reason':'exact_verified_identity'}
    row={'candidate':fact,'entity':entity,'old_value':80,'old_value_basis':'frozen_value',
        'baseline_time':1000,'old_sources':[{'source_url':'https://old.example/spec','source_type':'community'}],
        'logical_candidate_id':'fact-a','task_scopes':['main','linked'],
        'value_change_kind':'unchanged','source_membership_kind':'added','source_quality_kind':'unknown',
        'source_change_kind':'added','evidence_status':'accepted','evidence_verification_status':'verified',
        'model_proof':{'accepted':True,'version':0},'dependency_status':'resolved',
        'relation_readiness':{'status':'ready'},'comparison':{'status':'not_applicable'},
        'update_eligible':True,'new_source':{'url':'https://new.example/spec','body_hash':'hash'}}
    metadata={'domain_compare.v1':{'comparisons':{'fact-a':[row]},'baselines':{},'decisions':[]},
        'domain_models.v1':{'candidates':{'fact-a':{'domain_id':'specs','status':'validated',
            'versions':[{'version':0,'candidate':fact}],'reviews':[{'version':0,'verdict':'accept'}]},
            'raw-reject':{'domain_id':'specs','status':'program_rejected','original_raw':{'field_name':'rogue','normalized_value':'<script>bad</script>','source_url':'https://rejected.example'},'reason':'outside ownership'},
            'pending':{'domain_id':'compute','status':'pending','versions':[]}},
            'calls':[{'execution_id':'fixture-call','phase':'review','status':'completed','raw_request':{'api_key':'fixture-secret'},'response':{'api_key':'fixture-secret'}}]},
        'domain_entities.v1':{'entities':{'1':entity},'bindings':{}},
        'baseline':{'created_at':1000,'baseline_sha256':'frozen-hash','rule_bundle_hash':'bundle-hash'},
        'domain_executor_audit.v1':{'actor':'fixture-only'},
        'domain_budget.v1':{'stages':{'comparison':{'main':{'status':'partial_failure','scope':'main','reason':'fixture pending'}}}}}
    for key,val in metadata.items():
        with sqlite3.connect(root/run_id/'run.sqlite3') as db:
            db.execute('INSERT INTO metadata VALUES (?,?)',(key,json.dumps(val)))
    store.finish_run(run_id,gen,{'status':status,'statistics':{'independent_fields':1,'main':{'validated_candidates':1},'linked':{'validated_candidates':1}}})
    store.export_runtime(run_id);store.export_events(run_id)
    return root,row


def digest_tree(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


def test_v3_history_is_readonly_and_preserves_business_evidence(tmp_path,monkeypatch):
    root,row=fixture_run(tmp_path)
    before=digest_tree(root)
    monkeypatch.setattr(RunStore,'__init__',lambda *a,**k:pytest.fail('history must not initialize/migrate RunStore'))
    result=history.read_v3_run(root,'fixture-history')
    assert result['schema_version']=='hermes-open-web-v3'
    assert result['status']=='partial'
    assert result['formal_database_modified'] is False
    assert result['field_changes'][0]['proposed_value']==80
    assert result['field_changes'][0]['entity']['chip_model']=='Fixture X80 80GB SXM'
    assert result['field_changes'][0]['evidence_text']=='Public API_KEY=example; 80 GB exact quote'
    assert result['field_changes'][0]['source_membership_kind']=='added'
    assert result['field_changes'][0]['source_quality_kind']=='unknown'
    assert result['field_changes'][0]['conditions']=={'form_factor':'SXM'}
    assert result['raw_records']['metadata']['domain_compare.v1']['comparisons']['fact-a'][0]==row
    assert result['field_changes'][1]['status']=='program_rejected'
    assert result['field_changes'][1]['proposed_value']=='<script>bad</script>'
    assert result['field_changes'][2]['status']=='pending'
    assert 'fixture-secret' not in json.dumps(result)
    assert digest_tree(root)==before


@pytest.mark.parametrize('status',['success','no_findings','partial','failed','interrupted','preparing','running'])
def test_readonly_run_list_keeps_all_statuses(tmp_path,status):
    root,_=fixture_run(tmp_path)
    with sqlite3.connect(root/'control.sqlite3') as db:
        db.execute('UPDATE runs SET status=?,stage=?',(status,status))
    listed=history.list_v3_runs(root)
    assert listed[0]['status']==status
    assert listed[0]['active']==(status in {'preparing','running'})


def test_open_web_api_reads_v3_before_legacy_without_db_access(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from chip_model.server import app
    from chip_model import server
    root,_=fixture_run(tmp_path,'failed')
    monkeypatch.setattr(server,'get_db_path',lambda:tmp_path/'DO-NOT-OPEN.db')
    api=TestClient(app)
    listed=api.get('/api/v1/data-agent/open-web-runs?final_only=true')
    assert listed.status_code==200
    assert listed.json()['runs'][0]['status']=='failed'
    result=api.get('/api/v1/data-agent/open-web-runs/fixture-history')
    assert result.status_code==200 and result.json()['schema_version']=='hermes-open-web-v3'
    assert not (tmp_path/'DO-NOT-OPEN.db').exists()


def test_v3_history_rejects_paths_and_handles_no_initialized_run_db(tmp_path):
    root=tmp_path/'domain_runs';root.mkdir()
    with pytest.raises(ValueError):history.read_v3_run(root,'../escape')
    with pytest.raises(ValueError):history.read_v3_run(root,'missing')
    assert history.list_v3_runs(tmp_path/'absent')==[]
    assert not (tmp_path/'absent').exists()


def test_manual_form_readonly_fields_come_from_six_domain_contracts():
    from fastapi.testclient import TestClient
    from chip_model.server import app
    from chip_model.pipeline.domain_discovery.contracts import DOMAINS,load_domain
    response=TestClient(app).get('/api/v1/data-agent/domains')
    assert response.status_code==200
    rows=response.json()['domains']
    assert [r['domain_id'] for r in rows]==list(DOMAINS)
    root=Path(__file__).resolve().parents[1]/'.hermes-skills'
    assert all(row['fields']==load_domain(root,row['domain_id']).fields for row in rows)


def test_final_entity_reconciliation_overrides_old_accepted_task_binding(tmp_path):
    root,_=fixture_run(tmp_path)
    with sqlite3.connect(root/'fixture-history/run.sqlite3') as db:
        raw=json.loads(db.execute("SELECT value_json FROM metadata WHERE key='domain_entities.v1'").fetchone()[0])
        raw['entities']['1']['status']='ambiguous'
        db.execute("UPDATE metadata SET value_json=? WHERE key='domain_entities.v1'",(json.dumps(raw),))
    row=history.read_v3_run(root,'fixture-history')['field_changes'][0]
    assert row['entity']['status']=='ambiguous'
    assert row['update_eligible'] is False
    assert row['value_change_kind']=='unknown'


def test_saved_identity_review_and_program_remain_distinct_from_missing_comparison(tmp_path):
    from chip_model.pipeline.domain_discovery.store import _hash,_json
    root,row=fixture_run(tmp_path)
    candidate={**row['candidate'],'domain_id':'identity','field_name':'chip_type','normalized_value':'GPU'}
    proof={'logical_candidate_id':'identity-fact','accepted':True,'semantic_review':'accept',
           'version':0,'candidate_sha256':_hash(_json(candidate)),'execution_id':'identity-call'}
    with sqlite3.connect(root/'fixture-history/run.sqlite3') as db:
        models=json.loads(db.execute("SELECT value_json FROM metadata WHERE key='domain_models.v1'").fetchone()[0])
        models['candidates']['identity-fact']={'domain_id':'identity','status':'validated',
            'versions':[{'version':0,'candidate':candidate,'program':{'status':'program_valid','reason':'exact evidence'}}],
            'reviews':[{'version':0,'verdict':'accept','reason':'saved independent review'}]}
        models['calls'].append({'execution_id':'identity-call','status':'completed','phase':'review','response':{'verdict':'accept'}})
        entities={'entities':{'1':{**row['entity'],'identity_proofs':[proof]}},'bindings':{}}
        for key,val in [('domain_models.v1',models),('domain_entities.v1',entities)]:
            db.execute('UPDATE metadata SET value_json=? WHERE key=?',(json.dumps(val),key))
    result=history.read_v3_run(root,'fixture-history')
    projected=next(r for r in result['field_changes'] if r['id']=='identity-fact')
    assert projected['program_status']=='program_valid'
    assert projected['review_status']=='accept'
    assert projected['entity']['status']=='existing'
    assert projected['model_proof']==proof
    assert projected['evidence_verification_status']=='not_recorded'
    assert projected['value_change_kind']=='unknown'
    assert projected['update_eligible'] is False
    assert len(result['verified_entities'])==1


def test_entity_card_is_not_invented_from_name_or_unproven_status(tmp_path):
    root,_=fixture_run(tmp_path)
    assert history.read_v3_run(root,'fixture-history')['verified_entities']==[]


def test_uncompared_binding_never_promotes_final_update_eligibility(tmp_path):
    root,row=fixture_run(tmp_path)
    with sqlite3.connect(root/'fixture-history/run.sqlite3') as db:
        db.execute("UPDATE metadata SET value_json=? WHERE key='domain_compare.v1'",(json.dumps({'comparisons':{}}),))
        db.execute("UPDATE metadata SET value_json=? WHERE key='domain_entities.v1'",(json.dumps({'entities':{},'bindings':{'fact-a':[row]}}),))
    projected=history.read_v3_run(root,'fixture-history')['field_changes'][0]
    assert projected['binding_update_eligible'] is True
    assert projected['update_eligible'] is False
    assert projected['evidence_verification_status']=='not_recorded'


def test_exported_work_and_model_business_result_keep_exact_public_quote(tmp_path):
    root,row=fixture_run(tmp_path)
    path=root/'fixture-history/run.sqlite3'
    with sqlite3.connect(path) as db:
        models=json.loads(db.execute("SELECT value_json FROM metadata WHERE key='domain_models.v1'").fetchone()[0])
        models['calls'][0]['business_response']={'facts':[row['candidate']]}
        db.execute("UPDATE metadata SET value_json=? WHERE key='domain_models.v1'",(json.dumps(models),))
        db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)',('extract-op','hash',json.dumps({'stage':'extracting','api_key':'fixture-secret'}),'completed',json.dumps({'facts':[row['candidate']]}),'extracting',1,1))
    # Export rebuild is fixture preparation, never a history side effect.
    RunStore(root).export_runtime('fixture-history')
    result=history.read_v3_run(root,'fixture-history')
    assert result['model_calls'][0]['business_response']['facts'][0]==row['candidate']
    assert result['raw_records']['operations'][0]['result']['facts'][0]==row['candidate']
    assert result['raw_records']['work']['metadata']['domain_models.v1']['candidates']['fact-a']['versions'][0]['candidate']==row['candidate']
    assert 'fixture-secret' not in json.dumps(result)


def test_preparing_control_receipt_is_visible_even_before_folder_initialization(tmp_path,monkeypatch):
    root=tmp_path/'domain_runs';store=RunStore(root)
    store.start('fixture-key',{'domain':'compute'},'not-initialized')
    with sqlite3.connect(root/'control.sqlite3') as db:
        db.execute("UPDATE runs SET initialized=0 WHERE run_id='not-initialized'")
    # A crash can occur between control binding and folder creation.
    folder=root/'not-initialized'
    (folder/'run.sqlite3').unlink();folder.rmdir()
    before=digest_tree(root)
    assert history.list_v3_runs(root)[0]['status']=='preparing'
    assert history.read_v3_run(root,'not-initialized')['field_changes']==[]
    from fastapi.testclient import TestClient
    from chip_model import server
    monkeypatch.setattr(server,'get_db_path',lambda:tmp_path/'no-business-db.db')
    assert TestClient(server.app).get('/api/v1/data-agent/open-web-runs/not-initialized').status_code==200
    assert digest_tree(root)==before


@pytest.mark.parametrize('bad',[
    {'entity_key':'malformed'}, {'entity_key':['malformed']},
    {'evidence':'malformed'}, {'evidence':['malformed']},
    {'entity_key':{'vendor':'Vendor A','chip_model':123}},
    {'entity_key':{'vendor':['Vendor A'],'chip_model':{'name':'<script>invalid</script>'}}},
    {'field_name':'tdp_w','conditions':'malformed','context_refs':'malformed','source_url':['javascript:alert(1)']},
    {'domain_id':['specs'],'field_name':['vram_gb']},
    {'field_name':'unowned-field'}, 'raw scalar', ['raw','array'], None,False,0,[],{},
])
def test_actual_model_raw_rejection_shapes_are_readable_without_losing_siblings(tmp_path,bad):
    from tests.test_domain_compare import factory,capture,fact,audit
    build=factory.__wrapped__(tmp_path)
    ctx=build('raw-invalid-fixture')
    snap=capture(ctx)
    good=fact(snap)
    invalid={**good,**bad} if isinstance(bad,dict) and bad else bad
    produced=audit(ctx,snap,[good,invalid])
    rejected=next(r for r in produced if r['status']=='program_rejected')
    before=digest_tree(ctx['store'].root)
    result=history.read_v3_run(ctx['store'].root,ctx['id'])
    assert len(result['field_changes'])==2
    projected=next(r for r in result['field_changes'] if r['id']==rejected['logical_candidate_id'])
    assert projected['status']=='program_rejected'
    assert projected['rejection_reason']==rejected['reason']
    assert projected['raw_candidate']==invalid
    assert projected['candidate_source_ref']['snapshot_id']==snap['snapshot_id']
    assert projected['program_status']=='rejected'
    assert projected['review_status']=='not_executed'
    assert projected['update_eligible'] is False
    assert result['raw_records']['metadata']['domain_models.v1']['candidates'][rejected['logical_candidate_id']]['original_raw']==invalid
    assert history.list_v3_runs(ctx['store'].root)[0]['run_id']==ctx['id']
    assert digest_tree(ctx['store'].root)==before


def test_legacy_failed_run_is_inspectable_in_default_ui_list(tmp_path,monkeypatch):
    from chip_model.pipeline.open_web_test import _ensure_test_tables
    from chip_model.pipeline.test_workspace import runs_root
    from chip_model import server
    from fastapi.testclient import TestClient
    source=tmp_path/'never-open-business.db'
    folder=runs_root(source)/'legacy-failed';folder.mkdir(parents=True)
    _ensure_test_tables(folder/'data.db')
    (folder/'test_mode.json').write_text(json.dumps({'mode':'test','pipeline':'hermes-open-web'}))
    (folder/'manifest.json').write_text(json.dumps({'schema_version':'hermes-open-web-v2',
        'session_id':'legacy-failed','skill':'chip-specs','status':'failed',
        'started_at':'2026-01-01','finished_at':'2026-01-01','units':[]}))
    before=digest_tree(tmp_path)
    monkeypatch.setattr(server,'get_db_path',lambda:source)
    api=TestClient(server.app)
    listed=api.get('/api/v1/data-agent/open-web-runs?final_only=false').json()['runs']
    assert any(r['session_id']=='legacy-failed' and r['status']=='failed' for r in listed)
    assert api.get('/api/v1/data-agent/open-web-runs/legacy-failed').status_code==200
    assert not source.exists()
    assert digest_tree(tmp_path)==before


def test_actual_model_stage_call_ids_map_saved_coverage_scopes_without_domain_guess(tmp_path):
    from tests.test_domain_compare import factory,capture,fact,audit
    ctx=factory.__wrapped__(tmp_path)('stage-scope-fixture')
    main=capture(ctx,url='https://fixture.example/main')
    audit(ctx,main,[fact(main)],scope='main')
    # Same actual domain, different task provenance: domain equality cannot
    # classify main versus linked work.
    linked=capture(ctx,url='https://fixture.example/linked')
    audit(ctx,linked,[fact(linked)],scope='linked')
    identity=capture(ctx,url='https://fixture.example/identity')
    audit(ctx,identity,[fact(identity,'identity',field='chip_type',value='GPU',unit=None)],scope='identity')
    ctx['budget'].record_stage('extracting','completed',scope='extract-unknown',
                              reason='No recorded task/call binding',domain_id='specs')
    original=ctx['store'].metadata(ctx['id'],'domain_budget.v1')['stages']
    before=digest_tree(ctx['store'].root)
    result=history.read_v3_run(ctx['store'].root,ctx['id'])
    assert result['stages']==original
    views=result['stage_views']
    known=[v for v in views if v['recorded_scope']!='extract-unknown']
    assert len(known)==6
    assert {(v['domain_id'],tuple(v['scope_categories'])) for v in known}=={
        ('specs',('main',)),('specs',('linked',)),('identity',('identity',))}
    assert {(v['stage'],v['domain_id'],tuple(v['scope_categories'])) for v in known}=={
        (stage,domain,(scope,)) for stage in ('extracting','reviewing')
        for domain,scope in [('specs','main'),('specs','linked'),('identity','identity')]}
    assert all(v['scope_status']=='resolved' for v in known)
    assert all(v['scope_basis_refs'] for v in known)
    unknown=next(v for v in views if v['recorded_scope']=='extract-unknown')
    assert unknown['scope_categories']==[]
    assert unknown['scope_basis_refs']==[]
    assert digest_tree(ctx['store'].root)==before


def test_stage_scope_can_bind_explicit_saved_task_without_model_or_domain_guess(tmp_path):
    store=RunStore(tmp_path/'runs')
    store.start('scope-start',{'domain':'specs'},'task-scope-fixture')
    gen=store.claim('task-scope-fixture','fixture',100)
    task_id=store.enqueue('task-scope-fixture',gen,'task-key',
        {'domain':'specs','linked':True,'identity_dependency':False,'snapshot_id':'fixture'})
    with sqlite3.connect(store.root/'task-scope-fixture/run.sqlite3') as db:
        db.execute('INSERT INTO metadata VALUES (?,?)',('domain_budget.v1',json.dumps({'stages':{
            'extracting':{task_id:{'scope':task_id,'domain_id':'specs','status':'running','reason':'actual saved task'}}}})))
    view=history.read_v3_run(store.root,'task-scope-fixture')['stage_views'][0]
    assert view['scope_categories']==['linked']
    assert view['domain_id']=='specs'
    assert view['scope_basis_refs']==['tasks/'+task_id]


@pytest.mark.parametrize('keep_exact_requests',[True,False])
def test_overlapping_actual_chunk_roles_never_become_a_unique_claim(tmp_path,keep_exact_requests):
    from tests.test_domain_compare import factory,capture,fact,audit
    ctx=factory.__wrapped__(tmp_path)('overlap-scope-fixture')
    snapshot=capture(ctx)
    audit(ctx,snapshot,[fact(snapshot)],scope='main')
    # This reuses the actual same saved ModelService chunk, and records both
    # coverage roles. A single physical call does not have a unique role here.
    audit(ctx,snapshot,[fact(snapshot)],scope='linked')
    if not keep_exact_requests:
        def remove_request_refs(pages):
            pages['coverage_requests']={}
            return pages,None,None
        ctx['store'].update_metadata(ctx['id'],ctx['gen'],'domain_pages.v1',remove_request_refs)
    before=digest_tree(ctx['store'].root)
    views=history.read_v3_run(ctx['store'].root,ctx['id'])['stage_views']
    assert len(views)==2
    if keep_exact_requests:
        assert all(v['scope_status']=='ambiguous' for v in views)
        assert all(v['scope_categories']==['main','linked'] for v in views)
    else:
        assert all(v['scope_status']=='unresolved' for v in views)
        assert all(v['scope_categories']==[] for v in views)
        assert all(v['possible_scope_categories']==['main','linked'] for v in views)
        assert all(v['scope_basis_refs']==[] for v in views)
    assert digest_tree(ctx['store'].root)==before
