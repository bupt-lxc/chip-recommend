"""Safe incremental refresh for explicitly selected source links.

This module is deliberately check-only.  It may write operational control
tables and content-addressed snapshots, but it never writes business tables.
"""

from __future__ import annotations

import hashlib
import difflib
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import tempfile
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Comment, UnicodeDammit

from chip_model.database import get_db, get_db_path, get_project_root


RUN_TYPE = "source_refresh"
DEFAULT_SNAPSHOT_DIR = get_project_root() / "data" / "source_snapshots"
DEFAULT_HEADERS = {
    "User-Agent": "AISHPerf-SourceRefresh/1.0 (+incremental source monitor)",
    "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.1",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
SUCCESS_OUTCOMES = {"new", "changed", "unchanged"}
NOOP_OUTCOMES = {"not_due"}
PARTIAL_OUTCOMES = {"retry_wait", "failed", "unsupported"}
BUSINESS_TABLES = (
    "chips",
    "models",
    "chip_model_benchmarks",
    "chip_model_compatibility",
    "field_provenance",
)
DIFF_MAX_LINES = 200
DIFF_MAX_CHARS = 20_000
DIFF_SUMMARY_ITEMS = 6
DIFF_SUMMARY_CHARS = 1_200
CANDIDATE_FIELD_RULES = (
    ("vram_gb", "显存容量", ("vram", "hbm", "显存", "memory capacity")),
    (
        "vram_bw_gb_s",
        "显存带宽",
        ("memory bandwidth", "显存带宽", "hbm bandwidth"),
    ),
    ("tdp_w", "功耗/TDP", ("tdp", "功耗", "power consumption", "thermal design power")),
    (
        "precision_perf",
        "精度性能",
        ("tflops", "tops", "fp32", "fp16", "bf16", "fp8", "int8", "算力"),
    ),
    ("process_node_nm", "制程", ("制程", "process node", "nanometer", " nm")),
    (
        "interconnect_bw_gb_s",
        "互联带宽",
        ("interconnect", "互联", "nvlink", "infinity fabric", "hccs"),
    ),
    ("architecture", "芯片架构", ("architecture", "架构")),
    ("compute_units", "计算单元", ("compute unit", "cuda core", "tensor core", "计算单元")),
    ("price_usd", "美元价格", ("price", "usd", "$", "售价", "价格")),
    ("price_cny_wan", "人民币价格", ("人民币", "万元", "¥", "￥", "价格")),
    ("software_stack", "软件栈", ("software stack", "sdk", "cuda", "rocm", "软件栈")),
    ("compatible_frameworks", "兼容框架", ("pytorch", "tensorflow", "jax", "mindspore", "框架")),
    ("total_params_b", "模型参数量", ("parameters", "params", "参数量", "active parameters")),
    ("downloads", "模型下载量", ("downloads", "下载量")),
    ("likes", "模型收藏量", ("likes", "收藏")),
    ("benchmark", "实测结果", ("mlperf", "benchmark", "throughput", "latency", "mfu", "吞吐", "时延")),
    ("compatibility", "兼容适配", ("compatible", "supported", "validated", "兼容", "适配", "验证")),
    ("deployment", "部署信息", ("deployment", "vllm", "sglang", "tensorrt", "部署", "推理引擎")),
)


class SourceRefreshError(RuntimeError):
    """Base error for a source refresh run."""


class ConfigurationError(SourceRefreshError):
    """The requested refresh configuration is invalid."""


class RefreshAlreadyRunning(SourceRefreshError):
    """Another source refresh run currently owns the single-writer slot."""


class ResponseTooLarge(SourceRefreshError):
    """The response body exceeded the configured byte limit."""


class UnsupportedContentType(SourceRefreshError):
    """The pilot crawler cannot safely process this content type."""


class EmptyContent(SourceRefreshError):
    """The response succeeded but yielded no useful normalized content."""


class SnapshotWriteError(SourceRefreshError):
    """A changed response could not be saved atomically."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class CheckResult:
    link_id: int
    requested_url: str
    outcome: str
    checked_at: str
    description: str = ""
    vendor: str = ""
    category: str = ""
    final_url: str = ""
    http_status: int | None = None
    attempt_count: int = 0
    previous_raw_hash: str | None = None
    raw_hash: str | None = None
    previous_content_hash: str | None = None
    content_hash: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    response_bytes: int = 0
    snapshot_path: str | None = None
    normalized_snapshot_path: str | None = None
    diff_path: str | None = None
    diff_summary: str | None = None
    diff_added_lines: int = 0
    diff_removed_lines: int = 0
    diff_truncated: bool = False
    candidate_fields: list[dict[str, str]] = field(default_factory=list)
    outbound_links: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    next_check_at: str | None = None
    failure_count: int = 0
    retry_after_seconds: float | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("retry_after_seconds", None)
        return data


@dataclass
class RunSummary:
    mode: str
    status: str
    selected: int
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    results: list[dict] = field(default_factory=list)
    business_tables_modified: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def parse_link_ids(value: str | Iterable[int]) -> list[int]:
    """Parse a comma-separated list or iterable into ordered unique IDs."""
    if isinstance(value, str):
        raw = [part.strip() for part in value.split(",") if part.strip()]
        try:
            items = [int(part) for part in raw]
        except ValueError as exc:
            raise ConfigurationError(
                f"link_ids 必须是逗号分隔的正整数；收到 {value!r}。"
            ) from exc
    else:
        try:
            items = [int(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("link_ids 必须是正整数列表。") from exc

    if not items or any(item <= 0 for item in items):
        raise ConfigurationError("至少提供一个大于 0 的 link_id。")
    return list(dict.fromkeys(items))


def resolve_host_addresses(host: str) -> list[str]:
    """Resolve every A/AAAA result so fetches can reject non-public targets."""
    try:
        answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConfigurationError(f"来源域名无法解析：{host}。") from exc
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses:
        raise ConfigurationError(f"来源域名没有可用地址：{host}。")
    return addresses


def validate_source_url(
    url: str, *, resolver: Callable[[str], Iterable[str]] | None = None
) -> None:
    """Reject unsafe schemes and local targets, including DNS results before fetch."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(
            f"只允许带有效主机名的 http/https URL；收到 {url!r}。"
        )
    if parsed.username or parsed.password:
        raise ConfigurationError("来源 URL 不允许内嵌用户名或密码。")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost":
        raise ConfigurationError("来源 URL 不允许指向 localhost。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if resolver is None:
            return
        for raw_address in resolver(host):
            try:
                resolved = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
            except ValueError as exc:
                raise ConfigurationError(
                    f"来源域名解析结果无效：{raw_address!r}。"
                ) from exc
            if not resolved.is_global:
                raise ConfigurationError(
                    "来源域名解析到私有、回环或保留 IP，已拒绝访问。"
                )
        return
    if not address.is_global:
        raise ConfigurationError("来源 URL 不允许指向私有、回环或保留 IP。")


def normalize_content(raw: bytes, content_type: str, encoding: str | None) -> str:
    """Return deterministic full-body text used for semantic change hashing."""
    mime = content_type.split(";", 1)[0].strip().lower()
    is_pdf = mime == "application/pdf" or raw.startswith(b"%PDF-")
    supported = {
        "",
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/json",
        "text/json",
    }
    if mime not in supported and not is_pdf:
        raise UnsupportedContentType(
            f"测试抓取仅支持静态 HTML、PDF、纯文本和 JSON；收到 {mime or 'unknown'}。"
        )

    if is_pdf:
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join(
                f"[第 {index} 页]\n{page.extract_text() or ''}"
                for index, page in enumerate(reader.pages, 1)
            )
        except ImportError as exc:
            raise UnsupportedContentType("PDF 解析组件未安装。") from exc
        except Exception as exc:
            raise EmptyContent(f"PDF 文本提取失败：{exc}。") from exc
    else:
        text = raw.decode(encoding or "utf-8", errors="replace")
    if mime in {"application/json", "text/json"} and not is_pdf:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EmptyContent(f"JSON 响应无法解析：{exc.msg}。") from exc
        text = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    elif mime in {"", "text/html", "application/xhtml+xml"} and not is_pdf:
        soup = BeautifulSoup(text, "lxml")
        for tag in soup(
            [
                "script",
                "style",
                "noscript",
                "template",
                "svg",
                "iframe",
                "nav",
                "header",
                "footer",
                "form",
                "button",
            ]
        ):
            tag.decompose()
        for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
            comment.extract()
        root = soup.find("main") or soup.find("article") or soup.find("body") or soup
        text = root.get_text(separator="\n")

    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    normalized = "\n".join(line for line in lines if line).strip()
    if not normalized:
        raise EmptyContent("响应正文在规范化后为空，旧哈希不会被覆盖。")
    return normalized


def extract_outbound_links(
    raw: bytes,
    content_type: str,
    encoding: str | None,
    base_url: str,
    *,
    limit: int = 500,
) -> list[str]:
    """Extract ordered HTTPS links before HTML normalization removes hrefs."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime not in {"", "text/html", "application/xhtml+xml"}:
        return []
    text = raw.decode(encoding or "utf-8", errors="replace")
    soup = BeautifulSoup(text, "lxml")
    links: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = str(tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            continue
        clean = parsed._replace(fragment="").geturl()
        if clean in seen:
            continue
        seen.add(clean)
        links.append(clean)
        if len(links) >= max(1, limit):
            break
    return links


def _read_response_body(
    response: requests.Response, max_bytes: int, *, remaining_seconds=None
) -> bytes:
    """Read a bounded body, optionally checking a cumulative fetch deadline.

    The caller owns/closes the response. A deadline callback raises Timeout
    when exhausted; smaller chunks prevent a drip-fed stream from deferring
    the cumulative check until an entire 64 KiB buffer has arrived.
    """
    def check_deadline():
        if remaining_seconds is None:
            return
        remaining = remaining_seconds()
        # A streamed requests response keeps its urllib3 socket timeout from
        # the initial GET. Shrink it before each subsequent socket read so a
        # late stalled read cannot restart the original full timeout window.
        # Buffered/test responses may not expose a live socket at all.
        try:
            sock = response.raw._fp.fp.raw._sock
        except AttributeError:
            return
        if sock is not None:
            timeout = sock.gettimeout() if hasattr(sock, 'gettimeout') else None
            sock.settimeout(min(timeout, remaining) if timeout is not None else remaining)
    check_deadline()
    header_length = response.headers.get("Content-Length")
    if header_length:
        try:
            if int(header_length) > max_bytes:
                raise ResponseTooLarge(
                    f"响应声明大小 {header_length} bytes，超过上限 {max_bytes} bytes。"
                )
        except ValueError:
            pass

    body = bytearray()
    total = 0
    for chunk in response.iter_content(chunk_size=1 if remaining_seconds else 64 * 1024):
        if remaining_seconds is not None:
            check_deadline()
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(
                f"响应实际大小超过上限 {max_bytes} bytes；已停止读取。"
            )
        body.extend(chunk)
    if remaining_seconds is not None:
        remaining_seconds()
    return bytes(body)


def _detect_encoding(raw: bytes, declared: str | None) -> str:
    """Detect encoding from buffered bytes without consuming Response.content twice."""
    if declared and declared.lower() not in {"iso-8859-1", "latin-1"}:
        return declared
    detected = UnicodeDammit(raw, is_html=True).original_encoding
    return detected or declared or "utf-8"


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _snapshot_extension(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime == "application/pdf":
        return ".pdf"
    if mime in {"application/json", "text/json"}:
        return ".json"
    if mime == "text/plain":
        return ".txt"
    return ".html"


def _changed_lines(old_text: str, new_text: str) -> tuple[list[str], list[str]]:
    """Return removed and added lines without diff control markers."""
    removed: list[str] = []
    added: list[str] = []
    matcher = difflib.SequenceMatcher(
        None, old_text.splitlines(), new_text.splitlines(), autojunk=False
    )
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            removed.extend(old_lines[old_start:old_end])
        if tag in {"replace", "insert"}:
            added.extend(new_lines[new_start:new_end])
    return removed, added


def _compact_line(value: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def identify_candidate_fields(
    removed_lines: list[str], added_lines: list[str]
) -> list[dict[str, str]]:
    """Conservatively tag business fields mentioned by changed lines.

    The result is review metadata, not an extracted value and never a business
    table write.
    """
    # Prefer the new page text.  Automatic publication may only use an
    # ``added`` candidate as evidence; removed text remains useful for audit.
    evidence_lines = [
        ("added", line) for line in added_lines
    ] + [("removed", line) for line in removed_lines]
    candidates: list[dict[str, str]] = []
    for field_name, label, keywords in CANDIDATE_FIELD_RULES:
        for change_type, line in evidence_lines:
            folded = line.casefold()
            if any(keyword.casefold() in folded for keyword in keywords):
                candidates.append(
                    {
                        "field": field_name,
                        "label": label,
                        "change_type": change_type,
                        "evidence": _compact_line(line),
                    }
                )
                break
    return candidates


def build_content_diff(
    old_text: str,
    new_text: str,
    previous_hash: str,
    content_hash: str,
) -> dict:
    """Build a bounded unified diff and a compact human-readable summary."""
    removed, added = _changed_lines(old_text, new_text)
    diff_lines: list[str] = []
    char_count = 0
    truncated = False
    iterator = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"before-{previous_hash[:12]}.txt",
        tofile=f"after-{content_hash[:12]}.txt",
        n=2,
        lineterm="",
    )
    for line in iterator:
        line_size = len(line) + 1
        if len(diff_lines) >= DIFF_MAX_LINES or char_count + line_size > DIFF_MAX_CHARS:
            truncated = True
            break
        diff_lines.append(line)
        char_count += line_size
    if truncated:
        diff_lines.append("... diff 已截断，请查看新旧规范化正文快照 ...")

    highlights: list[str] = []
    for prefix, lines in (("-", removed), ("+", added)):
        for line in lines:
            compact = _compact_line(line)
            if compact:
                highlights.append(f"{prefix} {compact}")
            if len(highlights) >= DIFF_SUMMARY_ITEMS:
                break
        if len(highlights) >= DIFF_SUMMARY_ITEMS:
            break
    summary = f"新增 {len(added)} 行，删除 {len(removed)} 行"
    if highlights:
        summary += "；关键变化：" + "；".join(highlights)
    if truncated:
        summary += "；完整差异过长，已截断"
    if len(summary) > DIFF_SUMMARY_CHARS:
        summary = summary[: DIFF_SUMMARY_CHARS - 1] + "…"
    return {
        "text": "\n".join(diff_lines) + "\n",
        "summary": summary,
        "added_lines": len(added),
        "removed_lines": len(removed),
        "truncated": truncated,
        "candidate_fields": identify_candidate_fields(removed, added),
    }


class SourceRefresher:
    """Incrementally check source URLs without mutating business data."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        snapshot_dir: str | Path | None = None,
        *,
        session: requests.Session | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
        proxy: str | None = None,
        max_bytes: int = 5 * 1024 * 1024,
        max_attempts: int = 2,
        max_redirects: int = 5,
        stale_after_minutes: int = 360,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = utc_now,
        resolver: Callable[[str], Iterable[str]] = resolve_host_addresses,
    ) -> None:
        self.db_path = Path(db_path or get_db_path())
        self.snapshot_dir = Path(snapshot_dir or DEFAULT_SNAPSHOT_DIR)
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.session.max_redirects = max_redirects
        self.timeout = (connect_timeout, read_timeout)
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self.max_bytes = max_bytes
        self.max_attempts = max(1, max_attempts)
        self.max_redirects = max(0, max_redirects)
        self.stale_after_minutes = max(1, stale_after_minutes)
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        self.resolver = resolver

    def _ensure_schema(self) -> None:
        schema = (get_project_root() / "schema.sql").read_text(encoding="utf-8")
        with get_db(self.db_path) as db:
            db.executescript(schema)
            db.commit()

    def _select_sources(self, link_ids: list[int]) -> list[dict]:
        placeholders = ",".join("?" for _ in link_ids)
        with get_db(self.db_path, readonly=True) as db:
            rows = db.execute(
                f"SELECT id, url, description, vendor, category FROM link_library "
                f"WHERE id IN ({placeholders})",
                link_ids,
            ).fetchall()
        by_id = {int(row["id"]): dict(row) for row in rows}
        missing = [link_id for link_id in link_ids if link_id not in by_id]
        if missing:
            raise ConfigurationError(
                f"link_library 中不存在 link_id：{','.join(map(str, missing))}。"
            )
        selected = [by_id[link_id] for link_id in link_ids]
        for source in selected:
            validate_source_url(source["url"] or "")
        return selected

    def _start_run(self, link_ids: list[int], started: datetime) -> int:
        stale_before = iso_utc(started - timedelta(minutes=self.stale_after_minutes))
        started_at = iso_utc(started)
        try:
            with get_db(self.db_path) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "UPDATE update_runs SET status='abandoned', finished_at=?, "
                    "error_summary='进程未正常结束，已由后续运行标记为 abandoned。' "
                    "WHERE run_type=? AND status='running' AND started_at < ?",
                    (started_at, RUN_TYPE, stale_before),
                )
                cursor = db.execute(
                    "INSERT INTO update_runs "
                    "(run_type, mode, status, requested_link_ids, started_at, created_at) "
                    "VALUES (?, 'apply-state', 'running', ?, ?, ?)",
                    (RUN_TYPE, json.dumps(link_ids), started_at, started_at),
                )
                db.commit()
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise RefreshAlreadyRunning(
                "已有 source_refresh 正在运行；请等待其结束，或在确认旧任务失效后调整 "
                "--stale-after-minutes 再重试。"
            ) from exc
        except sqlite3.OperationalError as exc:
            raise SourceRefreshError(
                f"无法取得 SQLite 单写入锁：{exc}。请稍后重试。"
            ) from exc

    def _record_event(
        self,
        run_id: int,
        *,
        actor: str,
        stage: str,
        status: str,
        message: str,
        link_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        """Append one human-readable process event without mutating business data."""
        with get_db(self.db_path) as db:
            db.execute(
                "INSERT INTO update_run_events "
                "(run_id, link_id, actor, stage, status, message, details_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    link_id,
                    actor,
                    stage,
                    status,
                    message,
                    json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                    iso_utc(self.now_fn()),
                ),
            )
            db.commit()

    def _load_state(self, link_id: int) -> dict:
        with get_db(self.db_path, readonly=True) as db:
            row = db.execute(
                "SELECT * FROM source_monitor_state WHERE link_id=?", (link_id,)
            ).fetchone()
        return dict(row) if row else {}

    def _write_snapshot(self, raw: bytes, raw_hash: str, content_type: str) -> str:
        extension = _snapshot_extension(content_type)
        target_dir = self.snapshot_dir / raw_hash[:2]
        target = target_dir / f"{raw_hash}{extension}"
        if target.exists():
            return str(target)
        temp_path: Path | None = None
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, dir=target_dir, prefix=".tmp-"
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            return str(target)
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise SnapshotWriteError(
                f"无法原子写入快照目录 {target_dir}：{exc}。"
            ) from exc

    def _write_text_artifact(self, target: Path, text: str) -> str:
        if target.exists():
            return str(target)
        temp_path: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                delete=False,
                dir=target.parent,
                prefix=".tmp-",
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
            return str(target)
        except OSError as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise SnapshotWriteError(
                f"无法原子写入差异文件 {target.parent}：{exc}。"
            ) from exc

    def _write_normalized_snapshot(self, normalized: str, content_hash: str) -> str:
        target = (
            self.snapshot_dir
            / "normalized"
            / content_hash[:2]
            / f"{content_hash}.txt"
        )
        return self._write_text_artifact(target, normalized + "\n")

    def _load_previous_normalized(self, link_id: int, state: dict) -> str | None:
        """Load a prior normalized snapshot, including legacy raw-only baselines."""
        content_hash = state.get("content_hash")
        if not content_hash:
            return None
        normalized_path = (
            self.snapshot_dir
            / "normalized"
            / str(content_hash)[:2]
            / f"{content_hash}.txt"
        )
        if normalized_path.is_file():
            text = normalized_path.read_text(encoding="utf-8").rstrip("\n")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() == content_hash:
                return text

        with get_db(self.db_path, readonly=True) as db:
            row = db.execute(
                "SELECT snapshot_path, content_type FROM source_checks "
                "WHERE link_id=? AND content_hash=? AND snapshot_path IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (link_id, content_hash),
            ).fetchone()
        if not row or not row["snapshot_path"]:
            return None
        raw_path = Path(row["snapshot_path"])
        if not raw_path.is_file():
            return None
        try:
            normalized = normalize_content(
                raw_path.read_bytes(), row["content_type"] or "", None
            )
        except (OSError, EmptyContent, UnsupportedContentType):
            return None
        if hashlib.sha256(normalized.encode("utf-8")).hexdigest() != content_hash:
            return None
        self._write_normalized_snapshot(normalized, str(content_hash))
        return normalized

    def _write_diff(
        self, previous_hash: str, content_hash: str, diff_text: str
    ) -> str:
        target = (
            self.snapshot_dir
            / "diffs"
            / previous_hash[:2]
            / f"{previous_hash}..{content_hash}.diff"
        )
        return self._write_text_artifact(target, diff_text)

    def _failure_result(
        self,
        source: dict,
        state: dict,
        *,
        outcome: str,
        checked_at: str,
        started_perf: float,
        attempt_count: int,
        error_code: str,
        error_message: str,
        response: requests.Response | None = None,
        retry_after_seconds: float | None = None,
    ) -> CheckResult:
        return CheckResult(
            link_id=int(source["id"]),
            requested_url=source["url"],
            final_url=str(getattr(response, "url", "") or ""),
            outcome=outcome,
            http_status=getattr(response, "status_code", None),
            attempt_count=attempt_count,
            previous_raw_hash=state.get("raw_hash"),
            previous_content_hash=state.get("content_hash"),
            etag=state.get("etag"),
            last_modified=state.get("last_modified"),
            error_code=error_code,
            error_message=error_message,
            checked_at=checked_at,
            duration_ms=int((time.perf_counter() - started_perf) * 1000),
            retry_after_seconds=retry_after_seconds,
        )

    def _get_with_safe_redirects(
        self, url: str, headers: dict[str, str], *, remaining_seconds=None
    ) -> requests.Response:
        """Follow redirects manually so every destination is validated first."""
        current_url = url
        redirect_statuses = {301, 302, 303, 307, 308}
        for redirect_count in range(self.max_redirects + 1):
            if remaining_seconds is not None:
                remaining_seconds()
            validate_source_url(current_url, resolver=self.resolver)
            timeout = self.timeout
            if remaining_seconds is not None:
                remaining = remaining_seconds()
                # urllib3's total timeout bounds connect + first socket read;
                # the streaming reader separately enforces the total body time.
                from urllib3.util import Timeout
                timeout = Timeout(total=remaining, connect=min(timeout[0], remaining),
                                  read=min(timeout[1], remaining))
            response = self.session.get(
                current_url,
                headers=headers,
                timeout=timeout,
                proxies=self.proxies,
                allow_redirects=False,
                stream=True,
            )
            if remaining_seconds is not None:
                try:
                    remaining_seconds()
                except BaseException:
                    response.close()
                    raise
            if int(response.status_code) not in redirect_statuses:
                try:
                    validate_source_url(
                        str(response.url or current_url), resolver=self.resolver
                    )
                    if remaining_seconds is not None:
                        remaining_seconds()
                except BaseException:
                    response.close()
                    raise
                return response

            location = response.headers.get("Location")
            if not location:
                return response
            if redirect_count >= self.max_redirects:
                response.close()
                raise requests.exceptions.TooManyRedirects(
                    f"超过 {self.max_redirects} 次重定向。"
                )

            try:
                next_url = urljoin(str(response.url or current_url), location)
                validate_source_url(next_url, resolver=self.resolver)
                if remaining_seconds is not None:
                    remaining_seconds()
            except BaseException:
                response.close()
                raise
            response.close()
            current_url = next_url

        raise AssertionError("unreachable")

    def _check_source(self, source: dict, state: dict) -> CheckResult:
        checked = self.now_fn()
        checked_at = iso_utc(checked)
        started_perf = time.perf_counter()
        headers: dict[str, str] = {}
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        elif state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]

        for attempt in range(1, self.max_attempts + 1):
            response: requests.Response | None = None
            try:
                response = self._get_with_safe_redirects(source["url"], headers)
                status = int(response.status_code)
                if status == 304:
                    if not state.get("content_hash"):
                        return self._failure_result(
                            source,
                            state,
                            outcome="failed",
                            checked_at=checked_at,
                            started_perf=started_perf,
                            attempt_count=attempt,
                            error_code="unexpected_304",
                            error_message=(
                                "来源返回 304，但本地没有可复用的成功内容哈希；"
                                "请强制重新获取完整正文。"
                            ),
                            response=response,
                        )
                    return CheckResult(
                        link_id=int(source["id"]),
                        requested_url=source["url"],
                        final_url=str(response.url),
                        outcome="unchanged",
                        http_status=status,
                        attempt_count=attempt,
                        previous_raw_hash=state.get("raw_hash"),
                        raw_hash=state.get("raw_hash"),
                        previous_content_hash=state.get("content_hash"),
                        content_hash=state.get("content_hash"),
                        etag=response.headers.get("ETag") or state.get("etag"),
                        last_modified=(
                            response.headers.get("Last-Modified")
                            or state.get("last_modified")
                        ),
                        checked_at=checked_at,
                        duration_ms=int((time.perf_counter() - started_perf) * 1000),
                    )

                retryable_status = status in {408, 429} or 500 <= status < 600
                if retryable_status:
                    retry_after = _retry_after_seconds(
                        response.headers.get("Retry-After"), checked
                    )
                    if attempt < self.max_attempts and (retry_after or 0) <= 5:
                        delay = (
                            retry_after
                            if retry_after is not None
                            else 2 ** (attempt - 1)
                        )
                        response.close()
                        response = None
                        self.sleep_fn(delay)
                        continue
                    return self._failure_result(
                        source,
                        state,
                        outcome="retry_wait",
                        checked_at=checked_at,
                        started_perf=started_perf,
                        attempt_count=attempt,
                        error_code=f"http_{status}",
                        error_message=f"HTTP {status}，将在退避后重试。",
                        response=response,
                        retry_after_seconds=retry_after,
                    )

                if status < 200 or status >= 300:
                    return self._failure_result(
                        source,
                        state,
                        outcome="failed",
                        checked_at=checked_at,
                        started_perf=started_perf,
                        attempt_count=attempt,
                        error_code=f"http_{status}",
                        error_message=f"HTTP {status} 为非重试状态，请检查来源链接。",
                        response=response,
                    )

                content_type = response.headers.get("Content-Type", "")
                raw = _read_response_body(response, self.max_bytes)
                raw_hash = hashlib.sha256(raw).hexdigest()
                encoding = _detect_encoding(raw, response.encoding)
                normalized = normalize_content(raw, content_type, encoding)
                outbound_links = extract_outbound_links(
                    raw, content_type, encoding, str(response.url)
                )
                content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                previous_content_hash = state.get("content_hash")
                if not previous_content_hash:
                    outcome = "new"
                elif content_hash == previous_content_hash:
                    outcome = "unchanged"
                else:
                    outcome = "changed"

                snapshot_path = None
                normalized_snapshot_path = None
                diff_path = None
                diff_summary = None
                diff_added_lines = 0
                diff_removed_lines = 0
                diff_truncated = False
                candidate_fields: list[dict[str, str]] = []
                if outcome in {"new", "changed"}:
                    snapshot_path = self._write_snapshot(raw, raw_hash, content_type)
                    normalized_snapshot_path = self._write_normalized_snapshot(
                        normalized, content_hash
                    )
                if outcome == "changed":
                    previous_normalized = self._load_previous_normalized(
                        int(source["id"]), state
                    )
                    if previous_normalized is None:
                        diff_summary = (
                            "正文哈希发生变化，但历史规范化正文不可用，"
                            "本轮无法生成逐行差异。"
                        )
                    else:
                        diff = build_content_diff(
                            previous_normalized,
                            normalized,
                            str(previous_content_hash),
                            content_hash,
                        )
                        diff_path = self._write_diff(
                            str(previous_content_hash), content_hash, diff["text"]
                        )
                        diff_summary = diff["summary"]
                        diff_added_lines = diff["added_lines"]
                        diff_removed_lines = diff["removed_lines"]
                        diff_truncated = diff["truncated"]
                        candidate_fields = diff["candidate_fields"]

                return CheckResult(
                    link_id=int(source["id"]),
                    requested_url=source["url"],
                    final_url=str(response.url),
                    outcome=outcome,
                    http_status=status,
                    attempt_count=attempt,
                    previous_raw_hash=state.get("raw_hash"),
                    raw_hash=raw_hash,
                    previous_content_hash=previous_content_hash,
                    content_hash=content_hash,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    content_type=content_type,
                    response_bytes=len(raw),
                    snapshot_path=snapshot_path,
                    normalized_snapshot_path=normalized_snapshot_path,
                    diff_path=diff_path,
                    diff_summary=diff_summary,
                    diff_added_lines=diff_added_lines,
                    diff_removed_lines=diff_removed_lines,
                    diff_truncated=diff_truncated,
                    candidate_fields=candidate_fields,
                    outbound_links=outbound_links,
                    checked_at=checked_at,
                    duration_ms=int((time.perf_counter() - started_perf) * 1000),
                )
            except requests.exceptions.TooManyRedirects as exc:
                return self._failure_result(
                    source,
                    state,
                    outcome="failed",
                    checked_at=checked_at,
                    started_perf=started_perf,
                    attempt_count=attempt,
                    error_code="too_many_redirects",
                    error_message=f"重定向次数超过上限：{exc}。",
                    response=response,
                )
            except requests.exceptions.SSLError as exc:
                return self._failure_result(
                    source,
                    state,
                    outcome="failed",
                    checked_at=checked_at,
                    started_perf=started_perf,
                    attempt_count=attempt,
                    error_code="tls_error",
                    error_message=f"TLS 证书校验失败：{exc}。",
                    response=response,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt < self.max_attempts:
                    self.sleep_fn(2 ** (attempt - 1))
                    continue
                code = "timeout" if isinstance(exc, requests.exceptions.Timeout) else "connection_error"
                return self._failure_result(
                    source,
                    state,
                    outcome="retry_wait",
                    checked_at=checked_at,
                    started_perf=started_perf,
                    attempt_count=attempt,
                    error_code=code,
                    error_message=f"网络请求失败：{exc}。",
                    response=response,
                )
            except (ResponseTooLarge, EmptyContent, UnsupportedContentType) as exc:
                outcome = "unsupported" if isinstance(exc, UnsupportedContentType) else "failed"
                error_codes = {
                    ResponseTooLarge: "response_too_large",
                    EmptyContent: "empty_content",
                    UnsupportedContentType: "unsupported_content_type",
                }
                return self._failure_result(
                    source,
                    state,
                    outcome=outcome,
                    checked_at=checked_at,
                    started_perf=started_perf,
                    attempt_count=attempt,
                    error_code=error_codes[type(exc)],
                    error_message=str(exc),
                    response=response,
                )
            except SnapshotWriteError as exc:
                return self._failure_result(
                    source,
                    state,
                    outcome="failed",
                    checked_at=checked_at,
                    started_perf=started_perf,
                    attempt_count=attempt,
                    error_code="snapshot_write_error",
                    error_message=str(exc),
                    response=response,
                )
            except (requests.exceptions.RequestException, ConfigurationError) as exc:
                error_code = (
                    "configuration_error"
                    if isinstance(exc, ConfigurationError)
                    else "request_error"
                )
                return self._failure_result(
                    source,
                    state,
                    outcome="failed",
                    checked_at=checked_at,
                    started_perf=started_perf,
                    attempt_count=attempt,
                    error_code=error_code,
                    error_message=str(exc),
                    response=response,
                )
            finally:
                if response is not None:
                    response.close()

        raise AssertionError("unreachable")

    def _next_check_at(self, result: CheckResult, previous_failures: int) -> tuple[str, int]:
        checked = datetime.fromisoformat(result.checked_at.replace("Z", "+00:00"))
        if result.outcome in SUCCESS_OUTCOMES:
            delay = timedelta(days=1 if result.outcome in {"new", "changed"} else 7)
            return iso_utc(checked + delay), 0
        failures = previous_failures + 1
        if result.retry_after_seconds is not None:
            seconds = min(result.retry_after_seconds, 7 * 24 * 3600)
        elif result.outcome == "retry_wait":
            seconds = min(6 * 3600 * (2 ** (failures - 1)), 7 * 24 * 3600)
        else:
            seconds = min(24 * 3600 * (2 ** (failures - 1)), 30 * 24 * 3600)
        return iso_utc(checked + timedelta(seconds=seconds)), failures

    def _persist_result(self, run_id: int, result: CheckResult, state: dict) -> None:
        next_check_at, failures = self._next_check_at(
            result, int(state.get("failure_count") or 0)
        )
        result.next_check_at = next_check_at
        result.failure_count = failures
        with get_db(self.db_path) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                check_cursor = db.execute(
                    "INSERT INTO source_checks "
                    "(run_id, link_id, requested_url, final_url, outcome, http_status, "
                    "attempt_count, previous_raw_hash, raw_hash, previous_content_hash, "
                    "content_hash, etag, last_modified, content_type, response_bytes, "
                    "snapshot_path, error_code, error_message, checked_at, duration_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        result.link_id,
                        result.requested_url,
                        result.final_url,
                        result.outcome,
                        result.http_status,
                        result.attempt_count,
                        result.previous_raw_hash,
                        result.raw_hash,
                        result.previous_content_hash,
                        result.content_hash,
                        result.etag,
                        result.last_modified,
                        result.content_type,
                        result.response_bytes,
                        result.snapshot_path,
                        result.error_code,
                        result.error_message,
                        result.checked_at,
                        result.duration_ms,
                    ),
                )
                if result.outcome == "changed":
                    db.execute(
                        "INSERT INTO source_diffs "
                        "(source_check_id, run_id, link_id, previous_content_hash, "
                        "content_hash, added_lines, removed_lines, truncated, "
                        "diff_summary, diff_path, candidate_fields_json, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(check_cursor.lastrowid),
                            run_id,
                            result.link_id,
                            result.previous_content_hash,
                            result.content_hash,
                            result.diff_added_lines,
                            result.diff_removed_lines,
                            1 if result.diff_truncated else 0,
                            result.diff_summary,
                            result.diff_path,
                            json.dumps(
                                result.candidate_fields,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            result.checked_at,
                        ),
                    )
                if result.outcome in SUCCESS_OUTCOMES:
                    db.execute(
                        "INSERT INTO source_monitor_state "
                        "(link_id, etag, last_modified, raw_hash, content_hash, "
                        "last_checked_at, next_check_at, failure_count, last_http_status, "
                        "last_error_code, last_error_message, last_run_id, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,0,?,NULL,NULL,?,?) "
                        "ON CONFLICT(link_id) DO UPDATE SET "
                        "etag=excluded.etag, last_modified=excluded.last_modified, "
                        "raw_hash=excluded.raw_hash, content_hash=excluded.content_hash, "
                        "last_checked_at=excluded.last_checked_at, next_check_at=excluded.next_check_at, "
                        "failure_count=0, last_http_status=excluded.last_http_status, "
                        "last_error_code=NULL, last_error_message=NULL, "
                        "last_run_id=excluded.last_run_id, updated_at=excluded.updated_at",
                        (
                            result.link_id,
                            result.etag,
                            result.last_modified,
                            result.raw_hash,
                            result.content_hash,
                            result.checked_at,
                            next_check_at,
                            result.http_status,
                            run_id,
                            result.checked_at,
                        ),
                    )
                else:
                    db.execute(
                        "INSERT INTO source_monitor_state "
                        "(link_id, last_checked_at, next_check_at, failure_count, "
                        "last_http_status, last_error_code, last_error_message, last_run_id, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(link_id) DO UPDATE SET "
                        "last_checked_at=excluded.last_checked_at, next_check_at=excluded.next_check_at, "
                        "failure_count=excluded.failure_count, "
                        "last_http_status=excluded.last_http_status, "
                        "last_error_code=excluded.last_error_code, "
                        "last_error_message=excluded.last_error_message, "
                        "last_run_id=excluded.last_run_id, updated_at=excluded.updated_at",
                        (
                            result.link_id,
                            result.checked_at,
                            next_check_at,
                            failures,
                            result.http_status,
                            result.error_code,
                            result.error_message,
                            run_id,
                            result.checked_at,
                        ),
                    )
                db.commit()
            except sqlite3.Error as exc:
                db.rollback()
                raise SourceRefreshError(
                    f"link_id={result.link_id} 的检查结果未写入：{exc}。可安全重试。"
                ) from exc

    def _finish_run(self, run_id: int, summary: RunSummary, error_summary: str = "") -> None:
        with get_db(self.db_path) as db:
            db.execute(
                "UPDATE update_runs SET status=?, finished_at=?, counts_json=?, error_summary=? "
                "WHERE id=?",
                (
                    summary.status,
                    summary.finished_at,
                    json.dumps(summary.counts, ensure_ascii=False, sort_keys=True),
                    error_summary,
                    run_id,
                ),
            )
            db.commit()

    @staticmethod
    def _is_due(state: dict, now: datetime) -> bool:
        value = state.get("next_check_at")
        if not value:
            return True
        try:
            due_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return True
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        return due_at <= now.astimezone(timezone.utc)

    def run(
        self,
        link_ids: str | Iterable[int],
        *,
        apply_state: bool = False,
        force: bool = False,
    ) -> RunSummary:
        ids = parse_link_ids(link_ids)
        if not self.db_path.exists():
            raise ConfigurationError(f"数据库不存在：{self.db_path}。")
        if apply_state:
            self._ensure_schema()
        sources = self._select_sources(ids)
        started = self.now_fn()
        started_at = iso_utc(started)

        if not apply_state:
            return RunSummary(
                mode="dry-run",
                status="success",
                selected=len(sources),
                started_at=started_at,
                finished_at=started_at,
                counts={"selected": len(sources)},
                results=[
                    {
                        "link_id": int(source["id"]),
                        "url": source["url"],
                        "vendor": source.get("vendor") or "",
                        "category": source.get("category") or "",
                        "outcome": "selected",
                    }
                    for source in sources
                ],
            )

        run_id = self._start_run(ids, started)
        self._record_event(
            run_id,
            actor="orchestrator",
            stage="run_started",
            status="started",
            message=f"来源巡检开始，共选择 {len(sources)} 个来源。",
            details={"link_ids": ids, "selected": len(sources), "force": force},
        )
        self._record_event(
            run_id,
            actor="scheduler",
            stage="source_selection",
            status="success",
            message="已完成来源选择与 URL 安全校验。",
            details={
                "sources": [
                    {
                        "link_id": int(source["id"]),
                        "url": source["url"],
                        "vendor": source.get("vendor") or "",
                        "category": source.get("category") or "",
                    }
                    for source in sources
                ]
            },
        )
        results: list[CheckResult] = []
        fatal_error = ""
        try:
            for source in sources:
                state = self._load_state(int(source["id"]))
                if not force and not self._is_due(state, self.now_fn()):
                    self._record_event(
                        run_id,
                        link_id=int(source["id"]),
                        actor="scheduler",
                        stage="due_check",
                        status="skipped",
                        message="来源尚未到下次检查时间，本轮跳过。",
                        details={
                            "url": source["url"],
                            "next_check_at": state.get("next_check_at"),
                        },
                    )
                    results.append(
                        CheckResult(
                            link_id=int(source["id"]),
                            requested_url=source["url"],
                            outcome="not_due",
                            checked_at=iso_utc(self.now_fn()),
                            previous_raw_hash=state.get("raw_hash"),
                            raw_hash=state.get("raw_hash"),
                            previous_content_hash=state.get("content_hash"),
                            content_hash=state.get("content_hash"),
                            etag=state.get("etag"),
                            last_modified=state.get("last_modified"),
                            next_check_at=state.get("next_check_at"),
                            failure_count=int(state.get("failure_count") or 0),
                        )
                    )
                    continue
                self._record_event(
                    run_id,
                    link_id=int(source["id"]),
                    actor="fetcher",
                    stage="fetch",
                    status="started",
                    message="开始访问来源网页。",
                    details={"requested_url": source["url"]},
                )
                try:
                    result = self._check_source(source, state)
                except Exception as exc:  # final report boundary; never silently swallow
                    result = self._failure_result(
                        source,
                        state,
                        outcome="failed",
                        checked_at=iso_utc(self.now_fn()),
                        started_perf=time.perf_counter(),
                        attempt_count=0,
                        error_code=exc.__class__.__name__.lower(),
                        error_message=f"未预期错误：{exc}",
                    )
                result.description = str(source.get("description") or "")
                result.vendor = str(source.get("vendor") or "")
                result.category = str(source.get("category") or "")
                self._persist_result(run_id, result, state)
                fetch_status = (
                    "success" if result.outcome in SUCCESS_OUTCOMES else "failed"
                )
                self._record_event(
                    run_id,
                    link_id=result.link_id,
                    actor="fetcher",
                    stage="fetch",
                    status=fetch_status,
                    message=(
                        "来源网页访问完成。"
                        if fetch_status == "success"
                        else "来源网页访问未成功。"
                    ),
                    details={
                        "requested_url": result.requested_url,
                        "final_url": result.final_url,
                        "http_status": result.http_status,
                        "attempt_count": result.attempt_count,
                        "response_bytes": result.response_bytes,
                        "content_type": result.content_type,
                        "duration_ms": result.duration_ms,
                        "error_code": result.error_code,
                        "error_message": result.error_message,
                    },
                )
                if result.outcome in SUCCESS_OUTCOMES:
                    self._record_event(
                        run_id,
                        link_id=result.link_id,
                        actor="snapshot",
                        stage="snapshot",
                        status="success",
                        message=(
                            "已保存原始网页与规范化正文快照。"
                            if result.snapshot_path
                            else "正文未变化，复用已有内容快照。"
                        ),
                        details={
                            "raw_hash": result.raw_hash,
                            "content_hash": result.content_hash,
                            "snapshot_path": result.snapshot_path,
                            "normalized_snapshot_path": result.normalized_snapshot_path,
                        },
                    )
                    candidate_fields = [
                        field.get("label") or field.get("field")
                        for field in result.candidate_fields
                        if field.get("label") or field.get("field")
                    ]
                    self._record_event(
                        run_id,
                        link_id=result.link_id,
                        actor="parser",
                        stage="change_detection",
                        status="success",
                        message={
                            "new": "首次抓取，已建立内容基线。",
                            "unchanged": "规范化正文与已有基线一致。",
                            "changed": f"检测到正文变化，识别出 {len(candidate_fields)} 个候选字段。",
                        }.get(result.outcome, "正文检查完成。"),
                        details={
                            "outcome": result.outcome,
                            "diff_summary": result.diff_summary,
                            "added_lines": result.diff_added_lines,
                            "removed_lines": result.diff_removed_lines,
                            "candidate_fields": result.candidate_fields,
                            "diff_path": result.diff_path,
                        },
                    )
                results.append(result)
        except Exception as exc:
            fatal_error = str(exc)

        finished = self.now_fn()
        counts: dict[str, int] = {
            key: 0 for key in (*SUCCESS_OUTCOMES, *NOOP_OUTCOMES, *PARTIAL_OUTCOMES)
        }
        for result in results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        counts["selected"] = len(sources)
        status = "failed" if fatal_error else (
            "partial" if any(counts.get(key, 0) for key in PARTIAL_OUTCOMES) else "success"
        )
        summary = RunSummary(
            mode="apply-state",
            status=status,
            selected=len(sources),
            run_id=run_id,
            started_at=started_at,
            finished_at=iso_utc(finished),
            duration_ms=int((finished - started).total_seconds() * 1000),
            counts=counts,
            results=[result.to_dict() for result in results],
            business_tables_modified=False,
        )
        self._finish_run(run_id, summary, fatal_error)
        self._record_event(
            run_id,
            actor="orchestrator",
            stage="run_finished",
            status=status,
            message=(
                "来源巡检完成。" if not fatal_error else "来源巡检异常结束。"
            ),
            details={
                "counts": counts,
                "duration_ms": summary.duration_ms,
                "error_summary": fatal_error,
            },
        )
        if fatal_error:
            raise SourceRefreshError(
                f"运行 {run_id} 未完成：{fatal_error}；已完成来源保留，可在修复后重跑。"
            )
        return summary


def business_table_fingerprints(db_path: str | Path) -> dict[str, str]:
    """Stable fingerprints used by tests to prove check-only isolation."""
    fingerprints: dict[str, str] = {}
    with get_db(db_path, readonly=True) as db:
        for table in BUSINESS_TABLES:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            payload = json.dumps(
                [dict(row) for row in rows],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            fingerprints[table] = hashlib.sha256(payload).hexdigest()
    return fingerprints
