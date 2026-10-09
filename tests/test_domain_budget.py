"""Cumulative limits use isolated SQLite fixtures, never a network or business DB."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


def test_budget_module_exists():
    assert importlib.util.find_spec('chip_model.pipeline.domain_discovery.budget') is not None


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.mono = 0.0

    def epoch(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.now += seconds
        self.mono += seconds


@pytest.fixture
def setup(tmp_path):
    from chip_model.pipeline.domain_discovery.store import RunStore
    clock = Clock()
    store = RunStore(tmp_path / 'runs', clock=clock.epoch)
    store.start('start', {'domain': 'specs'}, 'run-a')
    generation = store.claim('run-a', 'worker', 100000)
    return store, generation, clock


def make_budget(setup, **kwargs):
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    store, generation, clock = setup
    return RunBudget(store, 'run-a', generation, monotonic=clock.monotonic,
                     epoch=clock.epoch, **kwargs)


@pytest.mark.parametrize('fatal,interrupted,incomplete,count,expected', [
    (False, False, False, 0, 'no_findings'),
    (False, False, False, 1, 'success'),
    (False, False, True, 1, 'partial'),
    (False, True, True, 1, 'interrupted'),
    (True, True, True, 1, 'failed'),
])
def test_terminal_precedence(fatal, interrupted, incomplete, count, expected):
    from chip_model.pipeline.domain_discovery.budget import derive_status
    assert derive_status(fatal, interrupted, incomplete, count) == expected


def test_queries_rounds_and_idempotent_payload(setup):
    budget = make_budget(setup)
    initial = ['q' + str(i) for i in range(10)]
    first = budget.reserve_queries('initial', initial, round_number=0)
    assert budget.reserve_queries('initial', initial, round_number=0) == first
    with pytest.raises(ValueError, match='conflict'):
        budget.reserve_queries('initial', ['other' + str(i) for i in range(10)], round_number=0)
    with pytest.raises(ValueError):
        budget.reserve_queries('second-initial', ['x' + str(i) for i in range(10)], round_number=0)
    budget.reserve_queries('supp1', ['s1'], round_number=1)
    budget.reserve_queries('supp2', ['s2' + str(i) for i in range(5)], round_number=2)
    with pytest.raises(ValueError):
        budget.reserve_queries('supp3', ['s3'], round_number=3)
    assert make_budget(setup).snapshot()['query_count'] == 16
    assert len(budget.snapshot()['rounds']) == 3


@pytest.mark.parametrize('queries,round_number', [(['q'] * 10, 0), (['q'], 0),
    (['q' + str(i) for i in range(11)], 0), ([], 1), (['q'] * 6, 1)])
def test_query_invalid_batch_is_atomic(setup, queries, round_number):
    budget = make_budget(setup)
    before = budget.snapshot()
    with pytest.raises(ValueError):
        budget.reserve_queries('bad', queries, round_number=round_number)
    assert budget.snapshot() == before


def test_twenty_queries_exact_limit_and_no_healthy_empty_retry(setup):
    budget = make_budget(setup)
    for round_number, count in [(0, 10), (1, 5), (2, 5)]:
        budget.reserve_queries(str(round_number), [f'{round_number}-{i}' for i in range(count)],
                               round_number=round_number)
    assert budget.snapshot()['query_count'] == 20
    assert budget.operation('healthy-empty', {}, lambda: [])['state'] == 'completed'
    assert budget.operation('healthy-empty', {}, lambda: pytest.fail('replayed'))['result'] == []
    with pytest.raises(ValueError):
        budget.operation('healthy-empty', {}, lambda: [], attempt=1)
    assert budget.snapshot()['technical_retries'] == {}


def test_results_and_main_candidates_dedupe_and_limits(setup):
    budget = make_budget(setup)
    budget.reserve_queries('initial', [str(i) for i in range(10)], round_number=0)
    for i in range(10):
        ids = [f'url-{i}-{j}' for j in range(10)]
        budget.admit_results('batch-' + str(i), str(i), ids)
    assert len(budget.snapshot()['main_candidates']) == 100
    budget.admit_results('dup', '0', ['url-0-0'])
    with pytest.raises(ValueError):
        budget.admit_results('extra-result', '0', ['new'])
    budget.reserve_queries('supp', ['extra'], round_number=1)
    with pytest.raises(ValueError):
        budget.admit_results('extra-main', 'extra', ['new'])
    assert len(budget.snapshot()['main_candidates']) == 100


def test_details_and_revisions_are_stable_logical_keys(setup):
    budget = make_budget(setup)
    for i in range(3):
        budget.reserve_detail('detail-' + str(i), 'snapshot-a')
    assert budget.reserve_detail('detail-0', 'snapshot-a')['count'] == 1
    with pytest.raises(ValueError):
        make_budget(setup).reserve_detail('new-client', 'snapshot-a')
    budget.reserve_detail('other', 'snapshot-b')
    budget.reserve_revision('rev', 'candidate-a')
    assert budget.reserve_revision('rev', 'candidate-a')['count'] == 1
    with pytest.raises(ValueError):
        make_budget(setup).reserve_revision('new-version', 'candidate-a')


def test_two_budget_instances_cannot_spend_last_detail_twice(setup):
    budgets = [make_budget(setup), make_budget(setup)]
    budgets[0].reserve_detail('one', 'snapshot')
    budgets[0].reserve_detail('two', 'snapshot')
    barrier = Barrier(2)
    def reserve(item):
        i, budget = item
        barrier.wait(timeout=10)
        try:
            return budget.reserve_detail('last-' + str(i), 'snapshot')['count']
        except ValueError:
            return 'denied'
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(reserve, enumerate(budgets)))
    assert sorted(outcomes, key=str) == [3, 'denied']


def test_stale_generation_cannot_change_budget_or_audit(setup):
    store, generation, clock = setup
    budget = make_budget(setup)
    before, events = budget.snapshot(), store.events('run-a')
    store.release('run-a', generation)
    store.claim('run-a', 'new-worker', 100000)
    with pytest.raises(ValueError, match='generation'):
        budget.reserve_detail('stale', 'snapshot')
    assert budget.snapshot() == before
    assert store.events('run-a') == events


def test_active_wait_backoff_and_inference_count_idle_does_not(setup):
    budget = make_budget(setup)
    clock = setup[2]
    budget.begin_active()
    clock.advance(5)  # waiting on an external request
    assert budget.tick()['active_ms'] == 5000
    clock.advance(2)  # backoff
    budget.tick()
    clock.advance(3)  # inference
    assert budget.end_active()['active_ms'] == 10000
    clock.advance(3600)  # queue/suspension
    resumed = make_budget(setup)
    resumed.begin_active()
    clock.advance(4)
    assert resumed.end_active()['active_ms'] == 14000


def test_crash_retains_heartbeat_and_bills_bounded_uncertainty(setup):
    budget = make_budget(setup, heartbeat_ms=1000)
    store, generation, clock = setup
    budget.begin_active()
    clock.advance(5)
    budget.tick()
    clock.advance(3600)
    store.release('run-a', generation)
    generation = store.claim('run-a', 'replacement', 100000)
    resumed = make_budget((store, generation, clock), heartbeat_ms=1000)
    resumed.begin_active()
    assert resumed.snapshot()['active_ms'] == 6000
    assert resumed.snapshot()['uncertainties'][0]['charged_ms'] == 1000
    assert resumed.snapshot()['segment']['generation'] == generation
    clock.advance(2)
    assert resumed.end_active()['active_ms'] == 8000


def test_reservation_not_refunded_after_crash_and_uncertainty_is_idempotent(setup):
    budget = make_budget(setup)
    budget.reserve_detail('inflight', 'snapshot')
    assert budget.mark_uncertain('inflight', 'network outcome unknown')['uncertain']
    assert budget.mark_uncertain('inflight', 'network outcome unknown')['uncertain']
    resumed = make_budget(setup)
    assert resumed.reserve_detail('inflight', 'snapshot')['count'] == 1
    assert resumed.snapshot()['page_details']['snapshot'] == 1


def test_shorter_active_limit_immutable_and_finalize_audit_allowed(setup):
    budget = make_budget(setup, active_seconds=1)
    clock = setup[2]
    budget.begin_active()
    clock.advance(2)
    assert budget.end_active()['active_ms'] == 1000
    with pytest.raises(ValueError, match='budget'):
        budget.reserve_detail('too-late', 'snapshot')
    budget.record_stage('searching', 'budget_stopped', scope='main', reason='unfinished pages')
    budget.record_stage('completed', 'completed', scope='main', reason='report saved')
    assert budget.stages()['searching']['main']['status'] == 'budget_stopped'
    with pytest.raises(ValueError, match='immutable'):
        make_budget(setup, active_seconds=2)


@pytest.mark.parametrize('seconds', [0, -1, 1801, True, 1.5])
def test_active_limit_invalid(setup, seconds):
    with pytest.raises(ValueError):
        make_budget(setup, active_seconds=seconds)


def test_stage_audit_does_not_infer_prior_completion(setup):
    budget = make_budget(setup)
    for status in ['not_executed', 'in_progress', 'partial_failure', 'failed', 'completed']:
        budget.record_stage('reviewing', status, scope='linked:compute', reason=status)
    assert budget.stages()['reviewing']['linked:compute']['status'] == 'completed'
    assert 'extracting' not in budget.stages()
    events = setup[0].events('run-a')
    assert [event['payload']['stage_status'] for event in events if event['stage'] == 'reviewing'] == [
        'not_executed', 'in_progress', 'partial_failure', 'failed', 'completed']
    assert setup[0].get_run('run-a')['stage'] == 'preparing'
    with pytest.raises(ValueError):
        budget.record_stage('reviewing', 'passed', scope='main', reason='invalid')


def test_confirmed_failure_allows_only_two_technical_retries(setup):
    budget = make_budget(setup)
    calls = []
    def fail():
        calls.append(1)
        raise TimeoutError('do not save raw credentials')
    for attempt in range(3):
        receipt = budget.operation('logical-fetch', {'url': 'https://example.org'}, fail, attempt=attempt)
        assert receipt['state'] == 'confirmed_failed'
        assert budget.operation('logical-fetch', {'url': 'https://example.org'}, fail,
                                attempt=attempt) == receipt
    with pytest.raises(ValueError):
        budget.operation('logical-fetch', {'url': 'https://example.org'}, fail, attempt=3)
    assert len(calls) == 3
    assert make_budget(setup).snapshot()['technical_retries']['logical-fetch'] == 2
    with pytest.raises(ValueError, match='conflict'):
        budget.operation('logical-fetch', {'url': 'different'}, fail, attempt=0)


def test_unknown_running_never_allows_same_generation_retry(setup):
    budget = make_budget(setup)
    def crash():
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        budget.operation('crash', {}, crash)
    assert budget.operation('crash', {}, lambda: pytest.fail('same generation replay'))['state'] == 'running'
    with pytest.raises(ValueError):
        budget.operation('crash', {}, lambda: None, attempt=1)
    assert budget.snapshot()['technical_retries'] == {}


def test_confirmed_failure_fallback_keeps_diagnostic_without_terminal_failure(setup):
    from chip_model.pipeline.domain_discovery.budget import derive_status
    budget = make_budget(setup)
    def fail():
        raise ConnectionError()
    budget.operation('query', {}, fail)
    assert budget.operation('query', {}, lambda: [], attempt=1)['state'] == 'completed'
    assert setup[0].list_operations('run-a')[0]['state'] == 'confirmed_failed'
    # Runtime derives these flags from outstanding intent records, not rejected
    # business facts or exhausted-but-completed search quotas.
    assert derive_status(False, False, False, 0) == 'no_findings'
    assert derive_status(True, False, False, 0) == 'failed'  # all necessary models unavailable
    assert derive_status(False, False, True, 1) == 'partial'  # other page reviewed


def test_atomic_metadata_callback_rollback_and_reserved_keys(setup):
    store, generation, clock = setup
    def update(state):
        return ({'count': 1}, 'ok', {'kind': 'metadata_test'})
    assert store.update_metadata('run-a', generation, 'test.counter', update) == 'ok'
    def broken(state):
        state['count'] = 2
        raise RuntimeError()
    with pytest.raises(RuntimeError):
        store.update_metadata('run-a', generation, 'test.counter', broken)
    assert store.metadata('run-a', 'test.counter') == {'count': 1}
    for key in ['identity', 'baseline', 'baseline_pending']:
        with pytest.raises(ValueError):
            store.update_metadata('run-a', generation, key, update)


def test_duplicates_within_result_batch_do_not_cost_extra_candidates(setup):
    budget = make_budget(setup)
    budget.reserve_queries('initial', [str(i) for i in range(10)], round_number=0)
    assert budget.admit_results('results', '0', ['canonical-url', 'canonical-url']) == {
        'query_count': 1, 'main_count': 1}


def test_reservation_accounts_known_active_time_before_external_effect(setup):
    budget = make_budget(setup, active_seconds=1)
    budget.begin_active()
    setup[2].advance(1)
    with pytest.raises(ValueError, match='budget'):
        budget.operation('too-late', {}, lambda: pytest.fail('must not execute'))
    assert budget.snapshot()['active_ms'] == 1000
    assert budget.snapshot()['logical_operations'] == {}


def test_crash_uncertainty_charge_is_retained_even_when_it_exhausts_budget(setup):
    budget = make_budget(setup, active_seconds=1)
    store, generation, clock = setup
    budget.begin_active()
    clock.advance(.5)
    budget.tick()
    store.release('run-a', generation)
    generation = store.claim('run-a', 'replacement', 100000)
    resumed = make_budget((store, generation, clock))
    assert resumed.begin_active()['active_ms'] == 1000
    assert resumed.snapshot()['segment'] is None
    assert resumed.snapshot()['uncertainties'][0]['charged_ms'] == 500
    with pytest.raises(ValueError, match='budget'):
        resumed.reserve_detail('new', 'snapshot')
    assert resumed.begin_active()['active_ms'] == 1000
    assert len(resumed.snapshot()['uncertainties']) == 1


def test_same_generation_new_object_cannot_steal_unclosed_active_segment(setup):
    budget = make_budget(setup)
    budget.begin_active()
    with pytest.raises(ValueError, match='owned'):
        make_budget(setup).begin_active()


def test_recover_unknown_attempt_keeps_its_original_reservation(setup):
    budget = make_budget(setup)
    store, generation, clock = setup
    with pytest.raises(KeyboardInterrupt):
        budget.operation('unknown', {}, lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    store.release('run-a', generation)
    generation = store.claim('run-a', 'replacement', 100000)
    assert store.recover_operations('run-a', generation) == 1
    resumed = make_budget((store, generation, clock))
    assert resumed.operation('unknown', {}, lambda: 'recovered')['state'] == 'completed'
    assert resumed.snapshot()['technical_retries'] == {'unknown': 1}
    assert len(resumed.snapshot()['logical_operations']['unknown']['attempts']) == 1


def test_stage_validation_and_terminal_bad_count(setup):
    from chip_model.pipeline.domain_discovery.budget import derive_status
    budget = make_budget(setup)
    with pytest.raises(ValueError):
        budget.record_stage('reviewing', [], scope='main', reason='bad')
    for bad_count in [-1, True, 1.5]:
        with pytest.raises(ValueError):
            derive_status(False, False, False, bad_count)


def test_linked_and_identity_stage_records_actual_domain_without_global_terminal(setup):
    budget = make_budget(setup)
    budget.record_stage('reviewing', 'completed', scope='linked:compute',
                        domain_id='compute', reason='review complete')
    budget.record_stage('reviewing', 'failed', scope='identity:sku-a',
                        domain_id='identity', reason='model unavailable')
    events = setup[0].events('run-a')
    assert [event['domain_id'] for event in events] == ['compute', 'identity']
    assert [event['payload']['domain_id'] for event in events] == ['compute', 'identity']
    assert setup[0].get_run('run-a')['status'] == 'preparing'


def test_duplicate_result_content_still_binds_exact_reservation_payload(setup):
    budget = make_budget(setup)
    budget.reserve_queries('initial', [str(i) for i in range(10)], round_number=0)
    budget.admit_results('results', '0', ['url', 'url'])
    with pytest.raises(ValueError, match='conflict'):
        budget.admit_results('results', '0', ['url'])


def test_short_policy_reused_when_resume_omits_override(setup):
    budget = make_budget(setup, active_seconds=17, heartbeat_ms=250)
    assert make_budget(setup).snapshot()['active_limit_ms'] == 17000
    assert make_budget(setup).snapshot()['heartbeat_ms'] == 250


def test_reserved_pending_operation_cannot_start_after_total_time_exhausted(setup):
    budget = make_budget(setup, active_seconds=1)
    with pytest.raises(KeyboardInterrupt):
        budget.operation('unknown', {}, lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    store, generation, clock = setup
    budget.begin_active()
    clock.advance(1)
    budget.end_active()
    store.release('run-a', generation)
    generation = store.claim('run-a', 'replacement', 100000)
    store.recover_operations('run-a', generation)
    resumed = make_budget((store, generation, clock))
    with pytest.raises(ValueError, match='budget'):
        resumed.operation('unknown', {}, lambda: pytest.fail('budget already exhausted'))
    assert store.list_operations('run-a')[0]['state'] == 'pending'


def test_completed_receipt_can_be_read_after_total_time_exhausted(setup):
    budget = make_budget(setup, active_seconds=1)
    receipt = budget.operation('done', {}, lambda: 'saved')
    budget.begin_active()
    setup[2].advance(1)
    budget.end_active()
    assert budget.operation('done', {}, lambda: pytest.fail('must use cached result')) == receipt


def test_linked_or_identity_scope_cannot_allocate_search_rounds(setup):
    budget = make_budget(setup)
    queries = [str(i) for i in range(10)]
    for scope in ['linked:compute', 'identity:sku-a']:
        with pytest.raises(ValueError):
            budget.reserve_queries(scope, queries, round_number=0, scope=scope)
    assert budget.snapshot()['query_count'] == 0


def test_budget_counters_are_visible_in_an_independent_process(setup):
    budget = make_budget(setup, active_seconds=17)
    budget.reserve_detail('saved', 'snapshot')
    budget.reserve_revision('saved-revision', 'candidate')
    code = (
        'import json,sys; '
        'from chip_model.pipeline.domain_discovery.store import RunStore; '
        'print(json.dumps(RunStore(sys.argv[1]).metadata("run-a", "domain_budget.v1")))'
    )
    result = subprocess.run([sys.executable, '-c', code, str(setup[0].root)],
                            cwd=Path(__file__).resolve().parents[1],
                            check=True, capture_output=True, text=True)
    persisted = json.loads(result.stdout)
    assert persisted['page_details'] == {'snapshot': 1}
    assert persisted['candidate_revisions'] == {'candidate': 1}
    assert persisted['active_limit_ms'] == 17000


def test_expiry_during_atomic_budget_update_rolls_back_metadata_and_event(setup):
    store, generation, clock = setup
    budget = make_budget(setup)
    before, events = budget.snapshot(), store.events('run-a')
    def expired(state):
        state['active_ms'] = 42
        clock.advance(100001)
        return state, None, {'kind': 'must_rollback'}
    with pytest.raises(ValueError, match='generation'):
        store.update_metadata('run-a', generation, budget.KEY, expired)
    assert budget.snapshot() == before
    assert store.events('run-a') == events


def test_missing_initialized_db_is_not_recreated_by_budget_extension(setup):
    budget = make_budget(setup)
    db = setup[0].run_path('run-a') / 'run.sqlite3'
    db.unlink()
    with pytest.raises(ValueError):
        budget.reserve_detail('bad', 'snapshot')
    with pytest.raises(ValueError):
        budget.snapshot()
    assert not db.exists()


def test_cross_generation_unknown_replays_consume_the_same_two_retry_allowance(setup):
    store, generation, clock = setup
    calls = []
    def crash():
        calls.append(1)
        raise KeyboardInterrupt()
    for i in range(3):
        budget = make_budget((store, generation, clock))
        with pytest.raises(KeyboardInterrupt):
            budget.operation('unstable', {}, crash)
        store.release('run-a', generation)
        generation = store.claim('run-a', f'replacement-{i}', 100000)
        store.recover_operations('run-a', generation)
    resumed = make_budget((store, generation, clock))
    with pytest.raises(ValueError, match='retry'):
        resumed.operation('unstable', {}, crash)
    assert len(calls) == 3
    assert resumed.snapshot()['technical_retries'] == {'unstable': 2}
    assert store.list_operations('run-a')[0]['attempts'] == 3
    assert store.list_operations('run-a')[0]['state'] == 'pending'


def test_recovered_unknown_and_explicit_failure_retry_share_allowance(setup):
    budget = make_budget(setup)
    store, generation, clock = setup
    with pytest.raises(KeyboardInterrupt):
        budget.operation('mixed', {}, lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    store.release('run-a', generation)
    generation = store.claim('run-a', 'replacement', 100000)
    store.recover_operations('run-a', generation)
    budget = make_budget((store, generation, clock))
    def fail():
        raise TimeoutError()
    assert budget.operation('mixed', {}, fail)['state'] == 'confirmed_failed'
    assert budget.operation('mixed', {}, fail, attempt=1)['state'] == 'confirmed_failed'
    with pytest.raises(ValueError, match='retry'):
        budget.operation('mixed', {}, fail, attempt=2)
    assert budget.snapshot()['technical_retries']['mixed'] == 2


def test_concurrent_heartbeat_and_foreground_tick_do_not_double_bill(setup, monkeypatch):
    budget = make_budget(setup)
    budget.begin_active()
    setup[2].advance(5)
    store = setup[0]
    original_update = store.update_metadata
    barrier = Barrier(2)
    def synchronized_update(*args, **kwargs):
        barrier.wait(timeout=10)
        return original_update(*args, **kwargs)
    monkeypatch.setattr(store, 'update_metadata', synchronized_update)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: budget.tick(), range(2)))
    assert budget.snapshot()['active_ms'] == 5000


def test_operation_metadata_prepare_cannot_overwrite_reserved_identity(setup):
    store, generation, clock = setup
    before = store.metadata('run-a', 'identity')
    for key in ['identity', 'baseline', 'baseline_pending']:
        with pytest.raises(ValueError):
            store.operation('run-a', generation, 'bad-' + key, {},
                            lambda: pytest.fail('must not execute'),
                            prepare_metadata=(key, lambda value, prior: ({'changed': True}, None)))
    assert store.metadata('run-a', 'identity') == before
    assert store.list_operations('run-a') == []


def test_specific_quota_exhaustion_does_not_block_other_stage_work(setup):
    budget = make_budget(setup)
    for i in range(3):
        budget.reserve_detail(str(i), 'snapshot')
    with pytest.raises(ValueError):
        budget.reserve_detail('over-quota', 'snapshot')
    assert budget.operation('review-other-page', {}, lambda: 'audited')['state'] == 'completed'
    budget.reserve_revision('revision', 'candidate')
    budget.record_stage('reviewing', 'completed', scope='main', reason='remaining pages reviewed')


def test_physical_prepare_bills_deadline_crossed_after_logical_reservation(setup, monkeypatch):
    budget = make_budget(setup, active_seconds=1)
    store, generation, clock = setup
    budget.begin_active()
    clock.advance(.999)
    original_operation = store.operation
    def delayed_operation(*args, **kwargs):
        clock.advance(.002)  # Crosses deadline after _reserve's last heartbeat.
        return original_operation(*args, **kwargs)
    monkeypatch.setattr(store, 'operation', delayed_operation)
    with pytest.raises(ValueError, match='budget'):
        budget.operation('crossed-deadline', {}, lambda: pytest.fail('external effect must be denied'))
    persisted = budget.snapshot()
    assert persisted['active_ms'] == 1000
    assert persisted['logical_operations']['crossed-deadline']['execution_count'] == 0
    assert store.list_operations('run-a') == []
    stopped = [event['payload'] for event in store.events('run-a')
               if event['payload']['kind'] == 'active_budget_stopped']
    assert stopped[-1]['reason'] == 'execution_deadline'
    assert stopped[-1]['charged_ms'] == 1


def test_pending_retry_deadline_denial_retains_receipt_without_increment(setup, monkeypatch):
    budget = make_budget(setup, active_seconds=1)
    store, generation, clock = setup
    with pytest.raises(KeyboardInterrupt):
        budget.operation('pending', {}, lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    store.release('run-a', generation)
    generation = store.claim('run-a', 'replacement', 100000)
    store.recover_operations('run-a', generation)
    budget = make_budget((store, generation, clock))
    budget.begin_active()
    clock.advance(.999)
    original_operation = store.operation
    def delayed_operation(*args, **kwargs):
        clock.advance(.002)
        return original_operation(*args, **kwargs)
    monkeypatch.setattr(store, 'operation', delayed_operation)
    with pytest.raises(ValueError, match='budget'):
        budget.operation('pending', {}, lambda: pytest.fail('retry must be denied'))
    assert budget.snapshot()['active_ms'] == 1000
    assert budget.snapshot()['logical_operations']['pending']['execution_count'] == 1
    receipt = store.list_operations('run-a')[0]
    assert receipt['state'] == 'pending'
    assert receipt['attempts'] == 1


def test_deadline_prepare_does_not_charge_time_already_billed_by_heartbeat(setup, monkeypatch):
    budget = make_budget(setup, active_seconds=1)
    store, generation, clock = setup
    budget.begin_active()
    clock.advance(.999)
    original_operation = store.operation
    def heartbeat_before_prepare(*args, **kwargs):
        clock.advance(.002)
        budget.tick()  # Independent heartbeat wins the high-water update.
        return original_operation(*args, **kwargs)
    monkeypatch.setattr(store, 'operation', heartbeat_before_prepare)
    with pytest.raises(ValueError, match='budget'):
        budget.operation('heartbeat-race', {}, lambda: pytest.fail('deadline reached'))
    assert budget.snapshot()['active_ms'] == 1000
    stopped = store.events('run-a')[-1]['payload']
    assert stopped['kind'] == 'active_budget_stopped'
    assert stopped['charged_ms'] == 0
    assert stopped['observed_ms'] == 0


def test_deferred_denial_commit_still_rejects_expired_generation(setup):
    store, generation, clock = setup
    def expire(value, receipt):
        clock.advance(100001)
        return {'charged': 1}, {'kind': 'must_rollback'}, 'denied'
    with pytest.raises(ValueError, match='generation'):
        store.operation('run-a', generation, 'expired-denial', {},
                        lambda: pytest.fail('expired callback'),
                        prepare_metadata=('test.denial', expire))
    assert store.metadata('run-a', 'test.denial') is None
    assert store.list_operations('run-a') == []
    assert store.events('run-a') == []
