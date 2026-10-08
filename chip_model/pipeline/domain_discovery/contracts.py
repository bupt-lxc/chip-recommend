"""Load bundle-owned discovery contracts and check real SQLite schemas."""

import json
import re
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator


DOMAINS = ("identity", "specs", "compute", "compatibility", "benchmark", "deployment")
# SQL identifier allowlist only. Business field ownership lives in domain.json.
_TABLES = frozenset({"chips", "chip_model_compatibility", "chip_model_benchmarks", "deployment_guides"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


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
