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
from pathlib import Path

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


class RunStore:
    """Connection-per-operation store; a clock returns persistent epoch seconds."""

    def __init__(self, root, clock=None):
        self.root = Path(root).resolve()
        self.clock = clock or time.time
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CONTROL_DDL)

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

    def start(self, key, payload, run_id):
        """Bind normalized business payload before initialization; replay its ID.

        The caller separates the idempotency key from its normalized payload.
        This store hashes that payload verbatim without dropping any fields.
        """
        path = self.run_path(run_id)
        key = _key(key)
        payload_json = _json(payload)
        payload_hash = _hash(payload_json)
        with self._transaction() as conn:
            existing = conn.execute('SELECT * FROM starts WHERE request_key=?', (key,)).fetchone()
            if existing is not None:
                if existing['payload_hash'] != payload_hash:
                    raise ValueError('启动请求幂等键 payload 冲突')
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
                binding = self._binding(conn, conn.execute('SELECT * FROM starts WHERE request_key=?', (key,)).fetchone())
        # A crash here leaves the original durable binding. Bootstrap repair is
        # allowed only until the run schema and initialized marker commit together.
        self._ensure_run(binding['run_id'])
        return binding

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
            row = conn.execute('SELECT * FROM starts WHERE run_id=? ORDER BY created_at LIMIT 1', (run_id,)).fetchone()
            return self._binding(conn, row)

    def claim(self, run_id, owner, lease_seconds):
        self.run_path(run_id)
        owner, lease_seconds = _key(owner), _seconds(lease_seconds)
        with self._transaction() as conn:
            self._run(conn, run_id)
            row = conn.execute('SELECT * FROM leases WHERE run_id=?', (run_id,)).fetchone()
            now = self.clock()
            if row['owner'] is not None and row['expires_at'] > now:
                raise ValueError('运行已有有效租约')
            generation = row['generation'] + 1
            conn.execute('UPDATE leases SET owner=?, generation=?, expires_at=? WHERE run_id=?',
                         (owner, generation, now + lease_seconds, run_id))
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

    @staticmethod
    def _receipt(row):
        item = dict(row)
        for name in ('payload', 'result'):
            raw = item.pop(name + '_json', None)
            item[name] = json.loads(raw) if raw is not None else None
        return item

    def operation(self, run_id, generation, request_id, payload, execute):
        """Persist running receipt, release transaction, execute, then fence result."""
        request_id, payload_json = _key(request_id), _json(payload)
        payload_hash = _hash(payload_json)
        with self._transaction(run_id, generation) as conn:
            row = conn.execute('SELECT * FROM run.operations WHERE request_id=?', (request_id,)).fetchone()
            if row is not None:
                if row['payload_hash'] != payload_hash:
                    raise ValueError('工具请求 payload 冲突')
                if row['state'] != 'pending':
                    return self._receipt(row)
                conn.execute("UPDATE run.operations SET state='running', generation=?, attempts=attempts+1 WHERE request_id=?",
                             (generation, request_id))
            else:
                stage = payload.get('stage') if isinstance(payload, dict) else None
                if not isinstance(stage, str):
                    stage = None
                conn.execute('INSERT INTO run.operations VALUES (?,?,?,?,?,?,?,?)',
                             (request_id, payload_hash, payload_json, 'running', None, stage, 1, generation))
        # Exceptions intentionally leave the running receipt for explicit recovery.
        result_json = _json(execute())
        with self._transaction(run_id, generation) as conn:
            conn.execute("UPDATE run.operations SET state='completed', result_json=? WHERE request_id=? AND generation=? AND state='running'",
                         (result_json, request_id, generation))
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

    def _baseline_metadata(self, conn, key='baseline'):
        row = conn.execute('SELECT value_json FROM run.metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

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
