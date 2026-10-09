"""V3 orchestration with real isolated stores and explicitly controlled adapters."""
import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from chip_model.pipeline.domain_discovery import runtime
from chip_model.pipeline.domain_discovery.rules import RULE_VERSION, BUNDLE_MANIFEST
from chip_model.pipeline.domain_discovery.search import ProviderResponse
from chip_model.pipeline.domain_discovery.store import RunStore, _file_hash
from tests.test_domain_tool_api import plan
from tests.test_domain_entities import Session

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('code,submitted,error', [(0, False, 'Hermes 已退出但未完成选择提交'),
    (1, False, 'Hermes 进程失败'), (0, True, None)])
def test_completion_uses_selection_store_not_stdout(code, submitted, error):
    check = getattr(runtime, 'hermes_completion_error', None)
    assert callable(check), 'V3 Hermes completion validation is missing'
    assert check(code, submitted) == error


class Provider:
    name = 'controlled-runtime-provider'
    calls = 0

    def search(self, query, limit, *, language):
        self.calls += 1
        return ProviderResponse(rows=[{'url': 'https://vendor.example/products',
            'title': 'Unknown-X accelerator specifications', 'snippet': 'Published specifications'}], outcome='success')


class Runner:
    """Fixture evidence is deliberately distinct from live Hermes validation."""
    calls = 0
    prompts = []

    def preflight(self, *, bundle_root, **kwargs):
        self.bundle_root = bundle_root
        manifest = json.loads((bundle_root / BUNDLE_MANIFEST).read_text(encoding='utf-8'))
        return {'resolver': {'rule_version': RULE_VERSION, 'files': {
            rel: {'path': str(bundle_root / rel), 'sha256': sha} for rel, sha in manifest['files'].items()}},
            'tool_names': ['open_web_search', 'open_web_preview', 'open_web_submit_selection'],
            'implementation': 'controlled-fixture', 'version': 'fixture'}

    def run(self, *, prompt, context, service, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        binding = {'contract_version': 3, 'pipeline': 'hermes-open-web-v3', 'run_id': context.run_id,
            **{k: context.store.get_run(context.run_id)['payload'][k] for k in ('domain', 'scope_type', 'chip')}}
        result = service.handle('search', {**binding, 'action': 'execute', 'request_id': 'search', 'plan': plan()})
        version = result['list_version']
        items = service.handle('search', {**binding, 'action': 'results', 'list_version': version})['items']
        service.handle('preview', {**binding, 'mode': 'initial', 'request_id': 'preview', 'list_version': version})
        decisions = [{'candidate_id': item['candidate_id'], 'decision': 'select', 'reason': '官方规格文档',
            'expected_info': '明确规格', 'linked_domains': [], 'entity_clues': [], 'matched_category': None} for item in items]
        if decisions:
            service.handle('selection', {**binding, 'action': 'append', 'request_id': 'append',
                'list_version': version, 'decisions': decisions})
        self.receipt = service.handle('selection', {**binding, 'action': 'finalize', 'request_id': 'final', 'list_version': version})
        return SimpleNamespace(returncode=0, stdout='untrusted diagnostic', stderr='')


class Model:
    model_name = 'controlled-runtime-model'

    def __init__(self):
        self.calls = []

    def call_model(self, **request):
        self.calls.append(request)
        return {'facts': [], 'linked_domains': []}


@pytest.fixture
def deps(tmp_path):
    source = tmp_path / 'source.db'
    with sqlite3.connect(source) as db:
        db.executescript((ROOT / 'tests/fixtures/domain_discovery/business_schema.sql').read_text(encoding='utf-8'))
        db.execute('CREATE TABLE models (id TEXT)')
        db.executemany('INSERT INTO chips (vendor,chip_model) VALUES (?,?)', [('Vendor', f'Known-{i}') for i in range(25)])
    installed = tmp_path / 'installed'
    shutil.copytree(ROOT / '.hermes-skills', installed)
    return {'source_db': source, 'installed_skill_root': installed, 'store': RunStore(tmp_path / 'runs'),
        'request_id': 'start-1', 'hermes_runner': Runner(), 'search_provider': [Provider()],
        'page_fetcher': Session('Vendor A X80 64GB PCIe GPU: 64GB HBM3'), 'call_model': Model(),
        'resolver': lambda _: ['93.184.216.34'], 'backoff': lambda _: None}


def run(deps, chip=None, resume=None):
    execute = getattr(runtime, 'run_domain', None)
    assert callable(execute), 'resumable V3 runtime is missing'
    return execute({'domain': 'specs', 'chip': chip, 'scope_type': 'chip' if chip else 'discovery',
                    'resume_run_id': resume}, deps)


def test_default_discovery_is_one_run_with_frozen_skill_and_no_chip_units(deps):
    before = _file_hash(deps['source_db'])
    result = run(deps)
    assert result['status'] == 'no_findings', result
    assert deps['hermes_runner'].calls == 1
    assert len(deps['store'].list_tasks(result['run_id'])) == 1
    assert _file_hash(deps['source_db']) == before
    assert result['statistics']['formal_business_writes'] == 0
    prompt = deps['hermes_runner'].prompts[0]
    assert all(item in prompt for item in ('RUN_ID=', 'SCHEMA_VERSION=hermes-open-web-v3',
        'BUNDLE_SHA=', 'resume_state', 'plan', 'select', 'complete'))
    assert len(deps['call_model'].calls) == 1
    artifact = Path(result['artifact_path']) / 'manifest.json'
    assert artifact.is_file(), 'terminal snapshot must be exported from authoritative store'
    saved = json.loads(artifact.read_text(encoding='utf-8'))
    assert saved['status'] == result['status'] and saved['run_id'] == result['run_id']
    assert saved['stages']['searching']['main']['status'] == 'completed'


def test_initial_unknown_chip_clue_reaches_stable_backend_context_and_resume(deps):
    first = run(deps, 'Unknown-X')
    assert first['status'] == 'no_findings', first
    payload = json.loads(deps['call_model'].calls[0]['messages'][-1]['content'])
    assert payload['identity_context']['initial_chip_clue'] == 'Unknown-X'
    deps['request_id'] = 'new-resume-request'
    again = run(deps, resume=first['run_id'])
    assert again['status'] == 'no_findings'
    assert again['chip'] == 'Unknown-X'
    assert deps['hermes_runner'].calls == 1 and len(deps['call_model'].calls) == 1


def test_known_chip_clue_does_not_exclude_generic_metadata_with_relevant_full_body(deps):
    class GenericMetadata(Provider):
        def search(self, query, limit, *, language):
            self.calls += 1
            return ProviderResponse(rows=[{'url': 'https://vendor.example/products',
                'title': 'Product documentation', 'snippet': 'Manufacturer technical documentation'}], outcome='success')
    deps['search_provider'] = [GenericMetadata()]
    deps['page_fetcher'] = Session('Known-0 GPU manufacturer specifications: 64GB HBM3')
    result = run(deps, 'Known-0')
    assert result['status'] == 'no_findings', result
    assert len(deps['store'].list_tasks(result['run_id'])) == 1
    assert result['scope_type'] == 'chip' and result['chip'] == 'Known-0'
    saved = deps['store'].metadata(result['run_id'], 'domain_search.v1')
    assert next(iter(saved['candidates'].values()))['matches_chip_hint'] is False
    assert not any(e['reason'] == 'chip_hint_mismatch' for q in saved['queries'].values() for e in q['exclusions'])


def test_actual_linked_failure_is_partial_and_projection_preserves_failed_work(deps):
    import requests
    from tests.test_domain_entities import fact
    # Add a legitimate linked selection through the actual selection contract.
    class LinkedRunner(Runner):
        def run(self, **kwargs):
            service = kwargs['service']
            actual_handle = service.handle
            def linked(tool, payload):
                if tool == 'selection' and payload.get('action') == 'append':
                    for item in payload['decisions']:
                        item['linked_domains'] = ['compute']
                return actual_handle(tool, payload)
            service.handle = linked
            return super().run(**kwargs)
    class EvidenceModel(Model):
        def call_model(self, **request):
            self.calls.append(request)
            payload = json.loads(request['messages'][-1]['content'])
            if payload['domain_id'] == 'compute':
                raise requests.ConnectionError('controlled linked provider failure')
            if payload['phase'] == 'review':
                return {'verdict': 'accept', 'reason': '原文明确SKU参数'}
            page = payload['page']
            return {'facts': [fact(({**page, 'text': page['text']},), payload['domain_id'])], 'linked_domains': []}
    deps['hermes_runner'] = LinkedRunner()
    deps['call_model'] = EvidenceModel()
    result = run(deps)
    assert result['status'] == 'partial', result
    assert result['statistics']['validated_candidates'] == 2
    work = json.loads((Path(result['artifact_path']) / 'work.json').read_text(encoding='utf-8'))
    assert any(item['kind'] == 'chunk' and item['state'] == 'failed' for item in work['work_items'])
    assert any(item['kind'] == 'model_call' and item['state'] == 'failed' for item in work['work_items'])
    assert len(work['metadata']['domain_budget.v1']['logical_operations']) > 0


def test_no_findings_requires_real_healthy_search_and_no_selection_does_not_complete(deps):
    class EmptyProvider(Provider):
        def search(self, query, limit, *, language):
            self.calls += 1
            return ProviderResponse(rows=[], outcome='healthy_empty')
    deps['search_provider'] = [EmptyProvider()]
    result = run(deps)
    assert result['status'] == 'no_findings', result
    assert deps['search_provider'][0].calls == 10 and not deps['call_model'].calls
    class NoSearchRunner(Runner):
        def run(self, **kwargs):
            return SimpleNamespace(returncode=0, stdout='I am finished', stderr='')
    deps['hermes_runner'] = NoSearchRunner()
    deps['request_id'] = 'uncompleted-second-run'
    result = run(deps)
    assert result['status'] == 'failed' and '未完成选择提交' in result['error']
    assert not deps['store'].metadata(result['run_id'], 'domain_selection.v1')


def test_independent_business_rejection_completes_without_unnecessary_identity_work(deps):
    from tests.test_domain_entities import fact
    class RejectModel(Model):
        def call_model(self, **request):
            self.calls.append(request)
            payload = json.loads(request['messages'][-1]['content'])
            if payload['phase'] == 'review':
                return {'verdict': 'reject', 'reason': '单独审核认为证据不足'}
            page = payload['page']
            return {'facts': [fact(({**page, 'text': page['text']},), payload['domain_id'])]}
    deps['call_model'] = RejectModel()
    result = run(deps)
    assert result['status'] == 'no_findings'
    tasks = deps['store'].list_tasks(result['run_id'])
    assert len(tasks) == 1 and tasks[0]['state'] == 'completed'
    assert len(deps['call_model'].calls) == 2
    work = json.loads((Path(result['artifact_path']) / 'work.json').read_text(encoding='utf-8'))
    rejected = [item for item in work['work_items'] if item['kind'] == 'candidate']
    assert all(item['state'] == 'completed' and item['record_state'] == 'semantic_rejected' for item in rejected)


def test_program_rejection_does_not_claim_independent_review_was_executed(deps):
    deps['call_model'].call_model = lambda **kwargs: {'facts': [42]}
    result = run(deps)
    assert result['status'] == 'no_findings'
    audit = [row['payload'] for row in deps['store'].events(result['run_id'])]
    assert any(row.get('executor') == 'backend_extract' and row['kind'] == 'rejected' for row in audit)
    assert not any(row.get('executor') == 'backend_review' for row in audit)


def test_finalized_selection_survives_restart_without_repeating_search_or_pages(deps):
    class Crash(BaseException):
        pass
    def hook(stage):
        if stage == 'after_finalize':
            raise Crash('controlled worker crash')
    deps['stage_hook'] = hook
    first = run(deps)
    assert first['status'] == 'interrupted'
    assert deps['store'].metadata(first['run_id'], 'domain_selection.v1')['finalized']
    (deps['installed_skill_root'] / 'chip-specs/SKILL.md').write_text('changed after freezing', encoding='utf-8')
    deps['store'] = RunStore(deps['store'].root)
    deps.pop('stage_hook')
    deps['request_id'] = 'resume-2'
    again = run(deps, resume=first['run_id'])
    assert again['status'] == 'no_findings', again
    assert deps['hermes_runner'].calls == 1 and deps['search_provider'][0].calls == 10
    assert len(deps['call_model'].calls) == 1


def test_wrong_selected_skill_path_fails_before_external_search(deps):
    original = deps['hermes_runner'].preflight
    def bad(**kwargs):
        evidence = original(**kwargs)
        evidence['resolver']['files']['chip-specs/SKILL.md']['path'] = str(deps['installed_skill_root'] / 'chip-specs/SKILL.md')
        return evidence
    deps['hermes_runner'].preflight = bad
    result = run(deps)
    assert result['status'] == 'failed'
    assert deps['search_provider'][0].calls == 0
    assert len(deps['call_model'].calls) == 0


def test_blocked_model_is_stopped_at_absolute_budget_and_late_response_cannot_commit(deps):
    import threading
    import time
    entered, unblock, returned = threading.Event(), threading.Event(), threading.Event()
    original = deps['call_model'].call_model
    def blocked(**kwargs):
        entered.set()
        unblock.wait(10)
        try:
            return original(**kwargs)
        finally:
            returned.set()
    deps['call_model'].call_model = blocked
    deps['timeout'] = 2
    start = time.monotonic()
    result = run(deps)
    elapsed = time.monotonic() - start
    assert entered.is_set()
    assert result['status'] == 'partial' and elapsed < 3.5
    run_id = result['run_id']
    saved = deps['store'].metadata(run_id, 'domain_models.v1')
    unblock.set()
    assert returned.wait(3)
    time.sleep(.2)
    assert deps['store'].metadata(run_id, 'domain_models.v1') == saved
    assert deps['store'].get_run(run_id)['status'] == 'partial'
    deps['timeout'] = 1800
    deps['request_id'] = 'new-key-cannot-reset-budget'
    again = run(deps, resume=run_id)
    assert again['status'] == 'partial'
    assert len(deps['call_model'].calls) == 1


def test_default_adapter_reports_missing_actual_install_without_search(deps, monkeypatch):
    monkeypatch.setenv('HERMES_CLI_COMMAND', 'missing-hermes-command-for-runtime-test')
    deps.pop('hermes_runner')
    result = run(deps)
    assert result['status'] == 'failed'
    assert 'Hermes' in result['error'] and '安装' in result['error']
    assert deps['search_provider'][0].calls == 0


def test_legacy_entrypoint_delegates_v3_and_rejects_field_scope(deps, monkeypatch):
    from chip_model.pipeline import hermes_open_web_orchestrator as launcher
    calls = []
    def capture(request, dependencies):
        calls.append((request, dependencies))
        return {'status': 'no_findings', 'run_id': 'v3'}
    monkeypatch.setattr(runtime, 'run_domain', capture)
    result = launcher.run_hermes_open_web_test(source_db=deps['source_db'], skill='chip-specs')
    assert result['run_id'] == 'v3'
    assert calls[0][0]['chip'] is None and calls[0][0]['scope_type'] == 'discovery'
    with pytest.raises(ValueError, match='字段|数据域'):
        launcher.run_hermes_open_web_test(source_db=deps['source_db'], skill='chip-specs', target_fields=['tdp_w'])


def test_response_saved_before_task_commit_survives_worker_restart(deps, monkeypatch):
    original = deps['store'].complete_task
    class Crash(BaseException):
        pass
    def crash(*args, **kwargs):
        raise Crash('response persisted before task receipt')
    monkeypatch.setattr(deps['store'], 'complete_task', crash)
    first = run(deps)
    assert first['status'] == 'interrupted'
    tasks = deps['store'].list_tasks(first['run_id'])
    assert tasks[0]['state'] == 'running' and tasks[0]['attempts'] == 1
    assert len(deps['call_model'].calls) == 1
    monkeypatch.setattr(deps['store'], 'complete_task', original)
    deps['request_id'] = 'restart'
    final = run(deps, resume=first['run_id'])
    assert final['status'] == 'no_findings'
    assert len(deps['call_model'].calls) == 1
    task = deps['store'].list_tasks(first['run_id'])[0]
    assert task['state'] == 'completed' and task['attempts'] == 2


def test_runtime_fresh_binds_after_same_page_identity_dependency(deps):
    from tests.test_domain_entities import fact
    class EvidenceModel(Model):
        def call_model(self, **request):
            self.calls.append(request)
            payload = json.loads(request['messages'][-1]['content'])
            if payload['phase'] == 'review':
                return {'verdict': 'accept', 'reason': '原文明确SKU与参数'}
            page = payload['page']
            snapshot = {**page, 'text': page['text']}
            domain = payload['domain_id']
            candidate = fact((snapshot,), domain)
            return {'facts': [candidate], 'linked_domains': []}
    deps['call_model'] = EvidenceModel()
    result = run(deps)
    assert result['status'] == 'success', result
    tasks = deps['store'].list_tasks(result['run_id'])
    assert len(tasks) == 2
    assert tasks[1]['payload']['identity_dependency']
    assert tasks[1]['payload']['snapshot_id'] == tasks[0]['payload']['snapshot_id']
    assert result['statistics']['new_entities'] == 1
    assert result['statistics']['validated_candidates'] == 2
    assert len(deps['call_model'].calls) == 4


def test_terminal_commit_atomically_revokes_lease(deps):
    store = deps['store']
    store.start('one', {'domain': 'specs'}, 'run-final')
    generation = store.claim('run-final', 'owner', 5)
    finalizer = getattr(store, 'finish_run', None)
    assert callable(finalizer), 'terminal state and lease revocation must be atomic'
    finalizer('run-final', generation, {'status': 'partial', 'run_id': 'run-final'})
    assert store.get_run('run-final')['status'] == 'partial'
    assert store.metadata('run-final', 'domain_runtime_result.v1')['status'] == 'partial'
    with pytest.raises(ValueError, match='租约'):
        store.update_metadata('run-final', generation, 'late-result', lambda _: ({'bad': True}, None, None))


def test_finish_replays_only_identical_terminal_receipt_and_old_owner_cannot_replace(deps):
    store = deps['store']
    store.start('one', {'domain': 'specs'}, 'run-final')
    generation = store.claim('run-final', 'owner', 5)
    result = {'status': 'partial', 'run_id': 'run-final'}
    store.finish_run('run-final', generation, result)
    original_events = store.events('run-final')
    assert store.finish_run('run-final', generation, result) == result
    assert store.events('run-final') == original_events
    with pytest.raises(ValueError):
        store.finish_run('run-final', generation, dict(result, status='success'))
    next_generation = store.claim('run-final', 'new-owner', 5)
    with pytest.raises(ValueError, match='租约'):
        store.finish_run('run-final', generation, result)
    assert store.get_run('run-final')['status'] == 'partial'
    store.finish_run('run-final', next_generation, dict(result, status='no_findings'))


def test_expired_worker_cannot_commit_terminal_state(deps):
    class Clock:
        now = 1000.
        def __call__(self): return self.now
    clock = Clock()
    deps['clock'] = clock
    deps['store'] = RunStore(deps['store'].root, clock=clock)
    original = deps['call_model'].call_model
    def expire(**kwargs):
        clock.now += 6
        return original(**kwargs)
    deps['call_model'].call_model = expire
    result = run(deps)
    assert result['status'] == 'interrupted'
    assert result['terminal_persisted'] is False
    assert deps['store'].metadata(result['run_id'], 'domain_runtime_result.v1') is None
    assert deps['store'].list_tasks(result['run_id'])[0]['state'] == 'running'


@pytest.fixture
def sdk_runner(tmp_path):
    """A subprocess SDK fixture. It provides no real Hermes host proof."""
    import sys
    sdk = tmp_path / 'controlled-sdk'
    sdk.mkdir()
    files = {
        'cli.py': "import sys; print('controlled-SDK-fixture' if '--version' in sys.argv else '--skills --toolsets')",
        'tools/__init__.py': '',
        'tools/skills_tool.py': '''import json, os, yaml
from pathlib import Path
def skill_view(name, preprocess=False):
    home = Path(os.environ['HERMES_HOME'])
    bundle = Path(yaml.safe_load((home / 'config.yaml').read_text())['skills']['external_dirs'][0])
    p = bundle / name / 'SKILL.md'
    return json.dumps({'success': True, '_source_path': str(p), 'skill_dir': str(p.parent), 'content': p.read_text(encoding='utf-8')})
''',
        'tools/registry.py': '''from types import SimpleNamespace
class Registry:
    def __init__(self): self.entries = {}
    def get_tool_names_for_toolset(self, toolset): return list(self.entries)
    def get_schema(self, name): return self.entries[name]['schema']
    def get_entry(self, name): return SimpleNamespace(**self.entries[name])
registry = Registry()
''',
        'hermes_cli/__init__.py': '',
        'hermes_cli/plugins.py': '''import importlib.util, os
from pathlib import Path
from tools.registry import registry
def discover_plugins():
    p = Path(os.environ['HERMES_HOME']) / 'plugins/aishperf-open-web/__init__.py'
    spec = importlib.util.spec_from_file_location('controlled_plugin', p)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    class Context:
        def register_tool(self, **kwargs): registry.entries[kwargs['name']] = kwargs
    module.register(Context())
''',
        'run_agent.py': '''import json, os
from pathlib import Path
from tools.registry import registry
class AIAgent:
    def __init__(self, enabled_toolsets=None, skip_context_files=None, skip_memory=None,
        skip_background_review=None, run_budget_seconds=None, cwd=None, prefill_messages=None, **kwargs):
        self.valid_tool_names = set(registry.entries)
        self.tools = [{'function': value['schema']} for value in registry.entries.values()]
        Path(os.environ['HERMES_HOME'], 'agent-context.json').write_text(json.dumps({'cwd': cwd, 'toolsets': enabled_toolsets,
            'skip_context_files': skip_context_files, 'skip_memory': skip_memory, 'skip_background_review': skip_background_review}))
    def run_conversation(self, user_message):
        content = json.loads(user_message.split('\\n', 3)[3])
        binding = {'run_id': user_message.split('\\n')[0][7:], 'contract_version': 3, 'pipeline': 'hermes-open-web-v3',
            **{k:content[k] for k in ('domain','scope_type','chip')}}
        def tool(name, **kwargs):
            return json.loads(registry.entries[name]['handler']({**binding, **kwargs}))
        queries = [{'query_id': 'sdk-q'+str(i), 'text': '芯片 参数 '+str(i) if i<5 else 'GPU specs '+str(i),
            'language': 'zh' if i<5 else 'en', 'reason': '查找资料', 'page_type':'datasheet'} for i in range(10)]
        result = tool('open_web_search', action='execute', request_id='sdk-search', plan={'round_number':0,'queries':queries})
        version = result['list_version']
        items = tool('open_web_search', action='results', list_version=version)['items']
        tool('open_web_preview', mode='initial', request_id='sdk-preview', list_version=version)
        decisions = [{'candidate_id': i['candidate_id'], 'decision':'select','reason':'明确规格', 'expected_info':'芯片规格',
            'linked_domains':[],'entity_clues':[],'matched_category':None} for i in items]
        tool('open_web_submit_selection', action='append', request_id='sdk-append', list_version=version, decisions=decisions)
        tool('open_web_submit_selection', action='finalize', request_id='sdk-final', list_version=version)
'''}
    for relative, text in files.items():
        target = sdk / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return runtime.HermesRunner(command=[sys.executable, str(sdk / 'cli.py')], python=sys.executable, sdk_root=sdk)


def test_installed_adapter_executes_subprocess_resolver_plugin_and_three_real_http_boundaries(deps, sdk_runner):
    deps['hermes_runner'] = sdk_runner
    result = run(deps)
    assert result['status'] == 'no_findings', result
    report = deps['store'].metadata(result['run_id'], 'domain_hermes_preflight.v1')
    assert report['version'] == 'controlled-SDK-fixture'
    assert len(report['resolver']['files']) == 20
    assert all(Path(row['path']).is_relative_to(deps['store'].run_path(result['run_id'])) for row in report['resolver']['files'].values())
    assert not any((p / '.git').exists() for p in [sdk_runner.cwd, *sdk_runner.cwd.parents])
    assert not (sdk_runner.home / '.env').exists()
    context = json.loads((sdk_runner.home / 'agent-context.json').read_text())
    assert context['toolsets'] == ['aishperf_open_web']
    assert context['skip_context_files'] and context['skip_memory'] and context['skip_background_review']


def test_installed_adapter_rejects_agent_extra_tools_before_model_conversation(deps, sdk_runner):
    source = Path(sdk_runner.sdk_root) / 'run_agent.py'
    source.write_text(source.read_text(encoding='utf-8').replace("self.valid_tool_names = set(registry.entries)",
        "self.valid_tool_names = set(registry.entries) | {'terminal'}"), encoding='utf-8')
    deps['hermes_runner'] = sdk_runner
    result = run(deps)
    assert result['status'] == 'failed'
    assert deps['search_provider'][0].calls == 0
    assert not deps['store'].metadata(result['run_id'], 'domain_selection.v1')


def test_cancelled_adapter_cannot_launch_a_late_child(sdk_runner, monkeypatch):
    import subprocess
    sdk_runner.cancel()
    def forbidden(*args, **kwargs):
        pytest.fail('cancelled adapter launched a late subprocess')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    with pytest.raises(TimeoutError, match='cancel'):
        sdk_runner._invoke(['any-command'], timeout=1)


def test_installed_adapter_rejects_schema_identical_tool_from_another_handler_source(deps, sdk_runner):
    source = Path(sdk_runner.sdk_root) / 'hermes_cli/plugins.py'
    source.write_text(source.read_text(encoding='utf-8').replace(
        "def register_tool(self, **kwargs): registry.entries[kwargs['name']] = kwargs",
        "def register_tool(self, **kwargs):\n            kwargs['handler'] = lambda *a, **k: '{}'\n            registry.entries[kwargs['name']] = kwargs"), encoding='utf-8')
    deps['hermes_runner'] = sdk_runner
    result = run(deps)
    assert result['status'] == 'failed'
    assert deps['store'].metadata(result['run_id'], 'domain_hermes_preflight.v1') is None
    assert deps['search_provider'][0].calls == 0


def test_installed_adapter_requires_same_probed_plugin_bytes_at_execution(deps, sdk_runner):
    original = sdk_runner.run
    def changed(**kwargs):
        plugin = sdk_runner.home / 'plugins/aishperf-open-web/__init__.py'
        plugin.write_text(plugin.read_text(encoding='utf-8') + '\n# changed after actual probe\n', encoding='utf-8')
        return original(**kwargs)
    sdk_runner.run = changed
    deps['hermes_runner'] = sdk_runner
    result = run(deps)
    assert result['status'] == 'failed'
    assert deps['search_provider'][0].calls == 0
