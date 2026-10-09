"""One-snapshot linked jobs and identity dependencies, without a live worker."""
import copy
import importlib
import importlib.util

import pytest

from tests.test_domain_entities import env, fact, audit, resolver, entities, relation, Response
from chip_model.pipeline.domain_discovery.store import _hash, _json


@pytest.fixture
def runtime():
    name = 'chip_model.pipeline.domain_discovery.runtime'
    assert importlib.util.find_spec(name) is not None, 'linked runtime primitives are missing'
    return importlib.import_module(name)


def job(env, domain='specs', linked=False):
    return {'url': Response.url, 'candidate_id': 'page-a', 'domain':domain,
            'main_domain':'specs', 'linked':linked, 'snapshot_id':env[-1]['snapshot_id'],
            'expected_info':'Actual domain evidence', 'entity_clues':[],
            'matched_category':None, 'hints_verified':False,
            'list_version':'frozen-list', 'depth':int(linked)}


def scheduler(runtime, env):
    return runtime.LinkedDomainScheduler(env[0], 'run-a', env[1])


def test_selection_and_extraction_hints_merge_once_per_url_domain(runtime, env):
    selected = job(env, 'compute', True)
    first = env[0].enqueue('run-a', env[1], 'domain-job-' + _hash(_json([selected['url'], 'compute'])), selected)
    result = {'domain_id':'specs','snapshot_id':env[-1]['snapshot_id'],
              'chunks':[{'linked_domains':['compute','identity']}, {'linked_domains':['compute','benchmark','price','specs']}]}
    ids = scheduler(runtime, env).schedule_linked(job(env), result)
    tasks = env[0].list_tasks('run-a')
    assert first in ids and len(ids) == 3
    assert {task['payload']['domain'] for task in tasks} == {'compute','identity','benchmark'}
    assert all(task['payload']['snapshot_id'] == env[-1]['snapshot_id'] for task in tasks)
    assert scheduler(runtime, env).schedule_linked(job(env), result) == ids
    assert len(env[0].list_tasks('run-a')) == 3


def test_linked_cannot_create_second_business_fanout(runtime, env):
    linked = job(env, 'compute', True)
    result = {'domain_id':'compute','snapshot_id':env[-1]['snapshot_id'], 'chunks':[{'linked_domains':['benchmark','deployment']}]}
    assert scheduler(runtime, env).schedule_linked(linked, result) == []
    assert not env[0].list_tasks('run-a')
    assert scheduler(runtime, env).snapshot()['hints'][0]['ignored_reason'] == 'one_layer_only'


def test_linked_new_sku_gets_same_snapshot_identity_dependency(runtime, entities, env):
    result, _ = audit(env, 'specs', [fact(env, 'specs')], scope='linked')
    scheduled = scheduler(runtime, env)
    first = scheduled.schedule_identity(job(env, 'specs', True), result['candidates'])
    assert len(first) == 1
    task = env[0].list_tasks('run-a')[0]['payload']
    assert task['domain'] == 'identity' and task['task_scope'] == 'identity'
    assert task['identity_dependency'] and task['snapshot_id'] == env[-1]['snapshot_id']
    assert task['entity_hint']['full_sku'] == 'X80 64GB PCIe'
    assert scheduled.schedule_identity(job(env), result['candidates']) == first
    assert scheduled.schedule_linked(task, {'chunks':[{'linked_domains':['compute']}]}) == []


def test_successful_main_identity_reused_without_replaying_models(runtime, entities, env):
    identity, identity_model = audit(env, 'identity', [fact(env)])
    resolved = resolver(entities, env)
    resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    specs, specs_model = audit(env, 'specs', [fact(env, 'specs')], scope='linked')
    original = copy.deepcopy(specs_model.snapshot())
    assert scheduler(runtime, env).schedule_identity(job(env, 'specs', True), specs['candidates']) == []
    binding = resolved.bind_candidate(specs['candidates'][0]['logical_candidate_id'])
    assert binding['entity']['status'] == 'new'
    assert specs_model.snapshot() == original
    assert len(identity_model.client.calls) == 2 and len(specs_model.client.calls) == 2


def test_same_entity_main_linked_and_two_urls_counts_once(runtime, entities, env):
    identity, _ = audit(env, 'identity', [fact(env)])
    resolved = resolver(entities, env)
    first = resolved.register_identity(identity['candidates'][0]['logical_candidate_id'])
    # A second frozen page is a distinct evidence/source, never a chip count.
    visited = env[4].visit({'candidate_id':'page-b', 'url':'https://vendor.example/second'})
    snapshot = env[4].read_snapshot(visited['snapshot_id'])
    other_env = (*env[:-1], snapshot)
    second_identity, _ = audit(other_env, 'identity', [fact(other_env)], scope='linked')
    second = resolved.register_identity(second_identity['candidates'][0]['logical_candidate_id'])
    assert first['canonical_key'] == second['canonical_key']
    assert first['entity_id'] == second['entity_id']
    assert resolved.snapshot()['new_entity_count'] == 1
    assert len(resolved.snapshot()['entities'][first['entity_id']]['identity_proofs']) == 2


@pytest.mark.parametrize('domain', ['identity','specs','compute','compatibility','benchmark','deployment'])
def test_one_saved_job_loads_correct_frozen_domain_and_scope(runtime, entities, env, domain):
    from chip_model.pipeline.domain_discovery.models import ModelService
    from tests.test_domain_entities import Transport
    candidates = {
        'identity':fact(env), 'specs':fact(env,'specs'),
        'compute':fact(env,'compute',field='precision_support',value='BF16'),
        'compatibility':fact(env,'compatibility',field='software_stack',value='PyTorch 2.5'),
        'benchmark':relation(env),
        'deployment':relation(env,'deployment',target_table='deployment_guides',field_name='backend',
                              raw_value='vLLM',normalized_value='vLLM',unit=None,evidence_kind='explicit',
                              entity_key={'url':Response.url,'title':'Guide title','chip_model':'X80 64GB PCIe',
                                          'model_id':'org/Model-7B','source_type':'official'},conditions={})}
    candidate = candidates[domain]
    client = Transport([{'facts':[candidate],'linked_domains':['specs' if domain=='compute' else 'compute']}, {'verdict':'accept','reason':'Explicit supported facts.'}])
    payload = job(env, domain, True)
    tid = env[0].enqueue('run-a', env[1], 'domain-job-' + _hash(_json([payload['url'],domain])), payload)
    def factory(domain_id, task_scope):
        return ModelService(env[0], 'run-a', env[1], env[2], env[4], env[3],
                            domain_id=domain_id,task_scope=task_scope,client=client,backoff=lambda _:None)
    result = scheduler(runtime, env).execute_task(tid, factory)
    assert result['domain_id'] == domain and result['scope'] == 'linked'
    assert result['complete'] and len(client.calls) == 2
    assert env[0].list_tasks('run-a')[0]['state'] == 'completed'
    assert not any(task['payload']['domain']=='compute' for task in env[0].list_tasks('run-a')[1:])


def test_main_success_plus_linked_failure_is_partial(runtime):
    main = {'complete':True,'candidates':[{'status':'validated'}],'outstanding':[]}
    linked = {'complete':False,'candidates':[],'outstanding':[{'status':'review_failed'}]}
    assert runtime.domain_result_status(main, [linked]) == 'partial'
    assert runtime.domain_result_status(main, []) == 'success'
    assert runtime.domain_result_status({'complete':True,'candidates':[],'outstanding':[]}, []) == 'no_findings'


def test_rejected_scalar_and_incomplete_hint_do_not_abort_valid_sibling(runtime, entities, env):
    from chip_model.pipeline.domain_discovery.models import ModelService
    from tests.test_domain_entities import Transport
    candidate = fact(env, 'specs')
    invalid = copy.deepcopy(candidate)
    invalid.pop('evidence')
    client = Transport([{'facts':[42, invalid, candidate]}, {'verdict':'accept','reason':'Explicit SKU capacity.'}])
    payload = job(env, 'specs', True)
    tid = env[0].enqueue('run-a', env[1], 'mixed-page', payload)
    def factory(domain_id, task_scope):
        return ModelService(env[0], 'run-a', env[1], env[2], env[4], env[3], domain_id=domain_id,
                            task_scope=task_scope,client=client,backoff=lambda _:None)
    result = scheduler(runtime, env).execute_task(tid, factory)
    assert result['complete'] and len(result['candidates']) == 3
    assert len(result['bindings']) == 1
    assert len(result['identity_task_ids']) == 1


def test_unfinished_single_job_stays_recoverable_and_reuses_model_cache(runtime, entities, env):
    from chip_model.pipeline.domain_discovery.models import ModelService
    from tests.test_domain_entities import Transport
    client = Transport([{'facts':[fact(env,'specs')]}, {'verdict':'accept','reason':'Explicit SKU capacity.'}])
    class IncompleteOnce(ModelService):
        incomplete = True
        def extract_page(self, *args, **kwargs):
            result = super().extract_page(*args, **kwargs)
            if self.incomplete:
                self.incomplete = False
                return {**result, 'complete':False, 'outstanding':[{'status':'pending'}]}
            return result
    model = IncompleteOnce(env[0], 'run-a', env[1], env[2], env[4], env[3],
                           domain_id='specs',task_scope='linked',client=client,backoff=lambda _:None)
    tid = env[0].enqueue('run-a', env[1], 'resumable-page', job(env, 'specs', True))
    worker = scheduler(runtime, env)
    assert not worker.execute_task(tid, lambda *args:model)['complete']
    assert next(t for t in env[0].list_tasks('run-a') if t['task_id']==tid)['state'] == 'running'
    result = worker.execute_task(tid, lambda *args:model)
    assert result['complete']
    assert len(client.calls) == 2
    assert next(t for t in env[0].list_tasks('run-a') if t['task_id']==tid)['state'] == 'completed'
