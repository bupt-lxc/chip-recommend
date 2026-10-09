"""V3 tool protocol with real isolated storage and controlled providers/HTTP."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_domain_pages import Clock, Response, Session

ROOT = Path(__file__).parents[1]


def test_v3_service_exists():
    assert importlib.util.find_spec('chip_model.domain_tool_api') is not None


@pytest.fixture
def setup(tmp_path):
    from chip_model.domain_tool_api import ActiveToolContext, DomainToolService
    from chip_model.pipeline.domain_discovery.store import RunStore
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    from chip_model.pipeline.domain_discovery.search import SearchExecutor, ProviderResponse
    from chip_model.pipeline.domain_discovery.pages import PageService
    from chip_model.pipeline.domain_discovery.rules import freeze_bundle
    clock = Clock()
    store = RunStore(tmp_path / 'runs', clock=clock)
    store.start('start', {'domain': 'specs', 'scope_type': 'discovery', 'chip': None}, 'run-a')
    generation = store.claim('run-a', 'worker', 10000)
    budget = RunBudget(store, 'run-a', generation, monotonic=clock, epoch=clock)
    budget.begin_active()
    class Provider:
        name = 'controlled'
        calls = 0
        count = 12
        failure_text = None
        def search(self, query, limit, *, language):
            self.calls += 1
            if query == self.failure_text:
                return ProviderResponse(outcome='parse_error', error='controlled parser failure')
            return ProviderResponse(rows=[
                {'url': f'https://vendor.example/spec/{(i + self.calls * 2) % self.count}', 'title': 'SKU', 'snippet': 'metadata'}
                for i in range(self.count)], outcome='success' if self.count else 'healthy_empty')
    provider = Provider()
    search = SearchExecutor(store, budget, providers=[provider], backoff=lambda _: None)
    session = Session([Response() for _ in range(100)])
    pages = PageService(store, 'run-a', generation, budget, session=session,
                        resolver=lambda _: ['93.184.216.34'], monotonic=clock, backoff=lambda _: None)
    bundle = store.run_path('run-a') / 'skills'
    freeze_bundle(ROOT / '.hermes-skills', bundle)
    context = ActiveToolContext(store, 'run-a', generation, 'worker', budget, search, pages, bundle)
    service = DomainToolService(store, lambda run_id: context if run_id == 'run-a' else None)
    from chip_model.open_web_tool_server import create_open_web_tool_app
    client = TestClient(create_open_web_tool_app(token='test-secret', domain_service=service), client=('127.0.0.1', 50000))
    return client, store, context, provider, session


def request(**kwargs):
    return {'contract_version': 3, 'pipeline': 'hermes-open-web-v3', 'run_id': 'run-a',
            'domain': 'specs', 'scope_type': 'discovery', 'chip': None, **kwargs}


def post(setup, tool, **kwargs):
    return setup[0].post('/v1/' + tool, headers={'Authorization': 'Bearer test-secret'}, json=request(**kwargs))


def plan(round_number=0, count=10):
    return {'round_number': round_number, 'queries': [
        {'query_id': f'q{round_number}-{i}', 'text': f'加速器 文档 {round_number} {i}' if i < 5 else f'GPU docs {round_number} {i}',
         'language': 'zh' if i < 5 else 'en', 'reason': '查找资料', 'page_type': 'datasheet'} for i in range(count)]}


def search(setup):
    response = post(setup, 'search', action='execute', request_id='search', plan=plan())
    assert response.status_code == 200, response.text
    return response.json()


def all_items(setup, version):
    items, cursor = [], None
    while True:
        response = post(setup, 'search', action='results', list_version=version, cursor=cursor).json()
        items.extend(response['items'])
        cursor = response['next_cursor']
        if cursor is None:
            return items


def visited(setup, version):
    cursor, number = None, 0
    while True:
        response = post(setup, 'preview', mode='initial', request_id=f'preview-{number}', list_version=version, cursor=cursor)
        assert response.status_code == 200, response.text
        cursor = response.json()['next_cursor']
        number += 1
        if cursor is None:
            return


def decision(item, choice='select'):
    return {'candidate_id': item['candidate_id'], 'decision': choice, 'reason': '页面支持规格' if choice == 'select' else '无相关证据',
            'expected_info': '显存与功耗' if choice == 'select' else '', 'linked_domains': ['compute'] if choice == 'select' else [],
            'entity_clues': [], 'matched_category': None}


def test_strict_binding_auth_and_legacy_writes(setup):
    client = setup[0]
    assert client.post('/v1/search', json=request(action='results', list_version='x')).status_code == 401
    assert client.post('/v1/search', headers={'Authorization': 'Bearer bad'}, json={}).status_code == 401
    assert post(setup, 'search', action='execute', request_id='x', plan=plan(), generation=1).status_code == 422
    assert post(setup, 'search', action='results', list_version='x', domain='compute').status_code == 409
    assert post(setup, 'search', action='results', list_version='x', scope_type='chip').status_code == 409
    assert post(setup, 'search', action='results', list_version='x', contract_version='3').status_code == 422
    for version in (1, 2, 4):
        assert post(setup, 'search', action='results', list_version='x', contract_version=version).status_code == 409
    assert client.post('/v1/search', headers={'Authorization': 'Bearer test-secret'}, json={'run_id':'legacy','unit_id':'unit'}).status_code == 409


def test_paged_append_finalize_is_durable_and_never_extracts(setup):
    first = search(setup)
    version = first['list_version']
    items = all_items(setup, version)
    assert len(items) > 10
    assert post(setup, 'selection', action='finalize', request_id='too-early', list_version=version).status_code == 409
    assert post(setup, 'selection', action='append', request_id='unvisited', list_version=version, decisions=[decision(items[0])]).status_code == 409
    visited(setup, version)
    for offset in range(0, len(items), 10):
        response = post(setup, 'selection', action='append', request_id=f'append-{offset}', list_version=version,
                        decisions=[decision(i) for i in items[offset:offset+10]])
        assert response.status_code == 200, response.text
        assert len(json.dumps(response.json(), ensure_ascii=False)) <= 12000
    assert post(setup, 'selection', action='append', request_id='duplicate', list_version=version, decisions=[decision(items[0])]).status_code == 200
    assert post(setup, 'selection', action='append', request_id='conflict', list_version=version, decisions=[decision(items[0], 'reject')]).status_code == 409
    result = post(setup, 'selection', action='finalize', request_id='final', list_version=version)
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt['status'] == 'received'
    assert len(receipt['task_ids']) == len(items) * 2
    assert post(setup, 'selection', action='finalize', request_id='final', list_version=version).json() == receipt
    assert post(setup, 'selection', action='append', request_id='locked', list_version=version, decisions=[decision(items[0])]).status_code == 409
    assert post(setup, 'search', action='execute', request_id='supp', plan=plan(1, 1)).status_code == 409
    from chip_model.pipeline.domain_discovery.store import RunStore
    reopened = RunStore(setup[1].root, clock=setup[1].clock)
    tasks = reopened.list_tasks('run-a')
    assert all(t['state'] == 'pending' for t in tasks)
    claimed = reopened.claim_task('run-a', setup[2].generation, receipt['task_ids'][0])
    assert claimed['state'] == 'running'
    assert claimed['result'] is None
    assert len(setup[4].calls) == len(items)


def test_schema_and_frozen_tool_hash_are_concrete(setup):
    from chip_model.domain_tool_api import tool_contract, verify_tool_contract
    artifact = json.loads((ROOT / '.hermes-skills/_shared/tool-contract.json').read_text(encoding='utf-8'))
    assert artifact == tool_contract()
    plugin = ROOT / '.hermes-plugins/aishperf-open-web/tool-contract.json'
    assert json.loads(plugin.read_text(encoding='utf-8')) == artifact
    context = setup[2]
    assert verify_tool_contract(context.bundle_root) == artifact['tool_contract_sha256']
    frozen = context.bundle_root / '_shared/tool-contract.json'
    frozen.write_text('{}')
    assert post(setup, 'search', action='results', list_version='x').status_code == 503


def test_strict_decision_helper():
    from chip_model.pipeline.domain_discovery.contracts import validate_decisions
    selected = decision({'candidate_id': 'a'})
    assert validate_decisions(['a'], ['a'], [selected], True) == [selected]
    omitted = {key: value for key, value in selected.items() if key not in {'entity_clues', 'matched_category'}}
    assert validate_decisions(['a'], ['a'], [omitted], True) == [selected]
    for change in ({'candidate_id':'b'}, {'decision':'maybe'}, {'reason':' '}, {'expected_info':' '}, {'linked_domains':['bad']}):
        with pytest.raises(ValueError):
            validate_decisions(['a'], ['a'], [{**selected, **change}], True)
    with pytest.raises(ValueError):
        validate_decisions(['a'], [], [selected], False)
    with pytest.raises(ValueError):
        validate_decisions(['a'], ['a'], [selected, selected], False)
    with pytest.raises(ValueError, match='未完成'):
        validate_decisions(['a','b'], ['a','b'], [selected], True)


def test_healthy_empty_finalize_records_zero_jobs_without_fake_completion(setup):
    setup[3].count = 0
    result = search(setup)
    assert result['items'] == [] and result['intent_incomplete'] is False
    response = post(setup, 'selection', action='finalize', request_id='empty-final', list_version=result['list_version'])
    assert response.status_code == 200, response.text
    assert response.json()['task_ids'] == []
    assert response.json()['status'] == 'received'
    assert setup[1].get_run('run-a')['status'] != 'completed'
    assert setup[1].events('run-a')[-1]['payload']['job_count'] == 0


@pytest.mark.parametrize('reason_length', [200, 201, 400])
def test_two_step_explicit_intent_closure_does_not_repeat_search(setup, reason_length):
    first_plan = plan()
    setup[3].failure_text = first_plan['queries'][0]['text']
    first = search(setup)
    assert first['intent_incomplete']
    replacement = plan(1, 1)
    replacement['queries'][0]['replaces_query_id'] = 'q0-0'
    second = post(setup, 'search', action='execute', request_id='replacement', plan=replacement)
    assert second.status_code == 200, second.text
    assert second.json()['intent_incomplete']
    calls = setup[3].calls
    closure = {'action': 'complete_intents', 'request_id': 'closure', 'completions': [
        {'failed_query_id': 'q0-0', 'replacement_query_id': 'q1-0', 'reason': '已' * reason_length}]}
    ended = post(setup, 'search', **closure)
    assert ended.status_code == 200, ended.text
    assert post(setup, 'search', **closure).json() == ended.json()
    assert setup[3].calls == calls
    assert post(setup, 'search', **{**closure, 'plan': replacement}).status_code == 422
    from chip_model.pipeline.domain_discovery.search import SearchExecutor
    state = setup[1].metadata('run-a', SearchExecutor.KEY)
    assert SearchExecutor._incomplete(state) is False
    assert state['queries']['q0-0']['outcome'] == 'parse_error'
    assert state['intents']['q0-0']['replacement_query_id'] == 'q1-0'
    assert state['intents']['q0-0']['reason'] == '已' * reason_length


@pytest.mark.parametrize('reason', ['', ' ', 'x' * 401, 'bad\nreason'])
def test_invalid_intent_reason_rejects_replay_without_receipts(setup, reason):
    payload = {'action':'complete_intents', 'request_id':'bad-reason', 'completions':[
        {'failed_query_id':'none', 'replacement_query_id':'none', 'reason':reason}]}
    for _ in range(2):
        assert post(setup, 'search', **payload).status_code == 422
    assert not any(r['request_id'] == 'bad-reason' for r in setup[1].list_operations('run-a'))


@pytest.mark.parametrize('tool,payload', [
    ('search', {'action':'complete_intents', 'request_id':'ended-error', 'completions':[
        {'failed_query_id':'missing','replacement_query_id':'missing','reason':'no linked successful replacement'}]}),
    ('preview', {'mode':'detail','request_id':'ended-error','snapshot_id':'missing'})])
def test_finished_business_error_is_replayed_as_same_failure(setup, tool, payload):
    search(setup)
    first = post(setup, tool, **payload)
    assert first.status_code == 409
    replay = post(setup, tool, **payload)
    assert replay.status_code == first.status_code, replay.text
    assert replay.json() == first.json()
    root = next(r for r in setup[1].list_operations('run-a') if r['request_id'] == 'ended-error')
    assert root['state'] != 'running'


def test_selection_validation_failure_repeats_409_without_partial_receipt(setup):
    version = search(setup)['list_version']
    for action, extra in [('append', {'decisions':[decision({'candidate_id':'invented'}, 'reject')]}), ('finalize', {})]:
        payload = {'action':action, 'request_id':f'bad-{action}', 'list_version':version, **extra}
        first = post(setup, 'selection', **payload)
        assert first.status_code == 409
        assert post(setup, 'selection', **payload).json() == first.json()
        assert post(setup, 'selection', **payload).status_code == 409
        assert not any(r['request_id'] == f'bad-{action}' for r in setup[1].list_operations('run-a'))


def test_unknown_callback_crash_keeps_root_and_child_running_without_reexecution(setup, monkeypatch):
    from chip_model.domain_tool_api import DomainToolService
    class UnknownCrash(BaseException):
        pass
    def crash(*args, **kwargs):
        setup[3].calls += 1
        raise UnknownCrash('unknown physical result')
    monkeypatch.setattr(setup[3], 'search', crash)
    service = DomainToolService(setup[1], lambda _: setup[2])
    payload = request(action='execute', request_id='unknown-root', plan=plan())
    with pytest.raises(UnknownCrash):
        service.handle('search', payload)
    operations = setup[1].list_operations('run-a')
    assert len(operations) == 2
    assert all(op['state'] == 'running' and op['result'] is None for op in operations)
    assert service.handle('search', payload)['status'] == 'in_progress'
    assert setup[3].calls == 1
    assert all(op['state'] == 'running' for op in setup[1].list_operations('run-a'))


def test_expired_fence_error_never_becomes_a_completed_business_failure(setup, monkeypatch):
    from chip_model.domain_tool_api import DomainToolService
    context, store = setup[2], setup[1]
    original = context.search.complete_intent
    def expire_then_execute(*args):
        store.clock.advance(10001)
        return original(*args)
    monkeypatch.setattr(context.search, 'complete_intent', expire_then_execute)
    service = DomainToolService(store, lambda _: context)
    payload = request(action='complete_intents', request_id='expired-root', completions=[
        {'failed_query_id':'missing', 'replacement_query_id':'missing', 'reason':'pure validation'}])
    with pytest.raises(ValueError):
        service.handle('search', payload)
    root = next(op for op in store.list_operations('run-a') if op['request_id'] == 'expired-root')
    assert root['state'] == 'running' and root['result'] is None


def test_intent_completion_before_any_search_is_replayable_409(setup):
    client = TestClient(setup[0].app, client=('127.0.0.1', 50000), raise_server_exceptions=False)
    payload = request(action='complete_intents', request_id='before-search', completions=[
        {'failed_query_id':'missing', 'replacement_query_id':'missing', 'reason':'no saved search yet'}])
    for _ in range(2):
        result = client.post('/v1/search', headers={'Authorization':'Bearer test-secret'}, json=payload)
        assert result.status_code == 409, result.text


def test_business_error_with_unknown_running_child_cannot_complete_or_reset_it(setup):
    from chip_model.domain_tool_api import DomainToolService
    store, ctx = setup[1], setup[2]
    class UnknownCrash(BaseException):
        pass
    def crash():
        raise UnknownCrash('unknown child')
    with pytest.raises(UnknownCrash):
        store.operation('run-a', ctx.generation, 'unknown-child', {'kind':'physical'}, crash)
    service = DomainToolService(store, lambda _: ctx)
    payload = request(action='complete_intents', request_id='blocked-root', completions=[
        {'failed_query_id':'missing', 'replacement_query_id':'missing', 'reason':'no saved replacement'}])
    with pytest.raises(ValueError):
        service.handle('search', payload)
    operations = store.list_operations('run-a')
    assert all(op['state'] == 'running' and op['result'] is None for op in operations)
    assert service.handle('search', payload)['status'] == 'in_progress'
    assert all(op['state'] == 'running' for op in store.list_operations('run-a'))


def test_supplement_retains_visits_decisions_and_old_cursor_is_bound(setup):
    first = search(setup)
    old = first['list_version']
    items = all_items(setup, old)
    visited(setup, old)
    assert post(setup, 'selection', action='append', request_id='first-decision', list_version=old, decisions=[decision(items[0])]).status_code == 200
    calls = len(setup[4].calls)
    setup[3].count = 18
    supplemented = post(setup, 'search', action='execute', request_id='supplement', plan=plan(1, 1))
    assert supplemented.status_code == 200, supplemented.text
    new = supplemented.json()['list_version']
    assert new != old
    assert post(setup, 'search', action='results', list_version=new, cursor=first['next_cursor']).status_code == 409
    assert post(setup, 'selection', action='append', request_id='old-version', list_version=old, decisions=[decision(items[0])]).status_code == 409
    preview = post(setup, 'preview', mode='initial', request_id='new-preview', list_version=new, limit=10)
    assert preview.status_code == 200, preview.text
    assert len(setup[4].calls) == calls
    from chip_model.domain_tool_api import SELECTION_KEY
    assert setup[1].metadata('run-a', SELECTION_KEY)['decisions'][items[0]['candidate_id']] == decision(items[0])


def test_finalize_enqueue_and_lock_rollback_together_on_crash(setup, monkeypatch):
    version = search(setup)['list_version']
    items = all_items(setup, version)
    visited(setup, version)
    for offset in range(0, len(items), 10):
        assert post(setup, 'selection', action='append', request_id=f'save-{offset}', list_version=version, decisions=[decision(i) for i in items[offset:offset+10]]).status_code == 200
    store = setup[1]
    original = store._write_metadata
    def crash(conn, run_id, key, value, event):
        original(conn, run_id, key, value, event)
        raise RuntimeError('controlled crash after tasks and lock, before commit')
    with monkeypatch.context() as patch:
        patch.setattr(store, '_write_metadata', crash)
        with pytest.raises(RuntimeError, match='controlled crash'):
            post(setup, 'selection', action='finalize', request_id='atomic-final', list_version=version)
    from chip_model.domain_tool_api import SELECTION_KEY
    assert store.list_tasks('run-a') == []
    assert store.metadata('run-a', SELECTION_KEY)['finalized'] is False
    assert not any(r['request_id'] == 'atomic-final' for r in store.list_operations('run-a'))
    assert post(setup, 'selection', action='finalize', request_id='atomic-final', list_version=version).status_code == 200


def test_failed_actual_visit_can_reject_but_cannot_select(setup):
    version = search(setup)['list_version']
    item = all_items(setup, version)[0]
    setup[4].responses[0] = Response(status=404)
    response = post(setup, 'preview', mode='initial', request_id='failed-preview', list_version=version, limit=1)
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['visited'] is True
    assert response.json()['items'][0]['extractable'] is False
    assert post(setup, 'selection', action='append', request_id='cannot-select', list_version=version, decisions=[decision(item)]).status_code == 409
    assert post(setup, 'selection', action='append', request_id='can-reject', list_version=version, decisions=[decision(item, 'reject')]).status_code == 200


def test_snapshot_hash_failure_and_cross_candidate_requests_fail_closed(setup):
    version = search(setup)['list_version']
    item = all_items(setup, version)[0]
    assert post(setup, 'preview', mode='initial', request_id='one', list_version=version, limit=1).status_code == 200
    assert post(setup, 'selection', action='append', request_id='invented', list_version=version,
                decisions=[decision({'candidate_id': 'invented'})]).status_code == 409
    assert post(setup, 'selection', action='append', request_id='self-link', list_version=version,
                decisions=[{**decision(item), 'linked_domains': ['specs']}]).status_code == 409
    from chip_model.pipeline.domain_discovery.pages import PageService
    saved = setup[1].metadata('run-a', PageService.KEY)
    snap = saved['snapshots'][saved['visits'][item['candidate_id']]['snapshot_id']]
    setup[2].pages._artifact(snap['text_ref']).write_bytes(b'corrupted')
    assert post(setup, 'selection', action='append', request_id='corrupt', list_version=version, decisions=[decision(item)]).status_code == 409
    assert setup[1].list_tasks('run-a') == []


def test_auth_context_loopback_lease_and_unconfigured_service(setup):
    from chip_model.open_web_tool_server import create_open_web_tool_app
    app = create_open_web_tool_app(token='test-secret')
    local = TestClient(app, client=('127.0.0.1', 50000))
    remote = TestClient(app, client=('10.0.0.2', 50000))
    body = request(action='results', list_version='none')
    assert local.post('/v1/search', headers={'Authorization': 'Bearer test-secret'}, json=body).status_code == 503
    assert remote.post('/v1/search', headers={'Authorization': 'Bearer test-secret'}, json=body).status_code == 403
    assert post(setup, 'search', action='results', list_version='none', run_id='../escape').status_code == 422
    setup[2].generation += 1
    assert post(setup, 'search', action='results', list_version='none').status_code == 409


def test_detail_budget_same_payload_replay_conflict_and_finalized_cached_reads(setup):
    version = search(setup)['list_version']
    visited(setup, version)
    items = all_items(setup, version)
    from chip_model.pipeline.domain_discovery.pages import PageService
    pages = setup[1].metadata('run-a', PageService.KEY)
    snapshot_id = pages['visits'][items[0]['candidate_id']]['snapshot_id']
    first = post(setup, 'preview', mode='detail', request_id='detail', snapshot_id=snapshot_id, offset=0, length=30)
    assert first.status_code == 200
    assert post(setup, 'preview', mode='detail', request_id='detail', snapshot_id=snapshot_id, offset=0, length=30).json() == first.json()
    assert post(setup, 'preview', mode='detail', request_id='detail', snapshot_id=snapshot_id, offset=1, length=30).status_code == 409
    for i in range(2):
        assert post(setup, 'preview', mode='detail', request_id=f'detail-{i}', snapshot_id=snapshot_id, offset=i, length=30).status_code == 200
    assert post(setup, 'preview', mode='detail', request_id='fourth', snapshot_id=snapshot_id, offset=3, length=30).status_code == 409
    for offset in range(0, len(items), 10):
        assert post(setup, 'selection', action='append', request_id=f'reject-{offset}', list_version=version, decisions=[decision(i, 'reject') for i in items[offset:offset+10]]).status_code == 200
    assert post(setup, 'selection', action='finalize', request_id='lock-final', list_version=version).status_code == 200
    calls = len(setup[4].calls)
    assert post(setup, 'preview', mode='initial', request_id='cached-after-lock', list_version=version).status_code == 200
    assert post(setup, 'search', action='results', list_version=version).status_code == 200
    assert post(setup, 'search', action='complete_intents', request_id='locked-closure', completions=[
        {'failed_query_id':'x','replacement_query_id':'y','reason':'explicit'}]).status_code == 409
    assert len(setup[4].calls) == calls


def test_plugin_handlers_reach_real_v3_service_with_exported_arguments(setup, monkeypatch):
    """Controlled transport proves plumbing, not a live Hermes/model load."""
    from tests.test_hermes_open_web_plugin import _module, FakeContext
    module, registry = _module(), FakeContext()
    module.register(registry)
    def transport(path, payload):
        response = setup[0].post(path, headers={'Authorization':'Bearer test-secret'}, json=payload)
        assert response.status_code == 200, response.text
        return response.json()
    monkeypatch.setattr(module, '_post', transport)
    result = json.loads(registry.tools['open_web_search']['handler'](request(action='execute', request_id='via-plugin', plan=plan())))
    version = result['list_version']
    cursor, index = None, 0
    while True:
        preview = json.loads(registry.tools['open_web_preview']['handler'](request(
            mode='initial', request_id=f'plugin-preview-{index}', list_version=version, cursor=cursor)))
        selected = json.loads(registry.tools['open_web_submit_selection']['handler'](request(
            action='append', request_id=f'plugin-append-{index}', list_version=version,
            decisions=[decision(item, 'reject') for item in preview['items']])))
        assert selected['status'] == 'received_append'
        cursor, index = preview['next_cursor'], index + 1
        if cursor is None:
            break
    final = json.loads(registry.tools['open_web_submit_selection']['handler'](request(
        action='finalize', request_id='plugin-final', list_version=version)))
    assert final['task_ids'] == [] and final['status'] == 'received'
    assert all(t['schema']['strict'] is False for t in registry.tools.values())


def test_http_schema_is_discriminated_and_get_reads_real_state_without_lease(setup):
    schema = setup[0].app.openapi()
    assert schema['paths']['/v1/search']['post']['requestBody']['content']['application/json']['schema']['discriminator']['propertyName'] == 'action'
    assert '/v1/submit-selection' not in schema['paths']
    setup[1].release('run-a', setup[2].generation)
    result = setup[0].get('/v1/runs/run-a?contract_version=3', headers={'Authorization':'Bearer test-secret'})
    assert result.status_code == 200, result.text
    assert result.json()['read_only'] is True
    assert result.json()['current_stage'] == 'preparing'
    assert result.json()['task_counts'] == {}


def test_startup_schema_copy_mismatch_fails_before_context_or_model_calls(tmp_path):
    import shutil
    from chip_model.domain_tool_api import DomainToolService
    for relative in ('.hermes-skills/_shared/tool-contract.json', '.agents/skills/_shared/tool-contract.json',
                     '.hermes-plugins/aishperf-open-web/tool-contract.json', '.hermes-plugins/aishperf-open-web/plugin.yaml'):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    (tmp_path / '.hermes-plugins/aishperf-open-web/tool-contract.json').write_text('{}')
    with pytest.raises(ValueError, match='mismatch'):
        DomainToolService(None, lambda _: pytest.fail('context resolved before schema proof'), project_root=tmp_path)


def test_finalize_cannot_lock_old_list_while_supplement_is_unpublished(setup):
    version = search(setup)['list_version']
    items = all_items(setup, version)
    visited(setup, version)
    for offset in range(0, len(items), 10):
        assert post(setup, 'selection', action='append', request_id=f'pre-supp-{offset}', list_version=version,
                    decisions=[decision(i, 'reject') for i in items[offset:offset+10]]).status_code == 200
    from chip_model.pipeline.domain_discovery.search import SearchExecutor
    # The exact concurrent window after query outcomes succeeded, before publish.
    # A backend executor may share the fenced generation without the HTTP lock.
    def underway(state):
        state['plans']['1'] = {'request_id':'inflight', 'model_payload':plan(1, 1), 'providers':['controlled'], 'list_version':None}
        return state, None, None
    setup[1].update_metadata('run-a', setup[2].generation, SearchExecutor.KEY, underway)
    result = post(setup, 'selection', action='finalize', request_id='old-final', list_version=version)
    assert result.status_code == 409, result.text
    assert setup[1].list_tasks('run-a') == []
    from chip_model.domain_tool_api import SELECTION_KEY
    assert setup[1].metadata('run-a', SELECTION_KEY)['finalized'] is False


def test_runtime_resolver_cannot_bind_another_runs_context(setup):
    from chip_model.domain_tool_api import DomainToolService
    from chip_model.open_web_tool_server import create_open_web_tool_app
    version = search(setup)['list_version']
    setup[1].start('second-start', {'domain':'specs','scope_type':'discovery','chip':None}, 'run-b')
    service = DomainToolService(setup[1], lambda _: setup[2])
    client = TestClient(create_open_web_tool_app(token='test-secret', domain_service=service), client=('127.0.0.1',50000))
    result = client.post('/v1/search', headers={'Authorization':'Bearer test-secret'},
                         json=request(action='results', list_version=version, run_id='run-b'))
    assert result.status_code == 409, result.text


def test_worst_case_600_job_receipt_is_complete_bounded_and_replay_stable(setup):
    """Pure store transaction proof; does not fake 100 visited runtime pages."""
    store, context = setup[1], setup[2]
    jobs = [(f'job-{i}-{domain}', {'url':f'https://vendor.example/{i}', 'domain':domain})
            for i in range(100) for domain in ('identity','specs','compute','compatibility','benchmark','deployment')]
    def prepare(values):
        return {'receipt-proof': {'finalized':True}}, jobs, {'status':'received', 'current_stage':'selecting', 'task_ids':[]}, None
    first = store.commit_tool_request('run-a', context.generation, 'worst', {'kind':'bounded-proof'}, ['receipt-proof'], prepare)
    assert len(first['task_ids']) == 600
    assert len(set(first['task_ids'])) == 600
    assert len(json.dumps(first, ensure_ascii=False)) <= 12000
    second = store.commit_tool_request('run-a', context.generation, 'worst', {'kind':'bounded-proof'}, ['receipt-proof'],
                                       lambda _: pytest.fail('cached receipt callback executed'))
    assert second == first
    assert len(store.list_tasks('run-a')) == 600


def test_unverified_entity_category_hints_survive_decision_and_job_payload(setup):
    version = search(setup)['list_version']
    items = all_items(setup, version)
    visited(setup, version)
    hint = {**decision(items[0]), 'entity_clues':['Vendor X100-A 24GB PCIe'], 'matched_category':'specs'}
    decisions = [hint, *[decision(item, 'reject') for item in items[1:]]]
    for offset in range(0, len(items), 10):
        result = post(setup, 'selection', action='append', request_id=f'hints-{offset}', list_version=version, decisions=decisions[offset:offset+10])
        assert result.status_code == 200, result.text
    assert post(setup, 'selection', action='finalize', request_id='hints-final', list_version=version).status_code == 200
    jobs = setup[1].list_tasks('run-a')
    assert len(jobs) == 2
    for job in jobs:
        assert job['payload']['entity_clues'] == hint['entity_clues']
        assert job['payload']['matched_category'] == 'specs'
        assert job['payload']['hints_verified'] is False
    event = [e['payload'] for e in setup[1].events('run-a') if e['payload']['kind'] == 'selection_appended'][0]
    assert event['decisions'][0] == hint


@pytest.mark.parametrize('hint', [{'entity_clues':['x'] * 11}, {'entity_clues':['x' * 161]},
                                 {'entity_clues':[' ']}, {'matched_category':'price'}])
def test_hint_boundaries_are_strict_before_store_mutation(setup, hint):
    version = search(setup)['list_version']
    item = all_items(setup, version)[0]
    result = post(setup, 'selection', action='append', request_id='bad-hint', list_version=version, decisions=[{**decision(item), **hint}])
    assert result.status_code == 422, result.text


def test_same_search_request_resumes_pending_inner_operation_after_explicit_recovery(setup):
    from chip_model.pipeline.domain_discovery.store import _hash, _json
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    from chip_model.pipeline.domain_discovery.search import SearchExecutor
    from chip_model.pipeline.domain_discovery.pages import PageService
    ctx, store = setup[2], setup[1]
    query = plan()['queries'][0]
    logical = 'search-' + _hash(_json([query['query_id'], 'controlled']))
    class Crash(BaseException):
        pass
    def crash():
        raise Crash('controlled unknown physical operation')
    with pytest.raises(Crash):
        ctx.budget.operation(logical, {'stage':'searching','query':query,'provider':'controlled'}, crash)
    first = post(setup, 'search', action='execute', request_id='resumable', plan=plan())
    assert first.status_code == 200 and first.json()['status'] == 'in_progress'
    assert setup[3].calls == 0
    again = post(setup, 'search', action='execute', request_id='resumable', plan=plan())
    assert again.json()['status'] == 'in_progress'
    assert setup[3].calls == 0
    store.release('run-a', ctx.generation)
    ctx.generation = store.claim('run-a', 'restarted', 10000)
    ctx.owner = 'restarted'
    ctx.budget = RunBudget(store, 'run-a', ctx.generation, monotonic=store.clock, epoch=store.clock)
    ctx.budget.begin_active()
    ctx.search = SearchExecutor(store, ctx.budget, providers=[setup[3]], backoff=lambda _: None)
    ctx.pages = PageService(store, 'run-a', ctx.generation, ctx.budget, session=setup[4],
                           resolver=lambda _: ['93.184.216.34'], monotonic=store.clock, backoff=lambda _: None)
    assert store.recover_operations('run-a', ctx.generation) >= 1
    resumed = post(setup, 'search', action='execute', request_id='resumable', plan=plan())
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()['status'] == 'completed', resumed.text
    assert resumed.json()['list_version']
    assert setup[3].calls == 10
    assert ctx.budget.snapshot()['query_count'] == 10
    assert post(setup, 'search', action='execute', request_id='resumable', plan=plan()).json() == resumed.json()
    changed = plan()
    changed['queries'][0]['reason'] = 'changed payload'
    assert post(setup, 'search', action='execute', request_id='resumable', plan=changed).status_code == 409
    assert setup[3].calls == 10
