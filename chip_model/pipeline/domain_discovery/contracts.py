"""Load bundle-owned discovery contracts and check real SQLite schemas."""

import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator


DOMAINS = ("identity", "specs", "compute", "compatibility", "benchmark", "deployment")
DomainId = Literal['identity', 'specs', 'compute', 'compatibility', 'benchmark', 'deployment']


def _reason_text(value):
    if not value.strip() or any(unicodedata.category(char) in {'Cc', 'Cs'} for char in value):
        raise ValueError('reason must be nonblank Unicode without controls')
    return value


TextReason = Annotated[str, Field(min_length=1, max_length=400), AfterValidator(_reason_text)]
REASON_TEXT = TypeAdapter(TextReason, config=ConfigDict(strict=True))
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


class StrictToolModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


class ToolBinding(StrictToolModel):
    contract_version: Literal[3]
    pipeline: Literal['hermes-open-web-v3']
    run_id: str
    domain: DomainId
    scope_type: Literal['chip', 'discovery']
    chip: Annotated[str, Field(min_length=1, max_length=160)] | None

    @field_validator('contract_version', mode='before')
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError('contract_version must be integer 3')
        return value

    @field_validator('run_id')
    @classmethod
    def portable_run(cls, value):
        return validate_run_id(value)

    @field_validator('chip')
    @classmethod
    def normalized_chip(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError('chip must be normalized and nonblank')
        return value


class MutationBinding(ToolBinding):
    request_id: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator('request_id')
    @classmethod
    def request_key(cls, value):
        if _start_request_id(value) != value:
            raise ValueError('request_id must be normalized')
        return value


class ToolCursor(StrictToolModel):
    list_version: Annotated[str, Field(min_length=1, max_length=200)]
    offset: Annotated[int, Field(ge=0)]


class SearchExecute(MutationBinding):
    action: Literal['execute']
    plan: DiscoverySearchPlan


class IntentCompletion(StrictToolModel):
    failed_query_id: Annotated[str, Field(min_length=1, max_length=200)]
    replacement_query_id: Annotated[str, Field(min_length=1, max_length=200)]
    reason: TextReason


class SearchCompleteIntents(MutationBinding):
    action: Literal['complete_intents']
    completions: Annotated[list[IntentCompletion], Field(min_length=1, max_length=10)]


class SearchResults(ToolBinding):
    action: Literal['results']
    list_version: Annotated[str, Field(min_length=1, max_length=200)]
    cursor: ToolCursor | None = None


class PreviewInitial(MutationBinding):
    mode: Literal['initial']
    list_version: Annotated[str, Field(min_length=1, max_length=200)]
    cursor: ToolCursor | None = None
    limit: Annotated[int, Field(ge=1, le=10)] = 10


class PreviewDetail(MutationBinding):
    mode: Literal['detail']
    snapshot_id: Annotated[str, Field(min_length=1, max_length=200)]
    offset: Annotated[int, Field(ge=0)] = 0
    length: Annotated[int, Field(ge=1, le=4000)] = 4000


class ToolDecision(StrictToolModel):
    candidate_id: Annotated[str, Field(min_length=1, max_length=200)]
    decision: Literal['select', 'reject']
    reason: Annotated[str, Field(min_length=1, max_length=400)]
    expected_info: Annotated[str, Field(max_length=1000)]
    linked_domains: Annotated[list[DomainId], Field(max_length=5)]
    entity_clues: Annotated[list[Annotated[str, Field(min_length=1, max_length=160)]], Field(max_length=10)] = Field(default_factory=list)
    matched_category: DomainId | None = None

    @model_validator(mode='after')
    def valid_decision(self):
        if not self.reason.strip():
            raise ValueError('decision reason must be nonblank')
        if self.decision == 'select' and not self.expected_info.strip():
            raise ValueError('select expected_info must be nonblank')
        if len(set(self.linked_domains)) != len(self.linked_domains):
            raise ValueError('duplicate linked domains')
        if any(not clue.strip() or any(unicodedata.category(char) in {'Cc', 'Cs'} for char in clue)
               for clue in self.entity_clues):
            raise ValueError('entity clues must be nonblank Unicode without controls')
        return self


class SelectionAppend(MutationBinding):
    action: Literal['append']
    list_version: Annotated[str, Field(min_length=1, max_length=200)]
    decisions: Annotated[list[ToolDecision], Field(min_length=1, max_length=10)]


class SelectionFinalize(MutationBinding):
    action: Literal['finalize']
    list_version: Annotated[str, Field(min_length=1, max_length=200)]


SearchToolRequest = Annotated[SearchExecute | SearchResults | SearchCompleteIntents, Field(discriminator='action')]
PreviewToolRequest = Annotated[PreviewInitial | PreviewDetail, Field(discriminator='mode')]
SelectionToolRequest = Annotated[SelectionAppend | SelectionFinalize, Field(discriminator='action')]
SEARCH_TOOL = TypeAdapter(SearchToolRequest)
PREVIEW_TOOL = TypeAdapter(PreviewToolRequest)
SELECTION_TOOL = TypeAdapter(SelectionToolRequest)


def validate_decisions(major_ids, visited_ids, decisions, finalize=False, *, current_domain=None):
    """Validate decisions; visited_ids denotes sealed terminal page-visit results.

    The service matches the latest execution, excludes retry/backoff and
    unknown work, and validates snapshot IO.
    """
    if not isinstance(decisions, list) or type(finalize) is not bool:
        raise ValueError('decisions must be a list and finalize a boolean')
    major, visited = set(major_ids), set(visited_ids)
    validated = [ToolDecision.model_validate(item).model_dump() for item in decisions]
    ids = [item['candidate_id'] for item in validated]
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate candidate IDs')
    if not set(ids).issubset(major):
        raise ValueError('unknown candidate outside saved major list')
    if not set(ids).issubset(visited):
        raise ValueError('candidate requires an actual visit with a finished result')
    if current_domain and any(current_domain in item['linked_domains'] for item in validated):
        raise ValueError('linked domains must exclude current domain')
    if finalize and (set(ids) != major or not major.issubset(visited)):
        raise ValueError('未完成：every major candidate requires actual visit and decision')
    return validated


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


def _known_condition(value):
    return isinstance(value, str) and value.strip() not in {'', 'unknown', 'not_applicable', 'generic'}


def _positive_count(value):
    try:
        number = float(value)
        return not isinstance(value, bool) and math.isfinite(number) and number > 0 and number.is_integer()
    except (TypeError, ValueError):
        return False


def benchmark_metric_kind(field: str, contract: DomainContract) -> str | None:
    """Metric semantics within the frozen ownership allowlist, never ownership."""
    if field not in contract.fields.get('chip_model_benchmarks', []):
        return None
    if field in {'prefill_throughput','decode_throughput','throughput_tok_s',
                 'time_to_first_token_ms','inter_token_latency_ms','tpot_ms'}:
        return 'generation'
    if field == 'throughput_samples_s':
        return 'nongeneration'
    if field in {'gpu_hours','mfu_pct'}:
        return 'training'
    if field == 'memory_peak_mb':
        return 'memory'
    return None


def _absolute_benchmark_metric(candidate, contract):
    if benchmark_metric_kind(candidate.get('field_name'), contract) is None:
        return False
    value = candidate.get('normalized_value')
    try:
        absolute = not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0
    except (ValueError, TypeError):
        absolute = False
    return absolute and _known_condition(candidate.get('unit'))


def relation_readiness(candidate: dict, contract: DomainContract, *, metric_candidates=()) -> dict:
    """Restrict an audited relation using its frozen contract, without granting proof."""
    domain = contract.domain_id
    if candidate.get('domain_id') != domain:
        raise ValueError('candidate and frozen domain mismatch')
    table = candidate.get('target_table')
    if candidate.get('field_name') not in contract.fields.get(table, []):
        raise ValueError('field is outside frozen domain ownership')
    if table == 'chips':
        return {'status': 'ready', 'missing_conditions': []}
    key, conditions = candidate.get('entity_key', {}), candidate.get('conditions', {})
    missing = [name for name in contract.entity_keys[table] if not _known_condition(key.get(name))]
    if domain == 'benchmark':
        missing = [name for name in missing if name != 'chip_count']
        if not _positive_count(key.get('chip_count')):
            missing.append('chip_count')
        minimum = contract.conditions['minimum_evidence']
        metric_kind = benchmark_metric_kind(candidate.get('field_name'), contract)
        if 'absolute_metric_and_unit' in minimum:
            # Only the store-backed caller supplies sibling metric candidates
            # after checking their original independent review proofs and full
            # entity/report/configuration/snapshot identity.
            if not _absolute_benchmark_metric(candidate, contract) and not any(
                    _absolute_benchmark_metric(metric, contract) for metric in metric_candidates):
                missing.append('absolute_metric_and_unit')
        if 'distinguishable_report_suite_configuration' in minimum:
            if not any(_known_condition(conditions.get(name)) for name in ('hardware_config', 'hardware_configuration', 'configuration_id')):
                missing.append('report_configuration')
        training = key.get('workload_type') == 'training' or metric_kind == 'training'
        generative = contract.conditions['generative_metrics']
        if not training and (metric_kind == 'generation' or conditions.get('metric_class') in generative['applies_to']):
            for name in generative['required']:
                present = any(_positive_count(conditions.get(n)) for n in ('batch_size', 'concurrency')) if name == 'batch_size_or_concurrency' else _positive_count(conditions.get(name))
                if not present:
                    missing.append(name)
        if training:
            missing.extend(name for name in contract.conditions['training_metrics']['required']
                           if not _known_condition(conditions.get(name)))
    elif domain == 'deployment':
        for name, declaration in [('chip_model', 'chip_applicability'), ('model_id', 'model_applicability')]:
            value = key.get(name)
            if value in {'generic', 'not_applicable'}:
                if conditions.get(declaration) == value:
                    missing = [n for n in missing if n != name]
                elif name not in missing:
                    missing.append(name)
        from .search import canonical_url
        try:
            canonical_url(key.get('url'))
        except (ValueError, TypeError, AttributeError):
            missing.append('url')
    elif domain == 'compatibility' and candidate.get('field_name') == 'compat_status':
        status = candidate.get('normalized_value')
        strength = contract.conditions['compat_status'].get(status)
        if strength == 'actual_verification' and (candidate.get('source_type') == 'vendor_claim'
                or candidate.get('evidence_kind') != 'measured'
                or conditions.get('verification_kind') not in {'actual_test', 'reproducible_validation'}):
            return {'status': 'rejected', 'reason': 'verification_strength_mismatch', 'missing_conditions': []}
        if strength == 'vendor_statement' and candidate.get('source_type') not in {'vendor_claim', 'official_datasheet', 'official_news'}:
            return {'status': 'rejected', 'reason': 'verification_strength_mismatch', 'missing_conditions': []}
    return {'status': 'unresolved' if missing else 'ready',
            'reason': 'incomplete_evidence' if missing else 'minimum_conditions_passed',
            'missing_conditions': sorted(set(missing))}


def benchmark_comparison_key(candidate: dict, entity: dict | None = None) -> str:
    """All observed conditions, preserving opaque model/report identifiers.

    Unknown optional values differ from explicit values. A partial comparison
    key cannot by itself authorize matching or updating a historical record.
    """
    from .store import _json
    from .entities import candidate_entity_hint, identity_variants, normalized_sku
    identity = entity.get('canonical_key') if entity else None
    if entity is None:
        hints = candidate_entity_hint(candidate)
        try:
            from .models import _capacity_identity
            normalized = {'vendor':normalized_sku(hints['vendor']), 'full_sku':normalized_sku(hints['full_sku'])}
            if 'capacity' in hints:
                normalized['capacity'] = _capacity_identity(hints['capacity'])
            if 'form_factor' in hints:
                normalized['form_factor'] = normalized_sku(hints['form_factor'])
            identity = {'vendor':normalized['vendor'],'full_sku':normalized['full_sku'], **identity_variants(normalized)}
        except (KeyError, ValueError, TypeError):
            identity = hints
    keys = dict(candidate.get('entity_key', {}))
    if isinstance(keys.get('chip_model'), str):
        keys['chip_model'] = normalized_sku(keys['chip_model'])
    return _json({'entity_identity':identity, 'entity_key':keys,
                  'conditions': candidate.get('conditions', {})})
