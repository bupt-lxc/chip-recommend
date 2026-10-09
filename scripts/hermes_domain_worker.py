#!/usr/bin/env python3
"""Host V3 worker for the shared durable run store; no cron jobs or publishers."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chip_model.pipeline.domain_discovery.store import RunStore


def work_once(data_root, *, executor=None, dependencies=None, run_id=None, stop_event=None):
    """Claim before execution; runtime owns renewal and fenced terminal commit."""
    data_root = Path(data_root).resolve()
    store = RunStore(data_root / 'domain_runs')
    results = []
    pending = [run_id] if run_id is not None else store.dispatchable_runs()
    for rid in pending:
        if stop_event is not None and stop_event.is_set():
            break
        owner = 'host-worker-' + uuid.uuid4().hex
        try:
            generation = store.claim(rid, owner, 5, dispatch=True)
        except ValueError:
            continue  # Competing worker or completed run, never touch its state.
        try:
            config = store.dispatch_request(rid)
            request = config.get('payload') or store.get_run(rid)['payload']
            deps = {'source_db': data_root / 'data.db', **(dependencies or {}), 'store': store,
                'request_id': config['request_id'], 'timeout': config['timeout'], 'stop_event': stop_event,
                'preclaimed': {'run_id': rid, 'owner': owner, 'generation': generation}}
            # A cancelled/used runner cannot be reused by another invocation.
            execute = executor
            if execute is None:
                from chip_model.pipeline.domain_discovery.runtime import run_domain, HermesRunner
                execute = run_domain
                if 'hermes_runner' not in deps:
                    deps['hermes_runner'] = HermesRunner()
            result = dict(execute(request, deps))
            result['run_id'] = rid
            result['request_id'] = config['request_id']
            results.append(result)
        except BaseException as exc:
            from chip_model.pipeline.domain_discovery.search import redact
            result = {'run_id': rid, 'status': 'interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed',
                'artifact_path': str(store.run_path(rid)), 'error': redact(str(exc))[:2000]}
            try:
                store.finish_run(rid, generation, result)
                store.export_runtime(rid)
            except ValueError:
                result.update(status='interrupted', terminal_persisted=False)
            results.append(result)
    return results


def check_environment(data_root):
    """Read-only host topology diagnosis; missing items are explicit failures."""
    import shlex
    import shutil
    import subprocess
    from chip_model.pipeline.domain_discovery.rules import _verify, INSTALL_MANIFEST
    checks = []
    def add(name, ok, detail):
        checks.append({'item': name, 'ok': bool(ok), 'detail': str(detail)})
    root = Path(data_root).resolve()
    add('shared_data', root.is_dir() and (root / 'data.db').is_file(), root)
    add('writable_run_directory', root.is_dir() and os.access(root, os.W_OK), root / 'domain_runs')
    token = root / 'manual_control/admin_token'
    try:
        token_ok = token.is_file() and bool(token.read_text(encoding='utf-8').strip())
    except (OSError, UnicodeError):
        token_ok = False
    add('admin_token', token_ok, 'shared manual_control/admin_token required')
    container = os.getenv('AISH_PERF_CONTAINER_DATA_ROOT', '')
    mount_ok = False
    if container and PurePosixPath(container).is_absolute():
        try:
            inspected = subprocess.run(['docker', 'inspect', '--format', '{{json .Mounts}}', os.getenv('AISH_PERF_API_CONTAINER', 'chip-recommend')],
                capture_output=True, text=True, timeout=10)
            mounts = json.loads(inspected.stdout)
            mount_ok = inspected.returncode == 0 and any(
                Path(m.get('Source', '')).resolve() == root and m.get('Destination') == container and m.get('RW') is True
                for m in mounts)
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            pass
    add('host_container_path_map', mount_ok, f'{root} -> {container or "missing AISH_PERF_CONTAINER_DATA_ROOT"}; verified docker inspect RW bind mount required')
    command = shlex.split(os.getenv('HERMES_CLI_COMMAND', 'hermes'), posix=os.name != 'nt')
    executable = shutil.which(command[0]) if command else None
    add('hermes_cli', bool(executable), executable or 'Hermes CLI missing')
    python = os.getenv('HERMES_PYTHON', '')
    add('hermes_python', bool(python) and Path(python).is_file(), python or 'HERMES_PYTHON missing')
    if executable:
        try:
            for flag in ('--version', '--help'):
                result = subprocess.run([*command, flag], capture_output=True, text=True, timeout=10)
                add('hermes' + flag, result.returncode == 0 and (flag != '--help' or all(f in result.stdout for f in ('--skills','--toolsets'))), 'actual CLI probe')
        except (OSError, subprocess.TimeoutExpired) as exc:
            add('hermes_cli_capabilities', False, exc)
    if python and Path(python).is_file():
        import tempfile
        # Probe imports/signatures with an empty home. Never inherit credential
        # stores/configuration or create a model/agent during deployment checks.
        with tempfile.TemporaryDirectory(prefix='aishperf-check-') as home:
            environment = {k: os.environ[k] for k in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP') if k in os.environ}
            environment.update(HERMES_HOME=home, HOME=home, USERPROFILE=home, PYTHONDONTWRITEBYTECODE='1')
            if os.getenv('HERMES_SDK_ROOT'):
                environment['PYTHONPATH'] = str(Path(os.environ['HERMES_SDK_ROOT']).resolve())
            code = ("import inspect; from run_agent import AIAgent; "
                    "s=inspect.signature(AIAgent); "
                    "required={'enabled_toolsets','skip_context_files','skip_memory','skip_background_review','run_budget_seconds','cwd'}; "
                    "assert required<=set(s.parameters), 'unsupported AIAgent SDK'; print('SDK capabilities available')")
            try:
                probe = subprocess.run([python, '-c', code], cwd=home, env=environment, capture_output=True,
                    text=True, timeout=10)
                add('hermes_sdk', probe.returncode == 0, 'actual installed SDK import/signature probe; resolver proof remains per-run')
            except (OSError, subprocess.TimeoutExpired) as exc:
                add('hermes_sdk', False, type(exc).__name__)
    skill = Path(os.getenv('AISH_PERF_HERMES_SKILL_ROOT', str(Path.home() / '.hermes/skills')))
    try:
        manifest = _verify(skill.resolve(), INSTALL_MANIFEST)
        add('rule_package', True, manifest.get('rule_version', '3.0.0'))
    except (OSError, ValueError) as exc:
        add('rule_package', False, exc)
    import socket
    try:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
        add('loopback_listener', True, 'host can bind an ephemeral IPv4 loopback listener')
    except OSError as exc:
        add('loopback_listener', False, type(exc).__name__)
    return {'status': 'ready' if all(c['ok'] for c in checks) else 'failed', 'checks': checks,
        'runtime_preflight_required': True,
        'runtime_checks': ['actual Hermes resolver/plugin/schema proof',
            'authenticated per-run HTTP health token/run/schema/generation/frozen-bundle/path binding']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=ROOT / 'data')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--poll-seconds', type=float, default=3)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if not 0 < args.poll_seconds <= 60:
        parser.error('--poll-seconds must be in (0, 60]')
    if args.check:
        result = check_environment(args.data_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'ready' else 2
    stop = threading.Event()
    def shutdown(*_):
        stop.set()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, shutdown)
    while not stop.is_set():
        result = work_once(args.data_root, stop_event=stop)
        if args.once:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if any(r.get('status') not in ('success','no_findings') for r in result) else 0
        stop.wait(args.poll_seconds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
