"""V3 package tests; resolver reports below are simulated, not live Hermes proof."""

import hashlib
import importlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from chip_model.pipeline.domain_discovery.contracts import DOMAINS


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / ".hermes-skills"


def test_bundle_api_is_available():
    assert importlib.util.find_spec("chip_model.pipeline.domain_discovery.rules") is not None


@pytest.fixture
def rules():
    name = "chip_model.pipeline.domain_discovery.rules"
    assert importlib.util.find_spec(name) is not None, "missing frozen Skill bundle implementation"
    return importlib.import_module(name)


@pytest.fixture
def installed(tmp_path, rules):
    root = tmp_path / "installed"
    for domain in DOMAINS:
        package = root / f"chip-{domain}"
        package.mkdir(parents=True)
        shutil.copyfile(AUTHORITY / f"chip-{domain}" / "domain.json", package / "domain.json")
        (package / "SKILL.md").write_text(
            f"---\nname: chip-{domain}\ndescription: 用于领域资料发现。\n---\n"
            "字段范围以 domain.json 为准；规则版本 3.0.0。\n"
            + "\n".join(f"## {phase}\n测试阶段规则" for phase in rules.PHASES), encoding="utf-8"
        )
        references = package / "references"
        references.mkdir()
        (references / f"{domain}-rules.md").write_text(f"{domain} 测试规则", encoding="utf-8")
    (root / "_shared").mkdir()
    (root / "_shared" / "rules.md").write_text("测试公共规则", encoding="utf-8")
    (root / "installed.manifest.json").write_text(
        json.dumps(rules.build_installation_manifest(root), ensure_ascii=False), encoding="utf-8"
    )
    # Not on the whitelist, and must never appear in a frozen export.
    (root / "config.env").write_text("SECRET=do-not-export", encoding="utf-8")
    return root


def test_freeze_is_immutable_and_resume_uses_original_hash(installed, tmp_path, rules):
    destination = tmp_path / "bundle"
    manifest = rules.freeze_bundle(installed, destination)
    before = rules.load_phase(destination, "specs", "extract")
    assert manifest["bundle_sha256"] == hashlib.sha256(manifest["canonical_manifest"].encode()).hexdigest()
    assert json.loads(manifest["canonical_manifest"]) == manifest["files"]
    assert not (destination / "config.env").exists()
    (installed / "chip-specs" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert rules.load_phase(destination, "specs", "extract") == before
    assert rules.freeze_bundle(installed, destination) == manifest
    assert before["skill_text"].startswith("---")
    assert "## plan" in before["skill_text"] and "## review" in before["skill_text"]
    assert before["contract"]["domain_id"] == "specs"
    assert set(before["references"]) == set(before["contract"]["references"])


@pytest.mark.parametrize("damage", ["missing_reference", "missing_manifest", "hash_mismatch", "version_mismatch"])
def test_installed_package_damage_fails_before_copy(installed, tmp_path, rules, damage):
    if damage == "missing_reference":
        (installed / "_shared" / "rules.md").unlink()
    elif damage == "missing_manifest":
        (installed / "installed.manifest.json").unlink()
    elif damage == "hash_mismatch":
        (installed / "chip-specs" / "SKILL.md").write_text("changed", encoding="utf-8")
    else:
        path = installed / "installed.manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["rule_version"] = "2.0.0"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        rules.freeze_bundle(installed, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("damage", ["file", "missing_reference", "manifest", "incomplete", "malformed"])
def test_frozen_damage_never_falls_back_to_installed(installed, tmp_path, rules, damage):
    bundle = tmp_path / "bundle"
    rules.freeze_bundle(installed, bundle)
    path = bundle / "bundle.manifest.json"
    if damage == "file":
        (bundle / "chip-identity" / "SKILL.md").write_text("tampered", encoding="utf-8")
    elif damage == "missing_reference":
        (bundle / "_shared" / "rules.md").unlink()
    elif damage == "malformed":
        path.write_text("{", encoding="utf-8")
    else:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if damage == "manifest":
            manifest["bundle_sha256"] = "0" * 64
        else:
            del manifest["files"]["chip-compute/domain.json"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, FileNotFoundError)):
        rules.load_phase(bundle, "specs", "extract")
    with pytest.raises((ValueError, FileNotFoundError)):
        rules.freeze_bundle(installed, bundle)


@pytest.mark.parametrize("reference", ["../outside.md", "/absolute.md", "C:/secrets.env", "https://site/rules.md", "_shared/../rules.md", "_shared\\rules.md"])
def test_reference_whitelist_rejects_untrusted_paths(installed, tmp_path, rules, reference):
    path = installed / "chip-specs" / "domain.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["references"].append(reference)
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError):
        rules.freeze_bundle(installed, tmp_path / "bundle")


def test_reference_symlink_escape_is_rejected(installed, tmp_path, rules):
    outside = tmp_path / "outside.md"
    outside.write_text("untrusted", encoding="utf-8")
    shared = installed / "_shared" / "rules.md"
    shared.unlink()
    try:
        shared.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    with pytest.raises(ValueError, match="escape|root"):
        rules.freeze_bundle(installed, tmp_path / "bundle")


@pytest.mark.parametrize("removed", ["_shared/rules.md", "chip-specs/references/specs-rules.md"])
def test_required_references_cannot_be_removed_from_contract(installed, rules, removed):
    path = installed / "chip-specs" / "domain.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["references"].remove(removed)
    path.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(ValueError, match="required|必需"):
        rules.build_installation_manifest(installed)


def test_invalid_or_missing_phase_fails_explicitly(installed, tmp_path, rules):
    bundle = tmp_path / "bundle"
    rules.freeze_bundle(installed, bundle)
    with pytest.raises(ValueError, match="阶段"):
        rules.load_phase(bundle, "specs", "publish")
    path = installed / "chip-specs" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8").replace("## review", "## unsupported"), encoding="utf-8")
    with pytest.raises(ValueError, match="阶段"):
        rules.build_installation_manifest(installed)


def test_actual_resolver_report_must_match_every_frozen_file(installed, tmp_path, rules):
    bundle = tmp_path / "bundle"
    manifest = rules.freeze_bundle(installed, bundle)
    report = {"rule_version": "3.0.0", "files": {
        relative: {"path": str(bundle / relative), "sha256": digest}
        for relative, digest in manifest["files"].items()
    }}
    assert rules.verify_loaded_package(bundle, report) == manifest["bundle_sha256"]
    report["files"]["chip-specs/SKILL.md"]["path"] = str(installed / "chip-specs" / "SKILL.md")
    with pytest.raises(ValueError, match="frozen|冻结"):
        rules.verify_loaded_package(bundle, report)


@pytest.mark.parametrize("damage", ["missing", "digest", "version"])
def test_resolver_report_fails_closed(installed, tmp_path, rules, damage):
    bundle = tmp_path / "bundle"
    manifest = rules.freeze_bundle(installed, bundle)
    report = {"rule_version": "3.0.0", "files": {
        relative: {"path": str(bundle / relative), "sha256": digest}
        for relative, digest in manifest["files"].items()
    }}
    if damage == "missing":
        del report["files"]["_shared/rules.md"]
    elif damage == "digest":
        report["files"]["_shared/rules.md"]["sha256"] = "0" * 64
    else:
        report["rule_version"] = "2.0.0"
    with pytest.raises(ValueError):
        rules.verify_loaded_package(bundle, report)


def test_sync_installs_only_authority_and_preserves_independent_skills(tmp_path, rules):
    generated = tmp_path / "generated"
    installed = tmp_path / "installed"
    independent = generated / "chip-catalog" / "SKILL.md"
    independent.parent.mkdir(parents=True)
    independent.write_text("independent", encoding="utf-8")
    manifest = rules.synchronize_skills(AUTHORITY, generated, write=True, install_root=installed)
    for relative, digest in manifest["files"].items():
        assert (AUTHORITY / relative).read_bytes() == (generated / relative).read_bytes() == (installed / relative).read_bytes()
        assert hashlib.sha256((installed / relative).read_bytes()).hexdigest() == digest
    assert independent.read_text() == "independent"
    assert not (installed / "model-catalog").exists()
    rules.freeze_bundle(installed, tmp_path / "bundle")
    rules.synchronize_skills(AUTHORITY, generated)
    path = generated / "chip-specs" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash|哈希|mismatch"):
        rules.synchronize_skills(AUTHORITY, generated)


def test_source_generated_manifest_and_references_are_consistent(rules):
    rules.synchronize_skills(AUTHORITY, ROOT / ".agents" / "skills")
    for domain in DOMAINS:
        skill = (AUTHORITY / f"chip-{domain}" / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1].strip().splitlines()
        assert len(frontmatter) == 2
        assert frontmatter[0] == f"name: chip-{domain}"
        assert frontmatter[1].startswith("description: 用于")
        assert "domain.json" in skill
        assert [line for line in skill.splitlines() if line.startswith("## ")] == [f"## {phase}" for phase in rules.PHASES]


def test_legacy_setup_and_primary_installer_fail_before_mutation():
    for filename in ("_hermes_skills_setup.sh", "scripts/install_hermes_data_heartbeat_primary.sh"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "已归档" in text and "install_hermes_open_web_plugin.sh" in text
        assert text.index("exit 1") < text.index("mkdir") if "mkdir" in text else "exit 1" in text


def test_cli_check_is_readonly_and_does_not_require_hermes(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/sync_domain_skills.py"), "--check"], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_original_compute_archive_is_byte_identical_to_baseline():
    original = subprocess.run(["git", "show", "f9a22a5:.hermes-skills/chip-compute/SKILL.md"], cwd=ROOT, capture_output=True, check=True).stdout
    assert (ROOT / "docs/archive/hermes-skills/chip-compute/SKILL.md").read_bytes() == original


def test_installer_reads_authority_and_backs_up_before_sync():
    text = (ROOT / "scripts/install_hermes_open_web_plugin.sh").read_text(encoding="utf-8")
    assert '${PROJECT_ROOT}/.hermes-skills/${skill}' in text
    assert '.agents/skills/${skill}' not in text
    assert "--check" in text and "--write --install-root" in text
    assert '"${backup_dir}/skills/${skill}"' in text
    assert "_shared" in text and "installed.manifest.json" in text
    assert text.index('"${backup_dir}/skills/${skill}"') < text.index("--write --install-root")


def test_bundle_hash_is_portable_and_sorted(installed, tmp_path, rules):
    other = tmp_path / "other-installed"
    shutil.copytree(installed, other)
    first = rules.freeze_bundle(installed, tmp_path / "first")
    second = rules.freeze_bundle(other, tmp_path / "second")
    assert first == second
    assert list(first["files"]) == sorted(first["files"])
    assert "source_metadata" not in first
    assert all(not Path(relative).is_absolute() for relative in first["files"])


def test_git_checkout_preserves_manifest_bytes_with_autocrlf(tmp_path, rules):
    """Real local Git checkout, with Windows conversion enabled; no remote."""
    repo = tmp_path / "repo"
    checkout = tmp_path / "checkout"
    repo.mkdir()
    manifest = rules.build_installation_manifest(AUTHORITY)
    for prefix in (".hermes-skills", ".agents/skills"):
        for relative in (*manifest["files"], "installed.manifest.json"):
            target = repo / prefix / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / prefix / relative).read_bytes())
    shutil.copyfile(ROOT / ".gitattributes", repo / ".gitattributes")
    original_compute = "docs/archive/hermes-skills/chip-compute/SKILL.md"
    target = repo / original_compute
    target.parent.mkdir(parents=True)
    target.write_bytes((ROOT / original_compute).read_bytes())
    for command in (["init", "--quiet"], ["config", "core.autocrlf", "true"], ["add", "."], ["checkout-index", "--all", "--prefix=" + checkout.as_posix() + "/"]):
        subprocess.run(["git", "-C", str(repo), *command], capture_output=True, check=True)
    checked = rules.synchronize_skills(checkout / ".hermes-skills", checkout / ".agents/skills")
    assert checked["bundle_sha256"] == manifest["bundle_sha256"]
    assert (checkout / original_compute).read_bytes() == (ROOT / original_compute).read_bytes()
