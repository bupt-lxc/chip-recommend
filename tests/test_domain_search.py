"""Hermes plans, instrumented providers and durable pagination, without network."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest


def test_search_module_exists():
    assert importlib.util.find_spec('chip_model.pipeline.domain_discovery.search') is not None


def plan(round_number=0, count=10, prefix='q'):
    return {'round_number': round_number, 'queries': [
        {'query_id': f'{prefix}{i}', 'text': f'新型加速芯片 产品 {prefix}{i}' if i < 4 else f'new accelerator catalog {prefix}{i}',
         'language': 'zh' if i < 4 else 'en', 'reason': '发现新芯片', 'page_type': 'product'}
        for i in range(count)]}


class Provider:
    name = 'fixture'

    def __init__(self, outcome='success', count=2, shared=False):
        self.outcome, self.count, self.shared = outcome, count, shared
        self.calls = []

    def search(self, query, limit, *, language):
        from chip_model.pipeline.domain_discovery.search import ProviderResponse
        self.calls.append((query, limit, language))
        n = len(self.calls)
        rows = [{'url': f'https://vendor.example/{0 if self.shared else n}/{i}?sku=MLU370-S4&utm_source=test',
                 'title': '未知型号新品 产品', 'snippet': '尚未进入数据库', 'rank': i + 1}
                for i in range(self.count)] if self.outcome == 'success' else []
        return ProviderResponse(rows=rows, outcome=self.outcome, http_status=200 if self.outcome in {'success', 'healthy_empty'} else None,
                                endpoint='https://search.example/', locale=language)


@pytest.fixture
def setup(tmp_path):
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    from chip_model.pipeline.domain_discovery.store import RunStore
    store = RunStore(tmp_path / 'runs')
    store.start('start', {'domain': 'specs', 'scope_type': 'discovery', 'chip': None}, 'run-a')
    generation = store.claim('run-a', 'worker', 100000)
    budget = RunBudget(store, 'run-a', generation)
    return store, budget


def executor(setup, *providers):
    from chip_model.pipeline.domain_discovery.search import SearchExecutor
    store, budget = setup
    return SearchExecutor(store, budget, providers=providers or (Provider(),), backoff=lambda _: None)


def test_pagination_returns_every_complete_item_once_and_replay_never_searches(setup):
    provider = Provider(count=3)
    search = executor(setup, provider)
    first = search.execute('initial', plan())
    assert first['total'] == 30
    assert first['query_count'] == first['round_query_count'] == 10
    version = first['list_version']
    pages, cursor = [first], first['next_cursor']
    while cursor:
        page = search.results(version, cursor=cursor)
        pages.append(page)
        cursor = page['next_cursor']
    items = [item for page in pages for item in page['items']]
    assert len(items) == len({item['candidate_id'] for item in items}) == 30
    assert all(len(page['items']) <= 10 for page in pages)
    assert all(len(json.dumps(page, ensure_ascii=False)) <= 12000 for page in pages)
    assert all('sku=MLU370-S4' in item['url'] for item in items)
    assert all('utm_source' not in item['url'] for item in items)
    assert search.results(version, offset=10) == pages[1]
    assert executor(setup, provider).execute('initial', plan()) == first
    assert len(provider.calls) == 10
    assert [call[0] for call in provider.calls] == [q['text'] for q in plan()['queries']]
    state = setup[0].metadata('run-a', search.KEY)
    assert state['plans']['0']['model_payload'] == plan()
    assert len(state['queries']['q0']['attempts'][0]['raw_hits']) == 3


@pytest.mark.parametrize('change', [
    lambda p: p.update(target_fields=['vram_gb']),
    lambda p: p.update(round_number=True),
    lambda p: p['queries'].pop(),
    lambda p: p['queries'][0].update(text='x' * 121),
    lambda p: p['queries'][0].update(text=123),
    lambda p: p['queries'][0].update(language='fr'),
    lambda p: p['queries'][0].update(reason=''),
    lambda p: p['queries'][0].update(page_type=['product']),
    lambda p: p['queries'][0].update(query_id=p['queries'][1]['query_id']),
    lambda p: p['queries'][0].update(text=p['queries'][1]['text'].upper()),
    lambda p: [q.update(language='en') for q in p['queries']],
])
def test_strict_plan_rejection_precedes_budget_or_provider(setup, change):
    provider = Provider()
    search = executor(setup, provider)
    payload = plan()
    change(payload)
    before = setup[1].snapshot()
    with pytest.raises(ValueError):
        search.execute('bad', payload)
    assert setup[1].snapshot() == before
    assert provider.calls == []


def test_canonical_merge_preserves_all_provider_and_query_references(setup):
    left, right = Provider(shared=True), Provider(shared=True)
    right.name = 'second'
    result = executor(setup, left, right).execute('initial', plan())
    assert result['total'] == 2
    assert len(result['items'][0]['references']) == 20
    assert {ref['query_id'] for ref in result['items'][0]['references']} == {f'q{i}' for i in range(10)}
    assert {ref['provider'] for ref in result['items'][0]['references']} == {'fixture', 'second'}


@pytest.mark.parametrize('outcome', ['healthy_empty', 'network_error', 'http_error', 'parse_error'])
def test_healthy_empty_and_technical_failure_are_distinct_and_retry_bounded(setup, outcome):
    provider = Provider(outcome=outcome)
    search = executor(setup, provider)
    result = search.execute('initial', plan())
    assert result['total'] == 0
    queries = setup[0].metadata('run-a', search.KEY)['queries']
    assert {q['outcome'] for q in queries.values()} == {outcome}
    assert len(provider.calls) == (10 if outcome == 'healthy_empty' else 30)
    assert result['incomplete'] is (outcome != 'healthy_empty')
    assert all(len(q['attempts']) == (1 if outcome == 'healthy_empty' else 3) for q in queries.values())


def test_source_failure_is_retained_with_healthy_fallback_and_language_success(setup):
    class Mixed(Provider):
        name = 'mixed'

        def search(self, query, limit, *, language):
            self.outcome = 'network_error' if language == 'en' else 'success'
            return super().search(query, limit, language=language)

    search = executor(setup, Mixed(), Provider(outcome='healthy_empty'))
    result = search.execute('initial', plan())
    assert not result['incomplete']
    assert any(d['language'] == 'en' and d['outcome'] == 'network_error' for d in result['diagnostics'])
    assert any(d['language'] == 'zh' and d['outcome'] == 'success' for d in result['diagnostics'])
    attempts = setup[0].metadata('run-a', search.KEY)['queries']['q4']['attempts']
    assert attempts[0]['diagnostic']['outcome'] == 'network_error'
    assert all(a['diagnostic']['query_outcome'] == 'healthy_empty' for a in attempts)


def test_main_and_per_query_exclusions_remain_auditable_and_versions_immutable(setup):
    provider = Provider(count=12)
    search = executor(setup, provider)
    first = search.execute('initial', plan())
    assert first['total'] == 100
    initial_page = search.results(first['list_version'])
    second = search.execute('supplement', plan(1, 5, 's'))
    assert second['total'] == 100
    assert search.results(first['list_version']) == initial_page
    state = setup[0].metadata('run-a', search.KEY)
    reasons = {e['reason'] for q in state['queries'].values() for e in q['exclusions']}
    assert {'per_query_budget', 'main_candidate_budget'} <= reasons
    assert setup[1].snapshot()['query_count'] == 15


def test_supplements_use_persisted_cumulative_budget_and_reject_duplicates(setup):
    search = executor(setup, Provider(outcome='healthy_empty'))
    search.execute('initial', plan())
    duplicate = plan(1, 1, 's')
    duplicate['queries'][0]['text'] = plan()['queries'][0]['text']
    with pytest.raises(ValueError, match='duplicate'):
        search.execute('duplicate', duplicate)
    for round_number in (1, 2):
        payload = plan(round_number, 5, f's{round_number}')
        for q in payload['queries']:
            q['text'] += f' round {round_number}'
        executor(setup, Provider(outcome='healthy_empty')).execute(str(round_number), payload)
    with pytest.raises(ValueError):
        search.execute('third', plan(3, 1, 'last'))
    assert setup[1].snapshot()['query_count'] == 20


def test_replacement_needs_explicit_valid_intent_completion_and_keeps_failures(setup):
    search = executor(setup, Provider(outcome='network_error'))
    first = search.execute('initial', plan())
    assert first['incomplete']
    replacement = plan(1, 1, 'repair')
    replacement['queries'][0].update(text='新的厂商芯片技术文档', replaces_query_id='q0')
    search = executor(setup, Provider())
    repaired = search.execute('repair', replacement)
    assert repaired['incomplete']
    with pytest.raises(ValueError):
        search.complete_intent('invalid', 'q1', 'repair0', '已完成替代搜索')
    search.complete_intent('done', 'q0', 'repair0', '已完成替代搜索')
    state = setup[0].metadata('run-a', search.KEY)
    assert state['queries']['q0']['outcome'] == 'network_error'
    assert state['intents']['q0']['replacement_query_id'] == 'repair0'
    assert len(state['queries']['q0']['attempts']) == 3
    assert search.complete_intent('done', 'q0', 'repair0', '已完成替代搜索') == state['intents']['q0']


def test_oversize_single_item_is_explicit_failure_with_no_stuck_cursor(setup):
    class Huge(Provider):
        def search(self, query, limit, *, language):
            response = super().search(query, limit, language=language)
            response.rows[0]['snippet'] = '中' * 13000
            return response

    search = executor(setup, Huge(count=1))
    with pytest.raises(ValueError, match='item_too_large'):
        search.execute('initial', plan())
    state = setup[0].metadata('run-a', search.KEY)
    version = state['plans']['0']['list_version']
    assert len(state['versions'][version]['items']) == 10
    with pytest.raises(ValueError, match='item_too_large'):
        search.results(version)


def test_cursor_version_offset_and_types_are_validated(setup):
    search = executor(setup)
    result = search.execute('initial', plan())
    for kwargs in ({'offset': True}, {'offset': -1}, {'offset': 21}, {'cursor': 'bad'},
                   {'cursor': 'another:10'}, {'cursor': result['next_cursor'], 'offset': 1}):
        with pytest.raises(ValueError):
            search.results(result['list_version'], **kwargs)
    with pytest.raises(ValueError):
        search.results('unknown')


def test_optional_hint_matches_full_sku_without_implicit_model_token_filter(setup):
    class SKU(Provider):
        def search(self, query, limit, *, language):
            response = super().search(query, limit, language=language)
            response.rows = [{'url': f'https://vendor.example/{sku}', 'title': sku} for sku in ('MLU370-S4', 'MLU370-S8', 'NewUnknown-9')]
            return response

    open_result = executor(setup, SKU()).execute('initial', plan())
    assert open_result['total'] == 3
    # A separate run uses an explicit trusted chip hint; tool payload cannot set it.
    store = setup[0]
    store.start('other', {'domain': 'specs', 'chip': 'MLU370-S4'}, 'run-b')
    generation = store.claim('run-b', 'other', 100000)
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    hinted = executor((store, RunBudget(store, 'run-b', generation)), SKU()).execute('initial', plan())
    assert hinted['total'] == 1
    assert hinted['items'][0]['title'] == 'MLU370-S4'


def test_concurrent_same_plan_does_not_duplicate_network_or_budget(setup):
    entered, release = Event(), Event()

    class Waiting(Provider):
        def search(self, query, limit, *, language):
            if not self.calls:
                entered.set()
                assert release.wait(10)
            return super().search(query, limit, language=language)

    provider = Waiting()
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(executor(setup, provider).execute, 'initial', plan())
        assert entered.wait(10)
        simultaneous = executor(setup, provider).execute('initial', plan())
        assert simultaneous['status'] == 'in_progress'
        release.set()
        complete = worker.result(timeout=10)
    assert len(provider.calls) == 10
    assert executor(setup, provider).execute('initial', plan()) == complete
    assert setup[1].snapshot()['query_count'] == 10


def test_html_adapter_records_actual_locale_empty_challenge_and_transport(setup):
    from chip_model.pipeline.domain_discovery.search import HtmlSearchProvider
    from chip_model.pipeline.open_web_test import BingHtmlSearch

    class Session:
        def __init__(self, text, status=200):
            self.text, self.status, self.calls = text, status, []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return type('Response', (), {'text': self.text, 'status_code': self.status})()

    raw = BingHtmlSearch(proxy='http://user:secret@localhost:7897')
    adapter = HtmlSearchProvider(raw)
    raw.session = Session('<div id="b_results"><li class="b_algo"><h2><a href="https://vendor.example/new?sku=S4">新品</a></h2></li></div>')
    response = adapter.search('new accelerator', 10, language='en')
    assert response.outcome == 'success'
    assert raw.session.calls[0][0] == 'https://www.bing.com/search'
    assert raw.session.calls[0][1]['params']['q'] == 'new accelerator'
    assert raw.session.calls[0][1]['params']['setlang'] == 'en'
    assert raw.session.calls[0][1]['headers']['Accept-Language'].startswith('en')
    raw.session = Session('<div id="b_results">没有找到相关结果</div>')
    assert adapter.search('中文 芯片', 10, language='zh').outcome == 'healthy_empty'
    assert raw.session.calls[0][0] == 'https://cn.bing.com/search'
    raw.session = Session('<html>captcha verify you are human</html>')
    assert adapter.search('probe', 10, language='en').outcome == 'parse_error'
    raw.session = Session('<html>unrecognized page</html>')
    assert adapter.search('probe', 10, language='en').outcome == 'parse_error'
    raw.session = Session('', 503)
    assert adapter.search('probe', 10, language='en').outcome == 'http_error'


def test_diagnostics_redact_secrets_and_unsafe_url_is_raw_only(setup):
    from chip_model.pipeline.domain_discovery.search import ProviderResponse

    class Unsafe(Provider):
        def search(self, query, limit, *, language):
            self.calls.append(query)
            return ProviderResponse(rows=[{'url': 'http://127.0.0.1/private', 'title': 'unsafe'},
                                          {'url': 'https://vendor.example/new?api_key=secret', 'title': 'credential'}],
                                    outcome='success', error='proxy http://user:password@localhost:7897 token=SECRET',
                                    endpoint='https://search.example/?api_key=SECRET')

    result = executor(setup, Unsafe()).execute('initial', plan())
    assert result['total'] == 0
    serialized = json.dumps(setup[0].metadata('run-a', 'domain_search.v1'), ensure_ascii=False)
    assert 'SECRET' not in serialized and 'password' not in serialized and 'api_key=secret' not in serialized
    assert result['diagnostics'][0]['valid_url_count'] == 0


def test_query_payload_conflict_and_new_client_id_cannot_repeat_round(setup):
    search = executor(setup)
    first = search.execute('initial', plan())
    changed = plan()
    changed['queries'][0]['text'] = '不同搜索词'
    for request_id, payload in [('initial', changed), ('new-client', plan())]:
        with pytest.raises(ValueError, match='conflict'):
            search.execute(request_id, payload)
    assert search.results(first['list_version'])['total'] == 20
    assert setup[1].snapshot()['query_count'] == 10


def test_resume_after_crash_requires_recovery_and_inherits_physical_attempts(setup):
    class Crash(Provider):
        def search(self, query, limit, *, language):
            self.calls.append(query)
            raise KeyboardInterrupt('process interrupted')

    store, budget = setup
    crash = Crash()
    with pytest.raises(KeyboardInterrupt):
        executor(setup, crash).execute('initial', plan())
    store.release('run-a', budget.generation)
    generation = store.claim('run-a', 'resumed', 100000)
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    resumed_budget = RunBudget(store, 'run-a', generation)
    resumed = executor((store, resumed_budget), Provider())
    assert resumed.execute('initial', plan())['status'] == 'in_progress'
    assert store.recover_operations('run-a', generation) == 1
    result = resumed.execute('initial', plan())
    assert result['status'] == 'completed' and result['total'] == 20
    assert resumed_budget.snapshot()['query_count'] == 10
    assert sum(op['execution_count'] for op in resumed_budget.snapshot()['logical_operations'].values()) == 11
    with pytest.raises(ValueError):
        executor(setup).execute('supplement', plan(1, 1, 's'))


def test_provider_exception_is_audited_without_leaking_tokens(setup):
    class Broken(Provider):
        def search(self, query, limit, *, language):
            raise RuntimeError('adapter parser failed token=SECRET')

    search = executor(setup, Broken())
    result = search.execute('initial', plan())
    assert result['incomplete']
    state = setup[0].metadata('run-a', search.KEY)
    assert len(state['queries']['q0']['attempts']) == 3
    assert state['queries']['q0']['outcome'] == 'parse_error'
    assert 'SECRET' not in json.dumps(state)


def test_canonical_urls_keep_functional_bytes_and_reject_obfuscated_addresses():
    from chip_model.pipeline.domain_discovery.search import canonical_url
    assert canonical_url('https://EXAMPLE.com:443/product?sku=S4%2F80GB&view=full&utm_source=x#section') == 'https://example.com/product?sku=S4%2F80GB&view=full'
    for url in ('http://2130706433/', 'http://0x7f000001/', 'http://127.1/', 'http://127.0.0.1/',
                'http://[::1]/', 'https://user:pass@example.com/', 'file:///secret', 'https://example.com:7897/',
                'https://example.com/?access_token=private', 'https://foo.local/'):
        with pytest.raises(ValueError):
            canonical_url(url)


def test_shared_pagination_seam_is_pure_bounded_and_uses_versioned_object_cursor():
    from chip_model.pipeline.domain_discovery import search
    assert hasattr(search, 'serialize_page')
    items = [{'candidate_id': str(i), 'url': 'https://vendor.example/' + str(i), 'snippet': '中' * 2500}
             for i in range(12)]
    page = search.serialize_page(items, 0, {'list_version': 'v1'})
    assert 1 <= len(page['items']) < 10
    assert page['next_cursor'] == {'list_version': 'v1', 'offset': len(page['items'])}
    assert len(json.dumps(page, ensure_ascii=False)) <= 12000
    assert items[0]['snippet'] == '中' * 2500
    for offset, limit in [(True, 10), (-1, 10), (13, 10), (0, 11), (0, True)]:
        with pytest.raises(ValueError):
            search.serialize_page(items, offset, {'list_version': 'v1'}, limit=limit)
    with pytest.raises(ValueError, match='item_too_large'):
        search.serialize_page([{'candidate_id': 'big', 'url': 'https://vendor.example/' + 'a' * 13000}], 0, {'list_version': 'v1'})


def test_execute_cursor_is_versioned_object_and_cannot_cross_saved_versions(setup):
    search = executor(setup)
    first = search.execute('initial', plan())
    assert first['next_cursor'] == {'list_version': first['list_version'], 'offset': len(first['items'])}
    second = search.execute('supplement', plan(1, 1, 's'))
    with pytest.raises(ValueError, match='version'):
        search.results(second['list_version'], cursor=first['next_cursor'])
    for cursor in ({'list_version': first['list_version'], 'offset': True},
                   {'list_version': first['list_version'], 'offset': -1},
                   {'list_version': first['list_version'], 'offset': 10, 'limit': 100}):
        with pytest.raises(ValueError):
            search.results(first['list_version'], cursor=cursor)


def test_full_main_list_still_merges_supplementary_references(setup):
    provider = Provider(count=10)
    search = executor(setup, provider)
    first = search.execute('initial', plan())
    assert first['total'] == 100
    provider.shared = True
    # Match a URL already admitted by initial q0; total stays fixed but refs grow.
    provider.calls.clear()
    second = search.execute('supplement', plan(1, 1, 's'))
    state = setup[0].metadata('run-a', search.KEY)
    matching = [item for item in state['versions'][second['list_version']]['items'] if '/0/' in item['url']]
    assert not matching  # supplement /0/ is new and explicitly budget-excluded
    provider.shared = False
    third = search.execute('supplement-two', plan(2, 1, 't'))
    old = [item for item in state['versions'][first['list_version']]['items'] if '/2/' in item['url']]
    assert old
    new = setup[0].metadata('run-a', search.KEY)['versions'][third['list_version']]['items']
    assert all(any(ref['query_id'] == 't0' for ref in item['references']) for item in new if '/2/' in item['url'])
    assert third['total'] == 100


def test_intent_completion_live_state_is_separate_from_immutable_search_snapshot(setup):
    class OnceFailed(Provider):
        def search(self, query, limit, *, language):
            self.outcome = 'network_error' if query.endswith('q0') else 'healthy_empty'
            return super().search(query, limit, language=language)

    search = executor(setup, OnceFailed())
    first = search.execute('initial', plan())
    replacement = plan(1, 1, 's')
    replacement['queries'][0]['replaces_query_id'] = 'q0'
    search = executor(setup, Provider(outcome='healthy_empty'))
    second = search.execute('replacement', replacement)
    search.complete_intent('finish', 'q0', 's0', 'Hermes 已结束失败意图')
    replay = search.execute('replacement', replacement)
    assert replay['incomplete'] is second['incomplete'] is True
    assert replay['intent_incomplete'] is False
    assert first['intent_incomplete'] is True
    assert setup[1].stages()['searching']['main']['status'] == 'completed'


def test_encoded_api_credentials_are_redacted_in_raw_url_and_diagnostics():
    from chip_model.pipeline.domain_discovery.search import redact
    redacted = redact({'url': 'https://search.example/?%61pi_key=SECRET&sku=MLU370-S4',
                       'error': 'GET https://search.example/?access%5Ftoken=SECRET proxy http://u:p@localhost:7897'})
    assert 'SECRET' not in json.dumps(redacted)
    assert 'sku=MLU370-S4' in redacted['url']
    assert 'u:p@' not in redacted['error']
