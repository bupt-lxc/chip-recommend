"""Persistent V3 control state; all databases are isolated temporary fixtures."""

import hashlib
import importlib.util
import json
import sqlite3
import os
import shutil
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest


def test_store_module_exists():
    assert importlib.util.find_spec('chip_model.pipeline.domain_discovery.store') is not None


@pytest.fixture
def clock():
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    from chip_model.pipeline.domain_discovery.store import RunStore
    return RunStore(tmp_path / 'runs', clock=clock)


@pytest.fixture
def active(store):
    store.start('client-001', {'domain': 'specs'}, 'run-a')
    return store.claim('run-a', 'worker-a', 30)


def test_start_binding_survives_reopen_and_uses_canonical_json(store, clock):
    first = store.start(' client-001 ', {'domain': 'specs', 'chip': '芯片'}, 'run-a')
    reopened = type(store)(store.root, clock=clock)
    again = reopened.start('client-001', {'chip': '芯片', 'domain': 'specs'}, 'unused-id')
    assert again == first
    assert first['run_id'] == 'run-a'
    assert first['request_key'] == 'client-001'
    assert first['status'] == 'preparing'
    raw = '{"chip":"芯片","domain":"specs"}'
    assert first['payload_hash'] == hashlib.sha256(raw.encode()).hexdigest()
    assert not (store.root / 'unused-id').exists()
    assert not (store.root / 'run-a' / 'data.db').exists()
    with pytest.raises(ValueError, match='冲突'):
        store.start('client-001', {'domain': 'compute'}, 'run-b')
    with pytest.raises(ValueError, match='冲突'):
        store.start('another-client', {'domain': 'specs'}, 'run-a')


@pytest.mark.parametrize('run_id', ['../escape', '/abs', 'CON', 'com1', 'a/b', 'a\\b', '', 'a' * 129, '中文'])
def test_bad_run_ids_do_not_create_paths(store, run_id):
    before = set(store.root.iterdir())
    with pytest.raises(ValueError):
        store.start('k', {}, run_id)
    assert set(store.root.iterdir()) == before


@pytest.mark.parametrize('key', [None, '', ' ', 'a\x00b', 'a\nb', 'x' * 201, 3])
def test_invalid_keys(store, key):
    with pytest.raises(ValueError):
        store.start(key, {}, 'run-a')
    assert not (store.root / 'run-a').exists()


@pytest.mark.parametrize('payload', [{'x': float('nan')}, {'x': float('inf')}, {1: 'x'}, {'x': (1, 2)}, {'x': object()}])
def test_payload_must_be_json_without_coercion(store, payload):
    with pytest.raises(ValueError):
        store.start('k', payload, 'run-a')
    assert not (store.root / 'run-a').exists()


def test_two_connections_compete_for_single_lease(store, clock):
    store.start('k', {}, 'run-a')
    other = type(store)(store.root, clock=clock)
    barrier = Barrier(2)

    def claim(candidate):
        barrier.wait(timeout=10)
        try:
            return candidate.claim('run-a', 'worker', 30)
        except ValueError:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, [store, other]))
    assert sorted(results, key=str) == [1, 'conflict']


def test_fencing_expiry_release_and_renew(store, active, clock):
    store.renew('run-a', active, 10)
    clock.now += 11
    for action in [lambda: store.renew('run-a', active, 10),
                   lambda: store.release('run-a', active),
                   lambda: store.checkpoint('run-a', active, 'searching', {})]:
        with pytest.raises(ValueError, match='租约'):
            action()
    new = store.claim('run-a', 'worker-b', 20)
    assert new > active
    with pytest.raises(ValueError, match='租约'):
        store.checkpoint('run-a', active, 'searching', {})
    store.release('run-a', new)
    last = store.claim('run-a', 'worker-c', 20)
    assert last > new
    with pytest.raises(ValueError, match='租约'):
        store.renew('run-a', new, 20)


@pytest.mark.parametrize('seconds', [0, -1, float('nan'), float('inf'), True, '5'])
def test_invalid_lease_duration(store, active, seconds):
    with pytest.raises(ValueError):
        store.renew('run-a', active, seconds)


def test_operation_replays_and_callback_has_no_write_transaction(store, active):
    calls = []

    def execute():
        # Writes through a separate connection must not block on the callback.
        with sqlite3.connect(store.root / 'control.sqlite3', timeout=0.1) as conn:
            conn.execute('CREATE TABLE callback_probe (n INTEGER)')
        calls.append(1)
        return {'answer': '结果'}
    receipt = store.operation('run-a', active, 'client-001', {'q': 'x'}, execute)
    assert receipt['state'] == 'completed'
    assert receipt['result'] == {'answer': '结果'}
    assert receipt['attempts'] == 1
    assert store.operation('run-a', active, 'client-001', {'q': 'x'}, execute) == receipt
    assert calls == [1]
    with pytest.raises(ValueError, match='冲突'):
        store.operation('run-a', active, 'client-001', {'q': 'other'}, execute)


def test_concurrent_running_operation_returns_receipt(store, active, clock):
    entered, finish = Event(), Event()
    calls = []
    other = type(store)(store.root, clock=clock)

    def execute():
        calls.append(1)
        entered.set()
        assert finish.wait(10)
        return 42
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(store.operation, 'run-a', active, 'op', {}, execute)
        assert entered.wait(10)
        try:
            replay = other.operation('run-a', active, 'op', {}, execute)
            assert replay['state'] == 'running'
        finally:
            finish.set()
        assert future.result()['result'] == 42
    assert calls == [1]


def test_generation_changes_during_callback_fence_result(store, active, clock):
    def execute():
        clock.now += 31
        store.claim('run-a', 'replacement', 30)
        return 'stale'
    with pytest.raises(ValueError, match='租约'):
        store.operation('run-a', active, 'op', {}, execute)
    assert store.list_operations('run-a')[0]['state'] == 'running'
    assert store.list_operations('run-a')[0]['result'] is None


def test_crashed_operations_need_explicit_recovery_and_keep_attempts(store, active, clock):
    def fail():
        raise RuntimeError('process failure')
    with pytest.raises(RuntimeError):
        store.operation('run-a', active, 'failed', {}, fail)
    store.operation('run-a', active, 'finished', {}, lambda: 'saved')
    clock.now += 31
    new = store.claim('run-a', 'replacement', 30)
    assert store.operation('run-a', new, 'failed', {}, fail)['state'] == 'running'
    assert store.recover_operations('run-a', new) == 1
    receipt = store.operation('run-a', new, 'failed', {}, lambda: 'recovered')
    assert receipt['attempts'] == 2
    assert receipt['result'] == 'recovered'
    assert store.operation('run-a', new, 'finished', {}, fail)['result'] == 'saved'


def test_tasks_durable_keys_fencing_and_recovery(store, active, clock):
    task_id = store.enqueue('run-a', active, 'task-a', {'url': 'a'})
    assert store.enqueue('run-a', active, 'task-a', {'url': 'a'}) == task_id
    with pytest.raises(ValueError, match='冲突'):
        store.enqueue('run-a', active, 'task-a', {'url': 'b'})
    task = store.claim_task('run-a', active, task_id)
    assert task['attempts'] == 1
    done_id = store.enqueue('run-a', active, 'done', {})
    store.claim_task('run-a', active, done_id)
    store.complete_task('run-a', active, done_id, {'ok': True})
    clock.now += 31
    new = store.claim('run-a', 'replacement', 30)
    with pytest.raises(ValueError, match='租约'):
        store.complete_task('run-a', active, task_id, {})
    assert store.recover_tasks('run-a', new) == 1
    assert store.claim_task('run-a', new, task_id)['attempts'] == 2
    store.complete_task('run-a', new, task_id, 123)
    assert {task['state'] for task in store.list_tasks('run-a')} == {'completed'}
    assert store.recover_tasks('run-a', new) == 0


def test_checkpoints_and_export_derive_from_durable_events(store, active):
    first = store.checkpoint('run-a', active, 'searching', {'n': 1})
    second = store.checkpoint('run-a', active, 'completed', {'status': 'success'})
    events = store.events('run-a')
    assert [event['event_id'] for event in events] == [first, second]
    assert events[0]['sequence'] < events[1]['sequence']
    assert events[0]['domain_id'] == 'specs'
    assert store.get_run('run-a')['status'] == 'success'
    path = store.export_events('run-a')
    original = path.read_bytes()
    path.write_text('interrupted output', encoding='utf-8')
    assert store.export_events('run-a').read_bytes() == original
    assert len(store.events('run-a')) == 2


def make_source(path, wal=False):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE chips (id TEXT, chip_model TEXT)')
    conn.execute("INSERT INTO chips VALUES ('1', 'baseline')")
    conn.commit()
    return conn


def test_baseline_wal_backup_immutable_resume_and_source_untouched(store, active, tmp_path):
    source = tmp_path / 'source.db'
    conn = make_source(source, wal=True)
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        metadata = store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules-a', manifest={'domain': 'specs'})
        baseline = store.run_path('run-a') / 'data.db'
        with sqlite3.connect(f'{baseline.as_uri()}?mode=ro', uri=True) as snap:
            assert snap.execute('SELECT * FROM chips').fetchall() == [('1', 'baseline')]
            assert snap.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('chips',)]
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('SELECT COUNT(*) FROM chips').fetchone()[0] == 1
        conn.execute("INSERT INTO chips VALUES ('2', 'new live value')")
        conn.commit()
        assert store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules-a', manifest={'domain': 'specs'}) == metadata
        with sqlite3.connect(f'{baseline.as_uri()}?mode=ro', uri=True) as snap:
            assert snap.execute('SELECT COUNT(*) FROM chips').fetchone()[0] == 1
        assert metadata['pipeline'] == 'hermes-open-web-v3'
        assert metadata['contract_version'] == 3
        with pytest.raises(ValueError, match='规则'):
            store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules-b')
    finally:
        conn.close()


def test_baseline_corruption_fails_without_replacing_original(store, active, tmp_path):
    source = tmp_path / 'source.db'
    make_source(source).close()
    store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules-a')
    baseline = store.run_path('run-a') / 'data.db'
    baseline.chmod(0o600)
    baseline.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='基线'):
        store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules-a')
    assert baseline.read_bytes() == b'corrupted'


def test_legacy_version_is_rejected(store, tmp_path):
    legacy = store.root / 'legacy'
    legacy.mkdir()
    (legacy / 'manifest.json').write_text(json.dumps({'pipeline': 'hermes-open-web-v2'}))
    with pytest.raises(ValueError, match='版本'):
        store.start('resume-key', {}, 'legacy')
    assert not (legacy / 'run.sqlite3').exists()


def test_backup_failure_preserves_binding_and_does_not_seed(store, active, tmp_path):
    source = tmp_path / 'missing.db'
    with pytest.raises((ValueError, sqlite3.Error)):
        store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules-a')
    assert not source.exists()
    assert store.start('client-001', {'domain': 'specs'}, 'ignored')['run_id'] == 'run-a'
    assert store.list_tasks('run-a') == []
    assert not (store.run_path('run-a') / 'data.db').exists()


def test_storage_uses_rollback_journals(store, active):
    for path in [store.root / 'control.sqlite3', store.run_path('run-a') / 'run.sqlite3']:
        with sqlite3.connect(path) as conn:
            assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'


def test_binding_before_directory_creation_reserves_casefolded_id(store, clock, monkeypatch):
    def crash(_):
        raise RuntimeError('interrupted before mkdir')
    with monkeypatch.context() as patch:
        patch.setattr(store, '_ensure_run', crash)
        with pytest.raises(RuntimeError):
            store.start('original', {}, 'run-A')
    reopened = type(store)(store.root, clock=clock)
    with pytest.raises(ValueError, match='冲突'):
        reopened.start('other', {}, 'run-a')
    assert reopened.start('original', {}, 'unused')['run_id'] == 'run-A'
    assert reopened.get_run('run-A')['request_key'] == 'original'


def test_directory_junction_cannot_escape_or_alias_another_run(store, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    for name, target in [('escape', outside), ('alias', store.root / 'original')]:
        target.mkdir(exist_ok=True)
        link = store.root / name
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
        with pytest.raises(ValueError):
            store.start(name, {}, name)
        assert not (target / 'run.sqlite3').exists()


@pytest.mark.parametrize('takeover', [False, True])
def test_expired_and_stale_generation_reject_all_mutations(store, active, clock, takeover):
    task_id = store.enqueue('run-a', active, 'work', {})
    store.claim_task('run-a', active, task_id)
    clock.now += 31
    if takeover:
        store.claim('run-a', 'next', 30)
    def unexpected_callback():
        pytest.fail('expired operation executed')
    for action in [lambda: store.renew('run-a', active, 30),
                   lambda: store.release('run-a', active),
                   lambda: store.checkpoint('run-a', active, 'searching', {}),
                   lambda: store.operation('run-a', active, 'op', {}, unexpected_callback),
                   lambda: store.enqueue('run-a', active, 'new', {}),
                   lambda: store.claim_task('run-a', active, task_id),
                   lambda: store.complete_task('run-a', active, task_id, {}),
                   lambda: store.recover_tasks('run-a', active),
                   lambda: store.recover_operations('run-a', active)]:
        with pytest.raises(ValueError, match='租约'):
            action()
    assert store.events('run-a') == []
    assert store.list_operations('run-a') == []
    assert store.list_tasks('run-a')[0]['state'] == 'running'


@pytest.mark.parametrize('after_move', [False, True])
def test_baseline_initialization_crash_recovers_original_snapshot(store, active, tmp_path, monkeypatch, clock, after_move):
    from chip_model.pipeline.domain_discovery import store as module
    source = tmp_path / 'source.db'
    make_source(source).close()
    replace = module.os.replace
    def interrupted_replace(src, dst):
        if after_move:
            replace(src, dst)
        raise RuntimeError('process stopped at rename')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', interrupted_replace)
        with pytest.raises(RuntimeError, match='rename'):
            store.ensure_baseline('run-a', active, source, rule_bundle_hash='original-rules')
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("INSERT INTO chips VALUES ('2', 'later')")
        conn.commit()
    reopened = type(store)(store.root, clock=clock)
    with pytest.raises(ValueError, match='规则'):
        reopened.ensure_baseline('run-a', active, source, rule_bundle_hash='changed-rules')
    metadata = reopened.ensure_baseline('run-a', active, source, rule_bundle_hash='original-rules')
    baseline = reopened.run_path('run-a') / 'data.db'
    with closing(sqlite3.connect(baseline.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM chips').fetchone()[0] == 1
    assert metadata['baseline_sha256'] == hashlib.sha256(baseline.read_bytes()).hexdigest()


def test_run_database_identity_cannot_be_swapped(store, active):
    store.start('second', {}, 'run-b')
    shutil.copyfile(store.run_path('run-b') / 'run.sqlite3', store.run_path('run-a') / 'run.sqlite3')
    with pytest.raises(ValueError, match='标记'):
        store.checkpoint('run-a', active, 'searching', {})
    with pytest.raises(ValueError, match='标记'):
        store.start('client-001', {'domain': 'specs'}, 'ignored')


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_pending_baseline_damage_never_recaptures_live_source(store, active, tmp_path, monkeypatch, damage):
    from chip_model.pipeline.domain_discovery import store as module
    source = tmp_path / 'source.db'
    make_source(source).close()
    def stop(*_):
        raise RuntimeError('stop before rename')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', stop)
        with pytest.raises(RuntimeError):
            store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules')
    staging, = store.run_path('run-a').glob('.baseline-*.tmp')
    if damage == 'missing':
        staging.unlink()
    else:
        staging.write_bytes(b'broken')
    with pytest.raises(ValueError, match='基线'):
        store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules')
    assert not (store.run_path('run-a') / 'data.db').exists()


def test_lease_turnover_after_pending_commit_prevents_old_baseline_finalize(store, active, tmp_path, monkeypatch, clock):
    from chip_model.pipeline.domain_discovery import store as module
    source = tmp_path / 'source.db'
    make_source(source).close()
    original = module._file_hash
    count = 0
    next_generation = None
    def hash_and_takeover(path):
        nonlocal count, next_generation
        digest = original(path)
        count += 1
        if count == 2:  # pending marker committed; verification is outside lock
            clock.now += 31
            next_generation = type(store)(store.root, clock=clock).claim('run-a', 'next', 30)
        return digest
    with monkeypatch.context() as patch:
        patch.setattr(module, '_file_hash', hash_and_takeover)
        with pytest.raises(ValueError, match='租约'):
            store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules')
    assert not (store.run_path('run-a') / 'data.db').exists()
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("INSERT INTO chips VALUES ('2', 'later')")
        conn.commit()
    store.ensure_baseline('run-a', next_generation, source, rule_bundle_hash='rules')
    baseline = store.run_path('run-a') / 'data.db'
    with closing(sqlite3.connect(baseline.as_uri() + '?mode=ro', uri=True)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM chips').fetchone()[0] == 1


def test_crash_before_pending_commit_keeps_binding_and_can_initialize(store, active, tmp_path, monkeypatch):
    from chip_model.pipeline.domain_discovery import store as module
    source = tmp_path / 'source.db'
    make_source(source).close()
    def stop(_):
        raise RuntimeError('stop before pending marker')
    with monkeypatch.context() as patch:
        patch.setattr(module, '_file_hash', stop)
        with pytest.raises(RuntimeError):
            store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules')
    assert not list(store.run_path('run-a').glob('.baseline-*.tmp'))
    assert store.start('client-001', {'domain': 'specs'}, 'ignored')['run_id'] == 'run-a'
    assert store.ensure_baseline('run-a', active, source, rule_bundle_hash='rules')['baseline_sha256']


def test_export_interruption_keeps_committed_events_and_retry_is_idempotent(store, active, monkeypatch):
    from chip_model.pipeline.domain_discovery import store as module
    store.checkpoint('run-a', active, 'searching', {'n': 1})
    path = store.export_events('run-a')
    first = path.read_bytes()
    store.checkpoint('run-a', active, 'searching', {'n': 2})
    def stop(*_):
        raise OSError('audit disk interruption')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'replace', stop)
        with pytest.raises(OSError):
            store.export_events('run-a')
    assert path.read_bytes() == first
    records = store.events('run-a')
    assert len(records) == 2
    assert [json.loads(line) for line in store.export_events('run-a').read_text(encoding='utf-8').splitlines()] == records
    assert [json.loads(line) for line in store.export_events('run-a').read_text(encoding='utf-8').splitlines()] == records


def test_unknown_legacy_operation_does_not_create_control_database_in_legacy_directory(store):
    legacy = store.root / 'legacy'
    legacy.mkdir()
    (legacy / 'manifest.json').write_text('{"pipeline":"hermes-open-web-v2"}')
    with pytest.raises(ValueError, match='版本'):
        store.checkpoint('legacy', 1, 'searching', {})
    assert not (legacy / 'run.sqlite3').exists()


def test_invalid_callback_result_leaves_recoverable_receipt(store, active):
    with pytest.raises(ValueError):
        store.operation('run-a', active, 'bad-result', {}, lambda: float('nan'))
    assert store.list_operations('run-a')[0]['state'] == 'running'
    assert store.list_operations('run-a')[0]['result'] is None
