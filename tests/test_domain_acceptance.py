"""Controlled Fixture acceptance; never evidence of a live Hermes/model run."""
import copy
import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import requests
from fastapi.testclient import TestClient

from chip_model.domain_tool_api import tool_contract
from chip_model.open_web_tool_server import create_open_web_tool_app
from chip_model.pipeline.domain_discovery.history import read_v3_run
from chip_model.pipeline.domain_discovery.rules import BUNDLE_MANIFEST, RULE_VERSION
from chip_model.pipeline.domain_discovery.runtime import run_domain
from chip_model.pipeline.domain_discovery.search import ProviderResponse
from chip_model.pipeline.domain_discovery.store import RunStore, consistent_backup

FIXTURES = Path(__file__).parent / 'fixtures' / 'domain_discovery'
DOMAINS = ('identity', 'specs', 'compute', 'compatibility', 'benchmark', 'deployment')
ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((FIXTURES / 'acceptance.json').read_text(encoding='utf-8'))
TABLES = ('chips', 'models', 'chip_model_benchmarks', 'chip_model_compatibility',
          'deployment_guides', 'field_provenance')


def logical_digest(source, destination):
    """Consistent WAL-aware snapshot; every table, column, row and stable key."""
    consistent_backup(source, destination)
    records = {}
    with closing(sqlite3.connect(destination)) as db:
        for table in TABLES:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            assert columns, table
            rows = [dict(zip(columns, row)) for row in db.execute(f'SELECT * FROM "{table}"')]
            rows.sort(key=lambda r: (str(r.get('id')), json.dumps(r, sort_keys=True, ensure_ascii=False)))
            records[table] = {'columns': columns, 'rows': rows}
    canonical = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest(), records


def search_plan(round_number=0, count=10):
    return {'round_number': round_number, 'queries': [
        {'query_id': f'fixture-{round_number}-{i}',
         'text': f'芯片 文档 Fixture {round_number} {i}' if i < 5 else f'GPU documentation Fixture {round_number} {i}',
         'language': 'zh' if i < 5 else 'en', 'reason': 'Fixture discovery', 'page_type': 'datasheet'}
        for i in range(count)]}


class FixtureSearch:
    name = 'FixtureSearch'

    def __init__(self, urls=None, failure=None):
        self.urls = urls if urls is not None else [DATA['urls']['positive'], DATA['urls']['negative']]
        self.failure, self.calls = failure, []

    def search(self, query, limit, *, language):
        self.calls.append((query, limit, language))
        if self.failure == 'all' or self.failure == query:
            return ProviderResponse(outcome='network_error', error='Fixture provider unavailable')
        # Distribute 23 URLs over the ten actual queries without exceeding 10 hits.
        index = int(query.rsplit(' ', 1)[-1])
        urls = self.urls[index::10] if len(self.urls) > 10 else self.urls
        return ProviderResponse(rows=[{'url': url, 'title': 'Fixture GPU documentation',
            'snippet': 'Fixture metadata only', 'rank': i + 1} for i, url in enumerate(urls)],
            outcome='success' if urls else 'healthy_empty', http_status=200)


class FixturePages:
    headers = {}

    def __init__(self, mime='text/html', fail=False, body=None):
        self.mime, self.fail, self.calls, self.body = mime, fail, [], body

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail or url == DATA['urls']['unreachable']:
            raise requests.ConnectionError('Fixture page unreachable')
        extension = {'text/html': 'html', 'text/plain': 'txt', 'application/pdf': 'pdf'}[self.mime]
        body = self.body if self.body is not None else (FIXTURES / ('catalog.' + extension)).read_bytes()
        if 'healthy-no-fields' in url:
            body = (FIXTURES / 'healthy-empty.txt').read_bytes()
            mime = 'text/plain'
        else:
            mime = self.mime
        return SimpleNamespace(status_code=200, url=url, encoding='utf-8',
            headers={'Content-Type': mime}, iter_content=lambda **_: iter([body]), close=lambda: None)


class FixtureHermes:
    """Controlled conversation, provider schema validation and real HTTP routes."""

    def __init__(self, *, rounds=0, replace=False, lost_finalize=False, stop_after_page=None, linked=(), stop_hook=None,
                 skip_finalize=False):
        self.rounds, self.replace, self.lost_finalize = rounds, replace, lost_finalize
        self.stop_after_page, self.linked = stop_after_page, list(linked)
        self.stop_hook = stop_hook
        self.skip_finalize = skip_finalize
        self.trace, self.calls = [], 0

    def preflight(self, *, bundle_root, context, service, **kwargs):
        self.client = TestClient(create_open_web_tool_app(token='Fixture-token', domain_service=service),
                                 client=('127.0.0.1', 50000))
        self.binding = {'contract_version': 3, 'pipeline': 'hermes-open-web-v3', 'run_id': context.run_id,
            **{k: context.store.get_run(context.run_id)['payload'][k] for k in ('domain', 'scope_type', 'chip')}}
        manifest = json.loads((bundle_root / BUNDLE_MANIFEST).read_text(encoding='utf-8'))
        self.schemas = {t['name']: t['parameters'] for t in tool_contract()['tools']}
        # This resolver reports observed fixture files. It does not claim a live SDK.
        files = {rel: {'path': str(bundle_root / rel), 'sha256': hashlib.sha256((bundle_root / rel).read_bytes()).hexdigest()}
                 for rel in manifest['files']}
        assert len(files) == 20
        return {'resolver': {'rule_version': RULE_VERSION, 'files': files},
            'tool_names': list(self.schemas), 'implementation': 'Fixture HTTP conversation', 'version': 'Fixture'}

    def tool(self, endpoint, **payload):
        names = {'search': 'open_web_search', 'preview': 'open_web_preview', 'selection': 'open_web_submit_selection'}
        request = {**self.binding, **payload}
        jsonschema.validate(request, self.schemas[names[endpoint]])
        response = self.client.post('/v1/' + endpoint, headers={'Authorization': 'Bearer Fixture-token'}, json=request)
        self.trace.append({'classification': 'Fixture', 'tool': names[endpoint], 'request': request,
                           'status_code': response.status_code, 'response': response.json()})
        assert response.status_code == 200, response.text
        assert len(json.dumps(response.json(), ensure_ascii=False)) <= 12000
        return response.json()

    def run(self, *, context, **kwargs):
        self.calls += 1
        first = search_plan()
        result = self.tool('search', action='execute', request_id='Fixture-search-0', plan=first)
        for number in range(1, self.rounds + 1):
            plan = search_plan(number, 5)
            if self.replace and number == 1:
                plan['queries'][0]['replaces_query_id'] = first['queries'][0]['query_id']
            result = self.tool('search', action='execute', request_id=f'Fixture-search-{number}', plan=plan)
            if self.replace and number == 1:
                self.tool('search', action='complete_intents', request_id='Fixture-closure', completions=[{
                    'failed_query_id': first['queries'][0]['query_id'], 'replacement_query_id': plan['queries'][0]['query_id'],
                    'reason': 'Fixture successful replacement explicitly closes original intent'}])
        version, items, cursor = result['list_version'], [], None
        while True:
            response = self.tool('search', action='results', list_version=version, cursor=cursor)
            items.extend(response['items'])
            cursor = response['next_cursor']
            assert response['has_more'] == (cursor is not None)
            if not response['has_more']:
                break
        cursor, number = None, 0
        while items:
            response = self.tool('preview', mode='initial', request_id=f'Fixture-preview-{number}', list_version=version, cursor=cursor)
            number += 1
            if self.stop_after_page == number:
                if self.stop_hook:
                    self.stop_hook()
                time.sleep(.2)
                raise TimeoutError('Fixture stopped during selection')
            cursor = response['next_cursor']
            assert response['has_more'] == (cursor is not None)
            if not response['has_more']:
                break
        for offset in range(0, len(items), 10):
            decisions = []
            for item in items[offset:offset + 10]:
                selected = item['url'].split('?', 1)[0] in (DATA['urls']['positive'], DATA['urls']['negative'])
                decisions.append({'candidate_id': item['candidate_id'], 'decision': 'select' if selected else 'reject',
                    'reason': 'Fixture page contains relevant exact evidence' if selected else 'Fixture healthy page has no relevant field',
                    'expected_info': 'Fixture field evidence' if selected else '',
                    'linked_domains': self.linked if selected else [], 'entity_clues': [], 'matched_category': None})
            self.tool('selection', action='append', request_id=f'Fixture-append-{offset}', list_version=version, decisions=decisions)
        if self.skip_finalize:
            return SimpleNamespace(returncode=0, stdout='Fixture missing finalize', stderr='')
        self.receipt = self.tool('selection', action='finalize', request_id='Fixture-final', list_version=version)
        assert self.tool('selection', action='finalize', request_id='Fixture-final', list_version=version) == self.receipt
        (context.store.run_path(context.run_id) / 'fixture-hermes-trace.json').write_text(
            json.dumps(self.trace, ensure_ascii=False, indent=2), encoding='utf-8')
        if self.lost_finalize:
            raise ConnectionError('Fixture finalize response lost after server commit')
        return SimpleNamespace(returncode=0, stdout='Fixture controlled conversation', stderr='')


def candidate(template, page, domain, *, vendor='NVIDIA', sku='L40S 48GB PCIe', model=None):
    template = copy.deepcopy(template)
    quote = template['quote']
    start = page['start'] + page['text'].index(quote)
    unit = template.get('unit')
    table = template.get('target_table', 'chips')
    row = {'domain_id': domain, 'target_table': table, 'field_name': template['field_name'],
        'entity_key': {'vendor': vendor, 'chip_model': sku},
        'entity_hints': {'vendor': vendor, 'full_sku': sku, 'entity_type': 'board_sku'},
        'raw_value': template['value'], 'normalized_value': template['value'], 'unit': unit,
        'conversion': {'source_unit': unit, 'factor': 1, 'basis': 'decimal'} if unit else {},
        'scope': 'relation' if template.get('relation') else 'board_sku',
        'evidence_kind': 'measured' if domain == 'benchmark' else 'explicit', 'conditions': {},
        'evidence': {'snapshot_id': page['snapshot_id'], 'body_hash': page['body_hash'], 'quote': quote,
                     'start': start, 'end': start + len(quote)},
        'context_refs': [], 'explanation': 'Fixture pre-recorded extraction response.',
        'source_type': 'official_datasheet', 'confidence': 'high', 'confidence_reason': 'Fixture exact quote.'}
    if table != 'chips':
        row['entity_key'] = {'chip_model': sku, 'model_id': model or DATA['model_id']}
    if domain == 'benchmark':
        row['entity_key'].update(suite_name='Fixture-report', workload_type='inference', chip_count=2, precision='BF16')
        row['conditions'] = {'hardware_config': 'configuration-A', 'metric_class': 'generative_throughput',
                             'input_seq_length': 128, 'output_seq_length': 256, 'batch_size': 4}
    if table == 'deployment_guides':
        row['entity_key'].update(url=page['final_url'], title='Fixture Guide', source_type='official')
    row.update(template.get('changes', {}))
    return row


class FixtureModel:
    model_name = 'Fixture prerecorded model boundary'

    def __init__(self, *, facts=None, failures=(), revision=False, linked=(), missing_model=False, domain_facts=None,
                 phase_failures=()):
        self.facts, self.failures, self.revision = facts, set(failures), revision
        self.linked, self.missing_model, self.calls = list(linked), missing_model, []
        self.domain_facts = domain_facts or {}
        self.phase_failures = set(phase_failures)

    def call_model(self, **request):
        self.calls.append(copy.deepcopy(request))
        payload = json.loads(request['messages'][-1]['content'])
        domain, page = payload['domain_id'], payload['page']
        if domain in self.failures or payload['phase'] in self.phase_failures:
            raise requests.ConnectionError('Fixture technical model failure')
        if payload['phase'] == 'review':
            if self.revision:
                return {'verdict': 'revise', 'reason': 'Fixture repeat revision', 'candidate': payload['candidate']}
            rejected = page['requested_url'] == DATA['urls']['negative']
            return copy.deepcopy(DATA['domains'][domain]['negative_review' if rejected else 'independent_review'])
        if domain in self.domain_facts:
            templates = self.domain_facts[domain]
        elif domain == 'identity' and self.facts:
            templates = [dict(field_name='chip_type', value='GPU', quote=t['quote'], unit=None,
                              vendor=t.get('vendor', 'NVIDIA'), sku=t.get('sku', 'L40S 48GB PCIe')) for t in self.facts]
        elif self.facts is not None:
            templates = self.facts
        else:
            templates = DATA['domains'][domain]['negative' if page['requested_url'] == DATA['urls']['negative'] else 'positive']
        facts = [candidate(t, page, domain, vendor=t.get('vendor', 'NVIDIA'), sku=t.get('sku', 'L40S 48GB PCIe'),
                           model=DATA['missing_model_id'] if self.missing_model else None)
                 for t in templates if t['quote'] in page['text']]
        return {'facts': facts, 'linked_domains': [d for d in self.linked if d != domain]}


@pytest.fixture
def acceptance(tmp_path):
    def make(*, domain='specs', chip_count=1, urls=None, model=None, runner=None, mime='text/html', provider=None):
        source = tmp_path / 'Fixture-business.db'
        with closing(sqlite3.connect(source)) as db, db:
            db.executescript((FIXTURES / 'business_schema.sql').read_text(encoding='utf-8'))
            db.executescript((FIXTURES / 'business_aux_schema.sql').read_text(encoding='utf-8'))
            db.execute('INSERT INTO models(model_id,author,api_response_json) VALUES (?,?,?)',
                       (DATA['model_id'], 'Fixture model', '{"Fixture":true}'))
            if chip_count:
                db.execute('INSERT INTO chips(vendor,chip_model,vram_gb,form_factor,chip_type,sm_count) VALUES (?,?,?,?,?,?)',
                           ('NVIDIA', 'L40S 48GB PCIe', '48', 'PCIe', 'GPU', '142'))
            db.executemany('INSERT INTO chips(vendor,chip_model) VALUES (?,?)',
                           [('Fixture unused vendor', f'Fixture-unused-{i}') for i in range(max(0, chip_count - 1))])
        installed = tmp_path / 'Fixture-installed-skills'
        shutil.copytree(ROOT / '.hermes-skills', installed)
        deps = {'source_db': source, 'store': RunStore(tmp_path / 'domain_runs'), 'installed_skill_root': installed,
            'request_id': 'Fixture-start', 'hermes_runner': runner or FixtureHermes(),
            'search_provider': provider or FixtureSearch(urls), 'page_fetcher': FixturePages(mime),
            'call_model': model or FixtureModel(), 'resolver': lambda _: ['93.184.216.34'], 'backoff': lambda _: None}
        return deps, {'domain': domain, 'chip': None, 'scope_type': 'discovery', 'resume_run_id': None}
    return make


def execute(deps, request):
    before, _ = logical_digest(deps['source_db'], deps['source_db'].parent / ('before-' + deps['request_id'] + '.db'))
    result = run_domain(request, deps)
    after, _ = logical_digest(deps['source_db'], deps['source_db'].parent / ('after-' + deps['request_id'] + '.db'))
    assert before == after, 'Fixture business/provenance rows changed'
    return result


@pytest.mark.parametrize('domain', DOMAINS)
def test_six_domains_full_run_http_frozen_models_and_readonly_history(acceptance, domain):
    deps, request = acceptance(domain=domain)
    result = execute(deps, request)
    assert result['status'] == 'success', result
    assert result['statistics']['validated_candidates'] > 0
    assert result['statistics']['formal_business_writes'] == result['statistics']['formal_provenance_writes'] == 0
    assert result['statistics']['new_entities'] == 0
    assert result['execution_state']['pending_candidates'] == []
    assert result['execution_state']['unfinished_task_ids'] == []
    assert result['execution_state']['selection_finalization'] is None
    assert deps['hermes_runner'].calls == 1
    assert {row['tool'] for row in deps['hermes_runner'].trace} == set(deps['hermes_runner'].schemas)
    store, rid = deps['store'], result['run_id']
    audit = [event['payload'] for event in store.events(rid)]
    for executor in ('hermes', 'backend_extract', 'backend_review'):
        assert any(e.get('executor') == executor and e.get('kind') == 'loaded' for e in audit)
        assert any(e.get('executor') == executor and e.get('kind') == 'executed' for e in audit)
    assert len(store.metadata(rid, 'domain_hermes_preflight.v1')['resolver']['files']) == 20
    assert any(e.get('reference_hashes') for e in audit if e.get('executor') == 'backend_review')
    models = store.metadata(rid, 'domain_models.v1')
    negative = DATA['domains'][domain]['negative'][0]
    assert any(r['status'] in {'semantic_rejected', 'program_rejected'}
        and r['domain_id'] == domain and r['versions'][-1]['candidate']['field_name'] == negative['field_name']
        and r['versions'][-1]['candidate']['normalized_value'] == negative['value']
        for r in models['candidates'].values())
    comparisons = store.metadata(rid, 'domain_compare.v1')['comparisons']
    assert any(r[-1]['evidence_verification_status'] == 'verified' for r in comparisons.values())
    history = read_v3_run(store.root, rid)
    assert history['status'] == 'success' and history['field_changes']
    assert all(not r.get('publish_eligible', False) for r in history['field_changes'])
    if domain == 'deployment':
        assert any('API_KEY=example' in r['candidate']['evidence']['quote'] for r in history['field_changes'])


@pytest.mark.parametrize('chip_count', [0, 1, 1000])
def test_discovery_one_run_never_creates_per_baseline_chip_units(acceptance, chip_count):
    deps, request = acceptance(chip_count=chip_count, urls=[DATA['urls']['positive']])
    result = execute(deps, request)
    assert result['status'] == 'success', result
    assert deps['hermes_runner'].calls == 1
    tasks = deps['store'].list_tasks(result['run_id'])
    assert len(tasks) == 2  # One main page job plus its verified identity dependency.
    assert sum(not t['payload'].get('identity_dependency') for t in tasks) == 1
    assert not any('unit_id' in task['payload'] for task in tasks)
    assert result['statistics']['new_entities'] == (1 if chip_count == 0 else 0)


@pytest.mark.parametrize('mime', ['text/plain', 'application/pdf'])
def test_full_run_uses_real_text_and_pdf_snapshot_parsers(acceptance, mime):
    deps, request = acceptance(mime=mime, urls=[DATA['urls']['positive']])
    result = execute(deps, request)
    assert result['status'] == 'success', result
    pages = deps['store'].metadata(result['run_id'], 'domain_pages.v1')
    assert len(pages['snapshots']) == 1
    assert result['statistics']['main']['validated_candidates'] == 1


def test_23_urls_all_paged_visited_decided_and_healthy_empty_pages_do_not_fail(acceptance):
    urls = [*list(DATA['urls'].values())[:3], *[DATA['urls']['empty'] + f'/{i}' for i in range(20)]]
    assert len(urls) == DATA['paging_url_count']
    deps, request = acceptance(urls=urls)
    result = execute(deps, request)
    assert result['status'] == 'success', result
    trace = deps['hermes_runner'].trace
    results = [r['response'] for r in trace if r['request'].get('action') == 'results']
    previews = [r['response'] for r in trace if r['request'].get('mode') == 'initial']
    assert len(results) == len(previews) == 3
    assert results[-1]['has_more'] is previews[-1]['has_more'] is False
    items = [item for page in results for item in page['items']]
    assert len({item['candidate_id'] for item in items}) == 23
    assert len(deps['page_fetcher'].calls) == 23
    assert len(deps['store'].metadata(result['run_id'], 'domain_selection.v1')['decisions']) == 23
    assert result['execution_state']['pending_candidates'] == []
    tasks = deps['store'].list_tasks(result['run_id'])
    assert len(tasks) == 3
    assert sum(not t['payload'].get('identity_dependency') for t in tasks) == 2


def test_exact_initial_and_two_supplement_quota_healthy_empty_is_no_findings(acceptance):
    provider = FixtureSearch([])
    deps, request = acceptance(provider=provider, runner=FixtureHermes(rounds=2))
    result = execute(deps, request)
    assert result['status'] == 'no_findings', result
    assert len(provider.calls) == 20
    search = deps['store'].metadata(result['run_id'], 'domain_search.v1')
    assert [len(search['plans'][str(i)]['model_payload']['queries']) for i in range(3)] == [10, 5, 5]
    assert len(search['queries']) == 20 and search['intents'] == {}
    assert deps['call_model'].calls == []
    assert result['statistics']['validated_candidates'] == 0


def test_provider_fallback_preserves_original_query_diagnostics_without_partial(acceptance):
    broken, fallback = FixtureSearch(failure='all'), FixtureSearch([])
    fallback.name = 'Fixture healthy fallback'
    deps, request = acceptance(provider=[broken, fallback])
    result = execute(deps, request)
    assert result['status'] == 'no_findings', result
    search = deps['store'].metadata(result['run_id'], 'domain_search.v1')
    assert len(search['queries']) == 10
    for row in search['queries'].values():
        assert row['outcome'] == 'healthy_empty'
        diagnostics = [attempt['diagnostic'] for attempt in row['attempts']]
        assert any(d['outcome'] == 'network_error' for d in diagnostics)
        assert all(d['original_query'] == row['query']['text'] for d in diagnostics)
    assert result['execution_state']['search_failures'] == []


def test_explicit_replacement_closes_intent_retains_failure_and_charges_queries(acceptance):
    failed = search_plan()['queries'][0]['text']
    deps, request = acceptance(provider=FixtureSearch([], failure=failed), runner=FixtureHermes(rounds=1, replace=True))
    result = execute(deps, request)
    assert result['status'] == 'no_findings', result
    state = deps['store'].metadata(result['run_id'], 'domain_search.v1')
    assert state['queries']['fixture-0-0']['outcome'] == 'network_error'
    assert len(state['queries']['fixture-0-0']['attempts']) == 3
    assert state['intents']['fixture-0-0']['replacement_query_id'] == 'fixture-1-0'
    assert len(state['queries']) == 15
    assert len(deps['search_provider'].calls) == 17
    assert result['execution_state']['closed_search_intents']['fixture-0-0']['reason']


def test_all_necessary_model_calls_fail_three_times_without_format_only_validation(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']], model=FixtureModel(failures=['specs']))
    result = execute(deps, request)
    assert result['status'] == 'failed', result
    assert result['execution_state']['all_model_chains_failed']
    assert len(deps['call_model'].calls) == 3
    saved = deps['store'].metadata(result['run_id'], 'domain_models.v1')
    assert not saved['candidates']
    assert not any(c['phase'] == 'review' for c in saved['calls'])
    assert read_v3_run(deps['store'].root, result['run_id'])['field_changes'] == []


def test_main_accepted_linked_model_failure_is_partial_and_main_fact_is_readable(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']],
        runner=FixtureHermes(linked=['compute']), model=FixtureModel(failures=['compute']))
    result = execute(deps, request)
    assert result['status'] == 'partial', result
    assert result['statistics']['main']['validated_candidates'] == 1
    assert result['statistics']['linked']['validated_candidates'] == 0
    history = read_v3_run(deps['store'].root, result['run_id'])
    assert any(row['candidate']['domain_id'] == 'specs' for row in history['field_changes'])
    assert any(task['payload']['domain'] == 'compute' and task['state'] != 'completed' for task in history['linked_tasks'])


def test_budget_stop_keeps_each_pending_url_and_finalization_reason_without_fake_reject(acceptance):
    class FixtureClock:
        offset = 0
        def epoch(self):
            return time.time()
        def monotonic(self):
            return time.monotonic() + self.offset
    clock = FixtureClock()
    urls = [DATA['urls']['empty'] + f'/{i}' for i in range(23)]
    deps, request = acceptance(urls=urls, runner=FixtureHermes(stop_after_page=1,
        stop_hook=lambda: setattr(clock, 'offset', 30)))
    deps.update(timeout=30, clock=clock)
    result = execute(deps, request)
    assert result['status'] == 'partial', result
    pending = result['execution_state']['pending_candidates']
    assert len(pending) == 23 and len({p['candidate_id'] for p in pending}) == 23
    assert all(p['reason'] in {'visit_not_completed', 'decision_not_submitted'} for p in pending)
    assert sum(p['reason'] == 'visit_not_completed' for p in pending) == 13
    assert sum(p['reason'] == 'decision_not_submitted' for p in pending) == 10
    assert result['execution_state']['selection_finalization']['reason'] == 'finalize_not_submitted'
    selection = deps['store'].metadata(result['run_id'], 'domain_selection.v1') or {}
    assert not selection.get('decisions') and not selection.get('finalized')
    assert not deps['call_model'].calls


def test_finalize_response_loss_restart_uses_original_receipt_and_jobs_once(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']], runner=FixtureHermes(lost_finalize=True))
    first = execute(deps, request)
    assert first['status'] == 'failed', first
    rid = first['run_id']
    receipt = deps['store'].metadata(rid, 'domain_selection.v1')['receipt_id']
    tasks = deps['store'].list_tasks(rid)
    assert len(tasks) == 1 and not deps['call_model'].calls
    deps['store'] = RunStore(deps['store'].root)
    deps['request_id'] = 'Fixture-resume'
    second = execute(deps, {**request, 'resume_run_id': rid})
    assert second['status'] == 'success', second
    assert deps['hermes_runner'].calls == 1
    assert deps['store'].metadata(rid, 'domain_selection.v1')['receipt_id'] == receipt
    recovered = deps['store'].list_tasks(rid)
    assert [t['task_id'] for t in recovered if not t['payload'].get('identity_dependency')] == [t['task_id'] for t in tasks]
    assert len(recovered) == 2
    assert second['statistics']['main']['validated_candidates'] == 1


def new_chip_templates():
    return [dict(field_name='vram_gb', value=DATA[key]['value'], quote=DATA[key]['quote'], unit='GB',
                 vendor=DATA[key]['vendor'], sku=DATA[key]['chip_model']) for key in ('new_entity', 'second_variant')]


def test_same_page_two_complete_skus_resolve_common_identity_without_second_business_fanout(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']], model=FixtureModel(facts=new_chip_templates(), linked=['compute']))
    # The linked response may suggest further domains, but only one business layer runs.
    result = execute(deps, request)
    assert result['status'] == 'success', result
    assert result['statistics']['new_entities'] == 2
    store, rid = deps['store'], result['run_id']
    assert len(deps['search_provider'].calls) == 10
    assert deps['hermes_runner'].calls == 1
    tasks = store.list_tasks(rid)
    assert len([t for t in tasks if t['payload'].get('identity_dependency')]) == 2
    assert len([t for t in tasks if not t['payload'].get('identity_dependency')]) == 2
    comparison = store.metadata(rid, 'domain_compare.v1')['comparisons']
    specs = [r[-1] for r in comparison.values() if r[-1]['candidate']['domain_id'] == 'specs']
    assert {r['candidate']['normalized_value'] for r in specs} == {64, 80}
    assert len({r['entity']['canonical_key'] for r in specs}) == 2
    assert all(r['validated'] and r['entity']['identity_proofs'] for r in specs)
    assert result['statistics']['formal_business_writes'] == 0


def test_missing_model_dependency_preserves_accepted_evidence_without_creating_model(acceptance):
    deps, request = acceptance(domain='benchmark', urls=[DATA['urls']['positive']], model=FixtureModel(missing_model=True))
    result = execute(deps, request)
    assert result['status'] == 'success', result
    rows = [v[-1] for v in deps['store'].metadata(result['run_id'], 'domain_compare.v1')['comparisons'].values()]
    relation = next(r for r in rows if r['candidate']['target_table'] == 'chip_model_benchmarks')
    assert relation['validated'] and relation['evidence_status'] == 'accepted'
    assert relation['dependency_status'] == 'dependency_unresolved' and not relation['update_eligible']
    assert 'model:' + DATA['missing_model_id'] in relation['missing_dependencies']
    with closing(sqlite3.connect(deps['source_db'])) as db:
        assert db.execute('SELECT model_id FROM models').fetchall() == [(DATA['model_id'],)]


def test_unreachable_page_is_distinct_from_healthy_no_field(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['unreachable']])
    result = execute(deps, request)
    assert result['status'] == 'failed', result
    assert result['execution_state']['all_page_chains_failed']
    assert result['execution_state']['page_failures']
    assert len(deps['page_fetcher'].calls) == 3


def test_one_revision_limit_persists_unresolved_on_new_resume_request(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']], model=FixtureModel(revision=True))
    first = execute(deps, request)
    assert first['status'] == 'no_findings', first
    saved = deps['store'].metadata(first['run_id'], 'domain_models.v1')
    row = next(iter(saved['candidates'].values()))
    assert row['status'] == 'unresolved' and row['reason'] == 'revision_limit'
    assert len(row['versions']) == 2 and len(deps['call_model'].calls) == 3
    deps['request_id'] = 'Fixture-revision-new-key'
    second = execute(deps, {**request, 'resume_run_id': first['run_id']})
    assert second['status'] == 'no_findings'
    assert deps['store'].metadata(first['run_id'], 'domain_models.v1') == saved
    assert len(deps['call_model'].calls) == 3


def comparison_rows(deps, result, domain):
    return [r[-1] for r in deps['store'].metadata(result['run_id'], 'domain_compare.v1')['comparisons'].values()
            if r[-1]['candidate']['domain_id'] == domain]


def test_linked_discovery_first_new_identity_is_verified_common_dependency(acceptance):
    identity_templates = [dict(field_name='chip_type', value='GPU', unit=None, quote=t['quote'],
        vendor=t['vendor'], sku=t['sku']) for t in new_chip_templates()]
    # A healthy main page has no main-domain fields. Only the linked specs
    # candidates may trigger the first identity dependency for the new SKUs.
    model = FixtureModel(domain_facts={'compute': [], 'specs': new_chip_templates(), 'identity': identity_templates})
    deps, request = acceptance(domain='compute', urls=[DATA['urls']['positive']], model=model,
                               runner=FixtureHermes(linked=['specs']))
    result = execute(deps, request)
    assert result['status'] == 'success', result
    assert result['statistics']['new_entities'] == 2
    assert result['statistics']['main']['validated_candidates'] == 0
    assert result['statistics']['linked']['validated_candidates'] == 2
    rows = comparison_rows(deps, result, 'specs')
    assert len(rows) == 2 and all(r['validated'] and r['task_scopes'] == ['linked'] for r in rows)
    assert all(r['entity']['status'] == 'new' and r['entity']['identity_proofs'] for r in rows)
    tasks = deps['store'].list_tasks(result['run_id'])
    assert sum(not t['payload'].get('identity_dependency') for t in tasks) == 2
    identity_jobs = [t for t in tasks if t['payload'].get('identity_dependency')]
    assert {t['payload']['entity_hint']['full_sku'] for t in identity_jobs} == {
        DATA['new_entity']['chip_model'], DATA['second_variant']['chip_model']}
    assert all(t['payload']['linked'] and t['payload']['depth'] == 1 for t in identity_jobs)
    assert len(deps['search_provider'].calls) == 10


@pytest.mark.parametrize('missing', ['input_seq_length', 'output_seq_length', 'batch_size', 'hardware_config'])
def test_benchmark_missing_required_condition_retains_review_but_no_validated_metric(acceptance, missing):
    template = copy.deepcopy(DATA['domains']['benchmark']['positive'][0])
    conditions = {'hardware_config': 'configuration-A', 'metric_class': 'generative_throughput',
                  'input_seq_length': 128, 'output_seq_length': 256, 'batch_size': 4}
    conditions.pop(missing)
    template['changes'] = {'conditions': conditions}
    deps, request = acceptance(domain='benchmark', urls=[DATA['urls']['positive']],
        model=FixtureModel(domain_facts={'benchmark': [template]}))
    result = execute(deps, request)
    row = comparison_rows(deps, result, 'benchmark')[0]
    assert row['model_proof']['semantic_review'] == ('accept' if missing == 'hardware_config' else 'not_executed')
    if missing != 'hardware_config':
        saved = deps['store'].metadata(result['run_id'], 'domain_models.v1')['candidates'][row['logical_candidate_id']]
        assert saved['status'] == 'unresolved'
    assert row['relation_readiness']['status'] == 'unresolved'
    assert not row['validated'] and not row['update_eligible']
    assert result['statistics']['main']['validated_candidates'] == 0
    assert result['statistics']['condition_unresolved_candidates'] == 1


def test_optional_unknown_conditions_do_not_invent_required_generation_constraints(acceptance):
    template = copy.deepcopy(DATA['domains']['benchmark']['positive'][0])
    template['changes'] = {'conditions': {'hardware_config': 'configuration-A',
        'metric_class': 'generative_throughput', 'input_seq_length': 128, 'output_seq_length': 256,
        'batch_size': 4, 'framework': 'unknown', 'test_date': None, 'concurrency': None}}
    deps, request = acceptance(domain='benchmark', urls=[DATA['urls']['positive']],
        model=FixtureModel(domain_facts={'benchmark': [template]}))
    result = execute(deps, request)
    row = comparison_rows(deps, result, 'benchmark')[0]
    assert row['validated'] and row['relation_readiness']['status'] == 'ready'
    assert row['candidate']['conditions']['framework'] == 'unknown'


def test_ambiguous_historical_relation_preserves_accepted_metric_without_overwrite(acceptance):
    deps, request = acceptance(domain='benchmark', urls=[DATA['urls']['positive']])
    with closing(sqlite3.connect(deps['source_db'])) as db, db:
        db.executemany('INSERT INTO chip_model_benchmarks(chip_model,model_id,suite_name,workload_type,'
            'chip_count,precision,hardware_config,batch_size,input_seq_length,output_seq_length,throughput_tok_s) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)', [('L40S 48GB PCIe', DATA['model_id'], 'Fixture-report',
            'inference', '2', 'BF16', 'configuration-A', '4', '128', '256', value) for value in ('90', '95')])
    result = execute(deps, request)
    row = comparison_rows(deps, result, 'benchmark')[0]
    assert row['validated'] and row['evidence_status'] == 'accepted'
    assert row['comparison']['status'] == 'comparison_unresolved'
    assert not row['update_eligible'] and row['value_change_kind'] == 'unknown'
    assert result['statistics']['comparison_unresolved_candidates'] == 1


@pytest.mark.parametrize('archive', [False, True])
def test_same_value_new_url_source_membership_and_quality_are_independent(acceptance, archive):
    old_template = copy.deepcopy(DATA['domains']['specs']['positive'][0])
    old_template['quote'] = 'L40S 48GB PCIe GPU: 48 GB GDDR6'
    deps, request = acceptance(urls=[DATA['urls']['positive']], model=FixtureModel(facts=[old_template]))
    deps['page_fetcher'] = FixturePages(mime='text/plain', body=(FIXTURES / 'archive.txt').read_bytes())
    old = execute(deps, request)
    old_row = comparison_rows(deps, old, 'specs')[0]
    ref = {'run_id': old['run_id'], 'logical_candidate_id': old_row['logical_candidate_id'], 'version': 0}
    with closing(sqlite3.connect(deps['source_db'])) as db, db:
        db.execute('INSERT INTO field_provenance(table_name,row_id,field_name,new_value,source_url,source_type,'
            'confidence,evidence_ref_json) VALUES (?,?,?,?,?,?,?,?)', ('chips', '1', 'vram_gb', '48',
            DATA['urls']['positive'], 'community', 'low', json.dumps(ref) if archive else None))
    # A second complete runtime consumes the actual old producer archive.
    deps.update(request_id='Fixture-second', hermes_runner=FixtureHermes(), call_model=FixtureModel(),
                search_provider=FixtureSearch([DATA['urls']['positive'] + '?revision=2']),
                page_fetcher=FixturePages())
    current = execute(deps, request)
    row = comparison_rows(deps, current, 'specs')[0]
    assert row['value_change_kind'] == 'unchanged'
    assert row['source_membership_kind'] == 'added'
    assert row['source_quality_kind'] == ('upgraded' if archive else 'unknown')
    assert row['source_change_kind'] == ('upgraded' if archive else 'added')
    assert row['old_value'] == '48' and row['candidate']['normalized_value'] == 48
    assert row['validated'] and row['formal_business_writes'] == 0
    if archive:
        assert row['old_sources'][0]['verified_evidence']['model_proof']['accepted']


def test_wal_consistent_backup_includes_uncheckpointed_business_rows_and_digest_is_equal(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']])
    with closing(sqlite3.connect(deps['source_db'])) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE chips SET tdp_w=? WHERE id=1', ('Fixture WAL 350',))
        writer.execute('INSERT INTO field_provenance(table_name,row_id,field_name,new_value,source_url) VALUES (?,?,?,?,?)',
                       ('chips', '1', 'tdp_w', 'Fixture WAL 350', 'https://fixture.example/wal'))
        writer.commit()
        assert Path(str(deps['source_db']) + '-wal').stat().st_size > 0
        result = execute(deps, request)
        baseline = deps['store'].run_path(result['run_id']) / 'data.db'
        with closing(sqlite3.connect(baseline)) as snapshot:
            assert snapshot.execute('SELECT tdp_w FROM chips WHERE id=1').fetchone()[0] == 'Fixture WAL 350'
            assert snapshot.execute('SELECT new_value FROM field_provenance').fetchall() == [('Fixture WAL 350',)]
    assert result['status'] == 'success'


def test_external_writer_is_detected_then_check_repeats_after_writer_finishes(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']])
    before, _ = logical_digest(deps['source_db'], deps['source_db'].parent / 'Fixture-before-writer.db')
    def external_write(stage):
        assert stage == 'after_finalize'
        with closing(sqlite3.connect(deps['source_db'])) as writer, writer:
            writer.execute("UPDATE chips SET description='Fixture external writer' WHERE id=1")
    deps['stage_hook'] = external_write
    result = run_domain(request, deps)
    after, _ = logical_digest(deps['source_db'], deps['source_db'].parent / 'Fixture-after-writer.db')
    assert before != after, 'Controlled external interference must be detected'
    deps.pop('stage_hook')
    deps['request_id'] = 'Fixture-quiescent-repeat'
    repeated = execute(deps, {**request, 'resume_run_id': result['run_id']})
    assert repeated['status'] == 'success'
    assert repeated['statistics']['formal_business_writes'] == 0


def test_expired_old_generation_cannot_commit_and_resume_keeps_original_freeze_and_limits(acceptance):
    class FixtureClock:
        epoch_offset = 0
        def epoch(self):
            return time.time() + self.epoch_offset
        def monotonic(self):
            return time.monotonic()
    clock = FixtureClock()
    deps, request = acceptance(urls=[DATA['urls']['positive']])
    # The store and worker use one epoch; monotonic budget is independently stable.
    deps['clock'] = clock
    deps['store'] = RunStore(deps['store'].root, clock=clock.epoch)
    generations = {}
    def expire(stage):
        rid = deps['hermes_runner'].binding['run_id']
        generations['run_id'] = rid
        clock.epoch_offset = 10
        generations['new'] = deps['store'].claim(rid, 'Fixture-replacement-worker', 5)
    deps['stage_hook'] = expire
    first = execute(deps, request)
    rid = first['run_id']
    assert first['status'] == 'interrupted' and first['terminal_persisted'] is False
    assert generations['new'] == 2
    baseline = deps['store'].metadata(rid, 'baseline')
    budget = deps['store'].metadata(rid, 'domain_budget.v1')
    with pytest.raises(ValueError):
        deps['store'].update_metadata(rid, 1, 'stale-fixture', lambda _: ({'bad': True}, None, None))
    deps['store'].release(rid, generations['new'])
    deps.pop('stage_hook')
    deps['request_id'] = 'Fixture-new-generation'
    (deps['installed_skill_root'] / 'chip-specs/SKILL.md').write_text('Fixture post-freeze change', encoding='utf-8')
    second = execute(deps, {**request, 'resume_run_id': rid})
    assert second['status'] == 'success', second
    assert deps['store'].metadata(rid, 'baseline') == baseline
    newer = deps['store'].metadata(rid, 'domain_budget.v1')
    assert newer['active_limit_ms'] == budget['active_limit_ms']
    assert newer['active_ms'] >= budget['active_ms']
    assert deps['hermes_runner'].calls == 1 and len(deps['search_provider'].calls) == 10


@pytest.mark.parametrize('version', ['hermes-open-web-v1', 'hermes-open-web-v2'])
def test_legacy_history_missing_proof_is_readonly_and_cannot_be_resumed_as_v3(acceptance, version, monkeypatch):
    from chip_model import server
    from chip_model.pipeline.open_web_test import _ensure_test_tables
    from chip_model.pipeline.test_workspace import runs_root
    deps, request = acceptance(urls=[DATA['urls']['positive']])
    current = execute(deps, request)
    assert current['status'] == 'success'
    old_id = 'Fixture-legacy-' + version[-2:]
    folder = runs_root(deps['source_db']) / old_id
    folder.mkdir(parents=True)
    _ensure_test_tables(folder / 'data.db')
    (folder / 'test_mode.json').write_text(json.dumps({'mode': 'test', 'pipeline': 'hermes-open-web'}))
    (folder / 'manifest.json').write_text(json.dumps({'schema_version': version, 'session_id': old_id,
        'skill': 'chip-specs', 'status': 'failed', 'units': [], 'classification': 'Fixture'}))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
    monkeypatch.setattr(server, 'get_db_path', lambda: deps['source_db'])
    api = TestClient(server.app)
    detail = api.get('/api/v1/data-agent/open-web-runs/' + old_id)
    assert detail.status_code == 200, detail.text
    saved = detail.json()
    assert saved['schema_version'] == version and saved['status'] == 'failed'
    assert not saved.get('baseline') and not saved.get('stages')
    assert not saved.get('actual_skill_usage') and not saved.get('audit_report')
    assert saved['facts'] == []
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()} == before
    # V3 has its own root. A legacy folder there must also reject a new write.
    shutil.copytree(folder, deps['store'].root / old_id)
    deps['request_id'] = 'Fixture-legacy-resume-attempt'
    with pytest.raises(ValueError):
        run_domain({**request, 'resume_run_id': old_id}, deps)
    with pytest.raises(ValueError):
        deps['store'].start('Fixture-old-write', request, old_id)
    assert not (deps['store'].root / old_id / 'run.sqlite3').exists()


def test_history_projects_actual_skill_executor_evidence(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']])
    result = execute(deps, request)
    history = read_v3_run(deps['store'].root, result['run_id'])
    usage = history.get('actual_skill_usage')
    assert usage, 'Actual executor events must remain visible as readonly skill usage evidence'
    assert {row['executor'] for row in usage} == {'hermes', 'backend_extract', 'backend_review'}
    assert all(row['loaded'] and row['executed'] for row in usage)
    assert all(row['evidence_refs'] for row in usage)
    assert next(r for r in usage if r['executor'] == 'hermes')['loaded_file_count'] == 20


def test_logical_digest_checks_every_business_table_column_and_row(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']])
    assert execute(deps, request)['status'] == 'success'
    source = deps['source_db']
    with closing(sqlite3.connect(source)) as db, db:
        # Populate all relation/provenance tables with controlled data so no
        # empty-table comparison can hide an omitted table or column.
        db.execute("INSERT INTO chip_model_benchmarks(notes) VALUES ('Fixture metric')")
        db.execute("INSERT INTO chip_model_compatibility(notes) VALUES ('Fixture compatibility')")
        db.execute("INSERT INTO deployment_guides(url,notes) VALUES ('https://fixture.example/guide','Fixture guide')")
        db.execute("INSERT INTO field_provenance(notes) VALUES ('Fixture provenance')")
    sequence = 0
    for table in TABLES:
        with closing(sqlite3.connect(source)) as db:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
        # A mutation in every non-key column must change the canonical digest.
        for column in columns:
            if column == 'id':
                continue
            sequence += 1
            before, records = logical_digest(source, source.parent / f'Fixture-digest-before-{sequence}.db')
            assert records[table]['columns'] == columns and records[table]['rows']
            with closing(sqlite3.connect(source)) as db, db:
                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=(SELECT min(rowid) FROM "{table}")',
                           (f'Fixture digest mutation {sequence}',))
            after, _ = logical_digest(source, source.parent / f'Fixture-digest-after-{sequence}.db')
            assert before != after, (table, column)


def test_review_all_three_technical_failures_never_promote_program_valid_candidate(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']], model=FixtureModel(phase_failures=['review']))
    result = execute(deps, request)
    assert result['status'] == 'failed', result
    assert result['execution_state']['all_model_chains_failed']
    assert result['statistics']['validated_candidates'] == 0
    assert len(deps['call_model'].calls) == 4  # Extract succeeds, independent review fails three times.
    saved = deps['store'].metadata(result['run_id'], 'domain_models.v1')
    row = next(iter(saved['candidates'].values()))
    assert row['status'] == 'review_failed'
    assert row['versions'][0]['program']['status'] == 'program_valid'
    assert not any(review.get('verdict') == 'accept' for review in row['reviews'])
    assert all(not f.get('update_eligible') for f in read_v3_run(deps['store'].root, result['run_id'])['field_changes'])


def test_hermes_exit_zero_without_finalize_is_partial_with_durable_selection_gap(acceptance):
    deps, request = acceptance(urls=[DATA['urls']['positive']], runner=FixtureHermes(skip_finalize=True))
    result = execute(deps, request)
    assert result['status'] == 'partial', result
    assert '未完成选择提交' in result['error']
    assert result['execution_state']['selection_finalization']['reason'] == 'finalize_not_submitted'
    assert result['execution_state']['selection']['decisions']
    assert not result['execution_state']['selection'].get('finalized')
    assert not deps['store'].list_tasks(result['run_id']) and not deps['call_model'].calls


@pytest.mark.parametrize('domain', DOMAINS)
def test_fixture_dataset_has_six_positive_and_negative_domains(domain):
    dataset = json.loads((FIXTURES / 'acceptance.json').read_text(encoding='utf-8'))
    assert dataset['classification'] == 'Fixture'
    case = dataset['domains'][domain]
    assert case['positive'] and case['negative'] and case['independent_review']
    assert case['expected']['status'] == 'success'
