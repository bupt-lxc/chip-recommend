"""Probe source diagnostics, not live search accuracy."""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from chip_model.pipeline.domain_discovery.search import HtmlSearchProvider
try:
    from scripts import probe_open_web_search as probe
except ImportError:
    probe = None


class Session:
    def __init__(self, result):
        self.result, self.calls = result, []

    def get(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return SimpleNamespace(status_code=200, text=self.result)


def provider(name, result):
    return HtmlSearchProvider(SimpleNamespace(name=name, session=Session(result),
        timeout=20, proxies=None, _destination=lambda value: value))


def test_sources_run_independently_without_masking_failed_source():
    assert probe is not None, 'Task16 diagnostic probe has not been implemented'
    failed = provider('bing-html', requests.ConnectionError('http://user:secret@proxy.test:7897'))
    good = provider('so360-html', '<li class="res-list"><h3 class="res-title"><a href="https://vendor.example/chip">Chip</a></h3></li>')
    result = probe.run_probe((failed, good), proxy='http://user:secret@proxy.test:7897')
    assert result['status'] == 'failed'
    assert result['request_count'] == 6
    assert [r['outcome'] for r in result['diagnostics']] == ['network_error'] * 3 + ['success'] * 3
    assert result['fallback_attempted'] is False
    assert 'secret' not in json.dumps(result)
    for source in (failed, good):
        assert len(source.adapter.session.calls) == 3
        for (_, call), expected in zip(source.adapter.session.calls, probe.PROBES):
            assert call['params']['q'] == expected['query']
            assert call['timeout'] <= 15
    assert result['diagnostics'][0]['original_query'] == probe.PROBES[0]['query']
    assert result['diagnostics'][0]['actual_query'] == probe.PROBES[0]['query']
    assert result['diagnostics'][3]['valid_urls'] == ['https://vendor.example/chip']


@pytest.mark.parametrize('html,outcome', [
    ('<html>captcha</html>', 'parse_error'),
    ('<div id="b_results">no results found</div>', 'healthy_empty'),
    ('<html>unknown layout</html>', 'parse_error'),
])
def test_empty_and_parse_failure_remain_distinct(html, outcome):
    assert probe is not None, 'Task16 diagnostic probe has not been implemented'
    result = probe.run_probe((provider('bing-html', html),))
    assert {r['outcome'] for r in result['diagnostics']} == {outcome}
    assert result['status'] == ('healthy' if outcome == 'healthy_empty' else 'failed')


def test_main_selects_real_source_env_proxy_and_rejects_unsupported_arguments(tmp_path, monkeypatch, capsys):
    assert probe is not None, 'Task16 diagnostic probe has not been implemented'
    sources = (provider('bing-html', '<div id="b_results">no results</div>'),)
    proxies = []
    monkeypatch.setattr(probe, 'default_providers', lambda **kw: proxies.append(kw['proxy']) or sources)
    monkeypatch.setenv('DATA_AGENT_PROXY', 'http://user:password@proxy.test:7897')
    output = tmp_path / 'probe.json'
    assert probe.main(['--provider', 'bing-html', '--output', str(output)]) == 0
    assert proxies == ['http://user:password@proxy.test:7897']
    assert 'password' not in output.read_text(encoding='utf-8')
    assert 'password' not in capsys.readouterr().out
    for args in (['--provider', 'invented'], ['--workers', '2'], ['--field', 'vram_gb'], ['--output', str(tmp_path / 'data.db')]):
        with pytest.raises(SystemExit) as error:
            probe.main(args)
        assert error.value.code == 2


def make_db(path):
    with sqlite3.connect(path) as db:
        for table in probe.BUSINESS_TABLES:
            db.execute(f'CREATE TABLE "{table}" (id TEXT, value TEXT)')
            db.execute(f'INSERT INTO "{table}" VALUES (?, ?)', ('one', '原始'))


def test_business_digest_reads_consistent_wal_and_every_column_row_without_source_writes(tmp_path):
    assert probe is not None, 'Task16 diagnostic probe has not been implemented'
    source = tmp_path / 'source.db'
    make_db(source)
    with sqlite3.connect(source) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO models VALUES (?, ?)', ('two', 'WAL'))
        writer.commit()
        first = probe.business_digest(source)
        assert first['tables']['models']['row_count'] == 2
        assert first == probe.business_digest(source)
        writer.execute('UPDATE deployment_guides SET value=?', ('changed',))
        writer.commit()
        assert first['sha256'] != probe.business_digest(source)['sha256']
    assert not (tmp_path / 'domain_runs').exists()
    missing = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        probe.business_digest(missing)
    assert not missing.exists()


def test_output_cannot_overwrite_source_db(tmp_path):
    assert probe is not None, 'Task16 diagnostic probe has not been implemented'
    source = tmp_path / 'source.db'
    make_db(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(SystemExit):
        probe.main(['--db', str(source), '--output', str(source)])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_default_environment_proxy_route_is_recorded_without_mutating_session(monkeypatch):
    monkeypatch.delenv('https_proxy', raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://name:hidden@proxy.example:7897')
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    source = provider('bing-html', '<div id="b_results">no results</div>')
    source.adapter.session.trust_env = True
    result = probe.run_probe((source,))
    assert source.adapter.session.trust_env is True
    assert result['diagnostics'][0]['effective_proxies']['https'] == 'http://[REDACTED]@proxy.example:7897'
    assert result['diagnostics'][0]['explicit_proxy'] is None
    assert 'hidden' not in json.dumps(result)
    source.adapter.proxies = {'http': 'http://explicit:private@route.example:7897',
        'https': 'http://explicit:private@route.example:7897'}
    result = probe.run_probe((source,), proxy=source.adapter.proxies['https'])
    assert result['diagnostics'][0]['effective_proxies']['https'] == 'http://[REDACTED]@route.example:7897'
    assert 'private' not in json.dumps(result)


def test_digest_exclusive_lock_has_bounded_backup_deadline(tmp_path):
    source = tmp_path / 'locked.db'
    make_db(source)
    with sqlite3.connect(source) as writer:
        writer.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        with pytest.raises(TimeoutError, match='backup deadline'):
            probe.business_digest(source, backup_timeout=0.2)
        assert time.monotonic() - started < 1.5
        writer.rollback()
    assert probe.business_digest(source)['tables']['chips']['row_count'] == 1


@pytest.mark.parametrize('symlink_source', [False, True])
def test_output_hardlink_to_source_is_rejected_before_provider_without_database_changes(
    tmp_path, monkeypatch, symlink_source,
):
    source = tmp_path / 'source.db'
    make_db(source)
    output = tmp_path / 'output.json'
    os.link(source, output)
    source_argument = source
    if symlink_source:
        source_argument = tmp_path / 'source-alias.db'
        source_argument.symlink_to(source)
    before_bytes = source.read_bytes()
    before_digest = probe.business_digest(source)
    calls = []
    monkeypatch.setattr(probe, 'default_providers', lambda **kw: calls.append(kw) or (
        provider('bing-html', '<div id="b_results">no results</div>'),))
    with pytest.raises(SystemExit) as rejected:
        probe.main(['--db', str(source_argument), '--output', str(output)])
    assert rejected.value.code == 2
    assert calls == []
    assert source.read_bytes() == output.read_bytes() == before_bytes
    assert probe.business_digest(source) == before_digest


@pytest.mark.parametrize('setting', ['explicit', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'])
@pytest.mark.parametrize('prefix', ['//', ''])
def test_schemeless_proxy_credentials_are_redacted_without_changing_route(
    tmp_path, monkeypatch, capsys, setting, prefix,
):
    password = 'task16_synthetic_proxy_password'
    raw_proxy = prefix + 'testuser:' + password + '@proxy.example:7897'
    for name in ('DATA_AGENT_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    if setting != 'explicit':
        monkeypatch.setenv(setting, raw_proxy)
    observed = []
    source = provider('bing-html', '<div id="b_results">no results</div>')
    def factory(*, proxy):
        observed.append(proxy)
        source.adapter.proxies = {'http': proxy, 'https': proxy} if proxy else None
        return (source,)
    monkeypatch.setattr(probe, 'default_providers', factory)
    output = tmp_path / 'route.json'
    argv = ['--provider', 'bing-html', '--output', str(output)]
    if setting == 'explicit':
        argv += ['--proxy', raw_proxy]
    assert probe.main(argv) == 0
    assert observed == [raw_proxy if setting == 'explicit' else None]
    text = output.read_text(encoding='utf-8')
    assert password not in text + capsys.readouterr().out
    saved = json.loads(text)
    assert saved['request_count'] == 3
    assert saved['status'] == 'healthy'
    diagnostic = saved['diagnostics'][0]
    value = diagnostic['explicit_proxy'] if setting == 'explicit' else diagnostic['ambient_proxy_environment'][setting]
    assert '[REDACTED]' in value and 'proxy.example:7897' in value


def test_schemeless_proxy_in_request_error_is_redacted(tmp_path, monkeypatch, capsys):
    password = 'task16_synthetic_error_proxy_password'
    raw_proxy = '//testuser:' + password + '@proxy.example:7897'
    source = provider('bing-html', requests.ConnectionError('Unable to connect via ' + raw_proxy + '; ' + password))
    source.adapter.proxies = {'http': raw_proxy, 'https': raw_proxy}
    monkeypatch.setattr(probe, 'default_providers', lambda **kw: (source,))
    output = tmp_path / 'error.json'
    assert probe.main(['--proxy', raw_proxy, '--output', str(output)]) == 2
    text = output.read_text(encoding='utf-8')
    assert password not in text + capsys.readouterr().out
    assert {row['outcome'] for row in json.loads(text)['diagnostics']} == {'network_error'}


def test_no_proxy_bypass_list_with_ipv6_preserves_healthy_all_source_diagnostics(tmp_path, monkeypatch):
    for name in ('DATA_AGENT_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    bypass = 'localhost,127.0.0.1,::1'
    monkeypatch.setenv('NO_PROXY', bypass)
    sources = (
        provider('bing-html', '<div id="b_results">no results</div>'),
        provider('so360-html', '<div id="main">no results</div>'),
        provider('duckduckgo-html', '<div id="links">no results</div>'),
    )
    monkeypatch.setattr(probe, 'default_providers', lambda **kw: sources)
    output = tmp_path / 'no-proxy.json'
    assert probe.main(['--provider', 'all', '--output', str(output)]) == 0
    saved = json.loads(output.read_text(encoding='utf-8'))
    assert saved['status'] == 'healthy' and saved['request_count'] == 9
    assert all(row['ambient_proxy_environment']['NO_PROXY'] == bypass for row in saved['diagnostics'])


def test_invalid_proxy_returns_safe_failed_json_without_parser_exception_or_transport(
    tmp_path, monkeypatch, capsys,
):
    password = 'task16_synthetic_secret'
    raw_proxy = 'http://user:' + password + '@bad[host'
    calls = []
    monkeypatch.setattr(probe, 'default_providers', lambda **kw: calls.append(kw) or (
        provider('bing-html', '<div id="b_results">no results</div>'),))
    output = tmp_path / 'invalid.json'
    assert probe.main(['--proxy', raw_proxy, '--output', str(output)]) == 2
    text = output.read_text(encoding='utf-8')
    captured = capsys.readouterr()
    assert password not in text + captured.out + captured.err
    assert 'Traceback' not in captured.out + captured.err
    assert json.loads(text)['status'] == 'failed'
    assert calls == []
    assert password not in probe._proxy_error_audit('invalid ' + raw_proxy + ' bare ' + password, [raw_proxy])
