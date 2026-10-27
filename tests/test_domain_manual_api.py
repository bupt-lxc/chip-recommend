import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from chip_model.database import get_db_path, set_db_path
from chip_model.server import app
from chip_model.pipeline.domain_discovery.store import RunStore


@pytest.fixture
def client(tmp_path):
    source = tmp_path / 'data.db'
    with sqlite3.connect(source) as db:
        db.executescript((Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8'))
    old = get_db_path()
    set_db_path(source)
    token = tmp_path / 'manual_control/admin_token'
    token.parent.mkdir()
    token.write_text('fixture-token', encoding='utf-8')
    try:
        yield TestClient(app), {'X-Data-Agent-Admin': 'fixture-token'}, source
    finally:
        set_db_path(old)


def test_v3_api_durable_binding_and_conflict(client):
    api, headers, source = client
    payload = dict(mode='test', pipeline='open_web', domain='specs', request_id=' client-001 ')
    first = api.post('/api/v1/data-agent/manual-run', headers=headers, json=payload)
    assert first.status_code == 202, first.text
    second = TestClient(app).post('/api/v1/data-agent/manual-run', headers=headers, json=payload)
    assert second.status_code == 202
    assert first.json()['run_id'] == second.json()['run_id'] == first.json()['session_id']
    assert first.json()['schema_version'] == 'hermes-open-web-v3'
    assert first.json()['request_id'] == 'client-001'
    store = RunStore(source.parent / 'domain_runs')
    run = store.get_run(first.json()['run_id'])
    assert run['status'] == run['stage'] == 'preparing'
    assert run['payload'] == dict(domain='specs', chip=None, scope_type='discovery', resume_run_id=None)
    conflict = api.post('/api/v1/data-agent/manual-run', headers=headers, json={**payload, 'domain': 'compute'})
    assert conflict.status_code == 409, conflict.text


def test_v3_auth_precedes_body_validation(client):
    api, _, source = client
    response = api.post('/api/v1/data-agent/manual-run', json={'pipeline': 'open_web', 'domain': 'bad'})
    assert response.status_code == 401
    assert not (source.parent / 'domain_runs').exists()


@pytest.mark.parametrize('extra', [dict(mode='formal'), dict(queries=[]), dict(target_fields=['vram_gb']),
    dict(chips=['A','B']), dict(skill='chip-compute'), dict(limit=5), dict(domain='bad'),
    dict(pipeline='known_sources')])
def test_v3_bad_public_inputs_return_400(client, extra):
    api, headers, _ = client
    response = api.post('/api/v1/data-agent/manual-run', headers=headers,
        json={**dict(mode='test', pipeline='open_web', domain='specs'), **extra})
    assert response.status_code == 400, response.text


def test_header_key_normalization_and_generated_key_notice(client):
    api, headers, _ = client
    body = dict(mode='test', pipeline='open_web', domain='specs')
    mismatch = api.post('/api/v1/data-agent/manual-run', headers={**headers,'Idempotency-Key':'other'},
        json={**body,'request_id':'body'})
    assert mismatch.status_code == 400
    first = api.post('/api/v1/data-agent/manual-run', headers={**headers,'Idempotency-Key':' header-key '}, json=body)
    assert first.status_code == 202 and first.json()['request_id'] == 'header-key'
    generated = api.post('/api/v1/data-agent/manual-run', headers=headers, json=body)
    assert generated.status_code == 202
    assert generated.json()['idempotency_key_generated'] is True
    assert 'retry' in generated.json()['idempotency_notice'].lower()

def test_resume_key_replay_does_not_requeue_and_semantic_conflicts_409(client):
    api, headers, source = client
    first = api.post('/api/v1/data-agent/manual-run', headers=headers,
        json=dict(domain='specs',chip='Original X',request_id='first')).json()
    store = RunStore(source.parent / 'domain_runs')
    generation = store.claim(first['run_id'],'fixture',5)
    store.finish_run(first['run_id'],generation,{'status':'interrupted'})
    payload = dict(domain='specs',resume_run_id=first['run_id'],request_id='resume')
    receipt = api.post('/api/v1/data-agent/manual-run',headers=headers,json=payload)
    assert receipt.status_code == 202
    generation = store.claim(first['run_id'],'fixture-two',5)
    store.finish_run(first['run_id'],generation,{'status':'no_findings'})
    assert api.post('/api/v1/data-agent/manual-run',headers=headers,json=payload).json()['status'] == 'no_findings'
    assert store.dispatchable_runs() == []
    for other in (dict(domain='compute'),dict(chip='Other'),dict(resume_run_id=None)):
        assert api.post('/api/v1/data-agent/manual-run',headers=headers,json={**payload,**other}).status_code == 409


@pytest.mark.parametrize('body', [dict(mode='test',queries=[]),dict(mode='test',target_fields=[]),
    dict(mode='test',chip='A'),dict(mode='test',chips=['A'])])
def test_v3_parameters_never_fall_into_legacy(client, body):
    api, headers, _ = client
    assert api.post('/api/v1/data-agent/manual-run',headers=headers,json=body).status_code == 400

def test_api_storage_failure_returns_503(client, monkeypatch):
    api, headers, _ = client
    from chip_model.pipeline.domain_discovery import store
    def unavailable(*args,**kwargs):
        raise OSError('fixture shared volume unavailable')
    monkeypatch.setattr(store.RunStore,'start',unavailable)
    result = api.post('/api/v1/data-agent/manual-run',headers=headers,json={'domain':'specs','request_id':'unavailable'})
    assert result.status_code == 503 and 'fixture shared volume unavailable' in result.text

@pytest.mark.parametrize('marker',[{'scope_type':'discovery'}, {'schema_version':'hermes-open-web-v3'}, {'contract_version':3}])
def test_contract_markers_cannot_enter_legacy_queue(client,marker):
    api,headers,source=client
    body={'mode':'test',**marker}
    assert api.post('/api/v1/data-agent/manual-run',json=body).status_code == 401
    assert api.post('/api/v1/data-agent/manual-run',headers=headers,json=body).status_code == 400
    assert not list((source.parent / 'manual_control/requests').glob('*.json'))


def test_api_persists_configured_nonstandard_source_for_host(client,monkeypatch):
    api,headers,default=client
    chosen=default.parent / 'chosen.db'
    with sqlite3.connect(chosen) as db:
        db.execute('CREATE TABLE marker (value TEXT)')
        db.execute("INSERT INTO marker VALUES ('chosen-api')")
    set_db_path(chosen)
    result=api.post('/api/v1/data-agent/manual-run',headers=headers,json={'domain':'specs','request_id':'chosen-api'})
    assert result.status_code == 202
    store=RunStore(default.parent / 'domain_runs')
    assert store.dispatch_request(result.json()['run_id'])['source_locator'] == 'chosen.db'
    assert 'source_locator' not in store.get_run(result.json()['run_id'])['payload']
    from scripts import hermes_domain_worker as worker
    seen=[]
    def execute(request,deps):
        with sqlite3.connect(deps['source_db'].as_uri()+'?mode=ro',uri=True) as db:
            seen.append(db.execute('SELECT value FROM marker').fetchone()[0])
        out={'status':'no_findings'}
        deps['store'].finish_run(deps['preclaimed']['run_id'],deps['preclaimed']['generation'],out)
        return out
    worker.work_once(default.parent,executor=execute)
    assert seen == ['chosen-api']

@pytest.mark.parametrize('key',['source_locator','source_db','data_root'])
def test_internal_source_fields_are_not_public_api_parameters(client,key):
    api,headers,source=client
    response=api.post('/api/v1/data-agent/manual-run',headers=headers,json={'mode':'test','domain':'specs',key:'arbitrary-path'})
    assert response.status_code == 400
    assert not list((source.parent / 'manual_control/requests').glob('*.json'))


def test_api_binding_does_not_wait_for_exclusive_source_lock(client):
    import time
    api,headers,source=client
    blocker=sqlite3.connect(source,isolation_level=None)
    blocker.execute('BEGIN EXCLUSIVE')
    try:
        started=time.monotonic()
        response=api.post('/api/v1/data-agent/manual-run',headers=headers,json={'domain':'specs','request_id':'locked-api'})
        assert response.status_code==202,response.text
        assert time.monotonic()-started < 1.5
    finally:
        blocker.rollback()
        blocker.close()
