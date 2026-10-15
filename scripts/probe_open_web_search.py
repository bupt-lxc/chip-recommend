#!/usr/bin/env python3
"""Read-only source diagnostics; these short probes are never production plans."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chip_model.pipeline.domain_discovery.search import (  # noqa: E402
    HEALTHY, canonical_url, default_providers, redact,
)

PROBES = (
    {'language': 'zh', 'scope': 'discovery', 'query': 'AI 加速芯片 新发布'},
    {'language': 'en', 'scope': 'discovery', 'query': 'AI accelerator product launch'},
    {'language': 'en', 'scope': 'chip', 'query': 'NVIDIA L40S specifications'},
)
PROVIDERS = ('bing-html', 'so360-html', 'duckduckgo-html')
BUSINESS_TABLES = ('chips', 'models', 'chip_model_benchmarks',
    'chip_model_compatibility', 'deployment_guides', 'field_provenance')
PROXY_ENVIRONMENT = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')


def _proxy_parts(value):
    try:
        normalized = requests.utils.prepend_scheme_if_needed(value, 'http')
        parsed = urlsplit(normalized)
        # Bare user:password@host can look like an unknown URI scheme to the
        # parser. Only the audit copy treats that userinfo structurally.
        if not parsed.netloc and '@' in value:
            parsed = urlsplit('http://' + value.removeprefix('//'))
        return parsed
    except Exception:
        # Parser exceptions can themselves contain credentials. Never expose
        # them or invoke the same fallible parser again in an error handler.
        return None


def _proxy_audit(value):
    """Sanitize an audit copy of requests-supported proxy URL forms only."""
    if not isinstance(value, str) or not value:
        return value
    parsed = _proxy_parts(value)
    if parsed is None:
        return '[REDACTED INVALID PROXY]'
    if parsed.username is not None or parsed.password is not None:
        safe_netloc = '[REDACTED]@' + parsed.netloc.rsplit('@', 1)[-1]
        return redact(urlunsplit((parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment)))
    return redact(value)


def _proxy_mapping_audit(mapping):
    return {key: redact(value) if key.lower() in {'no', 'no_proxy'} else _proxy_audit(value)
        for key, value in mapping.items()}


def _transport_proxy_values(mapping):
    return [value for key, value in mapping.items() if key.lower() not in {'no', 'no_proxy'}]


def _proxy_error_audit(message, proxies):
    """Errors can embed raw scheme-less URLs or decoded known credentials."""
    secrets = set()
    for proxy in proxies:
        if not isinstance(proxy, str) or not proxy:
            continue
        parsed = _proxy_parts(proxy)
        if parsed is None:
            message = message.replace(proxy, '[REDACTED INVALID PROXY]')
            # Conservative userinfo-only fallback for malformed URLs. It does
            # not parse or publish the invalid hostname and also masks bare
            # credentials embedded separately by a transport exception.
            userinfo = proxy.rsplit('@', 1)[0].split('://', 1)[-1].removeprefix('//')
            credentials = userinfo.split(':', 1) if '@' in proxy else []
        else:
            normalized = parsed.geturl()
            credentials = (parsed.username, parsed.password)
            if parsed.username is not None or parsed.password is not None:
                safe = _proxy_audit(proxy)
                message = message.replace(normalized, safe).replace(proxy, safe)
        for credential in credentials:
            while credential:
                secrets.add(credential)
                decoded = unquote(credential)
                if decoded == credential:
                    break
                credential = decoded
    for secret in sorted(secrets, key=len, reverse=True):
        message = message.replace(secret, '[REDACTED]')
    return redact(message)


def business_digest(source, *, backup_timeout=5):
    """Hash all logical business content from one consistent WAL-aware backup.

    Source opens mode=ro. The backup is in memory; no business or RunStore
    destination is created. Missing tables are errors, never empty successes.
    """
    if type(backup_timeout) not in (int, float) or not math.isfinite(backup_timeout) or backup_timeout <= 0:
        raise ValueError('backup_timeout must be finite and positive')
    source = Path(source).resolve()
    records, tables = {}, {}
    deadline = time.monotonic() + backup_timeout
    def check_progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError('business database backup deadline exceeded; inspect external locks and retry')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=min(3, backup_timeout))) as origin:
        origin.execute('PRAGMA query_only=ON')
        with closing(sqlite3.connect(':memory:')) as snapshot:
            origin.backup(snapshot, pages=128, progress=check_progress, sleep=0.05)
            for table in BUSINESS_TABLES:
                columns = [row[1] for row in snapshot.execute(f'PRAGMA table_info("{table}")')]
                if not columns:
                    raise ValueError(f'missing business table: {table}')
                rows = [dict(zip(columns, row)) for row in snapshot.execute(f'SELECT * FROM "{table}"')]
                rows.sort(key=lambda row: (str(row.get('id')), json.dumps(row, sort_keys=True, ensure_ascii=False)))
                record = {'columns': columns, 'rows': rows}
                records[table] = record
                tables[table] = {'columns': columns, 'row_count': len(rows), 'sha256': _digest(record)}
    return {'sha256': _digest(records), 'tables': tables}


def _digest(value):
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def run_probe(providers, *, proxy=None):
    """Every source receives all three words directly, with no fallback/retry."""
    started_at = datetime.now(timezone.utc).isoformat()
    diagnostics = []
    for provider in providers:
        provider.adapter.timeout = min(provider.adapter.timeout, 15)
        for probe in PROBES:
            response = provider.search(probe['query'], 10, language=probe['language'])
            trust_env = getattr(provider.adapter.session, 'trust_env', True)
            ambient = requests.utils.get_environ_proxies(response.endpoint) if trust_env else {}
            effective = {**(getattr(provider.adapter.session, 'proxies', {}) or {}),
                **ambient, **(provider.adapter.proxies or {})}
            effective = {key: value for key, value in effective.items() if value is not None}
            environment = {name: os.getenv(name) for name in PROXY_ENVIRONMENT if os.getenv(name)}
            proxy_values = [proxy, *_transport_proxy_values(ambient),
                *_transport_proxy_values(effective), *_transport_proxy_values(environment)]
            urls, rejected = [], []
            for row in response.rows:
                try:
                    url = canonical_url(row['url'])
                except (KeyError, ValueError) as error:
                    rejected.append({'url': redact(row.get('url', '')), 'reason': redact(str(error))})
                else:
                    if url not in urls:
                        urls.append(url)
            diagnostics.append(redact({
                'provider': provider.name, 'language': probe['language'], 'scope': probe['scope'],
                'original_query': probe['query'], 'actual_query': probe['query'],
                'endpoint': response.endpoint, 'locale': response.locale,
                'http_status': response.http_status, 'network_status': response.network_status,
                'parse_status': response.parse_status, 'outcome': response.outcome,
                'error': _proxy_error_audit(response.error, proxy_values), 'elapsed_ms': response.elapsed_ms,
                'raw_result_count': len(response.rows), 'valid_urls': urls,
                'rejected_urls': rejected, 'explicit_proxy': _proxy_audit(proxy),
                'ambient_proxy_environment': _proxy_mapping_audit(environment),
                'ambient_proxies': _proxy_mapping_audit(ambient),
                'effective_proxies': _proxy_mapping_audit(effective),
                'session_trust_env': trust_env, 'fallback_used': False,
            }))
    return redact({'diagnostic_only': True, 'production_template': False,
        'status': 'healthy' if diagnostics and all(r['outcome'] in HEALTHY for r in diagnostics) else 'failed',
        'started_at': started_at,
        'configured_sources': [provider.name for provider in providers],
        'request_count': len(diagnostics), 'fallback_attempted': False,
        'diagnostics': diagnostics})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--db', type=Path, help='Optional read-only business database for before/after logical digest')
    parser.add_argument('--output', type=Path, default=ROOT / '.tmp/open-web-search-probe.json')
    parser.add_argument('--provider', choices=('all', *PROVIDERS), default='all')
    parser.add_argument('--proxy', default=os.getenv('DATA_AGENT_PROXY') or None,
        help='Explicit proxy; defaults to DATA_AGENT_PROXY. Empty means no explicit proxy; ambient requests proxies still apply.')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.suffix.lower() != '.json' or (args.db and output == args.db.resolve()):
        parser.error('--output must be a diagnostic .json file separate from --db')
    if args.db:
        try:
            same_file = output.samefile(args.db)
        except FileNotFoundError:
            same_file = False  # A missing path has no file identity to alias.
        except OSError as error:
            parser.error('cannot safely compare --output and --db file identity: ' + redact(str(error)))
        if same_file:
            parser.error('--output must not alias the --db file through a hard link or symbolic link')
    before = None
    try:
        configured_proxies = [args.proxy, *_transport_proxy_values(
            {name: os.getenv(name) for name in PROXY_ENVIRONMENT if os.getenv(name)})]
        if any(proxy and _proxy_parts(proxy) is None for proxy in configured_proxies):
            raise ValueError('unsupported proxy URL configuration; credentials withheld')
        if args.db:
            before = business_digest(args.db)
        providers = default_providers(proxy=args.proxy or None)
        if args.provider != 'all':
            providers = tuple(p for p in providers if p.name == args.provider)
        result = run_probe(providers, proxy=args.proxy or None)
        if args.db:
            after = business_digest(args.db)
            result['business_database'] = {'source': str(args.db.resolve()), 'before': before, 'after': after,
                'unchanged': before == after, 'scope': 'diagnostic interval only; no Hermes execution'}
            if before != after:
                result.update(status='failed', interference='business content changed; inspect other writers and repeat in a quiet window')
    except (OSError, ValueError, sqlite3.Error) as error:
        result = {'diagnostic_only': True, 'status': 'failed',
            'error': _proxy_error_audit(str(error), configured_proxies),
            'business_before': before}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(redact({'status': result['status'], 'output': str(output),
        'request_count': result.get('request_count', 0), 'diagnostics': result.get('diagnostics', []),
        'error': result.get('error')}), ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'healthy' else 2


if __name__ == '__main__':
    raise SystemExit(main())
