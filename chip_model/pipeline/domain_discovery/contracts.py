"""Load bundle-owned discovery contracts and check real SQLite schemas."""

import json
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


DOMAINS = ("identity", "specs", "compute", "compatibility", "benchmark", "deployment")
# SQL identifier allowlist only. Business field ownership lives in domain.json.
_TABLES = frozenset({"chips", "chip_model_compatibility", "chip_model_benchmarks", "deployment_guides"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_RESERVED_IDS = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
_START_KEYS = frozenset({"domain", "chip", "skill", "chips", "resume_run_id", "request_id", "mode", "pipeline"})


class DiscoverySearchQuery(BaseModel):
    """Hermes supplies the original words; validation never rewrites them."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

    query_id: str
    text: Annotated[str, Field(min_length=1, max_length=120)]
    language: Literal['zh', 'en']
    reason: Annotated[str, Field(min_length=1, max_length=400)]
    page_type: Annotated[str, Field(min_length=1, max_length=80)]
    replaces_query_id: str | None = None

    @field_validator('query_id', 'replaces_query_id')
    @classmethod
    def validate_identifier(cls, value):
        return validate_run_id(value) if value is not None else None

    @field_validator('text', 'reason', 'page_type')
    @classmethod
    def validate_text(cls, value):
        if not value.strip() or any(unicodedata.category(c) in {'Cc', 'Cs'} for c in value):
            raise ValueError('search text must be nonblank Unicode without controls')
        return value


class DiscoverySearchPlan(BaseModel):
    """Public search policy is fixed; no fields, provider or quota overrides."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

    round_number: Annotated[int, Field(ge=0, le=2)]
    queries: Annotated[list[DiscoverySearchQuery], Field(min_length=1, max_length=10)]

    @model_validator(mode='after')
    def validate_plan(self):
        if self.round_number == 0:
            if len(self.queries) != 10:
                raise ValueError('initial plan requires exactly 10 queries')
            for language in ('zh', 'en'):
                if sum(q.language == language for q in self.queries) < 3:
                    raise ValueError('initial plan requires at least 3 zh and 3 en queries')
            if any(q.replaces_query_id is not None for q in self.queries):
                raise ValueError('initial queries cannot replace an intent')
        elif len(self.queries) > 5:
            raise ValueError('supplement requires 1..5 queries')
        if len({q.query_id for q in self.queries}) != len(self.queries):
            raise ValueError('duplicate query IDs')
        if len({' '.join(q.text.split()).casefold() for q in self.queries}) != len(self.queries):
            raise ValueError('duplicate query text')
        return self


def validate_run_id(value: object) -> str:
    """Validate a portable v3 run ID; storage checks its actual scope later."""
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value) or value.upper() in _RESERVED_IDS:
        raise ValueError("续跑运行 ID 必须为 1–128 位字母、数字、下划线或连字符，以字母或数字开头，且不能使用系统保留名")
    return value


def _start_request_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 200
        or any(unicodedata.category(char) == "Cc" for char in value)
    ):
        raise ValueError("请求幂等键必须为非空字符串，最多 200 字符且不含控制字符")
    return value.strip()


def _start_chip(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 160:
        raise ValueError("芯片线索必须为非空字符串，最多 160 字符")
    return value.strip()


def normalize_start(payload: Mapping, idempotency_key: object = None) -> dict:
    """Normalize shared CLI/API business input without database access or mutation.

    Request keys are validated separately and never enter the canonical payload.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("启动请求必须为对象")
    unknown = set(payload) - _START_KEYS
    if unknown:
        raise ValueError(f"不支持的启动参数：{', '.join(sorted(map(str, unknown)))}；请按数据域运行")
    domain = payload.get("domain")
    if "domain" in payload and (not isinstance(domain, str) or domain not in DOMAINS):
        raise ValueError(f"数据域必须为：{', '.join(DOMAINS)}")
    if "skill" in payload:
        skill = payload["skill"]
        if not isinstance(skill, str) or skill not in tuple(f"chip-{item}" for item in DOMAINS):
            raise ValueError("Skill 必须为受支持数据域的 chip-<domain> 别名")
        skill_domain = skill.removeprefix("chip-")
        if domain is not None and domain != skill_domain:
            raise ValueError("数据域与 Skill 别名必须一致")
        domain = skill_domain
    if domain is None:
        raise ValueError("必须提供数据域 domain 或兼容 Skill 别名")
    if "mode" in payload and payload["mode"] != "test":
        raise ValueError("启动模式仅支持 test 测试")
    if "pipeline" in payload and payload["pipeline"] != "open_web":
        raise ValueError("启动流水线仅支持 open_web 开放互联网发现")

    chip = _start_chip(payload["chip"]) if payload.get("chip") is not None else None
    old_chip = None
    if "chips" in payload:
        chips = payload["chips"]
        if not isinstance(chips, list) or len(chips) > 1:
            raise ValueError("旧 chips 参数必须为列表，最多包含一条芯片线索")
        if chips:
            old_chip = _start_chip(chips[0])
    if chip is not None and old_chip is not None and chip != old_chip:
        raise ValueError("chip 与旧 chips 芯片线索必须一致")
    chip = chip or old_chip
    resume = payload.get("resume_run_id")
    if resume is not None:
        resume = validate_run_id(resume)
    body_key = _start_request_id(payload.get("request_id"))
    header_key = _start_request_id(idempotency_key)
    if body_key is not None and header_key is not None and body_key != header_key:
        raise ValueError("请求 request_id 与 Idempotency-Key 必须一致")
    return {"domain": domain, "chip": chip, "scope_type": "chip" if chip else "discovery", "resume_run_id": resume}


class DomainContract(BaseModel):
    """Machine-readable domain rules, loaded from an explicitly trusted bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    domain_id: str
    label: str
    skill_name: str
    rule_version: str
    fields: dict[str, list[str]]
    entity_keys: dict[str, list[str]]
    conditions: dict[str, JsonValue]
    units: dict[str, JsonValue]
    references: list[str]

    @model_validator(mode="after")
    def validate_contract(self) -> "DomainContract":
        if self.domain_id not in DOMAINS:
            raise ValueError(f"Unknown domain: {self.domain_id!r}")
        if self.skill_name != f"chip-{self.domain_id}":
            raise ValueError("skill_name must match domain_id")
        if self.rule_version != "3.0.0":
            raise ValueError("Unsupported rule_version; expected 3.0.0")
        if not self.label.strip():
            raise ValueError("label must not be blank")
        if not self.fields or set(self.fields) != set(self.entity_keys):
            raise ValueError("fields and entity_keys must name the same nonempty tables")
        for namespace in (self.fields, self.entity_keys):
            for table, names in namespace.items():
                if table not in _TABLES:
                    raise ValueError(f"Unsupported table: {table!r}")
                if not names or len(names) != len(set(names)):
                    raise ValueError(f"Empty or duplicate fields/entity keys in {table}")
                if any(not _IDENTIFIER.fullmatch(name) for name in names):
                    raise ValueError(f"Invalid column identifier in {table}")
        for table, fields in self.fields.items():
            overlap = set(fields) & set(self.entity_keys[table])
            allowed = {"vendor", "chip_model"} if self.domain_id == "identity" and table == "chips" else set()
            if overlap - allowed:
                raise ValueError(f"Scalar fields overlap entity keys in {table}: {sorted(overlap - allowed)}")
        if not self.references or len(self.references) != len(set(self.references)):
            raise ValueError("references must be explicit, nonempty and unique")
        for reference in self.references:
            # Bundles use portable POSIX paths, never URLs, drives, or traversal.
            parts = reference.split("/")
            if (
                not reference
                or "\\" in reference
                or ":" in reference
                or PurePosixPath(reference).is_absolute()
                or PureWindowsPath(reference).drive
                or any(part in {"", ".", ".."} for part in parts)
                or any(character in reference for character in "*?[]\x00")
            ):
                raise ValueError(f"Reference must be an explicit bundle-root-relative path: {reference!r}")
        return self


def _within_root(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes approved bundle root: {path}")
    return resolved


def _reject_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_domain(root: Path, domain_id: str) -> DomainContract:
    """Read one contract without following paths outside the approved root.

    Reference existence is checked when freezing bundles, after migration.
    """
    if domain_id not in DOMAINS:
        raise ValueError(f"Unknown domain: {domain_id!r}")
    approved_root = Path(root).resolve()
    path = _within_root(approved_root, approved_root / f"chip-{domain_id}" / "domain.json")
    contract = DomainContract.model_validate(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    )
    if contract.domain_id != domain_id or contract.skill_name != f"chip-{domain_id}":
        raise ValueError("Contract identity must match requested domain and skill_name")
    for reference in contract.references:
        _within_root(approved_root, approved_root / reference)
    return contract


def validate_domain_schema(connection: sqlite3.Connection, contract: DomainContract) -> None:
    """Check owned fields and relation keys using read-only schema introspection.

    Missing schema is an explicit error. This helper never creates or alters it.
    """
    tables = set(contract.fields) | set(contract.entity_keys)
    invalid = tables - _TABLES
    if invalid:
        raise ValueError(f"Unsupported table identifiers: {sorted(invalid)}")
    for table in sorted(tables):
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if not columns:
            raise ValueError(f"Missing table: {table}")
        required = set(contract.fields.get(table, [])) | set(contract.entity_keys.get(table, []))
        missing = required - columns
        if missing:
            raise ValueError(f"Missing schema columns in {table}: {', '.join(sorted(missing))}")
