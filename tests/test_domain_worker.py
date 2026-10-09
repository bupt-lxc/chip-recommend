import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.test_domain_runtime import deps
from tests.test_domain_tool_api import setup
from chip_model.pipeline import manual_control
from chip_model.pipeline.domain_discovery.store import RunStore

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def readable_worker_default(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'data.db') as db:
        db.execute('CREATE TABLE worker_fixture (id TEXT)')


def worker_module():
    path = ROOT / 'scripts/hermes_domain_worker.py'
    assert path.is_file(), 'host V3 worker is missing'
    spec = importlib.util.spec_from_file_location('domain_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_claims_persistent_request_once(tmp_path):
    start = getattr(manual_control, 'queue_domain_run', None)
    assert callable(start), 'shared V3 start binding is missing'
    receipt = start({'domain':'specs', 'request_id':'api'}, data_root=tmp_path)
    worker = worker_module()
    calls = []
    import threading
    entered, finish = threading.Event(), threading.Event()
    def execute(request, deps):
        calls.append((request, deps))
        entered.set()
        finish.wait(2)
        deps['store'].finish_run(receipt['run_id'], deps['preclaimed']['generation'], {'status':'no_findings'})
        return {'status':'no_findings'}
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(worker.work_once, tmp_path, executor=execute)
        assert entered.wait(2)
        two = pool.submit(worker.work_once, tmp_path, executor=execute)
        assert two.result() == []
        finish.set()
        assert one.result()[0]['status'] == 'no_findings'
    assert len(calls) == 1
    assert calls[0][1]['request_id'] == 'api'
    assert calls[0][0] == RunStore(tmp_path / 'domain_runs').get_run(receipt['run_id'])['payload']


def test_resume_inherits_clue_rejects_explicit_conflicts(tmp_path):
    start = getattr(manual_control, 'queue_domain_run', None)
    assert callable(start), 'shared V3 start binding is missing'
    first = start({'domain':'specs','chip':'Unknown X','request_id':'first'}, data_root=tmp_path, timeout=3)
    store = RunStore(tmp_path / 'domain_runs')
    generation = store.claim(first['run_id'], 'fixture', 5)
    store.finish_run(first['run_id'], generation, {'status':'interrupted'})
    for bad in ({'domain':'compute'}, {'chip':'Other'}):
        with pytest.raises(ValueError, match='续跑'):
            start({'domain':'specs','resume_run_id':first['run_id'],**bad}, data_root=tmp_path)
    resumed = start({'domain':'specs','resume_run_id':first['run_id'],'request_id':'resume'}, data_root=tmp_path)
    assert resumed['run_id'] == first['run_id']
    assert store.dispatch_request(first['run_id'])['timeout'] == 3


def test_old_dispatcher_does_not_touch_v3(tmp_path):
    import scripts.hermes_manual_run_dispatcher as old
    path = tmp_path / 'v3.json'
    raw = json.dumps({'pipeline':'hermes-open-web-v3','status':'queued','mode':'test'})
    path.write_text(raw, encoding='utf-8')
    assert old.dispatch(path, container='wrong', formal_job_id='', test_job_id='', hermes_python='missing')['status'] == 'queued'
    assert path.read_text(encoding='utf-8') == raw

def test_worker_runs_runtime_with_single_generation(deps):
    from chip_model.pipeline.manual_control import queue_domain_run
    root = deps['source_db'].parent
    receipt = queue_domain_run({'domain':'specs','request_id':'runtime-worker'}, data_root=root,source_db=deps['source_db'])
    worker = worker_module()
    result = worker.work_once(root, dependencies=deps)
    assert result[0]['status'] == 'no_findings', result
    store = RunStore(root / 'domain_runs')
    with store._connect() as conn:
        lease = dict(conn.execute('SELECT * FROM leases WHERE run_id=?',(receipt['run_id'],)).fetchone())
    assert lease['generation'] == 1 and lease['owner'] is None
    assert worker.work_once(root, dependencies=deps) == []



def test_cli_and_api_use_same_start_binding(tmp_path, capsys):
    from scripts import run_hermes_open_web_test as cli
    start = getattr(manual_control, 'queue_domain_run', None)
    assert callable(start)
    source = tmp_path / 'data.db'
    source.touch()
    receipt = start({'domain':'specs','chip':'New X','request_id':'shared'}, data_root=tmp_path)
    seen = []
    def execute(request, dependencies):
        seen.append((request, dependencies))
        result = {'status':'no_findings','run_id':dependencies['preclaimed']['run_id']}
        dependencies['store'].finish_run(result['run_id'], dependencies['preclaimed']['generation'], result)
        return result
    assert cli.main(['--domain','specs','--chip','New X','--request-id','shared','--db',str(source)], executor=execute) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['run_id'] == receipt['run_id']
    assert seen[0][0] == dict(domain='specs',chip='New X',scope_type='chip',resume_run_id=None)
    assert seen[0][1]['store'].root == (tmp_path / 'domain_runs').resolve()


def test_preflight_reports_missing_capabilities_without_creating_state(tmp_path, monkeypatch):
    worker = worker_module()
    monkeypatch.setenv('HERMES_CLI_COMMAND','missing-hermes-fixture')
    monkeypatch.setenv('HERMES_PYTHON',str(tmp_path / 'no-python'))
    monkeypatch.setenv('AISH_PERF_HERMES_SKILL_ROOT',str(tmp_path / 'no-skills'))
    report = worker.check_environment(tmp_path)
    assert report['status'] == 'failed'
    assert {'hermes_cli','hermes_python','rule_package','host_container_path_map'}.issubset(
        {c['item'] for c in report['checks'] if not c['ok']})
    assert not (tmp_path / 'domain_runs').exists()

def test_worker_default_missing_hermes_is_persisted_failed(deps, monkeypatch):
    monkeypatch.setenv('HERMES_CLI_COMMAND','missing-hermes-fixture')
    root = deps['source_db'].parent
    receipt = manual_control.queue_domain_run({'domain':'specs','request_id':'missing-hermes'},data_root=root,source_db=deps['source_db'])
    injected = {k:v for k,v in deps.items() if k != 'hermes_runner'}
    result = worker_module().work_once(root,dependencies=injected)[0]
    assert result['status'] == 'failed'
    assert 'CLI' in result['error']
    store = RunStore(root / 'domain_runs')
    assert store.get_run(receipt['run_id'])['status'] == 'failed'
    assert store.metadata(receipt['run_id'],'domain_runtime_result.v1')['error'] == result['error']


def test_preclaimed_wrong_owner_fails_without_release(deps):
    from chip_model.pipeline.domain_discovery import runtime
    store = deps['store']
    request = dict(domain='specs',chip=None,scope_type='discovery',resume_run_id=None)
    start = store.start('start-1',request,'preclaimed')
    generation = store.claim(start['run_id'],'real-owner',5)
    with pytest.raises(ValueError):
        runtime.run_domain(request,{**deps,'preclaimed':dict(run_id='preclaimed',generation=generation,owner='wrong')})
    store.validate_context('preclaimed',generation,'real-owner')


def test_worker_sigterm_marks_interrupted_and_fences_late_callbacks(deps):
    import threading
    root = deps['source_db'].parent
    receipt = manual_control.queue_domain_run({'domain':'specs','request_id':'stop'},data_root=root,source_db=deps['source_db'])
    stop = threading.Event()
    entered = threading.Event()
    class Blocking:
        def preflight(self,**kwargs):
            entered.set()
            stop.wait(3)
            import time
            time.sleep(.3)
            return deps['hermes_runner'].preflight(**kwargs)
        def cancel(self):
            pass
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(worker_module().work_once,root,dependencies={**deps,'hermes_runner':Blocking()},stop_event=stop)
        assert entered.wait(2)
        stop.set()
        out = result.result(3)[0]
    assert out['status'] == 'interrupted',out
    store = RunStore(root / 'domain_runs')
    assert store.get_run(receipt['run_id'])['status'] == 'interrupted'


def test_run_bridge_health_requires_token_live_lease_and_correct_binding(setup):
    client, store, context, *_ = setup
    url = '/v1/health/run-a'
    assert client.get(url).status_code == 401
    result = client.get(url,headers={'Authorization':'Bearer test-secret'})
    assert result.status_code == 200, result.text
    health = result.json()
    assert health['run_id'] == 'run-a'
    assert health['pipeline'] == 'hermes-open-web-v3' and health['contract_version'] == 3
    assert health['generation'] == context.generation
    assert health['run_path'] == str(store.run_path('run-a'))
    assert health['bundle_path'] == str(context.bundle_root.resolve())
    store.release('run-a',context.generation)
    assert client.get(url,headers={'Authorization':'Bearer test-secret'}).status_code == 409

def test_preflight_verifies_mount_actual_cli_sdk_and_package(deps, monkeypatch):
    import sys
    import subprocess
    root = deps['source_db'].parent
    import shutil
    shutil.copyfile(deps['source_db'], root / 'data.db')
    token = root / 'manual_control/admin_token'
    token.parent.mkdir()
    token.write_text('fixture',encoding='utf-8')
    monkeypatch.setenv('HERMES_CLI_COMMAND',sys.executable)
    monkeypatch.setenv('HERMES_PYTHON',sys.executable)
    monkeypatch.setenv('AISH_PERF_HERMES_SKILL_ROOT',str(deps['installed_skill_root']))
    monkeypatch.setenv('AISH_PERF_CONTAINER_DATA_ROOT','/app/data')
    probes = []
    def probe(command,**kwargs):
        probes.append(command)
        if command[:2] == ['docker','inspect']:
            out = json.dumps([{'Source':str(root),'Destination':'/app/data','RW':True}])
        elif command[-1] == '--help':
            out = '--skills --toolsets'
        else:
            out = 'fixture-capability'
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0,stdout=out,stderr='')
    monkeypatch.setattr(subprocess,'run',probe)
    report = worker_module().check_environment(root)
    assert report['status'] == 'preflight_required',report
    assert 'sqlite_atomic_write' in report['deferred_checks']
    assert any(c['item'] == 'hermes_sdk' and c['ok'] for c in report['checks'])
    assert report['runtime_preflight_required'] is True
    assert not (root / 'domain_runs').exists()

@pytest.mark.parametrize('key', ['generation','run_path','bundle_path','bundle_sha256','run_id','contract_version'])
def test_actual_runner_bridge_probe_rejects_mismatched_health(setup, monkeypatch, key):
    import io
    import urllib.request
    from chip_model.pipeline.domain_discovery.runtime import verify_loopback_bridge
    client, store, context, *_ = setup
    health = client.get('/v1/health/run-a',headers={'Authorization':'Bearer test-secret'}).json()
    health[key] = 'wrong'
    class Opener:
        def open(self,request,**kwargs):
            assert request.get_header('Authorization') == 'Bearer test-secret'
            return io.BytesIO(json.dumps(health).encode())
    monkeypatch.setattr(urllib.request,'build_opener',lambda *_: Opener())
    with pytest.raises(ValueError,match='binding mismatch'):
        verify_loopback_bridge('http://127.0.0.1:9000','test-secret',context,timeout=1)


def test_actual_runner_bridge_probe_accepts_fenced_http_health(setup, monkeypatch):
    import io
    import urllib.request
    from chip_model.pipeline.domain_discovery.runtime import verify_loopback_bridge
    client, store, context, *_ = setup
    class Opener:
        def open(self,request,**kwargs):
            response = client.get('/v1/health/run-a',headers={'Authorization':request.get_header('Authorization')})
            assert response.status_code == 200
            return io.BytesIO(response.content)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *_: Opener())
    assert verify_loopback_bridge('http://127.0.0.1:9000','test-secret',context,timeout=1)['bundle_sha256']


def test_worker_lost_lease_cannot_commit_old_error_or_revoke_new_worker(tmp_path):
    receipt = manual_control.queue_domain_run({'domain':'specs','request_id':'stale'},data_root=tmp_path)
    newer = []
    def lose_lease(request,deps):
        store = deps['store']
        old = deps['preclaimed']
        store.release(receipt['run_id'],old['generation'])
        generation = store.claim(receipt['run_id'],'newer',5)
        newer.append(generation)
        raise RuntimeError('old process failed')
    result = worker_module().work_once(tmp_path,executor=lose_lease)[0]
    assert result['terminal_persisted'] is False and result['status'] == 'interrupted'
    store = RunStore(tmp_path / 'domain_runs')
    store.validate_context(receipt['run_id'],newer[0],'newer')
    assert store.metadata(receipt['run_id'],'domain_runtime_result.v1') is None

def test_runtime_start_persists_timeout_before_budget_creation(deps, monkeypatch):
    from chip_model.pipeline.domain_discovery import runtime
    from chip_model.pipeline.domain_discovery import budget
    def fail_before_budget(*args,**kwargs):
        raise RuntimeError('fixture startup crash')
    fail_before_budget.KEY = budget.RunBudget.KEY
    monkeypatch.setattr(budget,'RunBudget',fail_before_budget)
    request = dict(domain='specs',chip=None,scope_type='discovery',resume_run_id=None)
    with pytest.raises(RuntimeError,match='startup crash'):
        runtime.run_domain(request,{**deps,'timeout':1})
    # Direct runtime's live lease is not dispatchable yet; inspect original binding.
    with deps['store']._connect() as conn:
        rid = conn.execute('SELECT run_id FROM starts WHERE request_key=?',('start-1',)).fetchone()[0]
    assert deps['store'].dispatch_request(rid)['timeout'] == 1

def test_cli_waiting_on_other_worker_uses_current_status_not_previous_result(tmp_path,capsys):
    from scripts import run_hermes_open_web_test as cli
    original = manual_control.queue_domain_run({'domain':'specs','request_id':'old'},data_root=tmp_path)
    store = RunStore(tmp_path / 'domain_runs')
    generation = store.claim(original['run_id'],'first-worker',5)
    store.finish_run(original['run_id'],generation,{'status':'partial','error':'previous stopped work'})
    manual_control.queue_domain_run({'domain':'specs','resume_run_id':original['run_id'],'request_id':'resume'},data_root=tmp_path)
    generation = store.claim(original['run_id'],'other-worker',5,dispatch=True)
    assert cli.main(['--domain','specs','--resume',original['run_id'],'--request-id','resume','--db',str(tmp_path / 'data.db')]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'running'
    assert 'previous stopped work' not in result.get('error','')
    store.validate_context(original['run_id'],generation,'other-worker')

@pytest.mark.parametrize('resume', [False, True])
def test_direct_retry_before_budget_reuses_original_timeout(deps, monkeypatch, resume):
    from chip_model.pipeline.domain_discovery import runtime, budget
    request = dict(domain='specs',chip=None,scope_type='discovery',resume_run_id=None)
    original = budget.RunBudget
    def fail(*args,**kwargs):
        raise RuntimeError('before budget record')
    fail.KEY = original.KEY
    monkeypatch.setattr(budget,'RunBudget',fail)
    with pytest.raises(RuntimeError,match='before budget'):
        runtime.run_domain(request,{**deps,'timeout':1})
    store = deps['store']
    with store._connect() as conn:
        lease = dict(conn.execute('SELECT * FROM leases').fetchone())
    store.release(lease['run_id'],lease['generation'])
    monkeypatch.setattr(budget,'RunBudget',original)
    monkeypatch.setattr(runtime,'_execute_domain',lambda *args: {'status':'no_findings'})
    retried = {**request,'resume_run_id':lease['run_id']} if resume else request
    runtime.run_domain(retried,{k:v for k,v in deps.items() if k != 'timeout'})
    assert store.metadata(lease['run_id'],original.KEY)['active_limit_ms'] == 1000
    assert store.dispatch_request(lease['run_id'])['timeout'] == 1


@pytest.mark.parametrize('payload', [
    {'domain':'specs','request_id':'new-domain'}, {'chip':'New X'}, {'resume_run_id':'old'},
    {'scope_type':'discovery'}, {'contract_version':3}])
def test_old_dispatcher_preserves_unmarked_domain_requests(tmp_path,monkeypatch,payload):
    from scripts import hermes_manual_run_dispatcher as old
    path = tmp_path / 'domain.json'
    raw = json.dumps({'pipeline':'open_web','mode':'test','status':'queued','limit':5,**payload})
    path.write_text(raw,encoding='utf-8')
    calls = []
    from types import SimpleNamespace
    monkeypatch.setattr(old.subprocess,'run',lambda *a,**k: calls.append(a) or SimpleNamespace(returncode=0,stdout='{}',stderr=''))
    old.dispatch(path,container='chip-recommend',formal_job_id='',test_job_id='',hermes_python='missing')
    assert path.read_text(encoding='utf-8') == raw
    assert calls == []


def _healthy_preflight(deps, monkeypatch):
    import sys, subprocess, shutil
    root = deps['source_db'].parent
    shutil.copyfile(deps['source_db'],root / 'data.db')
    token = root / 'manual_control/admin_token'
    token.parent.mkdir()
    token.write_text('fixture',encoding='utf-8')
    monkeypatch.setenv('HERMES_CLI_COMMAND',sys.executable)
    monkeypatch.setenv('HERMES_PYTHON',sys.executable)
    monkeypatch.setenv('AISH_PERF_HERMES_SKILL_ROOT',str(deps['installed_skill_root']))
    monkeypatch.setenv('AISH_PERF_CONTAINER_DATA_ROOT','/app/data')
    from types import SimpleNamespace
    def probe(command,**kwargs):
        output = json.dumps([{'Source':str(root),'Destination':'/app/data','RW':True}]) if command[:2] == ['docker','inspect'] else '--skills --toolsets'
        return SimpleNamespace(returncode=0,stdout=output,stderr='')
    monkeypatch.setattr(subprocess,'run',probe)
    return root


@pytest.mark.parametrize('broken', ['file','corrupt_control','missing_schema','permissions'])
def test_preflight_rejects_unusable_actual_run_root(deps,monkeypatch,broken):
    import os
    root = _healthy_preflight(deps,monkeypatch)
    runs = root / 'domain_runs'
    if broken == 'file':
        runs.write_text('ordinary file',encoding='utf-8')
    elif broken == 'corrupt_control':
        runs.mkdir()
        (runs / 'control.sqlite3').write_text('not sqlite',encoding='utf-8')
    elif broken == 'missing_schema':
        import sqlite3
        runs.mkdir()
        with sqlite3.connect(runs / 'control.sqlite3') as db:
            db.execute('CREATE TABLE wrong (id TEXT)')
    else:
        runs.mkdir()
        access = os.access
        monkeypatch.setattr(os,'access',lambda path,mode: False if Path(path).resolve() == runs else access(path,mode))
    report = worker_module().check_environment(root)
    assert report['status'] == 'failed', report
    assert any(not check['ok'] for check in report['checks'] if check['item'] in {'writable_run_directory','control_database'})

@pytest.mark.parametrize('status', ['queued','running'])
def test_old_main_loop_skips_unmarked_domain_before_any_state_change(tmp_path,monkeypatch,status):
    import sys
    from scripts import hermes_manual_run_dispatcher as old
    requests = tmp_path / 'manual_control/requests'
    requests.mkdir(parents=True)
    path = requests / 'domain.json'
    raw = json.dumps({'pipeline':'open_web','mode':'test','status':status,'limit':5,
        'domain':'specs','request_id':'new-domain','started_at':'2000-01-01T00:00:00+00:00'})
    path.write_text(raw,encoding='utf-8')
    monkeypatch.setattr(sys,'argv',['old','--data-root',str(tmp_path),'--formal-job-id','formal','--test-job-id','test','--once'])
    monkeypatch.setattr(old,'dispatch',lambda *a,**k: pytest.fail('new domain entered legacy dispatch'))
    assert old.main() == 0
    assert path.read_text(encoding='utf-8') == raw


@pytest.mark.parametrize('pipeline', ['known_sources','open_web'])
def test_request_id_alone_preserves_legacy_dispatch(tmp_path,monkeypatch,pipeline):
    from scripts import hermes_manual_run_dispatcher as old
    from types import SimpleNamespace
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps({'pipeline':pipeline,'mode':'test','status':'queued','limit':5,'request_id':'legacy-key'}),encoding='utf-8')
    calls=[]
    monkeypatch.setattr(old.subprocess,'run',lambda *a,**k: calls.append(a) or SimpleNamespace(returncode=0,stdout='{}',stderr=''))
    result = old.dispatch(path,container='chip-recommend',formal_job_id='',test_job_id='',hermes_python='missing',test_job_active=False)
    assert calls and result['status'] in {'completed','partial'}


def test_preflight_existing_control_is_readonly_and_defers_write_transactions(deps,monkeypatch):
    from chip_model.pipeline.domain_discovery.store import _file_hash
    root = _healthy_preflight(deps,monkeypatch)
    store = RunStore(root / 'domain_runs')
    control = store.root / 'control.sqlite3'
    before = _file_hash(control)
    files = {str(p.relative_to(root)) for p in root.rglob('*')}
    report = worker_module().check_environment(root)
    assert report['status'] == 'preflight_required'
    assert next(c for c in report['checks'] if c['item']=='control_database')['ok'] is True
    assert 'sqlite_atomic_write' in report['deferred_checks']
    assert _file_hash(control) == before
    assert {str(p.relative_to(root)) for p in root.rglob('*')} == files


def test_worker_before_first_budget_inherits_queued_timeout(deps,monkeypatch):
    from chip_model.pipeline.domain_discovery import runtime
    root = deps['source_db'].parent
    receipt = manual_control.queue_domain_run({'domain':'specs','request_id':'worker-timeout'},data_root=root,source_db=deps['source_db'],timeout=1)
    monkeypatch.setattr(runtime,'_execute_domain',lambda *a: {'status':'no_findings'})
    out = worker_module().work_once(root,dependencies={**deps,'timeout':1800})[0]
    assert out['status'] == 'no_findings'
    store = RunStore(root / 'domain_runs')
    assert store.metadata(receipt['run_id'],'domain_budget.v1')['active_limit_ms'] == 1000

def test_preflight_rejects_control_schema_without_required_keys(deps,monkeypatch):
    import sqlite3
    from chip_model.pipeline.domain_discovery.store import _CONTROL_DDL
    root = _healthy_preflight(deps,monkeypatch)
    runs = root / 'domain_runs'
    runs.mkdir()
    with sqlite3.connect(runs / 'control.sqlite3') as db:
        db.executescript(_CONTROL_DDL.replace(' PRIMARY KEY','').replace(' UNIQUE',''))
    report = worker_module().check_environment(root)
    assert report['status'] == 'failed'
    assert next(c for c in report['checks'] if c['item']=='control_database')['ok'] is False


def _marker_db(path,value):
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE marker (value TEXT)')
        db.execute('INSERT INTO marker VALUES (?)',(value,))
    return path


def test_cli_nonstandard_source_survives_independent_worker_race(tmp_path,capsys,monkeypatch):
    import sqlite3
    from scripts import run_hermes_open_web_test as cli, hermes_domain_worker as host
    default=_marker_db(tmp_path / 'data.db','default')
    chosen=_marker_db(tmp_path / 'chosen.db','chosen')
    real=host.work_once
    seen=[]
    def execute(request,deps):
        with sqlite3.connect(deps['source_db'].as_uri()+'?mode=ro',uri=True) as db:
            seen.append(db.execute('SELECT value FROM marker').fetchone()[0])
        out={'status':'no_findings'}
        deps['store'].finish_run(deps['preclaimed']['run_id'],deps['preclaimed']['generation'],out)
        return out
    def race(root,**kwargs):
        # Another host owns no CLI in-memory dependencies at all.
        real(root,executor=execute)
        return real(root,**kwargs)
    monkeypatch.setattr(host,'work_once',race)
    assert cli.main(['--domain','specs','--db',str(chosen),'--request-id','race-source'],executor=execute) == 0
    assert seen == ['chosen']
    assert json.loads(capsys.readouterr().out)['status'] == 'no_findings'


def test_internal_source_configuration_is_atomic_and_rejects_changes(tmp_path):
    _marker_db(tmp_path / 'chosen.db','chosen')
    _marker_db(tmp_path / 'other.db','other')
    receipt=manual_control.queue_domain_run({'domain':'specs','request_id':'bound'},data_root=tmp_path,source_db=tmp_path / 'chosen.db')
    store=RunStore(tmp_path / 'domain_runs')
    assert store.dispatch_request(receipt['run_id'])['source_locator'] == 'chosen.db'
    for payload in ({'domain':'specs','request_id':'bound'}, {'domain':'specs','request_id':'resume','resume_run_id':receipt['run_id']}):
        with pytest.raises(ValueError,match='来源'):
            manual_control.queue_domain_run(payload,data_root=tmp_path,source_db=tmp_path / 'other.db')
    assert store.dispatch_request(receipt['run_id'])['source_locator'] == 'chosen.db'


def test_source_outside_shared_root_rejected_before_queue(tmp_path):
    shared=tmp_path / 'shared'
    shared.mkdir()
    outside=_marker_db(tmp_path / 'outside.db','outside')
    with pytest.raises(ValueError,match='来源'):
        manual_control.queue_domain_run({'domain':'specs'},data_root=shared,source_db=outside)
    assert not (shared / 'domain_runs').exists()

@pytest.mark.parametrize('resume',[False,True])
def test_direct_runtime_source_binding_rejects_changed_dependencies(tmp_path,monkeypatch,resume):
    import sqlite3
    from chip_model.pipeline.domain_discovery import runtime
    chosen=_marker_db(tmp_path / 'chosen.db','chosen')
    other=_marker_db(tmp_path / 'other.db','other')
    store=RunStore(tmp_path / 'domain_runs')
    seen=[]
    def execute(*args):
        with sqlite3.connect(args[-1]['source_db'].as_uri()+'?mode=ro',uri=True) as db:
            seen.append(db.execute('SELECT value FROM marker').fetchone()[0])
        return {'status':'no_findings','run_id':args[1]}
    monkeypatch.setattr(runtime,'_execute_domain',execute)
    request={'domain':'specs','chip':None,'scope_type':'discovery','resume_run_id':None}
    dependencies={'source_db':chosen,'store':store,'request_id':'source-key'}
    original=runtime.run_domain(request,dependencies)
    events=store.events(original['run_id'])
    retry={**request,'resume_run_id':original['run_id']} if resume else request
    with pytest.raises(ValueError,match='来源'):
        runtime.run_domain(retry,{**dependencies,'source_db':other})
    assert store.events(original['run_id']) == events
    assert store.dispatch_request(original['run_id'])['source_locator']=='chosen.db'
    assert seen == ['chosen']


def test_locator_is_persisted_even_if_run_initialization_crashes(tmp_path,monkeypatch):
    _marker_db(tmp_path / 'chosen.db','chosen')
    ensure=RunStore._ensure_run
    monkeypatch.setattr(RunStore,'_ensure_run',lambda *a: (_ for _ in ()).throw(OSError('start crash')))
    with pytest.raises(OSError,match='start crash'):
        manual_control.queue_domain_run({'domain':'specs','request_id':'crash-source'},data_root=tmp_path,
            source_db=tmp_path / 'chosen.db',timeout=7)
    monkeypatch.setattr(RunStore,'_ensure_run',ensure)
    store=RunStore(tmp_path / 'domain_runs')
    with store._connect() as db:
        record=dict(db.execute('SELECT * FROM dispatches').fetchone())
    assert record['source_locator']=='chosen.db' and record['timeout_seconds']==7 and record['request_key']=='crash-source'


def test_resume_and_host_continue_using_original_locator(tmp_path,capsys,monkeypatch):
    import sqlite3
    from scripts import run_hermes_open_web_test as cli,hermes_domain_worker as host
    chosen=_marker_db(tmp_path / 'chosen.db','chosen')
    receipt=manual_control.queue_domain_run({'domain':'specs','request_id':'first-source'},data_root=tmp_path,source_db=chosen)
    store=RunStore(tmp_path / 'domain_runs')
    generation=store.claim(receipt['run_id'],'stopped',5)
    store.finish_run(receipt['run_id'],generation,{'status':'interrupted'})
    monkeypatch.setattr(cli,'get_db_path',lambda: tmp_path / 'data.db')
    seen=[]
    def execute(request,deps):
        with sqlite3.connect(deps['source_db'].as_uri()+'?mode=ro',uri=True) as db:
            seen.append(db.execute('SELECT value FROM marker').fetchone()[0])
        out={'status':'no_findings'}
        deps['store'].finish_run(deps['preclaimed']['run_id'],deps['preclaimed']['generation'],out)
        return out
    assert cli.main(['--domain','specs','--resume',receipt['run_id'],'--request-id','resume-source'],executor=execute)==0
    assert seen==['chosen']
    assert store.dispatch_request(receipt['run_id'])['source_locator']=='chosen.db'


def test_missing_legacy_locator_fails_closed_without_baseline_rewrite(tmp_path):
    import sqlite3
    chosen=_marker_db(tmp_path / 'chosen.db','chosen')
    store=RunStore(tmp_path / 'domain_runs')
    payload={'domain':'specs','chip':None,'scope_type':'discovery','resume_run_id':None}
    store.start('legacy-key',payload,'legacy-run',source_locator='chosen.db')
    before=store.get_run('legacy-run')
    with store._connect() as db:
        db.execute('ALTER TABLE dispatches DROP COLUMN source_locator')
    reopened=RunStore(store.root)
    with pytest.raises(ValueError,match='未配置'):
        reopened.dispatch_request('legacy-run')
    with pytest.raises(ValueError,match='未配置'):
        reopened.start('legacy-key',payload,'unused',source_locator='chosen.db')
    assert reopened.get_run('legacy-run')==before
    assert reopened.events('legacy-run')==[]

def test_source_sqlite_lock_is_inside_active_budget_and_persists_terminal(deps):
    import sqlite3,time
    root=deps['source_db'].parent
    receipt=manual_control.queue_domain_run({'domain':'specs','request_id':'locked-source'},data_root=root,
        source_db=deps['source_db'],timeout=1)
    blocker=sqlite3.connect(deps['source_db'],isolation_level=None)
    blocker.execute('BEGIN EXCLUSIVE')
    start=time.monotonic()
    try:
        out=worker_module().work_once(root,dependencies=deps)[0]
        elapsed=time.monotonic()-start
        assert elapsed < 3, (elapsed,out)
        assert out['status'] in {'partial','failed'} and out.get('terminal_persisted') is not False,out
        store=RunStore(root / 'domain_runs')
        state=store.metadata(receipt['run_id'],'domain_budget.v1')
        assert state is not None and state['active_limit_ms']==1000 and state['active_ms']>=1000
        assert store.get_run(receipt['run_id'])['status']==out['status']
        assert store.dispatchable_runs()==[]
    finally:
        blocker.rollback()
        blocker.close()
    resumed=manual_control.queue_domain_run({'domain':'specs','resume_run_id':receipt['run_id'],'request_id':'unlock-resume'},data_root=root)
    result=worker_module().work_once(root,dependencies=deps)[0]
    assert result['status']=='partial'
    assert RunStore(root / 'domain_runs').metadata(receipt['run_id'],'domain_budget.v1')['active_limit_ms']==1000


def test_shared_relative_locator_survives_different_container_host_paths(tmp_path):
    import shutil,sqlite3
    container=tmp_path / 'container-data'
    host=tmp_path / 'host-data'
    container.mkdir()
    chosen=_marker_db(container / 'chosen.db','mapped-source')
    receipt=manual_control.queue_domain_run({'domain':'specs','request_id':'mapped'},data_root=container,source_db=chosen)
    shutil.copytree(container,host)
    seen=[]
    def execute(request,deps):
        assert deps['source_db']==host / 'chosen.db'
        with sqlite3.connect(deps['source_db'].as_uri()+'?mode=ro',uri=True) as db:
            seen.append(db.execute('SELECT value FROM marker').fetchone()[0])
        out={'status':'no_findings'}
        deps['store'].finish_run(deps['preclaimed']['run_id'],deps['preclaimed']['generation'],out)
        return out
    assert worker_module().work_once(host,executor=execute)[0]['status']=='no_findings'
    assert seen==['mapped-source']
    assert RunStore(host / 'domain_runs').dispatch_request(receipt['run_id'])['source_locator']=='chosen.db'
