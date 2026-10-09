"""Strict V3 domain tools. A real runtime must register its active context.

This module never invents a worker lease, model executor or completion. Frozen
file proof verifies artifacts only; actual Hermes resolver proof is runtime work.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from chip_model.pipeline.domain_discovery import contracts as c
from chip_model.pipeline.domain_discovery.pages import PageService
from chip_model.pipeline.domain_discovery.rules import load_phase
from chip_model.pipeline.domain_discovery.search import SearchExecutor, redact, serialize_page
from chip_model.pipeline.domain_discovery.store import _hash, _json


ROOT = Path(__file__).resolve().parents[1]
TOOL_REFERENCE = '_shared/tool-contract.json'
SELECTION_KEY = 'domain_selection.v1'
ADAPTERS = {'search': c.SEARCH_TOOL, 'preview': c.PREVIEW_TOOL, 'selection': c.SELECTION_TOOL}
TOOL_NAMES = {'search': 'open_web_search', 'preview': 'open_web_preview', 'selection': 'open_web_submit_selection'}


class ToolProtocolError(ValueError):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def _provider_schema(adapter):
    """Generate a non-strict provider object from the exact HTTP action models.

    Provider schemas describe possible fields and common requirements. HTTP
    discriminated unions enforce per-action requirements/forbidden fields.
    A root union is deliberately not passed to function-calling providers.
    """
    union = adapter.json_schema()
    definitions = union['$defs']
    branches = [definitions[branch['$ref'].rsplit('/', 1)[-1]] for branch in union['oneOf']]
    required = set(branches[0]['required'])
    properties = {}
    for branch in branches:
        required.intersection_update(branch['required'])
        for name, schema in branch['properties'].items():
            if name not in properties:
                properties[name] = schema
            elif properties[name] != schema:
                previous = properties[name]
                if 'const' in schema and ('const' in previous or 'enum' in previous):
                    choices = previous.get('enum', [previous.get('const')])
                    properties[name] = {'type': 'string', 'enum': list(dict.fromkeys([*choices, schema['const']]))}
                else:
                    raise ValueError('incompatible public tool property: ' + name)
    return {'type': 'object', 'additionalProperties': False, '$defs': definitions,
            'properties': properties, 'required': sorted(required)}


def tool_contract():
    """The only schema source is the strict public Pydantic contracts."""
    body = {'contract_version': 3, 'pipeline': 'hermes-open-web-v3', 'tool_version': '3.0.0',
            'tools': [{'name': TOOL_NAMES[name], 'description': description, 'strict': False,
                       'parameters': _provider_schema(ADAPTERS[name])}
                      for name, description in (
                          ('search', 'Execute Hermes queries, read saved results, or explicitly complete replacement intents.'),
                          ('preview', 'Visit saved candidates or read bounded detail from a verified saved snapshot.'),
                          ('selection', 'Append paged decisions; finalize atomically freezes and enqueues asynchronous jobs.'))]}
    return {**body, 'tool_contract_sha256': _hash(_json(body))}


def verify_tool_contract(bundle_root=None, *, project_root=ROOT):
    """Compare schema artifacts and plugin manifest; optionally full frozen pack."""
    expected = tool_contract()
    root = Path(project_root)
    paths = [root / '.hermes-skills' / TOOL_REFERENCE,
             root / '.agents/skills' / TOOL_REFERENCE,
             root / '.hermes-plugins/aishperf-open-web/tool-contract.json']
    for path in paths:
        if json.loads(path.read_text(encoding='utf-8')) != expected:
            raise ValueError('tool contract schema/hash mismatch: ' + path.name)
    import yaml
    plugin = yaml.safe_load((root / '.hermes-plugins/aishperf-open-web/plugin.yaml').read_text(encoding='utf-8'))
    if (plugin.get('version') != expected['tool_version']
            or plugin.get('tool_contract_sha256') != expected['tool_contract_sha256']
            or plugin.get('provides_tools') != list(TOOL_NAMES.values())):
        raise ValueError('plugin manifest tool contract mismatch')
    if bundle_root is not None:
        # Every byte in the closure is verified by load_phase, not just this JSON.
        loaded = load_phase(Path(bundle_root), 'identity', 'select')
        frozen = loaded['references'].get(TOOL_REFERENCE)
        if frozen is None or json.loads(frozen) != expected:
            raise ValueError('frozen tool contract mismatch')
    return expected['tool_contract_sha256']


@dataclass
class ActiveToolContext:
    store: object
    run_id: str
    generation: int
    owner: str
    budget: object
    search: SearchExecutor
    pages: PageService
    bundle_root: Path
    lock: object = field(default_factory=threading.RLock)

    def validate(self):
        self.store.validate_context(self.run_id, self.generation, self.owner)
        for value in (self.budget, self.search, self.pages):
            if (value.store is not self.store or value.run_id != self.run_id
                    or value.generation != self.generation):
                raise ValueError('runtime context run/generation mismatch')
        if self.search.budget is not self.budget or self.pages.budget is not self.budget:
            raise ValueError('tools require the same active runtime budget instance')
        segment = self.budget.snapshot()['segment']
        if segment is None or segment['token'] != self.budget.token:
            raise ValueError('runtime active budget segment is not registered')


class DomainToolService:
    def __init__(self, store, context_resolver, *, project_root=ROOT):
        self.store, self.context_resolver, self.project_root = store, context_resolver, Path(project_root)
        # Fail before any run/network/model side effects when artifacts diverge.
        verify_tool_contract(project_root=self.project_root)

    @staticmethod
    def _current(search):
        plans = (search or {}).get('plans', {})
        versions = [p.get('list_version') for _, p in sorted(plans.items(), key=lambda item: int(item[0]))
                    if p.get('list_version')]
        if not versions:
            raise ValueError('no completed saved search list')
        return versions[-1]

    @staticmethod
    def _visits(pages):
        pages = pages or {}
        return set(pages.get('visits', {})) | {cid for cid, attempts in pages.get('attempts', {}).items() if attempts}

    @staticmethod
    def _unlocked(state):
        if state and state.get('finalized'):
            raise ValueError('selection finalized; collection and decisions locked')

    def handle(self, tool, payload):
        if not isinstance(payload, dict):
            raise ToolProtocolError(422, 'tool request must be an object')
        version = payload.get('contract_version')
        if type(version) is int and version != 3 or 'contract_version' not in payload:
            raise ToolProtocolError(409, 'version error: v1/v2 are read-only; v3 explicit contract required')
        try:
            request = ADAPTERS[tool].validate_python(payload)
        except ValidationError as exc:
            # Do not reflect untrusted input or potentially secret values.
            errors = [{'loc': list(e['loc']), 'type': e['type']} for e in exc.errors(include_input=False, include_context=False)[:10]]
            raise ToolProtocolError(422, {'message': 'invalid strict v3 request', 'error_count': exc.error_count(), 'errors': errors}) from exc
        try:
            run = self.store.get_run(request.run_id)
            canonical = run['payload']
            if (run['contract_version'] != 3 or run['pipeline'] != request.pipeline
                    or request.domain != canonical['domain']
                    or request.scope_type != canonical['scope_type'] or request.chip != canonical.get('chip')):
                raise ValueError('run domain/scope binding mismatch')
            context = self.context_resolver(request.run_id)
            if context is None:
                raise ToolProtocolError(503, 'v3 runtime active context is not registered')
            if context.store is not self.store or context.run_id != request.run_id:
                raise ValueError('context store/run binding mismatch')
            with context.lock:
                context.validate()
                try:
                    verify_tool_contract(context.bundle_root, project_root=self.project_root)
                except (OSError, ValueError) as exc:
                    raise ToolProtocolError(503, 'frozen tool contract verification failed') from exc
                result = self._dispatch(context, tool, request, payload)
                if len(json.dumps(result, ensure_ascii=False)) > 12000:
                    raise ValueError('response_envelope_too_large')
                return result
        except ToolProtocolError:
            raise
        except (ValueError, KeyError) as exc:
            raise ToolProtocolError(409, str(exc)) from exc

    def _dispatch(self, ctx, tool, request, payload):
        if isinstance(request, c.SearchResults):
            return ctx.search.results(request.list_version, cursor=request.cursor.model_dump() if request.cursor else None)
        if tool == 'selection':
            return self._selection(ctx, request, payload)
        def execute():
            state = self.store.metadata(ctx.run_id, SELECTION_KEY)
            if tool == 'search':
                self._unlocked(state)
                if isinstance(request, c.SearchExecute):
                    return ctx.search.execute(request.request_id, payload['plan'])
                completions = []
                for index, item in enumerate(request.completions):
                    completions.append(ctx.search.complete_intent(
                        'intent-' + _hash(_json([request.request_id, index])), item.failed_query_id,
                        item.replacement_query_id, item.reason))
                return {'status': 'received', 'completed_intents': completions, 'current_stage': 'searching'}
            if isinstance(request, c.PreviewDetail):
                return ctx.pages.detail(request.request_id, request.snapshot_id, offset=request.offset, length=request.length)
            cursor = request.cursor.model_dump() if request.cursor else None
            if state and state.get('finalized'):
                # After lock only preexisting batch cache or actual saved visits;
                # never call visit() or manufacture pending fetches.
                search = self.store.metadata(ctx.run_id, SearchExecutor.KEY)
                saved = search['versions'].get(request.list_version)
                if saved is None:
                    raise ValueError('unknown saved search list version')
                offset = ctx.pages._cursor(request.list_version, cursor, 0)
                pages = self.store.metadata(ctx.run_id, PageService.KEY) or ctx.pages._empty()
                records = []
                for item in saved['items']:
                    record = pages['visits'].get(item['candidate_id'])
                    if record is None:
                        raise ValueError('finalized preview cannot fetch an absent saved visit')
                    if record.get('extractable'):
                        ctx.pages.read_snapshot(record['snapshot_id'])
                    records.append(ctx.pages._preview_item(record))
                return serialize_page(records, offset, {'list_version': request.list_version, 'mode': 'initial',
                                                        'stage': 'previewing'}, limit=request.limit)
            return ctx.pages.preview(request.list_version, cursor=cursor, limit=request.limit)
        def execute_with_business_failure():
            try:
                return execute()
            except (ValueError, KeyError) as exc:
                # Only a normally ended local business failure is terminal.
                # An invalid fence or a still-running child remains unknown;
                # BaseException/crashes never enter this handler at all.
                ctx.validate()
                children = self.store.list_operations(ctx.run_id)
                if any(item['request_id'] != request.request_id and item['state'] == 'running' for item in children):
                    raise
                return {'status': 'rejected', 'tool_error': {'status_code': 409, 'detail': redact(str(exc))[:1000]}}
        receipt = self.store.operation(ctx.run_id, ctx.generation, request.request_id,
                                       {'tool': tool, 'request': payload}, execute_with_business_failure,
                                       pending_result=lambda result: result.get('status') == 'in_progress'
                                           or any(item.get('status') == 'pending' for item in result.get('items', [])))
        if receipt['state'] == 'pending' and receipt['result'] is not None:
            return receipt['result']
        if receipt['state'] != 'completed':
            return {'status': 'in_progress', 'receipt_id': request.request_id}
        if receipt['result'].get('status') == 'rejected' and 'tool_error' in receipt['result']:
            error = receipt['result']['tool_error']
            raise ToolProtocolError(error['status_code'], error['detail'])
        return receipt['result']

    def read_run(self, run_id):
        """Readonly status needs no worker lease and never reconstructs old data."""
        run = self.store.get_run(run_id)
        search = self.store.metadata(run_id, SearchExecutor.KEY) or {}
        selection = self.store.metadata(run_id, SELECTION_KEY) or {}
        tasks = self.store.list_tasks(run_id)
        counts = {}
        for task in tasks:
            counts[task['state']] = counts.get(task['state'], 0) + 1
        try:
            version = self._current(search)
        except ValueError:
            version = None
        return {'run_id': run_id, 'pipeline': run['pipeline'], 'contract_version': run['contract_version'],
                'domain': run['payload']['domain'], 'scope_type': run['payload']['scope_type'],
                'chip': run['payload'].get('chip'), 'status': run['status'], 'current_stage': run['stage'],
                'list_version': version, 'candidate_count': len(search.get('versions', {}).get(version, {}).get('items', [])),
                'decision_count': len(selection.get('decisions', {})), 'finalized': bool(selection.get('finalized')),
                'task_counts': counts, 'read_only': True}

    def _selection(self, ctx, request, payload):
        # Full snapshot IO/hash checks stay outside the short SQLite transaction.
        pages = self.store.metadata(ctx.run_id, PageService.KEY) or ctx.pages._empty()
        selections = request.decisions if isinstance(request, c.SelectionAppend) else [
            c.ToolDecision.model_validate(item) for item in (self.store.metadata(ctx.run_id, SELECTION_KEY) or {}).get('decisions', {}).values()]
        for item in selections:
            if item.decision == 'select':
                visit = pages['visits'].get(item.candidate_id)
                if not visit or not visit.get('extractable') or not visit.get('snapshot_id'):
                    raise ValueError('selected candidate has no extractable full saved snapshot')
                snapshot = ctx.pages.read_snapshot(visit['snapshot_id'])
                if not snapshot['text'].strip():
                    raise ValueError('selected snapshot body is unreadable')
        def prepare(values):
            search, pages = values[SearchExecutor.KEY], values[PageService.KEY] or ctx.pages._empty()
            state = values[SELECTION_KEY] or {'decisions': {}, 'finalized': False}
            self._unlocked(state)
            version = self._current(search)
            if version != request.list_version:
                raise ValueError('current list_version mismatch')
            ids = [item['candidate_id'] for item in search['versions'][version]['items']]
            visited = self._visits(pages)
            if isinstance(request, c.SelectionAppend):
                decisions = c.validate_decisions(ids, visited, [item.model_dump() for item in request.decisions],
                                                 current_domain=request.domain)
                for item in decisions:
                    prior = state['decisions'].get(item['candidate_id'])
                    if prior is not None and prior != item:
                        raise ValueError('candidate decision payload conflict')
                    state['decisions'].setdefault(item['candidate_id'], item)
                result = {'status': 'received_append', 'list_version': version, 'decision_count': len(state['decisions']),
                          'pending_count': len(ids) - len(state['decisions']), 'current_stage': 'selecting'}
                return {SELECTION_KEY: state}, [], result, {'kind': 'selection_appended', 'stage': 'selecting',
                                                           'list_version': version, 'decisions': decisions,
                                                           'hints_verified': False, 'candidate_ids': [d['candidate_id'] for d in decisions]}
            decisions = c.validate_decisions(ids, visited, list(state['decisions'].values()), True, current_domain=request.domain)
            if any(not plan.get('list_version') for plan in search['plans'].values()):
                raise ValueError('未完成：search round is in flight or not published')
            if SearchExecutor._incomplete(search):
                raise ValueError('未完成：unresolved search intents cannot finalize')
            jobs = []
            by_id = {item['candidate_id']: item for item in search['versions'][version]['items']}
            for decision in decisions:
                if decision['decision'] != 'select':
                    continue
                candidate, visit = by_id[decision['candidate_id']], pages['visits'][decision['candidate_id']]
                for domain in [request.domain, *decision['linked_domains']]:
                    job = {'url': candidate['url'], 'candidate_id': decision['candidate_id'], 'domain': domain,
                           'main_domain': request.domain, 'linked': domain != request.domain,
                           'snapshot_id': visit['snapshot_id'], 'expected_info': decision['expected_info'],
                           'entity_clues': decision['entity_clues'], 'matched_category': decision['matched_category'],
                           'hints_verified': False,
                           'list_version': version, 'depth': 1 if domain != request.domain else 0}
                    jobs.append(('domain-job-' + _hash(_json([candidate['url'], domain])), job))
            state.update(finalized=True, list_version=version, receipt_id=request.request_id)
            result = {'status': 'received', 'receipt_id': request.request_id, 'list_version': version,
                      'task_ids': [], 'current_stage': 'selecting', 'queued_stage': 'extracting' if jobs else None,
                      'decision_count': len(decisions), 'selected_count': sum(d['decision'] == 'select' for d in decisions)}
            return {SELECTION_KEY: state}, jobs, result, {'kind': 'selection_finalized', 'stage': 'selecting',
                                                        'list_version': version, 'decision_count': len(decisions), 'job_count': len(jobs)}
        return self.store.commit_tool_request(ctx.run_id, ctx.generation, request.request_id,
                     {'tool': 'selection', 'request': payload}, [SELECTION_KEY, SearchExecutor.KEY, PageService.KEY], prepare)
