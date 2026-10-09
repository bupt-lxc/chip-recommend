"""Fenced, cumulative budgets for a single frozen discovery run.

All reservations precede side effects and survive restarts, including unused
or unknown reservations. A client ID identifies one immutable payload, while
logical query/snapshot/candidate/operation IDs enforce the actual limits.
Linked domains and identity dependencies use this same instance/run state.
Query text/language validation and canonical URL identity belong to callers.

Worker must heartbeat while waiting/backing off/inferencing and close active
segments before suspension. A crashed open segment preserves durable elapsed
time plus one configured heartbeat interval of conservative uncertainty.
Runtime must heartbeat independently of blocking model/network callbacks, at
most heartbeat_ms apart while active. The bounded charge is an estimate under
that scheduling obligation, not a claim about unobserved wall time; epoch
idle gaps are never counted as active time. No worker/thread is started here.
"""

from __future__ import annotations

import math
import time
import uuid

from .contracts import DOMAINS
from .store import _hash, _json, _key


STAGE_STATUSES = frozenset({'not_executed', 'in_progress', 'completed',
                          'partial_failure', 'failed', 'budget_stopped'})


def derive_status(fatal, interrupted, incomplete, validated_count):
    """Derive only from execution coverage and audited final candidate counts.

    Runtime supplies fatal for broken isolation/rules/infrastructure, or all
    necessary chains unavailable with no audited deliverable. Local failures
    alongside reviewed pages are incomplete, not fatal. Correct business
    rejection, unresolved dependencies/comparison and a fully used quota do
    not make execution incomplete. Recovered fallback retains diagnostics but
    removes its intent's outstanding failure. Zero validated candidates can
    therefore be no_findings. Program-only validation does not count here.
    """
    if type(validated_count) is not int or validated_count < 0:
        raise ValueError('validated_count must be a nonnegative integer')
    if fatal:
        return 'failed'
    if interrupted:
        return 'interrupted'
    if incomplete:
        return 'partial'
    return 'success' if validated_count else 'no_findings'


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def _ids(values, *, distinct=True):
    if not isinstance(values, list):
        raise ValueError('IDs must be a list')
    keys = [_key(value) for value in values]
    if distinct and len(set(keys)) != len(keys):
        raise ValueError('IDs must be distinct')
    return keys


class RunBudget:
    """One run's durable limits, with injectable monotonic and epoch clocks.

    active_seconds/heartbeat_ms are immutable on first configuration. Omit
    them on resume to reuse the original values. operation retries are total
    attempts 0,1,2 keyed by stable logical ID, independent of client IDs.
    """

    KEY = 'domain_budget.v1'

    def __init__(self, store, run_id, generation, *, active_seconds=None,
                 heartbeat_ms=None, monotonic=None, epoch=None):
        self.store, self.run_id, self.generation = store, run_id, generation
        self.domain_id = store.get_run(run_id)['payload'].get('domain')
        self.monotonic, self.epoch = monotonic or time.monotonic, epoch or time.time
        self.token = uuid.uuid4().hex
        self._segment_start = None
        if active_seconds is not None:
            _integer(active_seconds, 1, 1800, 'active_seconds')
        if heartbeat_ms is not None:
            _integer(heartbeat_ms, 1, 1800000, 'heartbeat_ms')

        def configure(state):
            if state is None:
                state = {'active_limit_ms': (active_seconds or 1800) * 1000,
                         'heartbeat_ms': heartbeat_ms or 1000, 'active_ms': 0,
                         'segment': None, 'uncertainties': [], 'reservations': {},
                         'rounds': {}, 'query_ids': [], 'query_count': 0,
                         'query_results': {}, 'main_candidates': [],
                         'page_details': {}, 'technical_retries': {},
                         'candidate_revisions': {}, 'logical_operations': {}, 'stages': {}}
            elif ((active_seconds is not None and state['active_limit_ms'] != active_seconds * 1000)
                  or (heartbeat_ms is not None and state['heartbeat_ms'] != heartbeat_ms)):
                raise ValueError('budget configuration is immutable')
            return state, None, None
        self._update(configure)

    def _update(self, update, **kwargs):
        return self.store.update_metadata(self.run_id, self.generation, self.KEY, update, **kwargs)

    def snapshot(self):
        return self.store.metadata(self.run_id, self.KEY)

    def _available(self, state):
        if state['active_ms'] >= state['active_limit_ms']:
            raise ValueError('active budget exhausted')
        segment = state['segment']
        if segment is not None and segment['generation'] != self.generation:
            raise ValueError('recover the abandoned active segment before reserving work')

    def _reserve(self, request_id, payload, spend, *, failed_operation=None):
        request_id = _key(request_id)
        payload_hash = _hash(_json(payload))
        # Persist measured elapsed time before deciding whether another external
        # effect may start. Denying the quota must not roll this heartbeat back.
        if self._segment_start is not None:
            self.tick()
        def reserve(state):
            existing = state['reservations'].get(request_id)
            if existing is not None:
                if existing['payload_hash'] != payload_hash:
                    raise ValueError('reservation payload conflict')
                return state, existing['result'], None
            self._available(state)
            result = spend(state)
            state['reservations'][request_id] = {'payload': payload, 'payload_hash': payload_hash,
                                                 'result': result, 'uncertain': False}
            return state, result, {'kind': 'budget_reservation', 'request_id': request_id,
                                  'payload_hash': payload_hash, 'reservation': payload, 'result': result}
        return self._update(reserve, failed_operation=failed_operation)

    def reserve_queries(self, request_id, query_ids, *, round_number, scope='main'):
        if scope != 'main':
            raise ValueError('linked/identity scope cannot allocate new search rounds')
        query_ids = _ids(query_ids)
        _integer(round_number, 0, 2, 'round_number')
        if ((round_number == 0 and len(query_ids) != 10)
                or (round_number != 0 and not 1 <= len(query_ids) <= 5)):
            raise ValueError('initial batch requires exactly 10; supplement requires 1..5 queries')
        def spend(state):
            key = str(round_number)
            if key in state['rounds']:
                raise ValueError('search round already reserved')
            if round_number and str(round_number - 1) not in state['rounds']:
                raise ValueError('search rounds must be reserved in order')
            if set(query_ids) & set(state['query_ids']):
                raise ValueError('query logical IDs cannot be reused in a new round')
            if state['query_count'] + len(query_ids) > 20:
                raise ValueError('query budget exhausted')
            state['rounds'][key] = query_ids
            state['query_ids'].extend(query_ids)
            state['query_count'] += len(query_ids)
            return {'round_number': round_number, 'query_ids': query_ids,
                    'query_count': state['query_count']}
        return self._reserve(request_id, {'kind': 'queries', 'round': round_number,
                                         'query_ids': query_ids}, spend)

    def admit_results(self, request_id, query_id, candidate_ids):
        """Caller provides canonical IDs; denied batches stay outside the main list.

        Caller must persist/report budget exclusions separately, not silently
        drop them. Admission is atomic; it never truncates an input batch.
        """
        query_id, candidate_ids = _key(query_id), _ids(candidate_ids, distinct=False)
        def spend(state):
            if query_id not in state['query_ids']:
                raise ValueError('query was not reserved')
            query_results = state['query_results'].get(query_id, [])
            new_results = list(dict.fromkeys(query_results + candidate_ids))
            main = list(dict.fromkeys(state['main_candidates'] + candidate_ids))
            if len(new_results) > 10 or len(main) > 100:
                raise ValueError('result/main candidate budget exhausted')
            state['query_results'][query_id] = new_results
            state['main_candidates'] = main
            return {'query_count': len(new_results), 'main_count': len(main)}
        return self._reserve(request_id, {'kind': 'results', 'query_id': query_id,
                                         'candidate_ids': candidate_ids}, spend)

    def _reserve_count(self, request_id, logical_id, field, limit, kind):
        logical_id = _key(logical_id)
        def spend(state):
            count = state[field].get(logical_id, 0) + 1
            if count > limit:
                raise ValueError(f'{kind} budget exhausted')
            state[field][logical_id] = count
            return {'logical_id': logical_id, 'count': count}
        return self._reserve(request_id, {'kind': kind, 'logical_id': logical_id}, spend)

    def reserve_detail(self, request_id, snapshot_id):
        """Charge a detail read of a saved snapshot; plain preview is read-only."""
        return self._reserve_count(request_id, snapshot_id, 'page_details', 3, 'detail')

    def reserve_revision(self, request_id, candidate_id):
        """Candidate ID is the root logical candidate, never a version/client ID."""
        return self._reserve_count(request_id, candidate_id, 'candidate_revisions', 1, 'revision')

    @staticmethod
    def operation_id(logical_id, attempt):
        logical_id = _key(logical_id)
        _integer(attempt, 0, 2, 'attempt')
        return 'budget-op-' + _hash(_json([logical_id, attempt]))

    def operation(self, logical_id, payload, execute, *, attempt=0):
        """Reserve and execute one logical network/model attempt (max three).

        Same attempt reuses receipt/reservation. A new attempt requires the
        prior callback to have exited with confirmed failure. Unknown running
        work requires RunStore old-generation recovery before replaying its
        same attempt, never a speculative parallel retry. Normal empty results
        are completed and cannot trigger a technical retry. Caller separately
        reserves any search-round or detail/revision quota this operation uses.
        Replaying an abandoned pending attempt also consumes a physical retry;
        all attempt IDs together allow at most three actual callback starts.
        """
        logical_id = _key(logical_id)
        operation_id = self.operation_id(logical_id, attempt)
        predecessor = self.operation_id(logical_id, attempt - 1) if attempt else None
        payload_hash = _hash(_json(payload))
        def spend(state):
            logical = state['logical_operations'].get(logical_id)
            if logical is not None and logical['payload_hash'] != payload_hash:
                raise ValueError('logical operation payload conflict')
            if logical is None:
                if attempt:
                    raise ValueError('initial logical operation missing')
                logical = state['logical_operations'][logical_id] = {
                    'payload_hash': payload_hash, 'attempts': [], 'execution_count': 0}
            if attempt != len(logical['attempts']):
                raise ValueError('technical retry attempts must be sequential')
            logical['attempts'].append(operation_id)
            return {'operation_id': operation_id, 'attempt': attempt}
        self._reserve(operation_id, {'kind': 'operation', 'logical_id': logical_id,
                                    'attempt': attempt, 'payload': payload}, spend,
                      failed_operation=predecessor)
        def prepare_execution(state, prior_receipt):
            accounting = {}
            if self._segment_start is not None:
                # Sample again in the final fenced execution transaction: a
                # logically reserved operation may have crossed its deadline
                # while waiting to acquire this transaction's write lock.
                elapsed_ms, epoch = self._elapsed()
                accounting = self._bill_elapsed(state, elapsed_ms, epoch)
            if state['active_ms'] >= state['active_limit_ms']:
                return state, {'kind': 'active_budget_stopped', 'reason': 'execution_deadline',
                               'operation_id': operation_id, 'active_ms': state['active_ms'],
                               **accounting}, 'active budget exhausted'
            self._available(state)
            logical = state['logical_operations'][logical_id]
            executions = logical['execution_count'] + 1
            if executions > 3:
                raise ValueError('technical retry budget exhausted')
            logical['execution_count'] = executions
            if executions > 1:
                state['technical_retries'][logical_id] = executions - 1
            if prior_receipt is not None:
                state['reservations'][operation_id].update(
                    uncertain=True, reason='abandoned callback external outcome unknown')
            return state, {'kind': 'budget_execution', 'logical_id': logical_id,
                           'operation_id': operation_id, 'physical_attempt': executions,
                           'recovered_unknown': prior_receipt is not None, **accounting}
        return self.store.operation(self.run_id, self.generation, operation_id,
                                    payload, execute, confirmed_failure=True,
                                    predecessor=predecessor,
                                    prepare_metadata=(self.KEY, prepare_execution))

    def mark_uncertain(self, request_id, reason):
        request_id, reason = _key(request_id), _key(reason)
        def mark(state):
            reservation = state['reservations'].get(request_id)
            if reservation is None:
                raise ValueError('unknown reservation')
            if reservation['uncertain']:
                if reservation['reason'] != reason:
                    raise ValueError('uncertainty reason conflict')
                return state, reservation, None
            reservation.update(uncertain=True, reason=reason)
            return state, reservation, {'kind': 'reservation_uncertain',
                                        'request_id': request_id, 'reason': reason}
        return self._update(mark)

    def begin_active(self):
        now, epoch = self._clock(self.monotonic), self._clock(self.epoch)
        def begin(state):
            old = state['segment']
            if old is not None:
                if old['token'] == self.token:
                    return state, state, None
                if old['generation'] >= self.generation:
                    raise ValueError('active segment already owned by this generation')
                charge = min(state['heartbeat_ms'], state['active_limit_ms'] - state['active_ms'])
                state['active_ms'] += charge
                state['uncertainties'].append({'kind': 'abandoned_active_segment',
                    'generation': old['generation'], 'token': old['token'],
                    'last_heartbeat_epoch': old['heartbeat_epoch'], 'charged_ms': charge,
                    'unobserved_time_unknown': True})
                state['segment'] = None
            if state['active_ms'] >= state['active_limit_ms']:
                # Commit crash accounting even when it consumes the remaining
                # budget; no new segment or expensive operation can then start.
                return state, state, {'kind': 'active_budget_stopped',
                                      'active_ms': state['active_ms']}
            state['segment'] = {'token': self.token, 'generation': self.generation,
                                'started_epoch': epoch, 'heartbeat_epoch': epoch,
                                'observed_ms': 0}
            return state, state, {'kind': 'active_begin', 'generation': self.generation,
                                  'active_ms': state['active_ms']}
        result = self._update(begin)
        if self._segment_start is None and result['segment'] is not None:
            self._segment_start = now
        return result

    @staticmethod
    def _clock(clock):
        value = clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('clock must return finite seconds')
        return value

    def _elapsed(self):
        if self._segment_start is None:
            raise ValueError('begin_active is required for a local monotonic segment')
        now, epoch = self._clock(self.monotonic), self._clock(self.epoch)
        elapsed_ms = int((now - self._segment_start) * 1000)
        if elapsed_ms < 0:
            raise ValueError('monotonic clock moved backwards')
        return elapsed_ms, epoch

    def _bill_elapsed(self, state, elapsed_ms, epoch, *, close=False):
        segment = state['segment']
        if segment is None or segment['token'] != self.token or segment['generation'] != self.generation:
            raise ValueError('active segment ownership conflict')
        # Heartbeat and foreground may sample the same/older monotonic
        # offset concurrently. Only the durable high-water mark determines
        # the delta, under the same transaction as the cumulative charge.
        delta = max(0, elapsed_ms - segment['observed_ms'])
        charged = min(delta, state['active_limit_ms'] - state['active_ms'])
        state['active_ms'] += charged
        segment['observed_ms'] = max(segment['observed_ms'], elapsed_ms)
        segment['heartbeat_epoch'] = max(segment['heartbeat_epoch'], epoch)
        if close:
            state['segment'] = None
        return {'charged_ms': charged, 'observed_ms': delta, 'active_ms': state['active_ms']}

    def _tick(self, close):
        elapsed_ms, epoch = self._elapsed()
        def tick(state):
            accounting = self._bill_elapsed(state, elapsed_ms, epoch, close=close)
            return state, state, {'kind': 'active_end' if close else 'active_heartbeat',
                                  **accounting}
        result = self._update(tick)
        if close:
            self._segment_start = None
        return result

    def tick(self):
        return self._tick(False)

    def end_active(self):
        return self._tick(True)

    def record_stage(self, stage, status, *, scope, reason, domain_id=None):
        """Record actual scoped stage results without changing current stage.

        RunStore.checkpoint sets the current stage separately; reaching a later
        stage never fabricates completion of earlier stages. Available even
        after budget exhaustion, so finalization/reporting remains possible.
        """
        stage, scope, reason = _key(stage), _key(scope), _key(reason)
        if not isinstance(status, str) or status not in STAGE_STATUSES:
            raise ValueError('unknown stage status')
        domain_id = domain_id if domain_id is not None else self.domain_id
        if domain_id not in DOMAINS:
            raise ValueError('unknown stage domain')
        result = {'status': status, 'reason': reason, 'scope': scope, 'domain_id': domain_id}
        def record(state):
            state['stages'].setdefault(stage, {})[scope] = result
            return state, result, {'kind': 'stage_result', 'stage': stage,
                                   'stage_status': status, 'scope': scope, 'reason': reason,
                                   'domain_id': domain_id}
        return self._update(record)

    def stages(self):
        return self.snapshot()['stages']
