"""Visited-page evidence, immutable full snapshots and explicit body coverage.

Only run-local artifacts/metadata are written. Fetching uses SourceRefresher's
DNS/redirect safety path without invoking its database or refresh workflow.
No extraction/model completion is inferred from a successful page download.
Offsets everywhere are Unicode character offsets into the saved full text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..source_refresh import (
    ConfigurationError, EmptyContent, ResponseTooLarge, SourceRefresher,
    UnsupportedContentType, _detect_encoding, _read_response_body,
    extract_outbound_links, normalize_content, resolve_host_addresses,
)
from .search import SearchExecutor, canonical_url, redact, serialize_page
from .store import _hash, _json, _key


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def locate_preview(text, core_start=0):
    """Return an exact <=500-character slice, never a generated summary."""
    if not isinstance(text, str):
        raise ValueError('preview requires text')
    _integer(core_start, 0, len(text), 'core_start')
    end = min(len(text), core_start + 500)
    return {'start': core_start, 'end': end, 'text': text[core_start:end],
            'body_length': len(text), 'truncated': core_start != 0 or end != len(text)}


def _clean(text):
    text = unicodedata.normalize('NFKC', text)
    return re.sub(r'\s+', ' ', text).strip()


def render_html(raw, encoding):
    """Render retained body blocks and table rows with their source positions.

    Preserve all useful body text, including notes outside main/article. The
    core element controls the preview start only. Footer material survives
    when explicitly marked as notes or referenced by retained core anchors.
    Headers/notes are independent context references, never another SKU row.
    """
    soup = BeautifulSoup(raw.decode(encoding or 'utf-8', errors='replace'), 'lxml')
    title = _clean(soup.title.get_text(' ', strip=True)) if soup.title else ''
    root = soup.body or soup
    core = root.find('main') or root.find('article')
    linked_notes = {unquote(str(a['href'])[1:]) for a in root.find_all('a', href=True)
                    if str(a['href']).startswith('#')}
    retained_text = ' '.join(tag.get_text(' ', strip=True) for tag in root.find_all(['main', 'article', 'table']))
    # Inline markup (especially <sup>) and ordinary typesetting can insert
    # whitespace before a marker. Associate a visible footer note by its exact
    # marker token, rather than requiring adjacency to a preceding value.
    referenced_markers = set(re.findall(r'[*†‡]+|\[\d+\]', retained_text))
    def is_note(tag):
        marker = ' '.join([str(tag.get('id', '')), ' '.join(tag.get('class', [])), str(tag.get('role', ''))])
        return bool(re.search(r'footnote|endnote|doc-note|(?:^|[-_\s])notes?(?:$|[-_\s])', marker, re.I)
                    or tag.get('id') in linked_notes or tag.get('data-discovery-footnote') == '1')
    def visible_note(tag):
        if tag.name not in {'p', 'li', 'small', 'div'}:
            return False
        text = _clean(tag.get_text(' ', strip=True))
        marker = re.match(r'([*†‡]+|\[\d+\])(?:\s|(?=[A-Za-z\u3400-\u9fff]))', text)
        return bool((marker and marker[1] in referenced_markers)
                    or re.match(r'(?i)^(?:footnotes?|notes?)\s*:|^(?:注|注释|说明)\s*[：:]', text))
    # Move relevant footnotes to the footer's position before removing chrome.
    for footer in list(root.find_all('footer')):
        notes = [tag for tag in footer.find_all(True) if is_note(tag) or visible_note(tag)]
        notes = [tag for tag in notes if not any(parent in notes for parent in tag.parents)]
        if is_note(footer):
            footer.name = 'div'
            continue
        for note in notes:
            note['data-discovery-footnote'] = '1'
            footer.insert_before(note.extract())
        footer.decompose()
    for tag in list(root.find_all(['script', 'style', 'noscript', 'template', 'svg', 'iframe',
                                  'nav', 'form', 'button', 'head'])):
        tag.decompose()
    for tag in list(root.find_all('header')):
        if tag is not core and core not in tag.descendants and not tag.find(['h1', 'h2', 'h3']):
            tag.decompose()
    for comment in root.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    if core is None:
        dense = [node for node in root.find_all(['section', 'div']) if node.find(['p', 'table'])]
        if dense:
            # Prefer content volume; when wrappers carry the same text, prefer
            # the deeper container. This chooses a body core without removing
            # other retained body text from the auditable full snapshot.
            core = max(dense, key=lambda node: (
                sum(len(_clean(str(s))) for s in node.strings if not s.find_parent('a')),
                len(list(node.parents))))
    parts, blocks, cursor = [], [], 0
    core_start = None
    def emit(value, kind, node, table=None):
        nonlocal cursor, core_start
        value = _clean(value)
        if not value:
            return
        if parts:
            cursor += 1
        start = cursor
        if core_start is None and (core is None or node is core or core in node.parents):
            core_start = start
        parts.append(value)
        cursor += len(value)
        block = {'start': start, 'end': cursor, 'kind': kind}
        if table is not None:
            block['table_id'] = table
        if kind == 'heading':
            block['level'] = int(node.name[1])
        blocks.append(block)
    def walk(node):
        if isinstance(node, NavigableString):
            emit(str(node), 'paragraph', node)
            return
        if not isinstance(node, Tag):
            return
        if node.name == 'table':
            table_id = f'table-{len(blocks)}'
            caption = node.find('caption')
            if caption:
                emit(caption.get_text(' ', strip=True), 'table_caption', caption, table_id)
            for row in node.find_all('tr'):
                if row.find_parent('table') is not node:
                    continue
                cells = row.find_all(['th', 'td'], recursive=False)
                if cells:
                    emit(' | '.join(cell.get_text(' ', strip=True) for cell in cells),
                         'table_header' if all(cell.name == 'th' for cell in cells) or row.find_parent('thead') is not None else 'table_row', row, table_id)
            return
        if is_note(node):
            emit(node.get_text(' ', strip=True), 'footnote', node)
            return
        if re.fullmatch(r'h[1-6]', node.name):
            emit(node.get_text(' ', strip=True), 'heading', node)
            return
        if node.name in {'p', 'pre', 'li', 'dt', 'dd', 'blockquote'} and not node.find(['table', 'p', 'li']):
            emit(node.get_text(' ', strip=True), 'paragraph', node)
            return
        for child in node.children:
            walk(child)
    walk(root)
    text = '\n'.join(parts)
    if not text:
        raise EmptyContent('HTML body is empty after removing navigation')
    return {'text': text, 'title': title, 'core_start': core_start or 0, 'blocks': blocks}


def chunk_text(text, blocks=None, *, max_chars=8000):
    """Partition the entire body; repeat table/headings/notes by position only.

    Normal rows/paragraphs stay whole. An oversized single block is split with
    explicit continuation metadata, never dropped. Primary ranges partition
    the full text exactly; context ranges are source references, not coverage.
    """
    if not isinstance(text, str):
        raise ValueError('chunks require text')
    _integer(max_chars, 32, 60000, 'max_chars')
    blocks = blocks or [{'start': m.start(), 'end': m.end(), 'kind': 'paragraph'}
                        for m in re.finditer(r'[^\n]+', text)]
    for block in blocks:
        _integer(block['start'], 0, len(text), 'block start')
        _integer(block['end'], block['start'], len(text), 'block end')
    chunks, start = [], 0
    while start < len(text):
        maximum = min(start + max_chars, len(text))
        # Include separators in the preceding range so nothing disappears.
        boundaries = [min(len(text), b['end'] + (text[b['end']:b['end'] + 1] == '\n'))
                      for b in blocks if start < b['end'] <= maximum]
        end = max((b for b in boundaries if b <= maximum), default=maximum)
        overlapping = [b for b in blocks if b['start'] < end and b['end'] > start]
        tables = {b.get('table_id') for b in overlapping if b.get('table_id')}
        references = [b for b in blocks if b['kind'] == 'footnote' or
                      (b.get('table_id') in tables and b['kind'] in {'table_header', 'table_caption'})]
        headings = {}
        for block in blocks:
            if block['kind'] == 'heading' and block['start'] <= start:
                level = block.get('level', 1)
                headings = {k: v for k, v in headings.items() if k < level}
                headings[level] = block
        references.extend(headings.values())
        refs = sorted({(b['start'], b['end'], b['kind']) for b in references})
        continuation = any(b['start'] < start or b['end'] > end for b in overlapping)
        chunks.append({'start': start, 'end': end, 'continuation': continuation,
                       'context_refs': [{'start': a, 'end': b, 'kind': kind} for a, b, kind in refs]})
        start = end
    return chunks


def coverage_ranges(length, updates=()):
    """Latest explicit outcome wins per character; untouched text is pending."""
    _integer(length, 0, 100_000_000, 'body length')
    boundaries = {0, length}
    for update in updates:
        _integer(update['start'], 0, length, 'coverage start')
        _integer(update['end'], update['start'], length, 'coverage end')
        if update['status'] not in {'pending', 'processed', 'failed'}:
            raise ValueError('unknown coverage status')
        boundaries.update((update['start'], update['end']))
    ranges = {'pending': [], 'processed': [], 'failed': []}
    ordered = sorted(boundaries)
    for start, end in zip(ordered, ordered[1:]):
        if start == end:
            continue
        status = 'pending'
        for update in updates:
            if update['start'] <= start and update['end'] >= end:
                status = update['status']
        target = ranges[status]
        if target and target[-1]['end'] == start:
            target[-1]['end'] = end
        else:
            target.append({'start': start, 'end': end})
    return {**ranges, 'body_length': length, 'complete': not ranges['pending'] and not ranges['failed']}


class _PageFailure(Exception):
    pass


class _Deadline(requests.Timeout):
    pass


class PageService:
    """Public worker/tool API; preview, detail and chunks share saved evidence.

    A caller supplies the worker's cumulative RunBudget and active heartbeat.
    This service never starts a worker, accesses a business DB, or runs a model.
    """

    KEY = 'domain_pages.v1'

    def __init__(self, store, run_id, generation, budget, *, session=None,
                 resolver=resolve_host_addresses, monotonic=None, max_bytes=5 * 1024 * 1024,
                 fetch_seconds=30, proxy=None, max_redirects=5, backoff=None):
        if (budget.store is not store or budget.run_id != run_id or budget.generation != generation):
            raise ValueError('page budget/run/generation mismatch')
        _integer(max_bytes, 1, 50 * 1024 * 1024, 'max_bytes')
        if type(fetch_seconds) not in (int, float) or not math.isfinite(fetch_seconds) or not 0 < fetch_seconds <= 1800:
            raise ValueError('fetch_seconds must be finite in (0,1800]')
        self.store, self.run_id, self.generation, self.budget = store, run_id, generation, budget
        self.root = store.run_path(run_id)
        self.monotonic, self.fetch_seconds, self.max_bytes = monotonic or time.monotonic, fetch_seconds, max_bytes
        if backoff is not None and not callable(backoff):
            raise ValueError('backoff must be callable')
        self.backoff = backoff if backoff is not None else time.sleep
        self.fetcher = SourceRefresher(db_path=self.root / 'unused-fetch.db',
                    snapshot_dir=self.root / 'source_snapshots', session=session, resolver=resolver,
                    max_attempts=1, max_bytes=max_bytes, proxy=proxy, max_redirects=max_redirects)

    @staticmethod
    def _empty():
        return {'visits': {}, 'attempts': {}, 'snapshots': {}, 'batches': {},
                'details': {}, 'coverage': {}, 'coverage_requests': {}}

    def _state(self):
        return self.store.metadata(self.run_id, self.KEY) or self._empty()

    def _update(self, callback):
        return self.store.update_metadata(self.run_id, self.generation, self.KEY,
            lambda state: callback(state or self._empty()))

    def _artifact(self, name):
        if not re.fullmatch(r'[0-9a-f]{64}\.(raw|txt)', name):
            raise ValueError('unsafe snapshot filename')
        directory = self.root / 'source_snapshots'
        path = directory / name
        if directory.resolve() != directory or path.resolve() != path:
            raise ValueError('snapshot path cannot contain symlinks/junctions')
        if not path.resolve().is_relative_to(self.root):
            raise ValueError('snapshot path escapes run root')
        return path

    def _save_bytes(self, name, data):
        path = self._artifact(name)
        path.parent.mkdir(exist_ok=True)
        path = self._artifact(name)
        digest = hashlib.sha256(data).hexdigest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('immutable snapshot hash conflict')
            return name
        staging = path.parent / ('.page-' + uuid.uuid4().hex + '.tmp')
        try:
            with staging.open('xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic exclusive install: an old owner cannot overwrite evidence.
            try:
                os.link(staging, path)
            except FileExistsError:
                if hashlib.sha256(self._artifact(name).read_bytes()).hexdigest() != digest:
                    raise ValueError('immutable snapshot hash conflict')
        finally:
            staging.unlink(missing_ok=True)
        return name

    def read_snapshot(self, snapshot_id):
        snapshot_id = _key(snapshot_id)
        record = self._state()['snapshots'].get(snapshot_id)
        if record is None:
            raise ValueError('unknown saved page snapshot')
        raw_path, text_path = self._artifact(record['raw_ref']), self._artifact(record['text_ref'])
        try:
            raw, body = raw_path.read_bytes(), text_path.read_bytes()
        except OSError as exc:
            raise ValueError('saved snapshot missing; refetch is forbidden') from exc
        if hashlib.sha256(raw).hexdigest() != record['raw_hash'] or hashlib.sha256(body).hexdigest() != record['body_hash']:
            raise ValueError('saved snapshot hash mismatch; refetch is forbidden')
        return {**record, 'raw': raw, 'text': body.decode('utf-8'),
                'raw_path': str(raw_path), 'text_path': str(text_path)}

    def _capture(self, candidate, attempt):
        cid, url = candidate['candidate_id'], candidate['url']
        started = self.monotonic()
        current = self.budget.snapshot()
        remaining = (current['active_limit_ms'] - current['active_ms']) / 1000
        deadline = started + min(self.fetch_seconds, remaining)
        def remaining_seconds():
            seconds = deadline - self.monotonic()
            if seconds <= 0:
                raise _Deadline('cumulative fetch deadline exhausted')
            return seconds
        record = {'candidate_id': cid, 'requested_url': url, 'final_url': None,
                  'visited': True, 'extractable': False, 'status': 'in_progress',
                  'visited_at': self.store.clock(), 'method': 'http_get', 'http_status': None,
                  'raw_hash': None, 'body_hash': None, 'snapshot_id': None, 'preview': None,
                  'execution_id': uuid.uuid4().hex, 'attempt': attempt, 'generation': self.generation}
        def started_visit(state):
            state['attempts'].setdefault(cid, []).append(record)
            return state, None, {'kind': 'page_visit_started', 'stage': 'previewing',
                                 'candidate_id': cid, 'execution_id': record['execution_id'], 'attempt': attempt}
        self._update(started_visit)
        def finish_attempt(state):
            for index, saved in enumerate(state['attempts'][cid]):
                if saved['execution_id'] == record['execution_id']:
                    state['attempts'][cid][index] = record
                    return
            raise ValueError('page execution start record missing')
        response = None
        category = 'network_error'
        try:
            canonical_url(url)
            response = self.fetcher._get_with_safe_redirects(url, {}, remaining_seconds=remaining_seconds)
            record.update(final_url=canonical_url(str(response.url or url)), http_status=int(response.status_code))
            if not 200 <= record['http_status'] < 300:
                category = 'http_error'
                raise _PageFailure(f'HTTP {record["http_status"]}')
            mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if mime not in {'', 'text/html', 'application/xhtml+xml', 'text/plain', 'application/pdf', 'application/json', 'text/json'}:
                category = 'unsupported_mime'
                raise _PageFailure(f'unsupported content type: {mime}')
            raw = _read_response_body(response, self.max_bytes, remaining_seconds=remaining_seconds)
            record['raw_hash'] = hashlib.sha256(raw).hexdigest()
            raw_ref = self._save_bytes(_hash(_json([cid, record['raw_hash']])) + '.raw', raw)
            record['raw_ref'] = raw_ref
            encoding = _detect_encoding(raw, response.encoding)
            category = 'pdf_parse_error' if mime == 'application/pdf' or raw.startswith(b'%PDF-') else 'empty_body'
            if mime in {'', 'text/html', 'application/xhtml+xml'} and not raw.startswith(b'%PDF-'):
                rendered = render_html(raw, encoding)
            else:
                text = normalize_content(raw, mime, encoding)
                # PDF page markers alone cannot prove a text body was extracted.
                if (mime == 'application/pdf' or raw.startswith(b'%PDF-')) and not re.sub(r'\[第 \d+ 页\]|\s+', '', text):
                    raise EmptyContent('PDF contains no extractable text')
                rendered = {'text': text, 'title': '', 'core_start': 0, 'blocks': []}
            remaining_seconds()
            body = rendered['text'].encode('utf-8')
            record['body_hash'] = hashlib.sha256(body).hexdigest()
            snapshot_id = _hash(_json([cid, record['raw_hash'], record['body_hash']]))
            text_ref = self._save_bytes(snapshot_id + '.txt', body)
            links = []
            for link in extract_outbound_links(raw, mime, encoding, record['final_url']):
                try:
                    links.append(canonical_url(link))
                except ValueError:
                    continue
            snapshot = {'snapshot_id': snapshot_id, 'candidate_id': cid, 'requested_url': url,
                        'final_url': record['final_url'], 'visited_at': record['visited_at'],
                        'http_status': record['http_status'], 'method': record['method'], 'mime': mime,
                        'raw_hash': record['raw_hash'], 'body_hash': record['body_hash'],
                        'raw_ref': raw_ref, 'text_ref': text_ref, 'body_length': len(rendered['text']),
                        'title': rendered['title'], 'core_start': rendered['core_start'],
                        'blocks': rendered['blocks'], 'outbound_links': links}
            record.update(status='success', extractable=True, snapshot_id=snapshot_id,
                          title=rendered['title'], preview=locate_preview(rendered['text'], rendered['core_start']))
            def success(state):
                state['snapshots'].setdefault(snapshot_id, snapshot)
                state['coverage'].setdefault(snapshot_id, [])
                state['visits'].setdefault(cid, record)
                finish_attempt(state)
                return state, record, {'kind': 'page_snapshot_saved', 'stage': 'previewing',
                                       'candidate_id': cid, 'snapshot_id': snapshot_id}
            self._update(success)
            return record
        except _Deadline as exc:
            category, error = 'deadline', exc
        except ConfigurationError as exc:
            category, error = 'unsafe_url', exc
        except ResponseTooLarge as exc:
            category, error = 'max_bytes', exc
        except UnsupportedContentType as exc:
            category, error = ('pdf_parser_missing' if 'PDF' in str(exc) else 'unsupported_mime'), exc
        except requests.exceptions.TooManyRedirects as exc:
            category, error = 'redirect_error', exc
        except requests.RequestException as exc:
            category, error = 'network_error', exc
        except (EmptyContent, _PageFailure) as exc:
            error = exc
        except (ValueError, OSError) as exc:
            category, error = 'snapshot_error', exc
        finally:
            if response is not None:
                response.close()
        record['status'] = 'failed'
        record['diagnostic'] = redact({'stage': 'previewing', 'error_category': category,
                        'error': str(error), 'attempt': attempt,
                        'recoverable': category == 'network_error' or
                            (category == 'http_error' and (record['http_status'] in {408, 425, 429} or record['http_status'] >= 500)),
                        'requested_url': url, 'final_url': record['final_url'], 'http_status': record['http_status']})
        def failure(state):
            finish_attempt(state)
            return state, record, {'kind': 'page_visit_failed', 'stage': 'previewing',
                                   'candidate_id': cid, 'diagnostic': record['diagnostic']}
        self._update(failure)
        raise _PageFailure(category)

    @staticmethod
    def _pending(candidate, reason):
        return {'candidate_id': candidate['candidate_id'], 'requested_url': candidate['url'],
                'visited': False, 'extractable': False, 'status': 'pending', 'reason': reason}

    def _pending_visit(self, candidate, reason):
        pending = self._pending(candidate, reason)
        attempts = self._state()['attempts'].get(candidate['candidate_id'], [])
        if attempts:
            previous = attempts[-1]
            pending.update(visited=True,
                           visited_at=previous['visited_at'], http_status=previous['http_status'],
                           final_url=previous['final_url'])
            if previous.get('diagnostic') is not None:
                pending['diagnostic'] = previous['diagnostic']
        return pending

    def _tick_active(self):
        """Bill the shared owned segment; runtime owns begin/end and heartbeat."""
        segment = self.budget.snapshot()['segment']
        if segment is not None and segment['token'] == self.budget.token:
            self.budget.tick()

    def _retry_wait(self, logical, failed_attempt):
        """A fenced stable receipt makes completed waits free on replay."""
        seconds = float(2 ** failed_attempt)
        request_id = 'page-backoff-' + _hash(_json([logical, failed_attempt]))
        payload = {'stage': 'previewing', 'kind': 'retry_backoff', 'logical_id': logical,
                   'failed_attempt': failed_attempt, 'seconds': seconds}
        def wait():
            self._tick_active()
            state = self.budget.snapshot()
            remaining = (state['active_limit_ms'] - state['active_ms']) / 1000
            if remaining <= 0:
                return {'status': 'budget_stopped', 'slept_seconds': 0}
            delay = min(seconds, remaining)
            try:
                self.backoff(delay)
            finally:
                self._tick_active()
            exhausted = self.budget.snapshot()['active_ms'] >= state['active_limit_ms']
            return {'status': 'budget_stopped' if exhausted else 'completed', 'slept_seconds': delay}
        receipt = self.store.operation(self.run_id, self.generation, request_id, payload, wait)
        if receipt['state'] != 'completed':
            return 'backoff_in_progress'
        return 'active_budget_exhausted' if receipt['result']['status'] == 'budget_stopped' else None

    def visit(self, candidate):
        """Visit a backend-owned candidate once, with <=3 accounted attempts."""
        cid = _key(candidate['candidate_id'])
        url = canonical_url(candidate['url'])
        candidate = {'candidate_id': cid, 'url': url}
        state = self._state()
        if cid in state['visits']:
            saved = state['visits'][cid]
            if saved['requested_url'] != url:
                raise ValueError('candidate URL conflict')
            if saved['extractable']:
                self.read_snapshot(saved['snapshot_id'])
            return saved
        logical = 'page-fetch-' + _hash(_json([cid, url]))
        for attempt in range(3):
            try:
                receipt = self.budget.operation(logical, {'stage': 'previewing', 'candidate_id': cid, 'url': url},
                            lambda: self._capture(candidate, attempt), attempt=attempt)
            except ValueError as exc:
                if str(exc) == 'active budget exhausted':
                    return self._pending_visit(candidate, 'active_budget_exhausted')
                if str(exc) == 'technical retry budget exhausted':
                    break
                raise
            if receipt['state'] in {'running', 'pending'}:
                return self._pending_visit(candidate, 'in_progress')
            if receipt['state'] == 'completed':
                return self._state()['visits'][cid]
            # A replayed failed receipt may precede a later unknown fetch.
            # Its retry policy belongs to that attempt's confirmed diagnostic,
            # not the most recent started record for the whole page.
            last = [record for record in self._state()['attempts'].get(cid, [])
                    if record['attempt'] == attempt and record.get('diagnostic') is not None]
            if last and not last[-1]['diagnostic']['recoverable']:
                break
            if attempt < 2:
                stopped = self._retry_wait(logical, attempt)
                if stopped is not None:
                    return self._pending_visit(candidate, stopped)
        attempts = self._state()['attempts'].get(cid, [])
        if not attempts:
            return self._pending_visit(candidate, 'retry_budget_exhausted')
        last = attempts[-1]
        if last['status'] == 'in_progress':
            return self._pending_visit(candidate, 'retry_budget_exhausted')
        def terminal(state):
            state['visits'].setdefault(cid, last)
            return state, state['visits'][cid], None
        return self._update(terminal)

    @staticmethod
    def _cursor(version, cursor, offset):
        if cursor is not None:
            if not isinstance(cursor, dict) or set(cursor) != {'list_version', 'offset'} or cursor['list_version'] != version or offset != 0:
                raise ValueError('cursor list_version/offset mismatch')
            offset = cursor['offset']
        if type(offset) is not int or offset < 0:
            raise ValueError('cursor offset must be a nonnegative integer')
        return offset

    @staticmethod
    def _preview_item(record):
        # Full titles, diagnostics and block metadata remain behind stable refs.
        result = {key: value for key, value in record.items() if key not in {'raw_ref', 'title', 'diagnostic'}}
        if 'title' in record:
            result['title'] = record['title'][:1000]
        if 'diagnostic' in record:
            result['diagnostic'] = {k: v for k, v in record['diagnostic'].items()
                                    if k not in {'error', 'requested_url', 'final_url'}}
            result['diagnostic']['error'] = record['diagnostic']['error'][:1000]
        result['page_ref'] = None
        if record['visited']:
            collection = 'attempts' if record['status'] == 'pending' else 'visits'
            result['page_ref'] = f'{PageService.KEY}/{collection}/{record["candidate_id"]}'
        return result

    def preview(self, list_version, *, cursor=None, offset=0, limit=10):
        """Return actual visits for one saved search version, <=10 whole items."""
        version = _key(list_version)
        offset = self._cursor(version, cursor, offset)
        _integer(limit, 1, 10, 'limit')
        search = self.store.metadata(self.run_id, SearchExecutor.KEY) or {}
        saved = search.get('versions', {}).get(version)
        if saved is None:
            raise ValueError('unknown saved search list version')
        candidates = saved['items']
        _integer(offset, 0, len(candidates), 'offset')
        batch_id = _hash(_json([version, offset, limit]))
        cached = self._state()['batches'].get(batch_id)
        if cached is not None:
            for item in cached['items']:
                if item.get('extractable'):
                    self.read_snapshot(item['snapshot_id'])
            return cached
        base = {'list_version': version, 'stage': 'previewing', 'mode': 'initial',
                'list_ref': f'{SearchExecutor.KEY}/versions/{version}'}
        items = [self._pending(c, 'not_executed') for c in candidates]
        response = serialize_page(items, offset, base, limit=limit)
        for index in range(offset, min(len(candidates), offset + limit)):
            record = self.visit(candidates[index])
            items[index] = self._preview_item(record)
            tentative = serialize_page(items, offset, base, limit=min(index - offset + 1, limit))
            # A visited item that does not fit is still durably saved, and is
            # returned by the next cursor without a repeated physical fetch.
            if len(tentative['items']) < index - offset + 1:
                response = tentative
                break
            response = tentative
            if record['status'] == 'pending':
                break
        if all(item['status'] != 'pending' for item in response['items']):
            def save(state):
                state['batches'].setdefault(batch_id, response)
                return state, state['batches'][batch_id], None
            response = self._update(save)
        return response

    def detail(self, request_id, snapshot_id, *, offset=0, length=4000):
        """Read saved body only; same request/payload is free, max3 per snapshot."""
        request_id, snapshot_id = _key(request_id), _key(snapshot_id)
        snapshot = self.read_snapshot(snapshot_id)
        _integer(offset, 0, len(snapshot['text']), 'offset')
        _integer(length, 1, 4000, 'length')
        payload = {'snapshot_id': snapshot_id, 'offset': offset, 'length': length}
        prior = self._state()['details'].get(request_id)
        if prior is not None:
            if prior['payload'] != payload:
                raise ValueError('detail payload conflict')
            if prior.get('response') is not None:
                return prior['response']
        end = min(len(snapshot['text']), offset + length)
        response = serialize_page([{'snapshot_id': snapshot_id, 'start': offset, 'end': end,
                                    'text': snapshot['text'][offset:end], 'body_hash': snapshot['body_hash']}],
                                  0, {'list_version': snapshot_id, 'mode': 'detail'})
        def register(state):
            old = state['details'].setdefault(request_id, {'payload': payload, 'response': None})
            if old['payload'] != payload:
                raise ValueError('detail payload conflict')
            return state, None, None
        self._update(register)
        self.budget.reserve_detail('page-detail-' + _hash(request_id), snapshot_id)
        def finish(state):
            state['details'][request_id]['response'] = response
            return state, response, {'kind': 'page_detail_read', 'stage': 'previewing',
                                    'snapshot_id': snapshot_id, 'start': offset, 'end': end}
        return self._update(finish)

    def chunks(self, snapshot_id, *, max_chars=8000):
        """Offer every body range to future model stages; none is auto processed."""
        snapshot = self.read_snapshot(snapshot_id)
        return {'snapshot_id': snapshot_id, 'body_hash': snapshot['body_hash'],
                'chunks': chunk_text(snapshot['text'], snapshot['blocks'], max_chars=max_chars),
                'coverage': coverage_ranges(len(snapshot['text']), self._state()['coverage'][snapshot_id])}

    def mark_coverage(self, request_id, snapshot_id, start, end, status):
        request_id = _key(request_id)
        snapshot = self.read_snapshot(snapshot_id)
        update = {'start': start, 'end': end, 'status': status}
        coverage_ranges(len(snapshot['text']), [update])
        payload = {'snapshot_id': snapshot_id, **update}
        def save(state):
            prior = state['coverage_requests'].get(request_id)
            if prior is not None and prior != payload:
                raise ValueError('coverage payload conflict')
            if prior is None:
                state['coverage_requests'][request_id] = payload
                state['coverage'][snapshot_id].append(update)
            result = coverage_ranges(len(snapshot['text']), state['coverage'][snapshot_id])
            return state, result, None if prior is not None else {'kind': 'page_coverage', 'stage': 'extracting', **payload}
        return self._update(save)
