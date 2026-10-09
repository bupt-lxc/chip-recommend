"""Real safe HTTP path, driven only by controlled in-memory responses."""

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import requests


def test_pages_module_exists():
    assert importlib.util.find_spec('chip_model.pipeline.domain_discovery.pages') is not None


HTML = '''<html><head><title>Vendor accelerator specifications</title></head><body>
<nav>导航 登录 无关菜单</nav><script>secret script</script>
<main><h1>Product accelerator</h1><p>Two distinct GPU SKUs. Memory and power below.</p>
<table><caption>Specifications</caption><tr><th>SKU</th><th>Memory (GB)</th><th>TDP (W)</th></tr>
<tr><td>X100-A</td><td>24</td><td>150*</td></tr><tr><td>X100-B</td><td>48</td><td>300*</td></tr></table>
<a href="#power-note">*</a></main><footer>Copyright 2026
<p id="power-note">* TDP is per accelerator, not server total.</p></footer></body></html>'''


class Response:
    def __init__(self, body=HTML.encode(), status=200, url='https://vendor.example/spec', mime='text/html', headers=None, step=None):
        self.body, self.status_code, self.url = body, status, url
        self.headers = {'Content-Type': mime, **(headers or {})}
        self.encoding, self.closed, self.step = 'utf-8', False, step

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self.body), chunk_size):
            if self.step:
                self.step()
            yield self.body[start:start + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def setup(tmp_path):
    from chip_model.pipeline.domain_discovery.store import RunStore
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    clock = Clock()
    store = RunStore(tmp_path / 'runs', clock=clock)
    store.start('start', {'domain': 'specs'}, 'run-a')
    generation = store.claim('run-a', 'worker', 100000)
    budget = RunBudget(store, 'run-a', generation, monotonic=clock, epoch=clock)
    return store, generation, budget, clock


def service(setup, responses, **kwargs):
    from chip_model.pipeline.domain_discovery.pages import PageService
    store, generation, budget, clock = setup
    session = Session(responses)
    reader = PageService(store, 'run-a', generation, budget, session=session,
                         resolver=kwargs.pop('resolver', lambda _: ['93.184.216.34']),
                         monotonic=clock, **kwargs)
    return reader, session


def save_list(setup, count=1, version='list-1'):
    from chip_model.pipeline.domain_discovery.search import SearchExecutor
    store, generation, _, _ = setup
    items = [{'candidate_id': f'candidate-{i}', 'url': f'https://vendor.example/spec/{i}',
              'title': 'Search title', 'snippet': 'Search snippet is not a preview'} for i in range(count)]
    store.update_metadata('run-a', generation, SearchExecutor.KEY,
                          lambda state: ({'versions': {version: {'items': items}}}, None, None))
    return version


@pytest.mark.parametrize('start', [0, 6, 50, 801])
def test_preview_is_unicode_slice_at_actual_core(start):
    from chip_model.pipeline.domain_discovery.pages import locate_preview
    text = '导航登录\n\n' + '核心🙂产品规格' * 100
    start = min(start, len(text))
    result = locate_preview(text, start)
    assert result['start'] == start
    assert 0 <= result['start'] <= result['end'] <= len(text)
    assert result['text'] == text[result['start']:result['end']]
    assert len(result['text']) <= 500
    if start == 6:
        assert result['text'].startswith('核心🙂')


def test_html_preserves_table_rows_units_required_footer_note():
    from chip_model.pipeline.domain_discovery.pages import render_html, chunk_text
    rendered = render_html(HTML.encode(), 'utf-8')
    text = rendered['text']
    assert rendered['title'] == 'Vendor accelerator specifications'
    assert '导航' not in text and 'secret script' not in text and 'Copyright' not in text
    assert 'X100-A | 24 | 150*' in text and 'X100-B | 48 | 300*' in text
    assert 'Memory (GB)' in text and 'TDP (W)' in text
    assert '* TDP is per accelerator, not server total.' in text
    chunks = chunk_text(text, rendered['blocks'], max_chars=70)
    assert ''.join(text[c['start']:c['end']] for c in chunks) == text
    for row in ['X100-A', 'X100-B']:
        chunk = next(c for c in chunks if row in text[c['start']:c['end']])
        contexts = [text[r['start']:r['end']] for r in chunk['context_refs']]
        assert any('Memory (GB)' in context for context in contexts)
        assert any('per accelerator' in context for context in contexts)


def test_preview_snapshot_and_resume_never_refetch(setup):
    reader, session = service(setup, [Response(url='https://vendor.example/spec/0')])
    version = save_list(setup)
    first = reader.preview(version)
    item = first['items'][0]
    assert item['visited'] and item['extractable'] and item['status'] == 'success'
    snapshot = reader.read_snapshot(item['snapshot_id'])
    assert item['preview']['text'] == snapshot['text'][item['preview']['start']:item['preview']['end']]
    assert item['preview']['text'] != 'Search snippet is not a preview'
    assert hashlib.sha256(snapshot['raw']).hexdigest() == item['raw_hash']
    restarted, empty = service(setup, [])
    assert restarted.preview(version) == first
    assert len(session.calls) == 1 and empty.calls == []
    assert not (reader.root / 'unused-fetch.db').exists()


def test_detail_reads_three_times_cache_and_conflict(setup):
    reader, session = service(setup, [Response(body=('正文🙂' * 3000).encode(), mime='text/plain')])
    snapshot_id = reader.preview(save_list(setup))['items'][0]['snapshot_id']
    first = reader.detail('detail-1', snapshot_id, offset=0)
    assert len(first['items'][0]['text']) <= 4000
    assert len(json.dumps(first, ensure_ascii=False)) <= 12000
    assert reader.detail('detail-1', snapshot_id, offset=0) == first
    with pytest.raises(ValueError, match='conflict'):
        reader.detail('detail-1', snapshot_id, offset=1)
    reader.detail('detail-2', snapshot_id, offset=4000)
    reader.detail('detail-3', snapshot_id, offset=8000)
    with pytest.raises(ValueError, match='detail budget exhausted'):
        reader.detail('detail-4', snapshot_id, offset=0)
    assert len(session.calls) == 1


def test_long_body_complete_chunks_and_explicit_coverage(setup):
    reader, _ = service(setup, [Response(body=(('Long paragraph 24 GB.\n' * 4000) + 'LATE FOOTNOTE 300 W').encode(), mime='text/plain')])
    item = reader.preview(save_list(setup))['items'][0]
    result = reader.chunks(item['snapshot_id'], max_chars=900)
    text = reader.read_snapshot(item['snapshot_id'])['text']
    assert len(text) > 60000 and 'LATE FOOTNOTE' in text
    assert ''.join(text[c['start']:c['end']] for c in result['chunks']) == text
    assert result['coverage']['pending'] == [{'start': 0, 'end': len(text)}]
    assert not result['coverage']['complete']
    reader.mark_coverage('mark-1', item['snapshot_id'], 0, 500, 'processed')
    reader.mark_coverage('mark-2', item['snapshot_id'], 400, 800, 'processed')
    reader.mark_coverage('mark-3', item['snapshot_id'], 800, 900, 'failed')
    coverage = reader.chunks(item['snapshot_id'])['coverage']
    assert coverage['processed'] == [{'start': 0, 'end': 800}]
    assert coverage['failed'] == [{'start': 800, 'end': 900}]
    assert coverage['pending'] == [{'start': 900, 'end': len(text)}]


@pytest.mark.parametrize('body,mime,status,category', [
    (b'', 'text/html', 200, 'empty_body'),
    (b'failure', 'text/html', 503, 'http_error'),
    (b'\x89PNG', 'image/png', 200, 'unsupported_mime'),
    (b'%PDF-garbage', 'application/pdf', 200, 'pdf_parse_error'),
])
def test_failures_are_actual_visits_closed_and_not_extractable(setup, body, mime, status, category):
    responses = [Response(body=body, mime=mime, status=status) for _ in range(3)]
    reader, session = service(setup, responses)
    result = reader.preview(save_list(setup))['items'][0]
    assert result['visited'] and not result['extractable']
    assert result['diagnostic']['error_category'] == category
    assert result['diagnostic']['stage'] == 'previewing'
    assert all(r.closed for r in responses)
    assert len(session.calls) == 3
    assert reader.preview('list-1')['items'][0] == result
    assert len(session.calls) == 3


def test_network_failure_redacted_and_retry_budget_three(setup):
    reader, session = service(setup, [requests.ConnectionError('Authorization: Basic c2VjcmV0\nhttps://user:pass@proxy.example') for _ in range(3)])
    result = reader.preview(save_list(setup))['items'][0]
    assert result['visited'] and result['diagnostic']['error_category'] == 'network_error'
    serialized = json.dumps(setup[0].metadata('run-a', reader.KEY))
    assert 'c2VjcmV0' not in serialized and 'user:pass' not in serialized
    assert len(session.calls) == 3


def test_private_dns_and_private_redirect_never_reach_private_host(setup):
    reader, session = service(setup, [], resolver=lambda _: ['127.0.0.1'])
    item = reader.preview(save_list(setup))['items'][0]
    assert item['diagnostic']['error_category'] == 'unsafe_url'
    assert session.calls == []


def test_redirect_revalidates_and_closes_every_response(setup):
    responses = [Response(status=302, headers={'Location': 'http://127.0.0.1/secret'}) for _ in range(3)]
    reader, session = service(setup, responses)
    item = reader.preview(save_list(setup))['items'][0]
    assert item['diagnostic']['error_category'] == 'unsafe_url'
    assert all(response.closed for response in responses)
    assert len(session.calls) == 3


def test_body_limit_and_cumulative_deadline_close_responses(setup):
    responses = [Response(body=b'x' * 1025, mime='text/plain') for _ in range(3)]
    reader, _ = service(setup, responses, max_bytes=1024)
    assert reader.preview(save_list(setup))['items'][0]['diagnostic']['error_category'] == 'max_bytes'
    assert all(r.closed for r in responses)


def test_deadline_during_stream_is_total_budget_not_per_read(setup):
    clock = setup[3]
    response = Response(body=b'x' * 5000, mime='text/plain', step=lambda: clock.advance(0.6))
    reader, session = service(setup, [response], fetch_seconds=1)
    item = reader.preview(save_list(setup))['items'][0]
    assert item['diagnostic']['error_category'] == 'deadline'
    assert response.closed and len(session.calls) == 1


def test_active_budget_stop_preserves_pending_not_fake_visit(setup):
    budget = setup[2]
    budget.begin_active()
    setup[3].advance(1800)
    reader, session = service(setup, [])
    item = reader.preview(save_list(setup))['items'][0]
    assert item['status'] == 'pending' and not item['visited'] and not item['extractable']
    assert item['page_ref'] is None
    assert session.calls == []


def test_pagination_of_twelve_pages_cursor_bound_and_cached(setup):
    reader, session = service(setup, [Response() for _ in range(12)])
    version = save_list(setup, 12)
    first = reader.preview(version)
    assert len(first['items']) <= 10 and first['has_more']
    assert len(json.dumps(first, ensure_ascii=False)) <= 12000
    assert reader.preview(version) == first
    second = reader.preview(version, cursor=first['next_cursor'])
    assert len(first['items']) + len(second['items']) == 12
    assert len(session.calls) == 12
    with pytest.raises(ValueError, match='cursor'):
        reader.preview(version, cursor={'list_version': 'other', 'offset': 1})


def test_snapshot_tampering_and_symlink_escape_never_refetch(setup, tmp_path):
    reader, session = service(setup, [Response()])
    item = reader.preview(save_list(setup))['items'][0]
    snapshot = reader.read_snapshot(item['snapshot_id'])
    Path(snapshot['text_path']).write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='hash'):
        reader.detail('detail', item['snapshot_id'])
    assert len(session.calls) == 1


def test_pdf_missing_parser_is_explicit(setup, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    reader, _ = service(setup, [Response(body=b'%PDF-', mime='application/pdf') for _ in range(3)])
    assert reader.preview(save_list(setup))['items'][0]['diagnostic']['error_category'] == 'pdf_parser_missing'


def test_pdf_fixture_extracts_real_text(setup):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 10 280 Td (X100-A Memory 24 GB TDP 150 W) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    reader, _ = service(setup, [Response(body=output.getvalue(), mime='application/pdf')])
    item = reader.preview(save_list(setup))['items'][0]
    assert item['extractable'] and 'X100-A Memory 24 GB' in item['preview']['text']


def test_body_dense_core_and_heading_hierarchy_keep_full_text():
    from chip_model.pipeline.domain_discovery.pages import render_html, locate_preview
    html = b'<body><header><h1>Site welcome</h1></header><aside>Account Help</aside><section><h2>GPU specifications</h2><p>X100 memory 24 GB, TDP 150 W.</p></section></body>'
    result = render_html(html, 'utf-8')
    assert 'Site welcome' in result['text'] and 'Account Help' in result['text']
    assert locate_preview(result['text'], result['core_start'])['text'].startswith('GPU specifications')


def test_snapshot_directory_symlink_is_rejected(setup, tmp_path):
    reader, session = service(setup, [Response() for _ in range(3)])
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        (reader.root / 'source_snapshots').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('host does not permit creating symlinks')
    item = reader.preview(save_list(setup))['items'][0]
    assert not item['extractable'] and item['diagnostic']['error_category'] == 'snapshot_error'
    assert list(outside.iterdir()) == []


def test_resumed_fetch_failure_reuses_confirmed_failure_receipts(setup):
    reader, session = service(setup, [requests.ConnectionError('down') for _ in range(3)])
    first = reader.preview(save_list(setup))
    store, generation, _, clock = setup
    store.release('run-a', generation)
    newer = store.claim('run-a', 'next-worker', 100000)
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    budget = RunBudget(store, 'run-a', newer, monotonic=clock, epoch=clock)
    resumed, fresh_session = service((store, newer, budget, clock), [])
    assert resumed.preview('list-1') == first
    assert fresh_session.calls == [] and len(session.calls) == 3


def test_final_redirect_url_dns_error_always_closes_response(setup):
    responses = [Response(url='https://private.example/spec') for _ in range(3)]
    reader, session = service(setup, responses, resolver=lambda host: ['127.0.0.1'] if host == 'private.example' else ['93.184.216.34'])
    assert reader.preview(save_list(setup))['items'][0]['diagnostic']['error_category'] == 'unsafe_url'
    assert all(response.closed for response in responses)
    assert len(session.calls) == 3


def test_serialized_oversize_whole_item_is_explicit_and_saved(setup):
    from chip_model.pipeline.domain_discovery.search import OversizeItemError, SearchExecutor
    store, generation, _, _ = setup
    url = 'https://vendor.example/spec?q=' + 'a' * 12000
    candidate = {'candidate_id': 'large', 'url': url}
    store.update_metadata('run-a', generation, SearchExecutor.KEY,
                          lambda state: ({'versions': {'large-list': {'items': [candidate]}}}, None, None))
    reader, session = service(setup, [Response(url=url)])
    with pytest.raises(OversizeItemError):
        reader.preview('large-list')
    # The preflight can deny a row too large even before HTTP; otherwise any
    # completed physical visit must be persisted, never silently discarded.
    assert not session.calls or 'large' in store.metadata('run-a', reader.KEY)['visits']


def test_fetch_deadline_shrinks_underlying_socket_read_timeout():
    from chip_model.pipeline.source_refresh import _read_response_body
    class Socket:
        timeouts = []
        def settimeout(self, seconds):
            self.timeouts.append(seconds)
    from types import SimpleNamespace
    socket = Socket()
    response = Response(body=b'ab', mime='text/plain')
    response.raw = SimpleNamespace(_fp=SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=socket))))
    remaining = iter([3.0, 2.0, 1.0, 0.5, 0.25])
    assert _read_response_body(response, 100, remaining_seconds=lambda: next(remaining)) == b'ab'
    assert socket.timeouts and min(socket.timeouts) <= 1.0


def test_malformed_redirect_closes_response(setup):
    responses = [Response(status=302, headers={'Location': 'http://['}) for _ in range(3)]
    reader, _ = service(setup, responses)
    assert not reader.preview(save_list(setup))['items'][0]['extractable']
    assert all(response.closed for response in responses)


def test_elapsed_failure_preserves_actual_visit_when_retry_budget_stops(setup):
    clock = setup[3]
    budget = setup[2]
    budget.begin_active()
    reader, session = service(setup, [])
    def fail_after_elapsed(url, **kwargs):
        session.calls.append((url, kwargs))
        clock.advance(1800)
        raise requests.ConnectionError('connection lost after wait')
    session.get = fail_after_elapsed
    item = reader.preview(save_list(setup))['items'][0]
    assert item['status'] == 'pending' and item['visited']
    assert item['diagnostic']['error_category'] == 'network_error'
    assert not item['extractable'] and len(session.calls) == 1


def test_redirect_limit_has_distinct_diagnostic_and_no_hidden_retries(setup):
    responses = [Response(status=302, headers={'Location': '/again'}) for _ in range(6)]
    reader, session = service(setup, responses, max_redirects=1)
    item = reader.preview(save_list(setup))['items'][0]
    assert item['diagnostic']['error_category'] == 'redirect_error'
    assert len(session.calls) == 6 and all(response.closed for response in responses)
    operations = setup[2].snapshot()['logical_operations']
    assert list(operations.values())[0]['execution_count'] == 3


def test_pdf_magic_with_html_mime_blank_page_is_not_extractable(setup):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    output = io.BytesIO()
    writer.write(output)
    reader, _ = service(setup, [Response(body=output.getvalue(), mime='text/html') for _ in range(3)])
    item = reader.preview(save_list(setup))['items'][0]
    assert not item['extractable'] and item['diagnostic']['error_category'] == 'pdf_parse_error'


def test_unknown_crash_attempts_are_visible_and_share_three_physical_starts(setup):
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    store, generation, budget, clock = setup
    version = save_list(setup)
    total_calls = 0
    for index in range(3):
        reader, session = service((store, generation, budget, clock), [])
        def crash(url, **kwargs):
            session.calls.append((url, kwargs))
            raise KeyboardInterrupt('simulated process termination')
        session.get = crash
        with pytest.raises(KeyboardInterrupt):
            reader.preview(version)
        pending = reader.preview(version)['items'][0]
        assert pending['status'] == 'pending' and pending['visited']
        assert not pending['extractable']
        assert '/attempts/' in pending['page_ref']
        total_calls += len(session.calls)
        store.release('run-a', generation)
        generation = store.claim('run-a', f'worker-{index}', 100000)
        store.recover_operations('run-a', generation)
        budget = RunBudget(store, 'run-a', generation, monotonic=clock, epoch=clock)
    final, empty = service((store, generation, budget, clock), [])
    item = final.preview(version)['items'][0]
    assert item['status'] == 'pending' and item['visited']
    assert item['reason'] == 'retry_budget_exhausted'
    assert total_calls == 3 and empty.calls == []
