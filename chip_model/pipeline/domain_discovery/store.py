"""Durable V3 run state and immutable business baselines.

Layout: root/control.sqlite3 and root/<run_id>/{run.sqlite3,data.db}.
All fenced writes attach the run DB to the control connection and check the
lease inside the same BEGIN IMMEDIATE transaction. Both use DELETE journals
and FULL synchronous on one filesystem (SQLite's super-journal protocol).
Callbacks and consistent backups run outside those short write transactions.

Receipts guarantee fenced persistence, not exactly-once external side effects:
after a process crash the owner must explicitly recover unfinished work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from .contracts import _start_request_id, validate_run_id


PIPELINE = 'hermes-open-web-v3'
CONTRACT_VERSION = 3

_CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS starts (
    request_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL, run_id TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, path_key TEXT UNIQUE NOT NULL,
    pipeline TEXT NOT NULL, contract_version INTEGER NOT NULL,
    status TEXT NOT NULL, stage TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dispatches (
    run_id TEXT PRIMARY KEY, request_key TEXT NOT NULL, timeout_seconds INTEGER NOT NULL,
    source_locator TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS leases (
    run_id TEXT PRIMARY KEY, owner TEXT, generation INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0
);
"""

_RUN_DDL = (
    """CREATE TABLE IF NOT EXISTS run.operations (
        request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        state TEXT NOT NULL, result_json TEXT, stage TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS run.tasks (
        task_key TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL,
        payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
        state TEXT NOT NULL, generation INTEGER NOT NULL, result_json TEXT,
        attempts INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS run.events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
        stage TEXT NOT NULL, domain_id TEXT, created_at REAL NOT NULL, payload_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS run.metadata (
        key TEXT PRIMARY KEY, value_json TEXT NOT NULL)""",
)


def _json(value):
    """Reject Python-only/coerced values, nonfinite numbers and circular input."""
    def check(item, seen):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) not in (dict, list) or id(item) in seen:
            raise ValueError('必须使用有限且无循环的 JSON 值')
        seen.add(id(item))
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError('JSON 对象键必须为字符串')
            children = item.values()
        else:
            children = item
        for child in children:
            check(child, seen)
        seen.remove(id(item))
    try:
        check(value, set())
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError('无效 JSON') from exc


def _hash(text):
    try:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except UnicodeError as exc:
        raise ValueError('无效 UTF-8 JSON') from exc


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _key(value):
    result = _start_request_id(value)
    if result is None:
        raise ValueError('请求幂等键必须为非空字符串')
    return result


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('租约时长必须为有限正数')
    return value


def consistent_backup(source, destination):
    """Backup a read-only SQLite source, including committed WAL transactions.

    Destination must be new; RunStore supplies an approved run-local staging
    path. This utility does not seed, migrate or change source journal settings.
    """
    source, destination = Path(source).resolve(), Path(destination)
    if source == destination.resolve():
        raise ValueError('备份目标不能为源数据库')
    # Exclusive creation also prevents accidentally replacing a saved baseline.
    with destination.open('xb'):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as origin:
            with closing(sqlite3.connect(destination)) as target:
                origin.backup(target)
                target.execute('PRAGMA journal_mode=DELETE')
                target.execute('PRAGMA synchronous=FULL')
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def validate_source_locator(locator):
    """Internal portable path only; container absolute paths never cross hosts."""
    if (not isinstance(locator, str) or not locator or ':' in locator or '\\' in locator
            or PurePosixPath(locator).is_absolute() or any(part in {'', '.', '..'} for part in locator.split('/'))
            or any(ord(char) < 32 for char in locator)):
        raise ValueError('来源数据库定位未配置或不安全；需要共享数据目录内的相对路径')
    return locator


def resolve_source_locator(data_root, locator):
    root = Path(data_root).resolve()
    locator = validate_source_locator(locator)
    source = (root / locator).resolve()
    if not source.is_relative_to(root):
        raise ValueError('来源数据库越出共享数据目录')
    if not source.is_file() or not os.access(source, os.R_OK) or source.stat().st_size == 0:
        raise ValueError('来源数据库必须为可读、已初始化的 SQLite 常规文件')
    # No SQLite connection/PRAGMA here: an exclusive source lock must not
    # block binding or the interval between lease claim and active supervision.
    # The runtime's supervised readonly backup and schema checks validate it.
    return source


def source_locator_for(data_root, source_db):
    root, source = Path(data_root).resolve(), Path(source_db).resolve()
    try:
        locator = source.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError('来源数据库必须位于共享数据目录内') from exc
    resolve_source_locator(root, locator)
    return locator


class RunStore:
    """Connection-per-operation store; a clock returns persistent epoch seconds."""

    def __init__(self, root, clock=None):
        self.root = Path(root).resolve()
        self.clock = clock or time.time
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CONTROL_DDL)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(dispatches)')}
            if 'source_locator' not in columns:
                conn.execute("ALTER TABLE dispatches ADD COLUMN source_locator TEXT NOT NULL DEFAULT ''")

    def _contained(self, path):
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError('路径越出允许的运行根目录')
        if resolved.exists() and resolved.stat().st_dev != self.root.stat().st_dev:
            raise ValueError('运行存储必须位于同一文件系统')
        return resolved

    def run_path(self, run_id):
        """Validate the public ID and resolve its directory within the sole root."""
        validate_run_id(run_id)
        path = self._contained(self.root / run_id)
        # Run directories must not alias each other through symlinks/junctions.
        if path != self.root / run_id:
            raise ValueError('运行目录不能使用符号链接或目录联接')
        return path

    @contextmanager
    def _connect(self, run_id=None, *, bootstrap=False):
        control = self._contained(self.root / 'control.sqlite3')
        run_db = None
        if run_id is not None:
            run_db = self._contained(self.run_path(run_id) / 'run.sqlite3')
        conn = sqlite3.connect(control, uri=True, isolation_level=None, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('PRAGMA synchronous=FULL')
            if run_db is not None:
                # Validate control identity before ATTACH can create any file
                # inside an unknown or legacy directory.
                run = self._run(conn, run_id)
                initialized = run['initialized']
                if initialized:
                    if not run_db.is_file() or run_db.stat().st_size == 0:
                        raise ValueError('已初始化的运行数据库缺失或为空，不能自动重建')
                elif not bootstrap:
                    raise ValueError('运行数据库尚未初始化；请重放原始启动请求')
                # mode=rw closes the existence-check/ATTACH gap: initialized
                # databases must never be implicitly created by SQLite.
                mode = 'rw' if initialized else 'rwc'
                conn.execute('ATTACH DATABASE ? AS run', (run_db.as_uri() + '?mode=' + mode,))
                tables = {row[0] for row in conn.execute("SELECT name FROM run.sqlite_master WHERE type='table'")}
                if initialized and not {'operations', 'tasks', 'events', 'metadata'} <= tables:
                    raise ValueError('已初始化的运行数据库 schema 缺失，不能自动修复')
                if 'metadata' in tables:
                    marker = self._baseline_metadata(conn, 'identity')
                    if marker != self._identity(run_id):
                        raise ValueError('运行身份或版本标记不匹配')
                conn.execute('PRAGMA run.journal_mode=DELETE')
                conn.execute('PRAGMA run.synchronous=FULL')
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self, run_id=None, generation=None, *, bootstrap=False):
        with self._connect(run_id, bootstrap=bootstrap) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                if generation is not None:
                    self._fence(conn, run_id, generation)
                yield conn
                # Expiry can occur during local processing as well as callbacks.
                if generation is not None:
                    self._fence(conn, run_id, generation)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _run(self, conn, run_id):
        row = conn.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('未知运行；旧运行版本不能续跑')
        if row['pipeline'] != PIPELINE or row['contract_version'] != CONTRACT_VERSION:
            raise ValueError('运行版本不兼容：需要 hermes-open-web-v3 合同版本 3')
        if 'initialized' not in row.keys():
            raise ValueError('运行控制数据库版本缺少持久初始化标记，不能自动续跑')
        return row

    def _fence(self, conn, run_id, generation):
        self._run(conn, run_id)
        row = conn.execute('SELECT * FROM leases WHERE run_id=?', (run_id,)).fetchone()
        if (type(generation) is not int or row is None or row['generation'] != generation
                or row['owner'] is None or row['expires_at'] <= self.clock()):
            raise ValueError('租约已失效或 generation 不匹配')

    def _binding(self, conn, row):
        run = self._run(conn, row['run_id'])
        return {'run_id': row['run_id'], 'request_key': row['request_key'],
                'payload': json.loads(row['payload_json']), 'payload_hash': row['payload_hash'],
                'created_at': row['created_at'], 'status': run['status'], 'stage': run['stage'],
                'pipeline': run['pipeline'], 'contract_version': run['contract_version']}

    def start(self, key, payload, run_id, *, timeout=1800, source_locator="data.db"):
        """Bind normalized business payload before initialization; replay its ID.

        The caller separates the idempotency key from its normalized payload.
        This store hashes that payload verbatim without dropping any fields.
        """
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise ValueError('timeout 必须为 1..1800 秒整数')
        source_locator = validate_source_locator(source_locator)
        path = self.run_path(run_id)
        key = _key(key)
        payload_json = _json(payload)
        payload_hash = _hash(payload_json)
        with self._transaction() as conn:
            existing = conn.execute('SELECT * FROM starts WHERE request_key=?', (key,)).fetchone()
            if existing is not None:
                if existing['payload_hash'] != payload_hash:
                    raise ValueError('启动请求幂等键 payload 冲突')
                config = conn.execute('SELECT source_locator FROM dispatches WHERE run_id=?', (existing['run_id'],)).fetchone()
                if config is None or not config['source_locator']:
                    raise ValueError('原运行来源数据库定位未配置；不能猜测默认来源')
                if config['source_locator'] != source_locator:
                    raise ValueError('启动请求来源数据库冲突')
                binding = self._binding(conn, existing)
            else:
                if conn.execute('SELECT 1 FROM runs WHERE path_key=?', (run_id.casefold(),)).fetchone():
                    raise ValueError('运行 ID 已绑定其他请求，冲突')
                if path.exists():
                    raise ValueError('未登记的运行目录或旧版本不能续跑')
                conn.execute('INSERT INTO starts VALUES (?,?,?,?,?)',
                             (key, payload_hash, payload_json, run_id, self.clock()))
                conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',
                             (run_id, run_id.casefold(), PIPELINE, CONTRACT_VERSION, 'preparing', 'preparing', 0))
                conn.execute('INSERT INTO leases(run_id) VALUES (?)', (run_id,))
                conn.execute('INSERT INTO dispatches VALUES (?,?,?,?)', (run_id, key, timeout, source_locator))
                binding = self._binding(conn, conn.execute('SELECT * FROM starts WHERE request_key=?', (key,)).fetchone())
        # A crash here leaves the original durable binding. Bootstrap repair is
        # allowed only until the run schema and initialized marker commit together.
        self._ensure_run(binding['run_id'])
        return binding

    def queue_resume(self, key, payload, *, source_locator=None):
        """Bind an explicit resume, preserving all original budget/configuration."""
        key = _key(key)
        run_id = payload['resume_run_id']
        original = self.get_run(run_id)['payload']
        chip = payload['chip'] if payload['chip'] is not None else original['chip']
        canonical = {**payload, 'chip': chip, 'scope_type': 'chip' if chip else 'discovery'}
        encoded = _json(canonical)
        with self._transaction(run_id) as conn:
            config = conn.execute('SELECT * FROM dispatches WHERE run_id=?', (run_id,)).fetchone()
            if config is None or not config['source_locator']:
                raise ValueError('原运行来源数据库定位未配置；不能猜测默认来源')
            if source_locator is not None and config['source_locator'] != validate_source_locator(source_locator):
                raise ValueError('续跑来源数据库与原运行冲突')
            existing = conn.execute('SELECT * FROM starts WHERE request_key=?', (key,)).fetchone()
            if existing is not None:
                if existing['payload_hash'] != _hash(encoded):
                    raise ValueError('启动请求幂等键 payload 冲突')
                return self._binding(conn, existing)
            if original['domain'] != payload['domain'] or (payload['chip'] is not None and payload['chip'] != original['chip']):
                raise ValueError('续跑数据域或最初芯片线索与原运行不一致')
            lease = conn.execute('SELECT * FROM leases WHERE run_id=?', (run_id,)).fetchone()
            if lease['owner'] is not None and lease['expires_at'] > self.clock():
                raise ValueError('运行已有有效租约，不能续跑')
            conn.execute('INSERT INTO starts VALUES (?,?,?,?,?)', (key, _hash(encoded), encoded, run_id, self.clock()))
            conn.execute('UPDATE dispatches SET request_key=? WHERE run_id=?', (key, run_id))
            conn.execute("UPDATE runs SET status='preparing',stage='preparing' WHERE run_id=?", (run_id,))
            return self._binding(conn, conn.execute('SELECT * FROM starts WHERE request_key=?', (key,)).fetchone())

    def dispatch_request(self, run_id):
        with self._connect() as conn:
            self._run(conn, run_id)
            row = conn.execute('SELECT * FROM dispatches WHERE run_id=?', (run_id,)).fetchone()
            if row is None or not row['source_locator']:
                raise ValueError('原运行来源数据库定位未配置；不能猜测默认来源')
            binding = conn.execute('SELECT * FROM starts WHERE request_key=?', (row['request_key'],)).fetchone()
            return {'request_id': row['request_key'], 'timeout': row['timeout_seconds'], 'source_locator': validate_source_locator(row['source_locator']), 'payload': json.loads(binding['payload_json'])}

    def dispatchable_runs(self):
        """Only queued or crashed workers; terminal runs need explicit resume."""
        with self._connect() as conn:
            return [row['run_id'] for row in conn.execute(
                "SELECT r.run_id FROM runs r JOIN leases l ON l.run_id=r.run_id "
                "WHERE r.pipeline=? AND r.contract_version=3 AND r.status IN ('preparing','running') "
                "AND (l.owner IS NULL OR l.expires_at<=?) ORDER BY r.run_id", (PIPELINE, self.clock()))]

    def _ensure_run(self, run_id):
        path = self.run_path(run_id)
        path.mkdir(exist_ok=True)
        with self._transaction(run_id, bootstrap=True) as conn:
            if self._run(conn, run_id)['initialized']:
                return
            for ddl in _RUN_DDL:
                conn.execute(ddl)
            conn.execute('INSERT OR IGNORE INTO run.metadata VALUES (?,?)',
                         ('identity', _json(self._identity(run_id))))
            conn.execute('UPDATE runs SET initialized=1 WHERE run_id=?', (run_id,))

    @staticmethod
    def _identity(run_id):
        return {'run_id': run_id, 'pipeline': PIPELINE, 'contract_version': CONTRACT_VERSION}

    def get_run(self, run_id):
        self.run_path(run_id)
        with self._connect() as conn:
            self._run(conn, run_id)
            row = conn.execute('SELECT * FROM starts WHERE run_id=? ORDER BY created_at,rowid LIMIT 1', (run_id,)).fetchone()
            return self._binding(conn, row)

    def claim(self, run_id, owner, lease_seconds, *, dispatch=False):
        self.run_path(run_id)
        owner, lease_seconds = _key(owner), _seconds(lease_seconds)
        with self._transaction() as conn:
            run = self._run(conn, run_id)
            if dispatch and run['status'] not in {'preparing', 'running'}:
                raise ValueError('运行不在可领取队列')
            row = conn.execute('SELECT * FROM leases WHERE run_id=?', (run_id,)).fetchone()
            now = self.clock()
            if row['owner'] is not None and row['expires_at'] > now:
                raise ValueError('运行已有有效租约')
            generation = row['generation'] + 1
            conn.execute('UPDATE leases SET owner=?, generation=?, expires_at=? WHERE run_id=?',
                         (owner, generation, now + lease_seconds, run_id))
            if dispatch:
                conn.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        return generation

    def renew(self, run_id, generation, lease_seconds):
        self.run_path(run_id)
        lease_seconds = _seconds(lease_seconds)
        with self._transaction() as conn:
            self._fence(conn, run_id, generation)
            conn.execute('UPDATE leases SET expires_at=? WHERE run_id=?', (self.clock() + lease_seconds, run_id))

    def release(self, run_id, generation):
        self.run_path(run_id)
        with self._transaction() as conn:
            self._fence(conn, run_id, generation)
            conn.execute('UPDATE leases SET owner=NULL, expires_at=0 WHERE run_id=?', (run_id,))

    def finish_run(self, run_id, generation, result):
        """Commit terminal snapshot and revoke callback authorization atomically.

        A late callback cannot slip a write between recording the terminal
        result and revoking its worker. Expired or superseded workers cannot
        publish a terminal status belonging to a newer generation.
        """
        if not isinstance(result, dict) or result.get('status') not in {
                'success', 'no_findings', 'partial', 'failed', 'interrupted'}:
            raise ValueError('运行终态无效')
        _json(result)
        with self._transaction(run_id) as conn:
            lease = conn.execute('SELECT * FROM leases WHERE run_id=?', (run_id,)).fetchone()
            terminal = self._baseline_metadata(conn, 'domain_runtime_terminal.v1')
            if (type(generation) is int and lease is not None and lease['generation'] == generation
                    and lease['owner'] is None and terminal and terminal['generation'] == generation):
                if terminal['result_sha256'] != _hash(_json(result)):
                    raise ValueError('运行终态快照 payload 冲突')
                return self._baseline_metadata(conn, 'domain_runtime_result.v1')
            self._fence(conn, run_id, generation)
            self._write_metadata(conn, run_id, 'domain_runtime_result.v1', result,
                {'kind': 'run_terminal', 'stage': 'completed', 'status': result['status'], 'generation': generation})
            self._write_metadata(conn, run_id, 'domain_runtime_terminal.v1',
                                 {'generation': generation, 'result_sha256': _hash(_json(result))}, None)
            self._fence(conn, run_id, generation)
            conn.execute('UPDATE runs SET status=?,stage=? WHERE run_id=?', (result['status'], 'completed', run_id))
            conn.execute('UPDATE leases SET owner=NULL,expires_at=0 WHERE run_id=?', (run_id,))
            return result

    @staticmethod
    def _receipt(row):
        item = dict(row)
        for name in ('payload', 'result'):
            raw = item.pop(name + '_json', None)
            item[name] = json.loads(raw) if raw is not None else None
        return item

    def operation(self, run_id, generation, request_id, payload, execute, *,
                  confirmed_failure=False, predecessor=None, prepare_metadata=None, pending_result=None):
        """Execute outside the transaction; replay never executes running work.

        Opt-in confirmed_failure records an Exception only after the callback
        has exited. BaseException/crash still leaves unknown running work. A
        predecessor gates a distinct retry attempt on confirmed failure; it
        never resets a running or completed receipt. External outcomes can
        remain unknown even when the callback has finished (e.g. a timeout).
        prepare_metadata=(key, pure_callback) atomically reserves the physical
        execution budget with the running receipt. It is skipped on cached or
        unknown-running replay; callback(value, prior_receipt) -> (value,event).
        An optional third item is a denial message: metadata/event commit under
        the lease fence, then ValueError is raised without claiming a receipt
        or executing the callback. This preserves deadline accounting on denial.
        pending_result is an optional pure result classifier for orchestration
        wrappers, not physical operations: delegated in-progress work stays
        pending so the same immutable request can resume. Delegated physical
        receipts continue to require explicit unknown-operation recovery.
        """
        request_id, payload_json = _key(request_id), _json(payload)
        if predecessor is not None:
            predecessor = _key(predecessor)
        payload_hash = _hash(payload_json)
        denial = None
        with self._transaction(run_id, generation) as conn:
            row = conn.execute('SELECT * FROM run.operations WHERE request_id=?', (request_id,)).fetchone()
            if row is not None:
                if row['payload_hash'] != payload_hash:
                    raise ValueError('工具请求 payload 冲突')
                if row['state'] != 'pending':
                    return self._receipt(row)
            else:
                if predecessor is not None:
                    previous = conn.execute('SELECT state FROM run.operations WHERE request_id=?',
                                            (predecessor,)).fetchone()
                    if previous is None or previous['state'] != 'confirmed_failed':
                        raise ValueError('重试需要前次已确认结束的失败；running/completed 不能重试')
            if prepare_metadata is not None:
                denial = self._prepare_operation(conn, run_id, prepare_metadata,
                                                  self._receipt(row) if row is not None else None)
            if denial is None and row is not None:
                conn.execute("UPDATE run.operations SET state='running', generation=?, attempts=attempts+1 WHERE request_id=?",
                             (generation, request_id))
            elif denial is None:
                stage = payload.get('stage') if isinstance(payload, dict) else None
                if not isinstance(stage, str):
                    stage = None
                conn.execute('INSERT INTO run.operations VALUES (?,?,?,?,?,?,?,?)',
                             (request_id, payload_hash, payload_json, 'running', None, stage, 1, generation))
        if denial is not None:
            raise ValueError(denial)
        # Exceptions intentionally leave the running receipt for explicit recovery.
        try:
            result = execute()
            result_json = _json(result)
            state = 'pending' if pending_result is not None and pending_result(result) else 'completed'
        except Exception as exc:
            if not confirmed_failure:
                raise
            # Raw exception strings may contain credentials or full requests.
            result_json = _json({'error_type': type(exc).__name__, 'outcome_unknown': True})
            state = 'confirmed_failed'
        with self._transaction(run_id, generation) as conn:
            conn.execute("UPDATE run.operations SET state=?, result_json=? WHERE request_id=? AND generation=? AND state='running'",
                         (state, result_json, request_id, generation))
            return self._receipt(conn.execute('SELECT * FROM run.operations WHERE request_id=?', (request_id,)).fetchone())

    def list_operations(self, run_id):
        return self._list(run_id, 'operations')

    def recover_operations(self, run_id, generation):
        """Make only abandoned older-generation receipts eligible for retry."""
        return self._recover(run_id, generation, 'operations')

    def enqueue(self, run_id, generation, task_key, payload):
        task_key, payload_json = _key(task_key), _json(payload)
        payload_hash = _hash(payload_json)
        with self._transaction(run_id, generation) as conn:
            row = conn.execute('SELECT * FROM run.tasks WHERE task_key=?', (task_key,)).fetchone()
            if row is not None:
                if row['payload_hash'] != payload_hash:
                    raise ValueError('任务 payload 冲突')
                return row['task_id']
            task_id = uuid.uuid4().hex
            conn.execute('INSERT INTO run.tasks VALUES (?,?,?,?,?,?,?,?)',
                         (task_key, task_id, payload_hash, payload_json, 'pending', generation, None, 0))
            return task_id

    def claim_task(self, run_id, generation, task_id):
        task_id = _key(task_id)
        with self._transaction(run_id, generation) as conn:
            row = conn.execute('SELECT * FROM run.tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError('未知任务')
            if row['state'] != 'pending':
                raise ValueError('任务不是 pending，不能重复领取')
            conn.execute("UPDATE run.tasks SET state='running', generation=?, attempts=attempts+1 WHERE task_id=?",
                         (generation, task_id))
            return self._receipt(conn.execute('SELECT * FROM run.tasks WHERE task_id=?', (task_id,)).fetchone())

    def complete_task(self, run_id, generation, task_id, result):
        task_id, result_json = _key(task_id), _json(result)
        with self._transaction(run_id, generation) as conn:
            row = conn.execute('SELECT * FROM run.tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None or row['generation'] != generation:
                raise ValueError('任务租约 generation 不匹配')
            if row['state'] == 'completed' and row['result_json'] == result_json:
                return self._receipt(row)
            if row['state'] != 'running':
                raise ValueError('任务状态或结果冲突')
            conn.execute("UPDATE run.tasks SET state='completed', result_json=? WHERE task_id=?", (result_json, task_id))
            return self._receipt(conn.execute('SELECT * FROM run.tasks WHERE task_id=?', (task_id,)).fetchone())

    def list_tasks(self, run_id):
        return self._list(run_id, 'tasks')

    def recover_tasks(self, run_id, generation):
        return self._recover(run_id, generation, 'tasks')

    def _recover(self, run_id, generation, table):
        # Table names are internal literals, never user input.
        with self._transaction(run_id, generation) as conn:
            cursor = conn.execute(f"UPDATE run.{table} SET state='pending' WHERE state='running' AND generation<?", (generation,))
            return cursor.rowcount

    def _list(self, run_id, table):
        self.get_run(run_id)
        with self._connect(run_id) as conn:
            return [self._receipt(row) for row in conn.execute(f'SELECT * FROM run.{table} ORDER BY rowid')]

    def checkpoint(self, run_id, generation, stage, result):
        stage, payload_json = _key(stage), _json(result)
        with self._transaction(run_id, generation) as conn:
            payload = json.loads(conn.execute('SELECT payload_json FROM starts WHERE run_id=? LIMIT 1', (run_id,)).fetchone()[0])
            domain_id = payload.get('domain') if isinstance(payload, dict) else None
            event_id = uuid.uuid4().hex
            conn.execute('INSERT INTO run.events(event_id,stage,domain_id,created_at,payload_json) VALUES (?,?,?,?,?)',
                         (event_id, stage, domain_id, self.clock(), payload_json))
            status = result.get('status', stage) if isinstance(result, dict) else stage
            if not isinstance(status, str):
                raise ValueError('运行状态必须为字符串')
            conn.execute('UPDATE runs SET status=?, stage=? WHERE run_id=?', (status, stage, run_id))
            return event_id

    def events(self, run_id):
        self.get_run(run_id)
        with self._connect(run_id) as conn:
            return [self._receipt(row) for row in conn.execute('SELECT * FROM run.events ORDER BY sequence')]

    def export_events(self, run_id):
        """Rebuild optional audit JSONL; interrupted exports never own state."""
        records = self.events(run_id)
        path = self._contained(self.run_path(run_id) / 'events.jsonl')
        staging = self._contained(path.with_name('.events-' + uuid.uuid4().hex + '.tmp'))
        try:
            with staging.open('x', encoding='utf-8', newline='\n') as handle:
                for record in records:
                    handle.write(_json(record) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
        return path

    def export_runtime(self, run_id):
        """Rebuild optional exports from one authoritative SQLite read snapshot.

        Work records retain the original protocols; projection names only
        translate their states for inspection, never authorize retries.
        Business rejection/unresolved evidence is completed work, while actual
        transport/parser failures and interrupted callbacks remain visible.
        """
        with self._connect(run_id) as conn:
            conn.execute('BEGIN')
            run = dict(self._run(conn, run_id))
            metadata = {r['key']: json.loads(r['value_json']) for r in conn.execute('SELECT * FROM run.metadata')}
            operations = [self._receipt(r) for r in conn.execute('SELECT * FROM run.operations ORDER BY rowid')]
            tasks = [self._receipt(r) for r in conn.execute('SELECT * FROM run.tasks ORDER BY rowid')]
        models = metadata.get('domain_models.v1', {})
        terminal = run['status'] in {'partial', 'interrupted', 'failed'}
        def state(raw):
            if raw in {'confirmed_failed', 'transport_failed', 'parser_rejected', 'extract_failed', 'review_failed'}:
                return 'failed'
            if raw == 'budget_stopped':
                return 'interrupted'
            if raw in {'completed', 'validated', 'semantic_rejected', 'program_rejected', 'unresolved'}:
                return 'completed'
            if raw in {'pending', 'running'}:
                return 'interrupted' if terminal else 'queued' if raw == 'pending' else 'running'
            return 'queued'
        projected = []
        for kind, rows, identifier in [('task', tasks, 'task_id'), ('operation', operations, 'request_id'),
                                      ('model_call', models.get('calls', []), 'execution_id')]:
            for row in rows:
                projected.append({'kind': kind, 'id': row[identifier], 'state': state(row['state'] if kind != 'model_call' else row['status']),
                    'record_state': row.get('state', row.get('status')), 'logical_id': row.get('logical_id'),
                    'phase': row.get('phase', row.get('stage')), 'generation': row.get('generation'),
                    'attempts': row.get('attempts', row.get('attempt')), 'record': row})
        for kind, rows in [('chunk', models.get('chunks', {})), ('candidate', models.get('candidates', {}))]:
            for identifier, row in rows.items():
                projected.append({'kind': kind, 'id': identifier, 'state': state(row['status']),
                                  'record_state': row['status'], 'record': row})
        budget = metadata.get('domain_budget.v1', {})
        snapshot = {'run_id': run_id, 'pipeline': PIPELINE, 'contract_version': CONTRACT_VERSION,
            'status': run['status'], 'current_stage': run['stage'], 'stages': budget.get('stages', {}),
            'metadata': metadata, 'work_items': projected,
            'state_mapping': {'pending': 'queued; interrupted in incomplete terminal run',
                'running': 'running; interrupted in incomplete terminal run',
                'transport/parser/extract/review failure': 'failed', 'budget_stopped': 'interrupted',
                'business rejected/unresolved': 'completed; preserve original evidence outcome'},
            'protocol_basis': 'original ModelService chunk_plans/chunks/candidates/calls and fenced operation receipts'}
        manifest = {**(metadata.get('domain_runtime_result.v1') or {}), 'run_id': run_id,
                    'status': run['status'], 'stages': snapshot['stages'], 'work_records_path': 'work.json'}
        for name, value in [('work.json', snapshot), ('manifest.json', manifest)]:
            path = self._contained(self.run_path(run_id) / name)
            staging = self._contained(path.with_name('.export-' + uuid.uuid4().hex + '.tmp'))
            try:
                with staging.open('x', encoding='utf-8', newline='\n') as handle:
                    handle.write(_json(value) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staging, path)
            finally:
                staging.unlink(missing_ok=True)
        return self.run_path(run_id) / 'manifest.json'

    def _baseline_metadata(self, conn, key='baseline'):
        row = conn.execute('SELECT value_json FROM run.metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def metadata(self, run_id, key):
        """Return a detached JSON metadata value; initialized identity is checked."""
        key = _key(key)
        with self._connect(run_id) as conn:
            return self._baseline_metadata(conn, key)

    @staticmethod
    def _mutable_metadata_key(key):
        key = _key(key)
        if key in {'identity', 'baseline', 'baseline_pending'}:
            raise ValueError('保留的运行元数据键不能修改')
        return key

    def _write_metadata(self, conn, run_id, key, value, event):
        value_json = _json(value)
        conn.execute('INSERT INTO run.metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                     (key, value_json))
        if event is not None:
            event_json = _json(event)
            stage = _key(event.get('stage', 'budget'))
            payload = json.loads(conn.execute('SELECT payload_json FROM starts WHERE run_id=? LIMIT 1', (run_id,)).fetchone()[0])
            domain_id = payload.get('domain') if isinstance(payload, dict) else None
            domain_id = event.get('domain_id', domain_id)
            conn.execute('INSERT INTO run.events(event_id,stage,domain_id,created_at,payload_json) VALUES (?,?,?,?,?)',
                         (uuid.uuid4().hex, stage, domain_id, self.clock(), event_json))

    def _prepare_operation(self, conn, run_id, prepare_metadata, receipt):
        key, prepare = prepare_metadata
        key = self._mutable_metadata_key(key)
        prepared = prepare(self._baseline_metadata(conn, key), receipt)
        if len(prepared) == 2:
            value, event = prepared
            denial = None
        else:
            value, event, denial = prepared
            if denial is not None:
                denial = _key(denial)
        self._write_metadata(conn, run_id, key, value, event)
        return denial

    def update_metadata(self, run_id, generation, key, update, *, failed_operation=None):
        """Atomically apply pure local math and optionally append an audit event.

        update(detached_value) -> (new_value, result, event_or_None). No IO or
        network belongs in this short callback. Metadata and its event share
        the same fenced control/run transaction. Reserved baseline/identity
        keys cannot be changed through this public extension seam.
        """
        key = self._mutable_metadata_key(key)
        if failed_operation is not None:
            failed_operation = _key(failed_operation)
        with self._transaction(run_id, generation) as conn:
            if key == 'domain_search.v1':
                selection = self._baseline_metadata(conn, 'domain_selection.v1')
                if selection and selection.get('finalized'):
                    raise ValueError('selection finalized; search collection is locked')
            if failed_operation is not None:
                receipt = conn.execute('SELECT state FROM run.operations WHERE request_id=?',
                                       (failed_operation,)).fetchone()
                if receipt is None or receipt['state'] != 'confirmed_failed':
                    raise ValueError('重试需要前次 confirmed_failed；不能重放 running/completed')
            value, result, event = update(self._baseline_metadata(conn, key))
            _json(result)
            self._write_metadata(conn, run_id, key, value, event)
            return result

    def commit_tool_request(self, run_id, generation, request_id, payload, keys, prepare):
        """Commit pure metadata validation, jobs and the final receipt together.

        prepare(detached_values) -> (updates, [(task_key,payload)], result,event).
        No filesystem, inference or network belongs inside this transaction.
        Result receives task_ids only for an enqueuing/finalization response.
        """
        request_id, payload_json = _key(request_id), _json(payload)
        digest = _hash(payload_json)
        keys = tuple(self._mutable_metadata_key(key) for key in keys)
        with self._transaction(run_id, generation) as conn:
            prior = conn.execute('SELECT * FROM run.operations WHERE request_id=?', (request_id,)).fetchone()
            if prior is not None:
                if prior['payload_hash'] != digest:
                    raise ValueError('tool request payload conflict')
                if prior['state'] != 'completed':
                    raise ValueError('tool request still running; explicit recovery required')
                return self._receipt(prior)['result']
            values = {key: self._baseline_metadata(conn, key) for key in keys}
            updates, tasks, result, event = prepare(values)
            if not set(updates).issubset(keys):
                raise ValueError('tool callback changed an undeclared metadata key')
            task_ids = []
            for task_key, task_payload in tasks:
                task_key, task_json = _key(task_key), _json(task_payload)
                task_hash = _hash(task_json)
                row = conn.execute('SELECT * FROM run.tasks WHERE task_key=?', (task_key,)).fetchone()
                if row is not None:
                    if row['payload_hash'] != task_hash:
                        raise ValueError('任务 payload 冲突')
                    task_id = row['task_id']
                else:
                    # 48-bit public IDs keep the worst-case 600-job receipt
                    # below 12000 characters. UNIQUE detects collisions closed.
                    task_id = uuid.uuid5(uuid.NAMESPACE_URL, run_id + ':' + task_key).hex[:12]
                    conn.execute('INSERT INTO run.tasks VALUES (?,?,?,?,?,?,?,?)',
                                 (task_key, task_id, task_hash, task_json, 'pending', generation, None, 0))
                if task_id not in task_ids:
                    task_ids.append(task_id)
            if 'task_ids' in result:
                result = {**result, 'task_ids': task_ids}
            result_json = _json(result)
            for key, value in updates.items():
                self._write_metadata(conn, run_id, key, value, event)
                event = None
            conn.execute('INSERT INTO run.operations VALUES (?,?,?,?,?,?,?,?)',
                         (request_id, digest, payload_json, 'completed', result_json,
                          result.get('current_stage', 'selecting'), 1, generation))
            if result.get('status') == 'received':
                conn.execute('UPDATE runs SET status=?, stage=? WHERE run_id=?',
                             ('in_progress', result['current_stage'], run_id))
            return result

    def validate_context(self, run_id, generation, owner):
        """Read-only auth fence for a runtime-provided context; never claims it."""
        with self._connect(run_id) as conn:
            self._fence(conn, run_id, generation)
            actual = conn.execute('SELECT owner FROM leases WHERE run_id=?', (run_id,)).fetchone()[0]
            if actual != owner:
                raise ValueError('runtime owner mismatch')

    def _verify_baseline(self, metadata, baseline, rule_bundle_hash, manifest):
        if metadata['pipeline'] != PIPELINE or metadata['contract_version'] != CONTRACT_VERSION:
            raise ValueError('基线运行版本不兼容')
        if metadata['rule_bundle_hash'] != rule_bundle_hash:
            raise ValueError('冻结规则哈希不一致，必须恢复原规则包')
        if manifest is not None and metadata['manifest'] != manifest:
            raise ValueError('原始 manifest 冲突')
        if not baseline.is_file() or _file_hash(baseline) != metadata['baseline_sha256']:
            raise ValueError('冻结基线缺失或哈希不一致')
        return metadata

    def ensure_baseline(self, run_id, generation, source, *, rule_bundle_hash, manifest=None):
        """Capture once; subsequent calls verify the original bytes and metadata.

        Persist the snapshot hash and staging filename before moving it. Recovery
        promotes only those original bytes, even if the source has since changed.
        Runtime freezes the Skill bundle once and supplies its digest.
        """
        rule_bundle_hash = _key(rule_bundle_hash)
        if manifest is not None:
            manifest = json.loads(_json(manifest))
        baseline = self._contained(self.run_path(run_id) / 'data.db')
        with self._transaction(run_id, generation) as conn:
            metadata = self._baseline_metadata(conn)
            pending = self._baseline_metadata(conn, 'baseline_pending')
        if metadata is not None:
            return self._verify_baseline(metadata, baseline, rule_bundle_hash, manifest)
        if pending is not None:
            return self._finish_baseline(run_id, generation, pending, rule_bundle_hash, manifest)
        if baseline.exists():
            raise ValueError('发现无原始标记的基线；需要显式修复，不能重新备份')
        staging = self._contained(baseline.with_name('.baseline-' + uuid.uuid4().hex + '.tmp'))
        retained = False
        try:
            consistent_backup(source, staging)
            metadata = {'pipeline': PIPELINE, 'contract_version': CONTRACT_VERSION,
                        'baseline_sha256': _file_hash(staging), 'rule_bundle_hash': rule_bundle_hash,
                        'manifest': manifest, 'created_at': self.clock()}
            with self._transaction(run_id, generation) as conn:
                existing = self._baseline_metadata(conn)
                pending = self._baseline_metadata(conn, 'baseline_pending')
                if existing is None and pending is None:
                    if baseline.exists():
                        raise ValueError('基线已存在且无原始标记')
                    pending = {'metadata': metadata, 'staging_name': staging.name}
                    conn.execute('INSERT INTO run.metadata VALUES (?,?)', ('baseline_pending', _json(pending)))
                    # Retain even if committing fails: an unreferenced temporary
                    # file is harmless; deleting a committed snapshot is not.
                    retained = True
            if existing is not None:
                return self._verify_baseline(existing, baseline, rule_bundle_hash, manifest)
            return self._finish_baseline(run_id, generation, pending, rule_bundle_hash, manifest)
        finally:
            if not retained:
                staging.unlink(missing_ok=True)

    def _finish_baseline(self, run_id, generation, pending, rule_bundle_hash, manifest):
        baseline = self._contained(self.run_path(run_id) / 'data.db')
        name = pending['staging_name']
        if Path(name).name != name or not name.startswith('.baseline-') or not name.endswith('.tmp'):
            raise ValueError('基线暂存路径无效')
        staging = self._contained(baseline.with_name(name))
        metadata = pending['metadata']
        # Potentially large file reads happen outside the control write lock.
        self._verify_baseline(metadata, baseline if baseline.exists() else staging, rule_bundle_hash, manifest)
        with self._transaction(run_id, generation) as conn:
            existing = self._baseline_metadata(conn)
            if existing is not None:
                if existing != metadata:
                    raise ValueError('原始基线元数据冲突')
                return existing
            if self._baseline_metadata(conn, 'baseline_pending') != pending:
                raise ValueError('原始基线恢复标记冲突')
            if not baseline.exists():
                os.replace(staging, baseline)
            baseline.chmod(0o444)
            conn.execute('INSERT INTO run.metadata VALUES (?,?)', ('baseline', _json(metadata)))
            conn.execute("DELETE FROM run.metadata WHERE key='baseline_pending'")
        return metadata
