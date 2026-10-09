"""Reusable single-job primitives. Task 12 owns actual runtime orchestration.

No worker launch, searches, publisher, heartbeat activation, or run_domain
entrypoint lives here. A caller supplies its bound frozen-domain ModelService.
"""
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
        result = service.extract_page(job['snapshot_id'], identity_context={})
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
