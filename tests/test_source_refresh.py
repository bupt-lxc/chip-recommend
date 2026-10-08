"""Tests for the check-only incremental source refresh pipeline."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest
import requests

from chip_model.pipeline.source_refresh import (
    RefreshAlreadyRunning,
    SourceRefresher,
    business_table_fingerprints,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: bytes = b"",
        *,
        url: str = "https://vendor.example/spec",
        headers: dict | None = None,
        encoding: str | None = "utf-8",
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.url = url
        self.headers = headers or {}
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size: int = 65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.headers: dict = {}
        self.max_redirects = 30
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    path = tmp_path / "source-refresh.db"
    schema = (Path(__file__).parent.parent / "schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute(
            "INSERT INTO link_library "
            "(id, url, description, vendor, category, created_at, updated_at) "
            "VALUES (1, 'https://vendor.example/spec', 'official chip spec', "
            "'Vendor', '芯片官方规格', '2026-09-07T00:00:00Z', '2026-09-07T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO chips (id, chip_model, vendor, created_at) "
            "VALUES (1, 'TestChip', 'Vendor', '2026-09-07T00:00:00Z')"
        )
        db.commit()
    return path


def make_refresher(
    temp_db: Path,
    tmp_path: Path,
    responses: list,
    **kwargs,
) -> tuple[SourceRefresher, FakeSession]:
    session = FakeSession(responses)
    refresher = SourceRefresher(
        db_path=temp_db,
        snapshot_dir=tmp_path / "snapshots",
        session=session,
        sleep_fn=lambda _: None,
        resolver=kwargs.pop("resolver", lambda _: ["93.184.216.34"]),
        **kwargs,
    )
    return refresher, session


def fetch_row(path: Path, sql: str, params: tuple = ()):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def test_dry_run_does_not_fetch_or_write(temp_db: Path, tmp_path: Path):
    before = business_table_fingerprints(temp_db)
    refresher, session = make_refresher(temp_db, tmp_path, [])

    summary = refresher.run("1")

    assert summary.mode == "dry-run"
    assert summary.counts == {"selected": 1}
    assert session.calls == []
    assert fetch_row(temp_db, "SELECT * FROM update_runs") is None
    assert not (tmp_path / "snapshots").exists()
    assert business_table_fingerprints(temp_db) == before


def test_first_200_creates_new_state_snapshot_and_no_business_change(
    temp_db: Path, tmp_path: Path
):
    before = business_table_fingerprints(temp_db)
    response = FakeResponse(
        200,
        b"<html><body><main><h1>Chip A</h1><p>VRAM 96 GB</p></main></body></html>",
        headers={"Content-Type": "text/html; charset=utf-8", "ETag": '"v1"'},
    )
    refresher, _ = make_refresher(temp_db, tmp_path, [response])

    summary = refresher.run([1], apply_state=True)

    assert summary.status == "success"
    assert summary.counts["new"] == 1
    result = summary.results[0]
    assert Path(result["snapshot_path"]).exists()
    assert Path(result["normalized_snapshot_path"]).read_text(encoding="utf-8") == (
        "Chip A\nVRAM 96 GB\n"
    )
    assert result["diff_path"] is None
    assert len(result["raw_hash"]) == 64
    assert len(result["content_hash"]) == 64
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["etag"] == '"v1"'
    assert state["failure_count"] == 0
    with sqlite3.connect(temp_db) as db:
        db.row_factory = sqlite3.Row
        events = [
            dict(row)
            for row in db.execute(
                "SELECT actor, stage, status, message, details_json "
                "FROM update_run_events ORDER BY id"
            ).fetchall()
        ]
    assert [(event["actor"], event["stage"]) for event in events] == [
        ("orchestrator", "run_started"),
        ("scheduler", "source_selection"),
        ("fetcher", "fetch"),
        ("fetcher", "fetch"),
        ("snapshot", "snapshot"),
        ("parser", "change_detection"),
        ("orchestrator", "run_finished"),
    ]
    assert json.loads(events[3]["details_json"])["http_status"] == 200
    assert json.loads(events[4]["details_json"])["snapshot_path"]
    assert events[-1]["status"] == "success"
    assert business_table_fingerprints(temp_db) == before


def test_streamed_body_with_default_latin1_encoding_uses_buffered_detection(
    temp_db: Path, tmp_path: Path
):
    body = "<main>显存容量 96 GB</main>".encode("utf-8")
    response = FakeResponse(
        200,
        body,
        headers={"Content-Type": "text/html"},
        encoding="ISO-8859-1",
    )
    refresher, _ = make_refresher(temp_db, tmp_path, [response])

    result = refresher.run([1], apply_state=True).results[0]

    assert result["outcome"] == "new"
    assert "显存容量 96 GB" in Path(result["normalized_snapshot_path"]).read_text(
        encoding="utf-8"
    )


def test_same_content_uses_conditional_header_and_does_not_duplicate_snapshot(
    temp_db: Path, tmp_path: Path
):
    body = b"<html><body><main>Stable specification</main></body></html>"
    first = FakeResponse(200, body, headers={"Content-Type": "text/html", "ETag": '"v1"'})
    refresher, _ = make_refresher(temp_db, tmp_path, [first])
    first_summary = refresher.run([1], apply_state=True)
    snapshot = Path(first_summary.results[0]["snapshot_path"])

    second = FakeResponse(200, body, headers={"Content-Type": "text/html", "ETag": '"v1"'})
    refresher, session = make_refresher(temp_db, tmp_path, [second])
    second_summary = refresher.run([1], apply_state=True, force=True)

    assert second_summary.counts["unchanged"] == 1
    assert second_summary.results[0]["snapshot_path"] is None
    assert second_summary.results[0]["normalized_snapshot_path"] is None
    assert session.calls[0]["headers"]["If-None-Match"] == '"v1"'
    assert snapshot.exists()
    assert len(list((tmp_path / "snapshots").rglob("*.html"))) == 1
    assert len(list((tmp_path / "snapshots" / "normalized").rglob("*.txt"))) == 1


def test_304_preserves_hash_and_validators(temp_db: Path, tmp_path: Path):
    body = b"<html><body><main>Stable specification</main></body></html>"
    initial = FakeResponse(
        200,
        body,
        headers={
            "Content-Type": "text/html",
            "ETag": '"v1"',
            "Last-Modified": "Sun, 07 Sep 2026 00:00:00 GMT",
        },
    )
    refresher, _ = make_refresher(temp_db, tmp_path, [initial])
    refresher.run([1], apply_state=True)
    old_state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")

    not_modified = FakeResponse(304, headers={"ETag": '"v1"'})
    refresher, session = make_refresher(temp_db, tmp_path, [not_modified])
    summary = refresher.run([1], apply_state=True, force=True)
    new_state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")

    assert summary.counts["unchanged"] == 1
    assert session.calls[0]["headers"] == {"If-None-Match": '"v1"'}
    assert new_state["raw_hash"] == old_state["raw_hash"]
    assert new_state["content_hash"] == old_state["content_hash"]
    assert new_state["last_modified"] == old_state["last_modified"]


def test_304_without_previous_success_is_rejected(temp_db: Path, tmp_path: Path):
    refresher, _ = make_refresher(temp_db, tmp_path, [FakeResponse(304)])

    summary = refresher.run([1], apply_state=True)

    assert summary.status == "partial"
    assert summary.results[0]["error_code"] == "unexpected_304"
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["content_hash"] is None


def test_last_modified_is_used_when_etag_is_unavailable(temp_db: Path, tmp_path: Path):
    modified = "Sun, 07 Sep 2026 00:00:00 GMT"
    first = FakeResponse(
        200,
        b"<main>Stable specification</main>",
        headers={"Content-Type": "text/html", "Last-Modified": modified},
    )
    refresher, _ = make_refresher(temp_db, tmp_path, [first])
    refresher.run([1], apply_state=True)

    refresher, session = make_refresher(temp_db, tmp_path, [FakeResponse(304)])
    summary = refresher.run([1], apply_state=True, force=True)

    assert summary.counts["unchanged"] == 1
    assert session.calls[0]["headers"] == {"If-Modified-Since": modified}


def test_redirect_to_private_ip_is_blocked_before_fetch(temp_db: Path, tmp_path: Path):
    redirect = FakeResponse(
        302,
        url="https://vendor.example/spec",
        headers={"Location": "http://127.0.0.1/admin"},
    )
    refresher, session = make_refresher(temp_db, tmp_path, [redirect])

    summary = refresher.run([1], apply_state=True)

    assert summary.status == "partial"
    assert summary.results[0]["error_code"] == "configuration_error"
    assert len(session.calls) == 1
    assert redirect.closed is True
    assert session.calls[0]["allow_redirects"] is False


def test_safe_redirect_is_followed(temp_db: Path, tmp_path: Path):
    redirect = FakeResponse(
        302,
        url="https://vendor.example/spec",
        headers={"Location": "/new-spec"},
    )
    final = FakeResponse(
        200,
        b"<main>Redirected specification</main>",
        url="https://vendor.example/new-spec",
        headers={"Content-Type": "text/html"},
    )
    refresher, session = make_refresher(temp_db, tmp_path, [redirect, final])

    summary = refresher.run([1], apply_state=True)

    assert summary.status == "success"
    assert summary.results[0]["final_url"] == "https://vendor.example/new-spec"
    assert [call["url"] for call in session.calls] == [
        "https://vendor.example/spec",
        "https://vendor.example/new-spec",
    ]
    assert redirect.closed is True


def test_changed_body_creates_new_snapshot(temp_db: Path, tmp_path: Path):
    old = FakeResponse(200, b"<main>VRAM 64 GB</main>", headers={"Content-Type": "text/html"})
    refresher, _ = make_refresher(temp_db, tmp_path, [old])
    refresher.run([1], apply_state=True)

    new = FakeResponse(200, b"<main>VRAM 96 GB</main>", headers={"Content-Type": "text/html"})
    refresher, _ = make_refresher(temp_db, tmp_path, [new])
    summary = refresher.run([1], apply_state=True, force=True)

    assert summary.counts["changed"] == 1
    result = summary.results[0]
    assert result["description"] == "official chip spec"
    assert result["vendor"] == "Vendor"
    assert result["category"] == "芯片官方规格"
    assert result["previous_content_hash"] != result["content_hash"]
    assert Path(result["diff_path"]).is_file()
    diff_text = Path(result["diff_path"]).read_text(encoding="utf-8")
    assert "-VRAM 64 GB" in diff_text
    assert "+VRAM 96 GB" in diff_text
    assert result["diff_added_lines"] == 1
    assert result["diff_removed_lines"] == 1
    assert "新增 1 行，删除 1 行" in result["diff_summary"]
    assert any(item["field"] == "vram_gb" for item in result["candidate_fields"])
    assert len(list((tmp_path / "snapshots").rglob("*.html"))) == 2
    stored = fetch_row(temp_db, "SELECT * FROM source_diffs WHERE link_id=1")
    assert stored["diff_path"] == result["diff_path"]
    assert '"field":"vram_gb"' in stored["candidate_fields_json"]


def test_changed_body_without_old_snapshot_reports_diff_unavailable(
    temp_db: Path, tmp_path: Path
):
    old = FakeResponse(200, b"<main>TDP 300 W</main>", headers={"Content-Type": "text/html"})
    refresher, _ = make_refresher(temp_db, tmp_path, [old])
    first = refresher.run([1], apply_state=True)
    Path(first.results[0]["snapshot_path"]).unlink()
    Path(first.results[0]["normalized_snapshot_path"]).unlink()

    new = FakeResponse(200, b"<main>TDP 350 W</main>", headers={"Content-Type": "text/html"})
    refresher, _ = make_refresher(temp_db, tmp_path, [new])
    summary = refresher.run([1], apply_state=True, force=True)

    result = summary.results[0]
    assert result["outcome"] == "changed"
    assert result["diff_path"] is None
    assert "历史规范化正文不可用" in result["diff_summary"]
    assert result["candidate_fields"] == []


def test_large_diff_is_truncated_but_counts_and_candidate_fields_are_kept(
    temp_db: Path, tmp_path: Path
):
    old_lines = "\n".join(f"old line {index}" for index in range(260))
    new_lines = "\n".join(
        [f"new line {index}" for index in range(260)] + ["FP16 800 TFLOPS"]
    )
    refresher, _ = make_refresher(
        temp_db,
        tmp_path,
        [FakeResponse(200, old_lines.encode(), headers={"Content-Type": "text/plain"})],
    )
    refresher.run([1], apply_state=True)
    refresher, _ = make_refresher(
        temp_db,
        tmp_path,
        [FakeResponse(200, new_lines.encode(), headers={"Content-Type": "text/plain"})],
    )

    result = refresher.run([1], apply_state=True, force=True).results[0]

    assert result["diff_truncated"] is True
    assert result["diff_added_lines"] == 261
    assert result["diff_removed_lines"] == 260
    assert Path(result["diff_path"]).stat().st_size < 25_000
    assert any(item["field"] == "precision_perf" for item in result["candidate_fields"])


@pytest.mark.parametrize(
    ("response", "outcome", "error_code"),
    [
        (FakeResponse(404), "failed", "http_404"),
        (FakeResponse(429, headers={"Retry-After": "120"}), "retry_wait", "http_429"),
        (FakeResponse(503), "retry_wait", "http_503"),
        (
            FakeResponse(200, b"image", headers={"Content-Type": "image/png"}),
            "unsupported",
            "unsupported_content_type",
        ),
        (
            FakeResponse(
                200,
                b"<html><script>x</script></html>",
                headers={"Content-Type": "text/html"},
            ),
            "failed",
            "empty_content",
        ),
    ],
)
def test_http_and_content_failures_are_classified(
    temp_db: Path, tmp_path: Path, response: FakeResponse, outcome: str, error_code: str
):
    refresher, _ = make_refresher(temp_db, tmp_path, [response], max_attempts=1)

    summary = refresher.run([1], apply_state=True)

    assert summary.status == "partial"
    assert summary.results[0]["outcome"] == outcome
    assert summary.results[0]["error_code"] == error_code
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["failure_count"] == 1
    assert state["content_hash"] is None


def test_pdf_without_parser_dependency_is_unsupported(
    temp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    response = FakeResponse(200, b"%PDF", headers={"Content-Type": "application/pdf"})
    refresher, _ = make_refresher(temp_db, tmp_path, [response], max_attempts=1)

    summary = refresher.run([1], apply_state=True)

    result = summary.results[0]
    assert summary.status == "partial"
    assert result["outcome"] == "unsupported"
    assert result["error_code"] == "unsupported_content_type"
    assert result["error_message"] == "PDF 解析组件未安装。"
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["failure_count"] == 1
    assert state["content_hash"] is None


def test_pdf_parse_failure_is_failed_content(
    temp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Isolate the optional parser boundary so installation/version cannot change classification.
    def reject_pdf(stream):
        raise ValueError("invalid PDF fixture")

    parser = ModuleType("pypdf")
    parser.PdfReader = reject_pdf
    monkeypatch.setitem(sys.modules, "pypdf", parser)
    response = FakeResponse(200, b"%PDF", headers={"Content-Type": "application/pdf"})
    refresher, _ = make_refresher(temp_db, tmp_path, [response], max_attempts=1)

    summary = refresher.run([1], apply_state=True)

    result = summary.results[0]
    assert summary.status == "partial"
    assert result["outcome"] == "failed"
    assert result["error_code"] == "empty_content"
    assert result["error_message"] == "PDF 文本提取失败：invalid PDF fixture。"
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["failure_count"] == 1
    assert state["content_hash"] is None


def test_timeout_retries_then_schedules_retry(temp_db: Path, tmp_path: Path):
    refresher, session = make_refresher(
        temp_db,
        tmp_path,
        [requests.exceptions.ReadTimeout("slow"), requests.exceptions.ReadTimeout("slow")],
        max_attempts=2,
    )

    summary = refresher.run([1], apply_state=True)

    assert len(session.calls) == 2
    assert summary.status == "partial"
    assert summary.results[0]["error_code"] == "timeout"
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["failure_count"] == 1
    assert state["next_check_at"] > state["last_checked_at"]


def test_retryable_response_is_closed_before_retry(temp_db: Path, tmp_path: Path):
    retry = FakeResponse(503)
    success = FakeResponse(
        200,
        b"<main>Available after retry</main>",
        headers={"Content-Type": "text/html"},
    )
    refresher, session = make_refresher(
        temp_db, tmp_path, [retry, success], max_attempts=2
    )

    summary = refresher.run([1], apply_state=True)

    assert len(session.calls) == 2
    assert retry.closed is True
    assert summary.status == "success"


def test_failed_refresh_does_not_replace_last_successful_hash(temp_db: Path, tmp_path: Path):
    good = FakeResponse(200, b"<main>Known good</main>", headers={"Content-Type": "text/html"})
    refresher, _ = make_refresher(temp_db, tmp_path, [good])
    refresher.run([1], apply_state=True, force=True)
    before = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")

    refresher, _ = make_refresher(temp_db, tmp_path, [FakeResponse(500)], max_attempts=1)
    refresher.run([1], apply_state=True, force=True)
    after = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")

    assert after["raw_hash"] == before["raw_hash"]
    assert after["content_hash"] == before["content_hash"]
    assert after["failure_count"] == 1


def test_not_due_skips_network_and_preserves_next_check(temp_db: Path, tmp_path: Path):
    good = FakeResponse(200, b"<main>Known good</main>", headers={"Content-Type": "text/html"})
    refresher, _ = make_refresher(temp_db, tmp_path, [good])
    refresher.run([1], apply_state=True)
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")

    refresher, session = make_refresher(temp_db, tmp_path, [])
    summary = refresher.run([1], apply_state=True)

    assert summary.status == "success"
    assert summary.counts["not_due"] == 1
    assert summary.results[0]["next_check_at"] == state["next_check_at"]
    assert session.calls == []


def test_snapshot_write_failure_is_not_recorded_as_success(temp_db: Path, tmp_path: Path):
    bad_root = tmp_path / "not-a-directory"
    bad_root.write_text("occupied", encoding="utf-8")
    response = FakeResponse(200, b"<main>Changed</main>", headers={"Content-Type": "text/html"})
    session = FakeSession([response])
    refresher = SourceRefresher(
        db_path=temp_db,
        snapshot_dir=bad_root,
        session=session,
        sleep_fn=lambda _: None,
        resolver=lambda _: ["93.184.216.34"],
    )

    summary = refresher.run([1], apply_state=True)

    assert summary.status == "partial"
    assert summary.results[0]["error_code"] == "snapshot_write_error"
    state = fetch_row(temp_db, "SELECT * FROM source_monitor_state WHERE link_id=1")
    assert state["content_hash"] is None


def test_dns_private_target_is_rejected_before_request(temp_db: Path, tmp_path: Path):
    refresher, session = make_refresher(
        temp_db,
        tmp_path,
        [],
        resolver=lambda _: ["127.0.0.1"],
        max_attempts=1,
    )

    result = refresher.run([1], apply_state=True, force=True).results[0]

    assert result["outcome"] == "failed"
    assert result["error_code"] == "configuration_error"
    assert "私有、回环或保留 IP" in result["error_message"]
    assert session.calls == []


def test_active_run_blocks_second_writer(temp_db: Path, tmp_path: Path):
    with sqlite3.connect(temp_db) as db:
        db.execute(
            "INSERT INTO update_runs "
            "(run_type, mode, status, requested_link_ids, started_at, created_at) "
            "VALUES ('source_refresh', 'apply-state', 'running', '[1]', "
            "'2999-01-01T00:00:00Z', '2999-01-01T00:00:00Z')"
        )
        db.commit()
    refresher, _ = make_refresher(temp_db, tmp_path, [])

    with pytest.raises(RefreshAlreadyRunning, match="正在运行"):
        refresher.run([1], apply_state=True)


def test_stale_run_is_abandoned_and_recovered(temp_db: Path, tmp_path: Path):
    with sqlite3.connect(temp_db) as db:
        cursor = db.execute(
            "INSERT INTO update_runs "
            "(run_type, mode, status, requested_link_ids, started_at, created_at) "
            "VALUES ('source_refresh', 'apply-state', 'running', '[1]', "
            "'2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')"
        )
        stale_run_id = cursor.lastrowid
        db.commit()
    response = FakeResponse(
        200,
        b"<main>Recovered run</main>",
        headers={"Content-Type": "text/html"},
    )
    refresher, _ = make_refresher(
        temp_db,
        tmp_path,
        [response],
        now_fn=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    summary = refresher.run([1], apply_state=True)

    old_run = fetch_row(temp_db, "SELECT * FROM update_runs WHERE id=?", (stale_run_id,))
    assert old_run["status"] == "abandoned"
    assert summary.status == "success"
    assert summary.run_id != stale_run_id


def test_schema_initialization_is_idempotent(temp_db: Path, tmp_path: Path):
    refresher, _ = make_refresher(temp_db, tmp_path, [])
    before = business_table_fingerprints(temp_db)

    refresher._ensure_schema()
    refresher._ensure_schema()

    assert business_table_fingerprints(temp_db) == before


def test_response_size_limit_is_enforced(temp_db: Path, tmp_path: Path):
    response = FakeResponse(
        200,
        b"x" * 11,
        headers={"Content-Type": "text/plain", "Content-Length": "11"},
    )
    refresher, _ = make_refresher(temp_db, tmp_path, [response], max_bytes=10)

    summary = refresher.run([1], apply_state=True)

    assert summary.results[0]["error_code"] == "response_too_large"
    assert summary.status == "partial"
