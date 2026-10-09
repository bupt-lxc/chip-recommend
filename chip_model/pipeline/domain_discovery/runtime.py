"""Resumable isolated Hermes selection and frozen backend discovery stages."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from .contracts import DOMAINS
from .entities import EntityResolver, candidate_entity_hint
from .models import ModelService
from .store import _hash, _json


def domain_result_status(main, linked):
    """Local completion summary; the runtime also accounts for other stages."""
    results = [main, *linked]
    if any(not r.get('complete', False) or r.get('outstanding') for r in results):
        return 'partial'
    for result in results:
        if 'bindings' in result:
            if any(row.get('validated') for row in result['bindings']):
                return 'success'
        elif any(row.get('status') == 'validated' for row in result.get('candidates', [])):
            return 'success'
    return 'no_findings'


class LinkedDomainScheduler:
    KEY = 'domain_linked.v1'

    def __init__(self, store, run_id, generation):
        self.store, self.run_id, self.generation = store, run_id, generation

    def snapshot(self):
        return self.store.metadata(self.run_id, self.KEY) or {'hints':[]}

    def _job(self, job):
        if job.get('domain') not in DOMAINS or job.get('main_domain') not in DOMAINS:
            raise ValueError('job outside frozen six domains')
        if not job.get('snapshot_id') or not job.get('url'):
            raise ValueError('job requires original URL and saved snapshot')
        if job.get('depth') not in (0, 1):
            raise ValueError('job exceeds one business layer')

    def schedule_linked(self, job, extraction):
        """Merge selection/extraction hints; linked/identity jobs record hints only."""
        self._job(job)
        if extraction.get('snapshot_id', job['snapshot_id']) != job['snapshot_id']:
            raise ValueError('linked result snapshot mismatch')
        hinted = sorted({domain for chunk in extraction.get('chunks', [])
                         for domain in chunk.get('linked_domains', []) if domain in DOMAINS})
        stopped = job.get('linked') or job.get('depth') == 1 or job.get('identity_dependency')
        existing = {task['payload']['domain']:task for task in self.store.list_tasks(self.run_id)
                    if task['payload'].get('url') == job['url'] and not task['payload'].get('identity_dependency')
                    and task['payload'].get('domain') != job['main_domain']}
        domains = [] if stopped else [domain for domain in DOMAINS
                    if domain != job['main_domain'] and (domain in hinted or domain in existing)]
        jobs = []
        for domain in domains:
            payload = existing[domain]['payload'] if domain in existing else {
                **job, 'domain':domain, 'linked':True, 'depth':1}
            if payload['snapshot_id'] != job['snapshot_id']:
                raise ValueError('URL/domain task already bound to another snapshot')
            jobs.append(('domain-job-' + _hash(_json([job['url'], domain])), payload))
        request = {'url':job['url'], 'snapshot_id':job['snapshot_id'], 'domain':job['domain'],
                   'hinted':hinted, 'domains':domains, 'stopped':bool(stopped)}
        def prepare(values):
            state = values[self.KEY] or {'hints':[]}
            state['hints'].append({**request, 'ignored_reason':'one_layer_only' if stopped else None})
            return {self.KEY:state}, jobs, {'task_ids':[], 'current_stage':'extracting'}, {
                'kind':'linked_domains_scheduled', 'stage':'extracting', 'domain_id':job['domain'], **request}
        receipt = self.store.commit_tool_request(self.run_id, self.generation,
                     'linked-schedule-' + _hash(_json(request)), request, [self.KEY], prepare)
        return receipt['task_ids']

    def schedule_identity(self, job, candidates):
        """Identity is a common dependency, deduped by URL and complete hint."""
        self._job(job)
        resolver = EntityResolver(self.store, self.run_id, self.generation)
        ids = []
        for supplied in candidates:
            row, candidate, _ = resolver._proof(supplied['logical_candidate_id'])
            if row['versions'][-1].get('program', {}).get('status') != 'program_valid':
                continue
            if row['status'] != 'validated':
                # Independent rejection/unresolved evidence is a completed
                # business decision. It needs no new identity model work to
                # become a terminal, non-validated comparison outcome.
                continue
            if candidate['evidence']['snapshot_id'] != job['snapshot_id']:
                raise ValueError('identity dependency candidate snapshot mismatch')
            if candidate['domain_id'] == 'identity':
                continue
            hint = candidate_entity_hint(candidate)
            if resolver.resolve_entity(hint)['status'] in {'new','existing','out_of_scope'}:
                continue
            try:
                canonical_hint, _ = resolver._hint(hint)
            except (ValueError, KeyError, TypeError):
                continue  # Preserve incomplete candidates; never guess a vendor/SKU.
            key = 'identity-dependency-' + _hash(_json([job['url'], canonical_hint]))
            payload = {**job, 'domain':'identity', 'linked':True, 'depth':1,
                       'task_scope':'identity', 'identity_dependency':True,
                       'entity_hint':hint, 'canonical_entity_hint':canonical_hint}
            existing = next((task for task in self.store.list_tasks(self.run_id)
                             if task['task_key'] == key), None)
            if existing is not None:
                if existing['payload']['snapshot_id'] != payload['snapshot_id']:
                    raise ValueError('identity dependency snapshot conflict')
                tid = existing['task_id']
            else:
                tid = self.store.enqueue(self.run_id, self.generation, key, payload)
            if tid not in ids:
                ids.append(tid)
        return ids

    def execute_task(self, task_id, model_factory):
        """Process one saved job with frozen extract/review, no worker orchestration.

        Factory(domain_id, task_scope) must return a bound ModelService. All
        identity postprocessing references its original saved version/proof.
        Recovery/retries/active-budget renewal are owned by the future runtime.
        """
        task = next((task for task in self.store.list_tasks(self.run_id) if task['task_id'] == task_id), None)
        if task is None:
            raise ValueError('unknown saved domain task')
        if task['state'] == 'completed':
            return task['result']
        job = task['payload']
        self._job(job)
        scope = 'identity' if job.get('identity_dependency') else 'linked' if job['linked'] else 'main'
        service = model_factory(job['domain'], scope)
        if (not isinstance(service, ModelService) or service.store is not self.store
                or service.run_id != self.run_id or service.generation != self.generation
                or service.domain_id != job['domain'] or service.task_scope != scope):
            raise ValueError('model factory returned an unbound domain service')
        if task['state'] == 'pending':
            self.store.claim_task(self.run_id, self.generation, task_id)
        elif task['state'] != 'running' or task['generation'] != self.generation:
            raise ValueError('task requires explicit old-generation recovery')
        # No later resolution is injected into a completed model input context.
        clue = self.store.get_run(self.run_id)['payload'].get('chip')
        # The original clue is immutable. Never inject later entity resolutions
        # into cached extraction/review inputs on recovery.
        context = {'initial_chip_clue': clue} if clue else {}
        result = service.extract_page(job['snapshot_id'], identity_context=context)
        resolver = EntityResolver(self.store, self.run_id, self.generation)
        if job['domain'] == 'identity':
            for row in result['candidates']:
                if row['status'] == 'validated':
                    resolver.register_identity(row['logical_candidate_id'])
        identity_ids = [] if job['domain'] == 'identity' else self.schedule_identity(job, result['candidates'])
        linked_ids = self.schedule_linked(job, result)
        # Invalid model candidates remain in the original result with their
        # rejection; only typed, program-passing versions enter dependency gates.
        bindings = [resolver.bind_candidate(row['logical_candidate_id']) for row in result['candidates']
                    if row['versions'][-1].get('program', {}).get('status') == 'program_valid']
        result = {**result, 'bindings':bindings, 'linked_task_ids':linked_ids, 'identity_task_ids':identity_ids}
        if result['complete'] and not result.get('outstanding'):
            self.store.complete_task(self.run_id, self.generation, task_id, result)
        else:
            def save_partial(state):
                state = state or {'hints':[]}
                state.setdefault('job_outcomes', {})[task_id] = result
                return state, None, {'kind':'domain_task_incomplete', 'stage':'extracting',
                                     'domain_id':job['domain'], 'task_id':task_id,
                                     'snapshot_id':job['snapshot_id'], 'outstanding':result['outstanding']}
            self.store.update_metadata(self.run_id, self.generation, self.KEY, save_partial)
        return result


def hermes_completion_error(returncode, submitted):
    if returncode != 0:
        return 'Hermes 进程失败'
    return None if submitted else 'Hermes 已退出但未完成选择提交'


def build_domain_prompt(run, bundle_root, resume_state):
    from .rules import load_phase
    phases = [load_phase(bundle_root, run['domain'], phase) for phase in ('plan', 'select', 'complete')]
    return ('RUN_ID=' + run['run_id'] + '\nSCHEMA_VERSION=hermes-open-web-v3\nBUNDLE_SHA='
            + phases[0]['bundle_sha256'] + '\n' + _json({
        'domain': run['domain'], 'chip': run.get('chip'), 'scope_type': run['scope_type'],
        'loaded_skill_phases': phases, 'resume_state': resume_state,
        'instructions': '执行完整冻结 Skill 的 plan/select/complete。初始10条短词查询，使用三个审计工具 '
        'open_web_search/open_web_preview/open_web_submit_selection；分页读取500字符预览，对主URL逐条提交选择或拒绝理由、'
        '预计信息和可选其他域。chip只是最初线索。finalize receipt仅代表选择完成，后台全文提取、独立审核和比较尚未完成。'
        '不得调用terminal/web/cron或发布。'}))


class _AuditModel:
    def __init__(self, client, store, run_id, generation, bundle):
        self.client, self.store, self.run_id, self.generation, self.bundle = client, store, run_id, generation, bundle
        self.model_name = getattr(client, 'model_name', 'configured-model')
        self.audit_secrets = getattr(client, 'audit_secrets', ())

    def event(self, kind, executor, domain, **details):
        self.store.update_metadata(self.run_id, self.generation, 'domain_executor_audit.v1',
            lambda state: (state or {}, None, {'kind': kind, 'executor': executor, 'domain_id': domain, **details}))

    def call_model(self, *, messages, timeout):
        from .rules import load_phase
        payload = json.loads(messages[-1]['content'])
        phase, domain = payload['phase'], payload['domain_id']
        executor = 'backend_' + phase
        frozen = load_phase(self.bundle, domain, phase)
        self.event('loaded', executor, domain, phase=phase, bundle_sha256=frozen['bundle_sha256'],
                   input_sha256=_hash(_json(messages)), reference_hashes=frozen['reference_hashes'])
        self.event('executed', executor, domain, phase=phase)
        try:
            call = getattr(self.client, 'call_model', self.client)
            response = call(messages=messages, timeout=timeout)
        except Exception:
            self.event('failed', executor, domain, phase=phase)
            raise
        return response


def _execute_domain(store, run_id, generation, owner, budget, deps):
    from chip_model.domain_tool_api import ActiveToolContext, DomainToolService, SELECTION_KEY, TOOL_NAMES, verify_tool_contract
    from .rules import freeze_bundle, load_phase, verify_loaded_package
    from .contracts import DomainContract, validate_domain_schema
    from .pages import PageService
    from .search import SearchExecutor, default_providers, redact
    from .models import OpenAIModelTransport
    from .compare import ComparisonService

    run = store.get_run(run_id)
    root = store.run_path(run_id)
    bundle = root / 'skill_bundle'
    manifest = freeze_bundle(deps['installed_skill_root'], bundle)
    baseline = store.ensure_baseline(run_id, generation, deps['source_db'],
        rule_bundle_hash=manifest['bundle_sha256'], manifest=manifest)
    with closing(sqlite3.connect((root / 'data.db').as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        for domain in DOMAINS:
            validate_domain_schema(db, DomainContract.model_validate(load_phase(bundle, domain, 'extract')['contract']))
    # Mandatory even for completed runs, before any network or model call.
    resolver = EntityResolver(store, run_id, generation)
    verify_tool_contract(bundle)
    prior = store.metadata(run_id, 'domain_runtime_result.v1')
    if prior and prior['status'] in {'success', 'no_findings'}:
        return prior
    store.recover_operations(run_id, generation)
    store.recover_tasks(run_id, generation)
    providers = deps.get('search_provider')
    if providers is None:
        providers = default_providers(proxy=deps.get('proxy'))
    elif not isinstance(providers, (list, tuple)):
        providers = [providers]
    search = SearchExecutor(store, budget, providers=providers, backoff=deps.get('backoff'))
    page_options = {k: deps[k] for k in ('resolver', 'proxy', 'backoff') if k in deps}
    pages = PageService(store, run_id, generation, budget, session=deps.get('page_fetcher'),
                        monotonic=budget.monotonic, **page_options)
    context = ActiveToolContext(store, run_id, generation, owner, budget, search, pages, bundle)
    service = DomainToolService(store, lambda rid: context if rid == run_id else None)
    runner = deps['hermes_runner']
    selection = store.metadata(run_id, SELECTION_KEY) or {}
    if not selection.get('finalized'):
        store.checkpoint(run_id, generation, 'preflight', {'status': 'running'})
        evidence = runner.preflight(bundle_root=bundle, context=context, service=service,
            timeout=_remaining(budget, 60))
        sha = verify_loaded_package(bundle, evidence['resolver'])
        if evidence.get('tool_names') != list(TOOL_NAMES.values()):
            raise ValueError('Hermes capability probe did not isolate the three audited tools')
        store.update_metadata(run_id, generation, 'domain_hermes_preflight.v1',
            lambda _: (evidence, None, {'kind': 'loaded', 'executor': 'hermes', 'bundle_sha256': sha,
                                       'resolver': evidence['resolver']}))
        store.checkpoint(run_id, generation, 'selecting', {'status': 'running'})
        prompt = build_domain_prompt({**run['payload'], 'run_id': run_id}, bundle, {
            'selection': selection, 'search': search._state(), 'tasks': store.list_tasks(run_id)})
        store.update_metadata(run_id, generation, 'domain_executor_audit.v1', lambda state: (state or {}, None,
            {'kind': 'executed', 'executor': 'hermes', 'phase': 'plan/select/complete'}))
        completed = runner.run(prompt=prompt, bundle_root=bundle, context=context, service=service,
                               timeout=_remaining(budget, 1800))
        selection = store.metadata(run_id, SELECTION_KEY) or {}
        diagnostic = {'returncode': completed.returncode,
            'stdout_tail': redact(str(getattr(completed, 'stdout', '') or ''))[-4000:],
            'stderr_tail': redact(str(getattr(completed, 'stderr', '') or ''))[-2000:]}
        error = hermes_completion_error(completed.returncode, selection.get('finalized', False))
        store.update_metadata(run_id, generation, 'domain_hermes_diagnostic.v1', lambda _: (diagnostic, None,
            {'kind': 'failed' if error else 'accepted', 'executor': 'hermes', 'phase': 'complete', 'error': error}))
        if error:
            raise ValueError(error)
    if deps.get('stage_hook'):
        deps['stage_hook']('after_finalize')
    budget.record_stage('selecting', 'completed', scope='main', reason='durable selection finalize receipt')
    store.checkpoint(run_id, generation, 'extracting', {'status': 'running'})
    client = deps.get('call_model')
    if client is None:
        client = OpenAIModelTransport()
    audited = _AuditModel(client, store, run_id, generation, bundle)
    scheduler = LinkedDomainScheduler(store, run_id, generation)
    def factory(domain, scope):
        return ModelService(store, run_id, generation, budget, pages, bundle, domain_id=domain,
            task_scope=scope, client=audited, backoff=deps.get('backoff'),
            model_seconds=deps.get('model_seconds', 60))
    processed = set()
    while True:
        # Process each pending/running task once per worker; partial remains
        # recoverable. Completed tasks never call extract/review again.
        task = next((t for t in store.list_tasks(run_id) if t['state'] != 'completed' and t['task_id'] not in processed), None)
        if task is None:
            break
        _remaining(budget, 60)
        processed.add(task['task_id'])
        result = scheduler.execute_task(task['task_id'], factory)
        audited.event('accepted' if result['complete'] else 'failed', 'backend_extract', result['domain_id'],
                      task_id=task['task_id'], scope=result['scope'], complete=result['complete'])
        for row in result['candidates']:
            accepted = row['status'] == 'validated'
            if row['status'] == 'program_rejected':
                audited.event('rejected', 'backend_extract', row['domain_id'],
                              logical_candidate_id=row['logical_candidate_id'], status=row['status'])
            elif row.get('reviews') or row['status'] in {'review_failed', 'pending', 'budget_stopped'}:
                audited.event('accepted' if accepted else 'failed' if row['status'].endswith('failed') else 'rejected',
                    'backend_review', row['domain_id'], logical_candidate_id=row['logical_candidate_id'], status=row['status'])
    store.checkpoint(run_id, generation, 'comparing', {'status': 'running'})
    comparisons = ComparisonService(store, run_id, generation, pages=pages)
    models = store.metadata(run_id, ModelService.KEY) or {}
    for identifier in models.get('candidates', {}):
        comparisons.compare_candidate(identifier)
    # Comparison/statistics rebind using ALL completed identity evidence. Cached
    # scheduler bindings are never authoritative for the final validated count.
    statistics = comparisons.statistics()
    incomplete = any(t['state'] != 'completed' for t in store.list_tasks(run_id))
    status = 'partial' if incomplete else 'success' if statistics['validated_candidates'] else 'no_findings'
    budget.record_stage('comparison', 'partial_failure' if incomplete else 'completed', scope='main',
        reason='unfinished tasks' if incomplete else 'all saved candidates compared')
    return {**run['payload'], 'run_id': run_id, 'status': status, 'statistics': statistics,
        'baseline_sha256': baseline['baseline_sha256'], 'bundle_sha256': manifest['bundle_sha256'],
        'selection_receipt_id': selection.get('receipt_id'), 'artifact_path': str(root),
        'pipeline': 'hermes-open-web-v3', 'contract_version': 3}


def _remaining(budget, cap):
    budget.tick()
    state = budget.snapshot()
    remaining = (state['active_limit_ms'] - state['active_ms']) / 1000
    if remaining <= 0:
        raise TimeoutError('active budget exhausted')
    return min(cap, remaining)


def run_domain(request, dependencies):
    """Run one domain, with replaceable real-boundary adapters and durable state.

    A supervisor accounts active time and renews the lease while callbacks block.
    Deadline stops authorization and fences late callbacks; requests inactivity
    timeouts alone are never treated as an absolute run deadline.
    """
    from .contracts import normalize_start
    from .budget import RunBudget
    from .store import RunStore
    from .search import redact
    deps = dict(dependencies)
    source = Path(deps['source_db']).resolve()
    deps['source_db'] = source
    canonical = normalize_start({k: v for k, v in request.items() if k != 'scope_type'})
    if request.get('scope_type', canonical['scope_type']) != canonical['scope_type']:
        raise ValueError('scope_type 与 chip 不一致')
    timeout = deps.get('timeout', 1800)
    if type(timeout) is not int or not 1 <= timeout <= 1800:
        raise ValueError('timeout 必须为 1..1800 秒整数')
    request_id = deps.get('request_id') or uuid.uuid4().hex
    clock = deps.get('clock')
    epoch = getattr(clock, 'epoch', clock) if clock else time.time
    monotonic = getattr(clock, 'monotonic', clock) if clock else time.monotonic
    store = deps.get('store') or RunStore(source.parent / 'domain_runs', clock=epoch)
    resume = canonical['resume_run_id']
    if resume:
        original = store.get_run(resume)['payload']
        if original['domain'] != canonical['domain'] or (canonical['chip'] is not None and canonical['chip'] != original['chip']):
            raise ValueError('续跑数据域或最初芯片线索与原运行不一致')
        run_id = resume
    else:
        started = store.start(request_id, canonical, 'run-' + uuid.uuid4().hex)
        run_id = started['run_id']
    owner = 'worker-' + uuid.uuid4().hex
    generation = store.claim(run_id, owner, 5)
    budget = RunBudget(store, run_id, generation, active_seconds=None if store.metadata(run_id, RunBudget.KEY) else timeout,
                       monotonic=monotonic, epoch=epoch)
    deps.setdefault('installed_skill_root', Path(os.getenv('AISH_PERF_HERMES_SKILL_ROOT', str(Path.home() / '.hermes/skills'))))
    deps.setdefault('hermes_runner', HermesRunner())
    done, outcome = threading.Event(), {}
    def worker():
        try:
            outcome['result'] = _execute_domain(store, run_id, generation, owner, budget, deps)
        except BaseException as exc:
            outcome['error'] = exc
        finally:
            done.set()
    result = None
    try:
        budget.begin_active()
        if budget.snapshot()['active_ms'] >= budget.snapshot()['active_limit_ms']:
            raise TimeoutError('active budget exhausted')
        threading.Thread(target=worker, daemon=True, name='domain-' + run_id).start()
        while not done.wait(0.05):
            _remaining(budget, 1800)
            store.renew(run_id, generation, 5)
        _remaining(budget, 1800)
        if 'error' in outcome:
            raise outcome['error']
        result = outcome['result']
    except BaseException as exc:
        selection = store.metadata(run_id, 'domain_selection.v1') or {}
        status = 'interrupted' if not isinstance(exc, Exception) else 'partial' if selection.get('finalized') or isinstance(exc, TimeoutError) else 'failed'
        result = {**store.get_run(run_id)['payload'], 'run_id': run_id, 'status': status,
                  'artifact_path': str(store.run_path(run_id)), 'error': redact(str(exc))[:2000]}
    finally:
        try:
            if budget._segment_start is not None:
                budget.end_active()
            if result is not None:
                result['request_id'] = request_id
                result['stages'] = budget.stages()
                store.finish_run(run_id, generation, result)
        except ValueError as exc:
            try:
                store.validate_context(run_id, generation, owner)
            except ValueError:
                # An expired/superseded worker has no authority to mark its
                # old run terminal. Expose the loss explicitly; never hide it
                # by claiming a successful durable terminal commit.
                result = {**store.get_run(run_id)['payload'], 'run_id': run_id,
                    'request_id': request_id, 'status': 'interrupted', 'terminal_persisted': False,
                    'artifact_path': str(store.run_path(run_id)), 'error': redact(str(exc))[:2000]}
            else:
                raise
        finally:
            try:
                if result is None:
                    store.release(run_id, generation)
            finally:
                cancel = getattr(deps['hermes_runner'], 'cancel', None)
                if cancel:
                    cancel()
    store.export_events(run_id)
    store.export_runtime(run_id)
    return result


class HermesRunner:
    """Installed SDK adapter. Unknown capabilities fail closed, with diagnostics.

    Credentials are taken only from an explicit provider environment allowlist.
    No user config/auth directory is copied into the per-run Hermes home.
    """
    PROVIDER_ENV = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENROUTER_API_KEY',
                    'HERMES_MODEL', 'HERMES_PROVIDER', 'ANTHROPIC_API_KEY')

    def __init__(self, command=None, *, python=None, sdk_root=None):
        import shlex
        self.command = command or shlex.split(os.getenv('HERMES_CLI_COMMAND', 'hermes'), posix=os.name != 'nt')
        self.python = python or os.getenv('HERMES_PYTHON')
        self.sdk_root = sdk_root or os.getenv('HERMES_SDK_ROOT')
        self.process = None
        self._process_lock = threading.Lock()
        self._cancelled = threading.Event()
        self.environment = None
        self.home = None
        self.server = None
        self.server_thread = None

    def _invoke(self, command, *, timeout):
        import subprocess
        from .search import redact
        with self._process_lock:
            if self._cancelled.is_set():
                raise TimeoutError('Hermes invocation cancelled')
            self.process = subprocess.Popen(command, cwd=self.cwd, env=self.environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                start_new_session=os.name != 'nt', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            process = self.process
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise TimeoutError('Hermes installed capability/run deadline exceeded') from None
        finally:
            with self._process_lock:
                if self.process is process:
                    self.process = None
        for secret in (self.environment.get(key) for key in self.PROVIDER_ENV):
            if secret:
                stdout, stderr = stdout.replace(secret, '[REDACTED]'), stderr.replace(secret, '[REDACTED]')
        return SimpleNamespace(returncode=process.returncode, stdout=redact(stdout), stderr=redact(stderr))

    def _bootstrap(self, mode, timeout):
        return self._invoke([self.python, '-c', _HERMES_BOOTSTRAP, str(self.bundle), mode, str(self.home)], timeout=timeout)

    def preflight(self, *, bundle_root, context, service, timeout):
        import shutil
        import tempfile
        context.validate()
        self._cancelled.clear()
        executable = shutil.which(self.command[0]) if self.command else None
        if executable is None:
            raise RuntimeError('Hermes CLI 未安装：无法探测实际安装能力')
        if self.python is None:
            # Python adjacent to the installed console entrypoint, never the
            # application's interpreter guessed as the Hermes SDK environment.
            candidate = Path(executable).parent / ('python.exe' if os.name == 'nt' else 'python')
            if not candidate.is_file():
                raise RuntimeError('Hermes SDK Python 未配置；请设置 HERMES_PYTHON 指向实际安装环境')
            self.python = str(candidate)
        self.bundle = Path(bundle_root).resolve()
        self.home = Path(tempfile.mkdtemp(prefix='aishperf-hermes-')).resolve()
        self.cwd = self.home / 'work'
        self.cwd.mkdir()
        if any((parent / '.git').exists() for parent in [self.cwd, *self.cwd.parents]):
            raise ValueError('Hermes dedicated cwd must be outside repository ancestors')
        (self.home / 'skills').mkdir()
        (self.home / 'empty-bundled-plugins').mkdir()
        plugin = self.home / 'plugins/aishperf-open-web'
        plugin.mkdir(parents=True)
        project = Path(__file__).resolve().parents[3]
        for name in ('__init__.py', 'plugin.yaml', 'tool-contract.json'):
            shutil.copyfile(project / '.hermes-plugins/aishperf-open-web' / name, plugin / name)
        # Query entrypoint names without importing any third-party plugins. The
        # bootstrap repeats this in the actual SDK environment before discovery.
        config = {'skills': {'external_dirs': [str(self.bundle)]},
                  'plugins': {'enabled': ['aishperf-open-web'], 'disabled': []},
                  'model': os.getenv('HERMES_MODEL', '')}
        (self.home / 'config.json').write_text(_json(config), encoding='utf-8')
        self.environment = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP',
            'HOME', 'USERPROFILE', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if key in os.environ}
        self.environment.update({key: os.environ[key] for key in self.PROVIDER_ENV if key in os.environ})
        self.environment.update({'HERMES_HOME': str(self.home), 'HERMES_ENABLE_PROJECT_PLUGINS': '0',
            'HERMES_BUNDLED_PLUGINS': str(self.home / 'empty-bundled-plugins'), 'PYTHONUTF8': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'TERMINAL_CWD': str(self.cwd)})
        if self.sdk_root:
            self.environment['PYTHONPATH'] = str(Path(self.sdk_root).resolve())
        started = time.monotonic()
        def remaining():
            value = timeout - (time.monotonic() - started)
            if value <= 0:
                raise TimeoutError('Hermes preflight deadline exceeded')
            return value
        version = self._invoke([*self.command, '--version'], timeout=remaining())
        help_output = self._invoke([*self.command, '--help'], timeout=remaining())
        if version.returncode or help_output.returncode or any(flag not in help_output.stdout for flag in ('--skills', '--toolsets')):
            raise RuntimeError('Hermes 实际 CLI 版本/能力探测失败：' + version.stderr[-500:])
        probe = self._bootstrap('probe', remaining())
        if probe.returncode:
            raise RuntimeError('Hermes 实际 SDK/resolver/plugin 探测失败：' + probe.stderr[-1500:])
        report = json.loads((self.home / 'resolver-report.json').read_text(encoding='utf-8'))
        return {**report, 'version': version.stdout.strip(), 'implementation': 'installed-sdk',
                'cli_help_sha256': _hash(help_output.stdout),
                'sdk_interpreter': str(Path(self.python).resolve()), 'dedicated_cwd': str(self.cwd)}

    def run(self, *, prompt, bundle_root, context, service, timeout):
        import secrets
        import socket
        import uvicorn
        from chip_model.open_web_tool_server import create_open_web_tool_app
        context.validate()
        if self.home is None or Path(bundle_root).resolve() != self.bundle:
            raise ValueError('Hermes successful preflight required for original frozen bundle')
        token = secrets.token_urlsafe(32)
        app = create_open_web_tool_app(token=token, domain_service=service)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, lifespan='off', ws='none'))
        self.server_thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.server_thread.is_alive() or time.monotonic() >= deadline:
                self.cancel()
                raise RuntimeError('Hermes loopback tool service failed to start')
            time.sleep(.01)
        self.environment.update({'AISH_PERF_OPEN_WEB_TOOL_URL': f'http://127.0.0.1:{port}',
                                 'AISH_PERF_OPEN_WEB_TOOL_TOKEN': token})
        (self.home / 'prompt.json').write_text(_json({'prompt': prompt, 'timeout': timeout}), encoding='utf-8')
        try:
            return self._bootstrap('execute', max(.001, deadline - time.monotonic()))
        finally:
            self.server.should_exit = True
            self.server_thread.join(timeout=1)
            sock.close()

    def cancel(self):
        import signal
        import subprocess
        self._cancelled.set()
        with self._process_lock:
            process = self.process
        if process is not None and process.poll() is None:
            if os.name == 'nt':
                # The PID is from Popen, never an interpolated external input.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                               timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.server:
            self.server.should_exit = True


_HERMES_BOOTSTRAP = r'''
import hashlib, importlib.metadata, inspect, json, os, sys
from pathlib import Path
bundle, mode, home = Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3]).resolve()
config = json.loads((home / 'config.json').read_text(encoding='utf-8'))
config['plugins']['disabled'] = [e.name for e in importlib.metadata.entry_points(group='hermes_agent.plugins')]
import yaml
(home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
from tools import skills_tool
from hermes_cli.plugins import discover_plugins
from tools.registry import registry
from run_agent import AIAgent
required = {'enabled_toolsets', 'skip_context_files', 'skip_memory', 'skip_background_review', 'run_budget_seconds',
            'cwd', 'prefill_messages'}
if not required.issubset(inspect.signature(AIAgent).parameters):
    raise RuntimeError('Unsupported installed AIAgent isolation capabilities')
manifest = json.loads((bundle / 'bundle.manifest.json').read_text(encoding='utf-8'))
records = {}
for domain in ('identity', 'specs', 'compute', 'compatibility', 'benchmark', 'deployment'):
    selected = json.loads(skills_tool.skill_view('chip-' + domain, preprocess=False))
    if selected.get('success') is not True:
        raise RuntimeError('Actual installed skill resolver refused frozen Skill: ' + domain)
    source = Path(selected.get('_source_path') or Path(selected['skill_dir']) / 'SKILL.md').resolve()
    if source != (bundle / ('chip-' + domain) / 'SKILL.md').resolve():
        raise RuntimeError('Actual installed resolver chose another Skill path: ' + str(source))
    if selected['content'] != source.read_text(encoding='utf-8'):
        raise RuntimeError('Actual installed Skill loader changed frozen content')
    relative = source.relative_to(bundle).as_posix()
    records[relative] = {'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    contract_path = source.with_name('domain.json')
    contract = json.loads(contract_path.read_text(encoding='utf-8'))
    for relative in ['chip-' + domain + '/domain.json', *contract['references']]:
        path = (source.parent.parent / relative).resolve()
        if path != (bundle / relative).resolve() or not path.is_relative_to(bundle):
            raise RuntimeError('Actual selected Skill reference escapes frozen root')
        records[relative] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
if set(records) != set(manifest['files']) or any(records[p]['sha256'] != sha for p, sha in manifest['files'].items()):
    raise RuntimeError('Actual installed resolver closure hash mismatch')
discover_plugins()
contract = json.loads((home / 'plugins/aishperf-open-web/tool-contract.json').read_text(encoding='utf-8'))
names = [tool['name'] for tool in contract['tools']]
plugin_source = (home / 'plugins/aishperf-open-web/__init__.py').resolve()
handler_sources = {}
if set(registry.get_tool_names_for_toolset('aishperf_open_web')) != set(names):
    raise RuntimeError('Installed plugin registry toolset differs from audited three tools')
for tool in contract['tools']:
    schema = registry.get_schema(tool['name'])
    if schema is None or schema.get('parameters') != tool['parameters']:
        raise RuntimeError('Installed provider tool object schema differs from frozen contract')
    entry = registry.get_entry(tool['name'])
    handler = inspect.unwrap(entry.handler)
    source = Path(inspect.getfile(handler)).resolve()
    if source != plugin_source:
        raise RuntimeError('Installed registry handler did not originate in dedicated audited plugin')
    handler_sources[tool['name']] = str(source)
report = {'resolver': {'rule_version': manifest['rule_version'], 'files': records}, 'tool_names': names,
          'handler_sources': handler_sources, 'plugin_sha256': hashlib.sha256(plugin_source.read_bytes()).hexdigest(),
          'sdk_source_sha256': hashlib.sha256(Path(inspect.getfile(AIAgent)).read_bytes()).hexdigest()}
if mode == 'execute':
    original_report = json.loads((home / 'resolver-report.json').read_text(encoding='utf-8'))
    if report != original_report:
        raise RuntimeError('Actual installed resolver/plugin/SDK changed since capability probe')
(home / 'resolver-report.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
if mode == 'execute':
    saved = json.loads((home / 'prompt.json').read_text(encoding='utf-8'))
    # The verified complete phase input includes the full Skill, machine
    # contract and every public reference; references cannot require shell/web.
    agent = AIAgent(model=os.environ.get('HERMES_MODEL', ''),
                    provider=os.environ.get('HERMES_PROVIDER') or None,
                    enabled_toolsets=['aishperf_open_web'], skip_context_files=True,
                    skip_memory=True, skip_background_review=True,
                    run_budget_seconds=saved['timeout'], cwd=str(home / 'work'),
                    prefill_messages=[], quiet_mode=True, save_trajectories=False)
    if set(agent.valid_tool_names) != set(names) or {t['function']['name'] for t in agent.tools} != set(names):
        raise RuntimeError('Actual Hermes agent enabled uncontrolled tools')
    for tool in agent.tools:
        frozen = next(s for s in contract['tools'] if s['name'] == tool['function']['name'])
        if tool['function']['parameters'] != frozen['parameters']:
            raise RuntimeError('Actual agent provider schema differs from audited object contract')
    agent.run_conversation(user_message=saved['prompt'])
'''
