"""Explicit V3 Skill packages: authority, verified copies and frozen run bundles.

No resolver flags or network access live here. A resolver report is evidence only
after the runtime obtains it from the actual Hermes loader (Task 12 preflight).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from .contracts import DOMAINS, _reject_duplicate_keys, load_domain


PHASES = ("plan", "select", "extract", "review", "complete")
RULE_VERSION = "3.0.0"
INSTALL_MANIFEST = "installed.manifest.json"
BUNDLE_MANIFEST = "bundle.manifest.json"


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict):
        raise ValueError(f"Manifest must be an object: {path.name}")
    return value


def _approved_path(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or any(char in relative for char in "*?[]\x00")
    ):
        raise ValueError("Expected an explicit bundle-root-relative path")
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes approved root: {relative}")
    return path


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _skill_document(text: str, skill_name: str) -> None:
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if not frontmatter:
        raise ValueError(f"Missing Skill frontmatter: {skill_name}")
    lines = frontmatter.group(1).splitlines()
    if (len(lines) != 2 or lines[0] != f"name: {skill_name}"
            or not lines[1].startswith("description: 用于")):
        raise ValueError(f"Skill frontmatter requires name and Chinese description only: {skill_name}")
    stages = re.findall(r"^## (.+)$", text, re.MULTILINE)
    if stages != list(PHASES):
        raise ValueError(f"Skill 阶段 must be exactly {PHASES}: {skill_name}")
    # Documentation refers to the full machine contract, rather than maintaining
    # another whitelist. Owned scalar fields are never parsed from prose.
    if "domain.json" not in text or RULE_VERSION not in text:
        raise ValueError(f"Skill must reference domain.json and rule version: {skill_name}")


def _contents(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for domain in DOMAINS:
        contract = load_domain(root, domain)
        required = {"_shared/rules.md", f"{contract.skill_name}/references/{domain}-rules.md"}
        if not required.issubset(contract.references):
            raise ValueError(f"Missing required public references: {domain}")
        relative_skill = f"{contract.skill_name}/SKILL.md"
        skill = _approved_path(root, relative_skill).read_bytes()
        _skill_document(skill.decode("utf-8").replace("\r\n", "\n"), contract.skill_name)
        paths = (f"{contract.skill_name}/domain.json", relative_skill, *contract.references)
        for relative in paths:
            # Explicit public references only; do not package machine config,
            # credentials, shell helpers, or files discovered by directory glob.
            if relative in contract.references and (
                not relative.endswith((".md", ".json"))
                or not (relative.startswith("_shared/") or relative.startswith(f"{contract.skill_name}/references/"))
            ):
                raise ValueError(f"Reference is outside the public reference whitelist: {relative}")
            path = _approved_path(root, relative)
            if not path.is_file():
                raise FileNotFoundError(f"Missing required package file: {relative}")
            files[relative] = path.read_bytes()
    return dict(sorted(files.items()))


def _manifest(contents: dict[str, bytes]) -> dict:
    files = {relative: _hash(data) for relative, data in contents.items()}
    canonical = json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        "format_version": 3,
        "rule_version": RULE_VERSION,
        "domain_versions": {domain: RULE_VERSION for domain in DOMAINS},
        "files": files,
        "canonical_manifest": canonical,
        "bundle_sha256": _hash(canonical.encode("utf-8")),
    }


def build_installation_manifest(authority_root: Path) -> dict:
    """Validate six complete packages and hash only their explicit public files."""
    root = Path(authority_root).resolve()
    return {**_manifest(_contents(root)), "source_metadata": {"source_root": root.name}}


def _verify(root: Path, filename: str) -> dict:
    actual = _manifest(_contents(root))
    saved = _read_json(_approved_path(root, filename))
    expected_keys = set(actual) | ({"source_metadata"} if filename == INSTALL_MANIFEST else set())
    if set(saved) != expected_keys:
        raise ValueError(f"Incomplete or unexpected manifest keys: {filename}")
    if any(saved.get(key) != value for key, value in actual.items()):
        raise ValueError(f"Package manifest hash/version mismatch: {filename}")
    if filename == INSTALL_MANIFEST:
        metadata = saved["source_metadata"]
        if not isinstance(metadata, dict) or set(metadata) != {"source_root"} or not isinstance(metadata["source_root"], str):
            raise ValueError("Invalid installation source metadata")
    return saved


def _save_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")


def freeze_bundle(installed_root: Path, destination: Path) -> dict:
    """Freeze a verified installation, or resume its already verified bundle.

    An existing destination is never repaired, overwritten or refreshed. Its
    full original content must still validate, independently of shared installs.
    """
    installed = Path(installed_root).resolve()
    target = Path(destination).absolute()
    if target.exists():
        if target.is_symlink():
            raise ValueError("Frozen destination must not be a symlink")
        return _verify(target.resolve(), BUNDLE_MANIFEST)
    if target.resolve().is_relative_to(installed):
        raise ValueError("Frozen destination must be separate from installed root")
    verified = _verify(installed, INSTALL_MANIFEST)
    content = _contents(installed)
    if _manifest(content)["bundle_sha256"] != verified["bundle_sha256"]:
        raise ValueError("Installed files changed while reading package")
    manifest = _manifest(content)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".skill-bundle-", dir=target.parent))
    try:
        for relative, data in content.items():
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            if _hash(output.read_bytes()) != manifest["files"][relative]:
                raise ValueError(f"Copied file hash mismatch: {relative}")
        _save_json(staging / BUNDLE_MANIFEST, manifest)
        _verify(staging.resolve(), BUNDLE_MANIFEST)
        if target.exists():
            raise ValueError("Refusing to overwrite a concurrently created frozen bundle")
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return manifest


def load_phase(bundle_root: Path, domain_id: str, phase: str) -> dict:
    """Return the full frozen Skill, contract and references for one executor."""
    if phase not in PHASES:
        raise ValueError(f"不支持的阶段: {phase}")
    root = Path(bundle_root).resolve()
    manifest = _verify(root, BUNDLE_MANIFEST)
    contract = load_domain(root, domain_id)
    relative_contract = f"{contract.skill_name}/domain.json"
    return {
        "domain_id": domain_id,
        "skill_name": contract.skill_name,
        "rule_version": contract.rule_version,
        "phase": phase,
        "skill_text": _approved_path(root, f"{contract.skill_name}/SKILL.md").read_text(encoding="utf-8"),
        "references": {relative: _approved_path(root, relative).read_text(encoding="utf-8") for relative in contract.references},
        "contract": contract.model_dump(),
        "bundle_sha256": manifest["bundle_sha256"],
        "contract_sha256": manifest["files"][relative_contract],
        "reference_hashes": {relative: manifest["files"][relative] for relative in contract.references},
    }


def verify_loaded_package(bundle_root: Path, report: dict) -> str:
    """Check actual-resolver evidence; test fixture reports are not live proof."""
    root = Path(bundle_root).resolve()
    manifest = _verify(root, BUNDLE_MANIFEST)
    if not isinstance(report, dict) or set(report) != {"rule_version", "files"} or report["rule_version"] != RULE_VERSION:
        raise ValueError("Missing or unsupported resolver load report")
    records = report["files"]
    if not isinstance(records, dict) or set(records) != set(manifest["files"]):
        raise ValueError("Resolver report must include every frozen contract/Skill/reference")
    for relative, digest in manifest["files"].items():
        record = records[relative]
        if (not isinstance(record, dict) or set(record) != {"path", "sha256"}
                or not isinstance(record["path"], str) or not Path(record["path"]).is_absolute()
                or Path(record["path"]).resolve() != _approved_path(root, relative).resolve()
                or record["sha256"] != digest):
            raise ValueError(f"Resolver did not load the frozen file/hash: {relative}")
    return manifest["bundle_sha256"]


def synchronize_skills(authority_root: Path, generated_root: Path, *, write: bool = False, install_root: Path | None = None) -> dict:
    """Check by default; write only six generated packages and required refs.

    No home-directory default, recursive package copy or independent-Skill delete.
    Installation source metadata is separate from deterministic content hashes.
    """
    root = Path(authority_root).resolve()
    content = _contents(root)
    manifest = {**_manifest(content), "source_metadata": {"source_root": root.name}}
    targets = [Path(generated_root).resolve()]
    if install_root is not None:
        targets.append(Path(install_root).resolve())
    if any(target == root or target.is_relative_to(root) or root.is_relative_to(target) for target in targets):
        raise ValueError("Copy roots must be separate from authority")
    if not write:
        _verify(root, INSTALL_MANIFEST)
        for target in targets:
            actual = _verify(target, INSTALL_MANIFEST)
            if actual != manifest:
                raise ValueError("Source/generated/installed manifest mismatch")
        return manifest
    for target in targets:
        target.mkdir(parents=True, exist_ok=True)
        for relative, data in content.items():
            output = _approved_path(target, relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            if _hash(output.read_bytes()) != manifest["files"][relative]:
                raise ValueError(f"Copied file hash mismatch: {relative}")
        _save_json(_approved_path(target, INSTALL_MANIFEST), manifest)
        _verify(target, INSTALL_MANIFEST)
    _save_json(_approved_path(root, INSTALL_MANIFEST), manifest)
    return manifest
