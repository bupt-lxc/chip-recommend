"""Execute Hermes-owned search words and paginate immutable saved candidates.

No business database, model lookup, semantic scoring, or Hermes runtime lives
here. Network adapters return diagnostics alongside raw hits; a plain legacy
list cannot establish whether zero hits mean a healthy search or broken HTML.
Every external attempt uses RunBudget's fenced physical-execution receipts.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup

from .contracts import DiscoverySearchPlan, REASON_TEXT
from .store import _hash, _json, _key


HEALTHY = frozenset({'success', 'healthy_empty'})
OUTCOMES = HEALTHY | {'network_error', 'http_error', 'parse_error'}
QUERY_FAILURES = (OUTCOMES - HEALTHY) | {'retry_budget_exhausted'}
MAX_RESPONSE_CHARS = 12000
_TRACKING = frozenset({'gclid', 'fbclid', 'msclkid', 'yclid', '_ga', 'mc_cid', 'mc_eid'})
_SECRET_NAME = r'(?:api[_-]?key|access[_-]?token|token|(?:proxy[-_])?authorization|password|secret|credential)'
_SECRET_KEYS = re.compile(r'^' + _SECRET_NAME + r'$', re.I)
_URL_CREDENTIALS = re.compile(r'([a-z][a-z0-9+.-]*://)[^/\s@]+@', re.I)
# Escape and ordinary-character branches are disjoint: an unterminated value
# cannot backtrack through exponentially many partitions of its backslashes.
_QUOTED_SECRET = re.compile(
    r'(?P<prefix>["\']' + _SECRET_NAME + r'["\']\s*:\s*)(?P<quote>["\'])(?:\\.|(?!(?P=quote))[^\\])*(?P=quote)', re.I | re.S)
_ESCAPED_QUOTED_SECRET = re.compile(
    r'(?P<prefix>\\["\']' + _SECRET_NAME + r'\\["\']\s*:\s*)(?P<quote>\\["\'])(?:(?!(?P=quote)).)*(?P=quote)', re.I | re.S)
_AUTHORIZATION_HEADER = re.compile(r'(?<![\w-])((?:proxy[-_])?authorization\s*[:=]\s*)[^\r\n,;"\']+', re.I)


class OversizeItemError(ValueError):
    """A complete item cannot fit; callers must not treat this as list EOF."""

    def __init__(self, item, offset):
        self.code = 'item_too_large'
        self.candidate_id = item.get('candidate_id') if isinstance(item, dict) else None
        self.offset = offset
        super().__init__(f'item_too_large at offset {offset}; candidate_id={self.candidate_id}')


def serialize_page(items, offset, base, limit=10):
    """Pure shared pagination: whole JSON items, <=10 rows and <=12000 chars.

    Cursor is bound to base.list_version. A large row is an explicit failure,
    with no fake EOF or advancing cursor that could hide an unread candidate.
    Backend callers may request fewer rows, never raise the policy ceiling.
    """
    if not isinstance(items, list) or not isinstance(base, dict):
        raise ValueError('pagination requires an item list and base object')
    if type(offset) is not int or not 0 <= offset <= len(items):
        raise ValueError('offset must be an integer within list total')
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('pagination limit must be 1..10')
    _key(base.get('list_version'))
    response = json.loads(_json(base))
    response.update(offset=offset, total=len(items), items=[], next_cursor=None, has_more=False)
    if len(json.dumps(response, ensure_ascii=False)) > MAX_RESPONSE_CHARS:
        raise ValueError('response_envelope_too_large')
    for item in items[offset:offset + limit]:
        item = json.loads(_json(item))
        end = offset + len(response['items']) + 1
        tentative = {**response, 'items': response['items'] + [item], 'has_more': end < len(items),
                     'next_cursor': {'list_version': base['list_version'], 'offset': end} if end < len(items) else None}
        if len(json.dumps(tentative, ensure_ascii=False)) > MAX_RESPONSE_CHARS:
            if not response['items']:
                raise OversizeItemError(item, offset)
            break
        response = tentative
    return response


def redact(value):
    """Remove credentials from diagnostics and persisted provider material."""
    if isinstance(value, dict):
        return {key: '[REDACTED]' if _SECRET_KEYS.fullmatch(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    # Encoded proxy values may nest inside query parameters or error strings.
    # Retain decoded output only when a sensitive pattern is found; normal
    # result URL bytes (including encoded SKU parameters) stay untouched.
    decoded, sensitive = value, False
    while True:
        # Scrub each decoding layer before opening the next, so a password's
        # encoded slash cannot obscure userinfo after further decoding. Mixed
        # plain and encoded credentials are handled in the same string.
        cleaned = _redact_plain(decoded)
        sensitive = sensitive or cleaned != decoded
        unquoted = unquote(cleaned)
        if unquoted == cleaned:
            return cleaned if sensitive else value
        decoded = unquoted


def _redact_plain(value):
    # Only named credential fields/headers are removed. Preserve unrelated
    # JSON/repr fields and punctuation so diagnostic context stays useful.
    for pattern in (_QUOTED_SECRET, _ESCAPED_QUOTED_SECRET):
        value = pattern.sub(lambda match: match['prefix'] + match['quote'] + '[REDACTED]' + match['quote'], value)
    value = _AUTHORIZATION_HEADER.sub(r'\1[REDACTED]', value)
    value = re.sub(r'([?&])([^=&#\s]+)=([^&#\s]*)',
                   lambda match: match[1] + match[2] + '=[REDACTED]' if _SECRET_KEYS.fullmatch(unquote(match[2])) else match[0], value)
    value = _URL_CREDENTIALS.sub(r'\1[REDACTED]@', value)
    value = re.sub(r'(?i)(bearer\s+)[^\s&,;]+', r'\1[REDACTED]', value)
    return re.sub(r'(?i)(' + _SECRET_NAME + r'\s*[=:]\s*)[^\s&;,]+',
                  r'\1[REDACTED]', value)


def canonical_url(value):
    """Preserve functional parameters and SKU paths; discard known tracking."""
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value) or '\\' in value:
        raise ValueError('unsafe URL')
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('unsafe URL scheme/address/credentials')
    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise ValueError('unsafe local URL')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Numeric/hex/octal alternative IP spellings are not public domain names.
        if '.' not in host or re.fullmatch(r'[0-9.]+|0x[0-9a-f]+', host) or not re.fullmatch(r'[a-z0-9.-]+', host):
            raise ValueError('unsafe hostname')
        if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
            raise ValueError('unsafe hostname')
    else:
        if not address.is_global:
            raise ValueError('unsafe private address')
        if address.version == 6:
            host = '[' + address.compressed + ']'
    port = parsed.port
    if port is not None and port not in {80, 443}:
        raise ValueError('unsafe URL port')
    netloc = host if port is None or (parsed.scheme, port) in {('http', 80), ('https', 443)} else f'{host}:{port}'
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if any(_SECRET_KEYS.fullmatch(key) for key, _ in params):
        raise ValueError('credential-bearing URL')
    # Keep the original functional bytes/order rather than urlencode all values.
    query = '&'.join(part for part in parsed.query.split('&') if part and not (
        unquote(part.split('=', 1)[0]).casefold().startswith('utm_')
        or unquote(part.split('=', 1)[0]).casefold() in _TRACKING))
    return urlunsplit((parsed.scheme, netloc, parsed.path or '/', query, ''))


@dataclass
class ProviderResponse:
    """Instrumented response; raw hits can exceed the admission quota."""

    rows: list[dict] = field(default_factory=list)
    outcome: str = 'healthy_empty'
    http_status: int | None = None
    endpoint: str = ''
    locale: str = ''
    error: str = ''
    network_status: str = 'ok'
    parse_status: str = 'ok'
    elapsed_ms: int = 0


class HtmlSearchProvider:
    """Reuse existing sessions and destination parser with explicit HTML health.

    English Bing requests use the global endpoint and en locale. 360 stays on
    its real Chinese endpoint, with truthful language headers and no claim
    that it is an English-index search engine. Response health is conservative:
    an unrecognized/challenge page is a parse failure, never healthy empty.
    """

    def __init__(self, adapter):
        self.adapter = adapter
        self.name = adapter.name
        if self.name not in {'bing-html', 'so360-html', 'duckduckgo-html'}:
            raise ValueError('unsupported raw HTML search adapter')

    def search(self, query, limit, *, language):
        if language not in {'en', 'zh'}:
            raise ValueError('unsupported search language')
        locale = 'en-US' if language == 'en' else 'zh-CN'
        params = {'q': query}
        if self.name == 'bing-html':
            endpoint = 'https://www.bing.com/search' if language == 'en' else 'https://cn.bing.com/search'
            params.update(setlang=language, mkt=locale)
            selector, anchor_selector, snippet_selector = 'li.b_algo', 'h2 a[href]', '.b_caption p'
            container = '#b_results'
        elif self.name == 'so360-html':
            endpoint = 'https://www.so.com/s'
            selector, anchor_selector, snippet_selector = 'li.res-list', 'h3.res-title a[data-mdurl], h3.res-title a[href]', '.res-list-summary, .res-desc'
            container = '#m-spread-left, #main, #container'
        else:
            endpoint = 'https://html.duckduckgo.com/html/'
            params['kl'] = 'us-en' if language == 'en' else 'cn-zh'
            selector, anchor_selector, snippet_selector = '.result', 'a.result__a', '.result__snippet'
            container = '#links, .results'
        started = time.monotonic()
        response = ProviderResponse(endpoint=endpoint, locale=locale)
        try:
            http = self.adapter.session.get(endpoint, params=params, timeout=self.adapter.timeout,
                                            proxies=self.adapter.proxies,
                                            headers={'Accept-Language': 'en-US,en;q=0.9' if language == 'en' else 'zh-CN,zh;q=0.9'})
        except requests.RequestException as exc:
            response.outcome, response.network_status = 'network_error', 'error'
            response.parse_status, response.error = 'not_attempted', redact(str(exc))
        else:
            response.http_status = http.status_code
            if not 200 <= http.status_code < 300:
                response.outcome, response.parse_status = 'http_error', 'not_attempted'
            else:
                try:
                    soup = BeautifulSoup(http.text, 'lxml')
                    text = soup.get_text(' ', strip=True).casefold()
                    if re.search(r'captcha|verify you are human|unusual traffic|人机验证|安全验证', text) or soup.select_one('#b_captcha, .anomaly-modal'):
                        raise ValueError('search challenge page')
                    nodes = soup.select(selector)
                    for node in nodes:
                        anchor = node.select_one(anchor_selector)
                        if anchor is None:
                            continue
                        url = str(anchor.get('data-mdurl') or anchor.get('href') or '').strip()
                        if self.name == 'duckduckgo-html':
                            url = self.adapter._destination(url)
                        snippet = node.select_one(snippet_selector)
                        response.rows.append({'url': url, 'title': anchor.get_text(' ', strip=True),
                                              'snippet': snippet.get_text(' ', strip=True) if snippet else '',
                                              'rank': len(response.rows) + 1})
                    if response.rows:
                        response.outcome = 'success'
                    elif (soup.select_one(container) is not None and re.search(
                            r'no results|no .*results found|did not match|没有找到|未找到|无相关|找不到', text)):
                        response.outcome = 'healthy_empty'
                    else:
                        raise ValueError('unrecognized or unparseable search response')
                except (ValueError, TypeError) as exc:
                    response.outcome, response.parse_status = 'parse_error', 'error'
                    response.error = redact(str(exc))
        response.elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return response


def default_providers(*, proxy=None):
    from ..open_web_test import BingHtmlSearch, SoHtmlSearch, DuckDuckGoHtmlSearch
    return tuple(HtmlSearchProvider(adapter(proxy=proxy)) for adapter in (BingHtmlSearch, SoHtmlSearch, DuckDuckGoHtmlSearch))


class _TechnicalFailure(Exception):
    """Callback exited, allowing RunBudget's next confirmed retry."""


class SearchExecutor:
    """One isolated run, with backend-owned providers and persisted quotas."""

    KEY = 'domain_search.v1'

    def __init__(self, store, budget, *, providers, backoff=None):
        if store is not budget.store:
            raise ValueError('search store must be the budget store')
        self.store, self.budget = store, budget
        self.run_id, self.generation = budget.run_id, budget.generation
        self.providers = tuple(providers)
        if not self.providers or len(self.providers) > 8 or len({p.name for p in self.providers}) != len(self.providers):
            raise ValueError('require 1..8 distinct instrumented providers')
        for provider in self.providers:
            _key(provider.name)
        self.backoff = backoff or (lambda attempt: time.sleep(min(2 ** attempt, 4)))
        self.chip_hint = store.get_run(self.run_id)['payload'].get('chip')

    @staticmethod
    def _empty():
        return {'plans': {}, 'queries': {}, 'candidates': {}, 'versions': {}, 'intents': {}, 'intent_requests': {}}

    def _state(self):
        return self.store.metadata(self.run_id, self.KEY) or self._empty()

    def _update(self, callback):
        return self.store.update_metadata(self.run_id, self.generation, self.KEY, callback)

    def _validate_saved(self, state, request_id, payload):
        key = str(payload['round_number'])
        for number, saved in state['plans'].items():
            if saved['request_id'] == request_id and number != key:
                raise ValueError('search request payload conflict')
        existing = state['plans'].get(key)
        if existing:
            if existing['request_id'] != request_id or existing['model_payload'] != payload:
                raise ValueError('search round/request payload conflict')
            return existing
        old_texts = {' '.join(q['query']['text'].split()).casefold() for q in state['queries'].values()}
        for query in payload['queries']:
            if query['query_id'] in state['queries'] or ' '.join(query['text'].split()).casefold() in old_texts:
                raise ValueError('duplicate persisted query ID/text')
            replacement = query.get('replaces_query_id')
            if replacement is not None:
                previous = state['queries'].get(replacement)
                if previous is None or previous.get('outcome') not in QUERY_FAILURES:
                    raise ValueError('replacement must reference a prior failed query intent')
        return None

    def execute(self, request_id, model_payload):
        """Validate exact Hermes payload, execute saved query intent, return page 1.

        Replays use immutable receipts. An unknown running attempt returns
        in_progress; only the worker's explicit old-generation recovery can
        authorize replay. Technical retries have stable provider/query IDs.
        """
        request_id = _key(request_id)
        _json(model_payload)
        validated = DiscoverySearchPlan.model_validate(model_payload)
        # Preserve the original model object (including absent optional fields).
        payload = json.loads(_json(model_payload))
        existing = self._validate_saved(self._state(), request_id, payload)
        if existing and existing.get('list_version'):
            return self._initial_response(existing['list_version'])
        self.budget.reserve_queries('search-round-' + _hash(request_id), [q.query_id for q in validated.queries],
                                    round_number=validated.round_number)

        def register(state):
            state = state or self._empty()
            saved = self._validate_saved(state, request_id, payload)
            if saved is None:
                state['plans'][str(validated.round_number)] = {
                    'request_id': request_id, 'model_payload': payload,
                    'providers': [p.name for p in self.providers], 'list_version': None}
                for query in payload['queries']:
                    state['queries'][query['query_id']] = {'query': query, 'attempts': [], 'exclusions': [], 'outcome': None}
            elif saved['providers'] != [p.name for p in self.providers]:
                raise ValueError('saved search provider configuration changed')
            return state, None, None
        self._update(register)
        self.budget.record_stage('searching', 'in_progress', scope='main', reason='Hermes plan executing')
        for query in payload['queries']:
            if not self._execute_query(query):
                return {'status': 'in_progress', 'round_number': validated.round_number, 'list_version': None,
                        'items': [], 'next_cursor': None, 'has_more': False}

        def publish(state):
            saved = state['plans'][str(validated.round_number)]
            if saved['list_version'] is None:
                candidates = list(state['candidates'].values())
                diagnostics = [a['diagnostic'] for q in state['queries'].values() for a in q['attempts']]
                diagnostics.extend(stop for q in state['queries'].values() for stop in q.get('provider_stops', {}).values())
                snapshot = {'items': candidates, 'diagnostics': diagnostics,
                            'incomplete': self._incomplete(state), 'round_number': validated.round_number,
                            'query_count': len(state['queries'])}
                version = _hash(_json(snapshot))
                state['versions'][version] = snapshot
                saved['list_version'] = version
            return state, saved['list_version'], {'kind': 'search_list_published', 'stage': 'searching',
                                                 'round_number': validated.round_number, 'list_version': saved['list_version']}
        version = self._update(publish)
        incomplete = self._state()['versions'][version]['incomplete']
        self.budget.record_stage('searching', 'partial_failure' if incomplete else 'completed', scope='main',
                                 reason='unresolved query failures' if incomplete else 'all query intents executed')
        return self._initial_response(version)

    @staticmethod
    def _incomplete(state):
        return any(q['outcome'] not in HEALTHY and qid not in state['intents'] for qid, q in state['queries'].items())

    def _capture(self, query, provider, provider_index, attempt):
        """Persist raw hits and diagnostics before signaling confirmed failure."""
        try:
            response = provider.search(query['text'], 10, language=query['language'])
            if not isinstance(response, ProviderResponse) or response.outcome not in OUTCOMES or not isinstance(response.rows, list):
                raise ValueError('provider must return instrumented ProviderResponse')
            raw = []
            valid_count = 0
            for row in response.rows:
                if not isinstance(row, dict) or not isinstance(row.get('url'), str):
                    raise ValueError('invalid provider hit')
                _json(row)
                try:
                    canonical_url(row['url'])
                    valid_count += 1
                except (ValueError, UnicodeError):
                    pass
                raw.append(redact(row))
            if response.outcome == 'healthy_empty' and raw:
                raise ValueError('healthy empty response contained hits')
            diagnostic = redact({'query_id': query['query_id'], 'provider': provider.name, 'language': query['language'],
                                 'original_query': query['text'], 'actual_query': query['text'],
                                 'outcome': response.outcome, 'http_status': response.http_status,
                                 'network_status': 'error' if response.outcome == 'network_error' else response.network_status,
                                 'parse_status': 'error' if response.outcome == 'parse_error' else response.parse_status,
                                 'valid_url_count': valid_count, 'raw_hit_count': len(raw), 'fallback': provider_index > 0,
                                 'attempt': attempt, 'endpoint': response.endpoint, 'locale': response.locale,
                                 'elapsed_ms': response.elapsed_ms, 'error': response.error})
            _json(diagnostic)
        except requests.RequestException as exc:
            raw = []
            diagnostic = self._error_diagnostic(query, provider, provider_index, attempt, 'network_error', exc)
        except Exception as exc:
            raw = []
            diagnostic = self._error_diagnostic(query, provider, provider_index, attempt, 'parse_error', exc)
        record = {'provider': provider.name, 'attempt': attempt, 'execution_id': uuid.uuid4().hex,
                  'raw_hits': raw, 'diagnostic': diagnostic}
        def save(state):
            attempts = state['queries'][query['query_id']]['attempts']
            # A recovered unknown execution is a new physical capture, even
            # when its provider material happens to be byte-for-byte equal.
            attempts.append(record)
            return state, record, {'kind': 'search_provider_attempt', 'stage': 'searching', **diagnostic}
        self._update(save)
        if diagnostic['outcome'] not in HEALTHY:
            raise _TechnicalFailure(diagnostic['outcome'])
        return record

    @staticmethod
    def _error_diagnostic(query, provider, index, attempt, outcome, exc):
        return redact({'query_id': query['query_id'], 'provider': provider.name, 'language': query['language'],
                       'original_query': query['text'], 'actual_query': query['text'], 'outcome': outcome,
                       'http_status': None, 'network_status': 'error' if outcome == 'network_error' else 'ok',
                       'parse_status': 'error' if outcome == 'parse_error' else 'not_attempted',
                       'valid_url_count': 0, 'raw_hit_count': 0, 'fallback': index > 0,
                       'attempt': attempt, 'endpoint': '', 'locale': '', 'elapsed_ms': 0, 'error': str(exc)})

    def _execute_query(self, query):
        qid = query['query_id']
        if self._state()['queries'][qid]['outcome'] is not None:
            return True
        for index, provider in enumerate(self.providers):
            if provider.name in self._state()['queries'][qid].get('provider_stops', {}):
                continue
            logical = 'search-' + _hash(_json([qid, provider.name]))
            for attempt in range(3):
                try:
                    receipt = self.budget.operation(logical, {'stage': 'searching', 'query': query, 'provider': provider.name},
                                                    lambda: self._capture(query, provider, index, attempt), attempt=attempt)
                except ValueError as exc:
                    if str(exc) != 'technical retry budget exhausted':
                        raise
                    # Recovery can consume physical attempts before logical
                    # attempt IDs reach 2. This provider is then terminal,
                    # while fallback and other queries still have their quota.
                    self._provider_stop(query, provider, index, logical)
                    break
                if receipt['state'] in {'running', 'pending'}:
                    return False
                if receipt['state'] == 'completed':
                    break
                if attempt < 2:
                    self.backoff(attempt)
        saved = self._state()['queries'][qid]
        healthy = [a for a in saved['attempts'] if a['diagnostic']['outcome'] in HEALTHY]
        rows = [(a['provider'], row) for a in healthy for row in a['raw_hits']]
        outcome = (('success' if rows else 'healthy_empty') if healthy else
                   saved['attempts'][-1]['diagnostic']['outcome'] if saved['attempts'] else 'retry_budget_exhausted')
        admitted = set()
        for row_index, (provider_name, row) in enumerate(rows):
            try:
                url = canonical_url(row['url'])
            except (ValueError, UnicodeError):
                self._exclude(qid, provider_name, row_index, row, 'unsafe_url')
                continue
            # Search metadata is shorter than the actual page. A missing SKU
            # in title/snippet/URL cannot establish absence from its full body.
            # Preserve targeted scope and expose the match only as an audit hint
            # for Hermes' later preview-based selection.
            hint_match = self._matches_hint(self.chip_hint, row) if self.chip_hint else None
            cid = _hash(url)
            if cid not in admitted and len(admitted) == 10:
                self._exclude(qid, provider_name, row_index, row, 'per_query_budget')
                continue
            try:
                self.budget.admit_results('search-admit-' + _hash(_json([qid, cid])), qid, [cid])
            except ValueError as exc:
                if 'result/main candidate budget exhausted' not in str(exc):
                    raise
                self._exclude(qid, provider_name, row_index, row, 'main_candidate_budget')
                continue
            admitted.add(cid)
            reference = {'query_id': qid, 'provider': provider_name, 'rank': row.get('rank', 0), 'raw_url': row['url']}
            def merge(state):
                item = state['candidates'].setdefault(cid, {'candidate_id': cid, 'url': url,
                    'title': row.get('title', ''), 'snippet': row.get('snippet', ''), 'references': []})
                if hint_match is not None:
                    item['matches_chip_hint'] = item.get('matches_chip_hint', False) or hint_match
                if reference not in item['references']:
                    item['references'].append(reference)
                return state, None, None
            self._update(merge)
        def finish(state):
            state['queries'][qid]['outcome'] = outcome
            for capture in state['queries'][qid]['attempts']:
                capture['diagnostic']['query_outcome'] = outcome
            for stop in state['queries'][qid].get('provider_stops', {}).values():
                stop['query_outcome'] = outcome
            return state, None, {'kind': 'search_query_outcome', 'stage': 'searching', 'query_id': qid, 'outcome': outcome}
        self._update(finish)
        return True

    def _provider_stop(self, query, provider, index, logical):
        """Audit a denied execution separately from actual provider attempts."""
        physical_count = self.budget.snapshot()['logical_operations'][logical]['execution_count']
        diagnostic = redact({'query_id': query['query_id'], 'provider': provider.name, 'language': query['language'],
                             'original_query': query['text'], 'actual_query': None, 'http_status': None,
                             'network_status': 'not_executed', 'parse_status': 'not_attempted',
                             'outcome': 'retry_budget_exhausted', 'query_outcome': None,
                             'physical_execution_count': physical_count, 'valid_url_count': 0,
                             'raw_hit_count': 0, 'fallback': index > 0,
                             'budget_ref': f'{self.budget.KEY}/logical_operations/{logical}'})
        def stop(state):
            stops = state['queries'][query['query_id']].setdefault('provider_stops', {})
            if provider.name in stops:
                return state, None, None
            stops[provider.name] = diagnostic
            return state, None, {'kind': 'search_provider_budget_stopped', 'stage': 'searching', **diagnostic}
        self._update(stop)

    @staticmethod
    def _matches_hint(hint, row):
        tokens = re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+', hint.casefold())
        if not tokens:
            return False
        pattern = r'(?<![a-z0-9_-])' + r'[-_\s]*'.join(map(re.escape, tokens))
        for key in ('url', 'title', 'snippet'):
            text = unquote(str(row.get(key, ''))).casefold()
            # A slash after a complete URL path segment is navigation, while
            # a slash in a displayed SKU can introduce another variant.
            boundary = r'(?![a-z0-9_-])' if key == 'url' else r'(?![a-z0-9_-]|/[a-z0-9])'
            if re.search(pattern + boundary, text):
                return True
        return False

    def _exclude(self, qid, provider, index, row, reason):
        record = {'provider': provider, 'raw_index': index, 'url': row['url'], 'reason': reason}
        def exclude(state):
            exclusions = state['queries'][qid]['exclusions']
            if record not in exclusions:
                exclusions.append(record)
            return state, None, None
        self._update(exclude)

    def complete_intent(self, request_id, failed_query_id, replacement_query_id, reason):
        """Hermes explicitly ends an intent after its linked replacement executes."""
        request_id, failed_query_id, replacement_query_id = map(_key, (request_id, failed_query_id, replacement_query_id))
        # A human explanation is not an idempotency key: preserve its exact
        # Unicode text and use the same 400-character public reason contract.
        reason = REASON_TEXT.validate_python(reason)
        payload = {'query_id': failed_query_id, 'replacement_query_id': replacement_query_id, 'reason': reason}
        def finish(state):
            state = state or self._empty()
            failed = state['queries'].get(failed_query_id)
            replacement = state['queries'].get(replacement_query_id)
            if (failed is None or failed['outcome'] not in QUERY_FAILURES or replacement is None
                    or replacement['query'].get('replaces_query_id') != failed_query_id or replacement['outcome'] not in HEALTHY):
                raise ValueError('intent completion requires an explicitly linked healthy replacement of a failed query')
            prior = state['intent_requests'].get(request_id)
            if prior is not None and prior != payload:
                raise ValueError('intent completion payload conflict')
            if prior is not None:
                return state, prior, None
            ended = state['intents'].get(failed_query_id)
            if ended is not None and ended != payload:
                raise ValueError('intent already ended with another completion')
            state['intent_requests'][request_id] = payload
            state['intents'][failed_query_id] = payload
            return state, payload, {'kind': 'search_intent_completed', 'stage': 'searching', **payload}
        result = self._update(finish)
        self.budget.record_stage('searching', 'partial_failure' if self._incomplete(self._state()) else 'completed',
                                 scope='main', reason='Hermes explicitly ended replacement intent')
        return result

    @staticmethod
    def _serialized_size(response):
        return len(json.dumps(response, ensure_ascii=False))

    def _initial_response(self, version):
        state = self._state()
        snapshot = state['versions'][version]
        diagnostics = snapshot['diagnostics']
        extra = {'status': 'completed', 'incomplete': snapshot['incomplete'],
                 'intent_incomplete': self._incomplete(state),
                 'query_count': snapshot.get('query_count', len({d['query_id'] for d in diagnostics})),
                 'round_query_count': len(state['plans'][str(snapshot['round_number'])]['model_payload']['queries']),
                 'round_number': snapshot['round_number'], 'diagnostics': diagnostics,
                 'diagnostics_ref': f'{self.KEY}/versions/{version}/diagnostics'}
        # Detailed diagnostics remain saved; compact execute summaries make
        # room for complete candidate rows, regardless of provider failures.
        if self._serialized_size(extra) > 5000:
            grouped = {}
            for diagnostic in diagnostics:
                key = (diagnostic['provider'], diagnostic['language'], diagnostic['outcome'])
                summary = grouped.setdefault(key, {'provider': key[0], 'language': key[1], 'outcome': key[2],
                                                   'attempt_count': 0, 'valid_url_count': 0})
                summary['attempt_count'] += 1
                summary['valid_url_count'] += diagnostic['valid_url_count']
            extra['diagnostics'] = list(grouped.values())
            if self._serialized_size(extra) > 5000:
                sources = {}
                for summary in grouped.values():
                    key = (summary['provider'], summary['language'])
                    source = sources.setdefault(key, {'provider': key[0], 'language': key[1], 'outcomes': {}})
                    source['outcomes'][summary['outcome']] = summary['attempt_count']
                extra['diagnostics'] = list(sources.values())
            if self._serialized_size(extra) > 5000:
                # Provider names are backend data and may be unusually long;
                # the saved reference always retains their full diagnostics.
                extra['diagnostics'] = []
        try:
            return self._page(version, 0, extra)
        except OversizeItemError:
            # The first page may fit with detailed diagnostics moved behind
            # their saved reference. A truly oversized single row still fails.
            return self._page(version, 0, {**extra, 'diagnostics': []})

    def results(self, list_version, *, cursor=None, offset=0):
        """Read only a saved list version; cursor never triggers search IO."""
        list_version = _key(list_version)
        if type(offset) is not int or offset < 0:
            raise ValueError('offset must be a nonnegative integer')
        if cursor is not None:
            if not isinstance(cursor, dict) or offset != 0:
                raise ValueError('cursor and offset conflict')
            if set(cursor) != {'list_version', 'offset'} or cursor['list_version'] != list_version:
                raise ValueError('cursor list version/offset mismatch')
            offset = cursor['offset']
            if type(offset) is not int or offset < 0:
                raise ValueError('cursor offset must be a nonnegative integer')
        return self._page(list_version, offset, {})

    def _page(self, version, offset, extra):
        snapshot = self._state()['versions'].get(version)
        if snapshot is None:
            raise ValueError('unknown saved list version')
        return serialize_page(snapshot['items'], offset, {'list_version': version,
                              'list_ref': f'{self.KEY}/versions/{version}', **extra})
