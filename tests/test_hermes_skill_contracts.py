from pathlib import Path
import re

import pytest

from chip_model.pipeline.data_agent import SKILL_FIELD_TARGETS


ROOT = Path(__file__).resolve().parents[1]
# Historical V2 contracts; these assertions do not prove active V3 execution.
SKILL_ROOT = ROOT / "docs" / "archive" / "hermes-skills"

FORMAL_SKILLS = (
    "url-discovery",
    "chip-catalog",
    "chip-basic",
    "chip-compute",
    "chip-interconnect",
    "chip-ecosystem",
    "chip-price",
    "benchmark-ingest",
    "deployment-ingest",
    "data-quality-review",
    "model-catalog",
    "model-activity",
)

REQUIRED_SECTIONS = (
    "## 何时使用",
    "## 输入契约",
    "## 执行流程",
    "## 输出契约",
    "## 拒绝与失败",
    "## 完成标准",
)


def _skill_path(skill_name: str) -> Path:
    if skill_name in {"model-catalog", "model-activity"}:
        return ROOT / ".hermes-skills" / skill_name / "SKILL.md"
    return SKILL_ROOT / skill_name / "SKILL.md"


@pytest.mark.parametrize("skill_name", FORMAL_SKILLS)
def test_formal_hermes_skills_are_self_contained(skill_name: str) -> None:
    path = _skill_path(skill_name)
    text = path.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert f"name: {skill_name}" in text
    assert "version: 2.0.0" in text
    description = next(
        line.removeprefix("description:").strip()
        for line in text.splitlines()
        if line.startswith("description:")
    )
    assert re.search(r"[\u4e00-\u9fff]", description)
    for section in REQUIRED_SECTIONS:
        assert section in text


@pytest.mark.parametrize(
    "skill_name",
    tuple(name for name in FORMAL_SKILLS if name != "data-quality-review"),
)
def test_extraction_skills_document_the_worker_contract(skill_name: str) -> None:
    text = (_skill_path(skill_name)).read_text(encoding="utf-8")
    for term in (
        "source_url",
        "evidence_text",
        "entity_key",
        "facts",
        "inbox",
        "discovered_sources",
    ):
        assert term in text


@pytest.mark.parametrize(
    "skill_name",
    (
        "chip-catalog",
        "chip-basic",
        "chip-compute",
        "chip-interconnect",
        "chip-ecosystem",
        "chip-price",
        "benchmark-ingest",
        "deployment-ingest",
    ),
)
def test_snapshot_extract_skills_document_runtime_provenance(skill_name: str) -> None:
    text = (_skill_path(skill_name)).read_text(encoding="utf-8")
    for term in (
        "job.input.url",
        "evidence_scope",
        "snapshot_path",
        "diff_path",
        "evidence_location",
        "source_type",
        "input.outbound_links",
        "{to_skill,topic,payload}",
        "{job_id,worker_id,status,output",
    ):
        assert term in text
    for confidence in ("high", "medium", "low"):
        assert confidence in text


@pytest.mark.parametrize(
    "skill_name", tuple(name for name in FORMAL_SKILLS if name in SKILL_FIELD_TARGETS)
)
def test_formal_skills_name_every_owned_field(skill_name: str) -> None:
    text = (_skill_path(skill_name)).read_text(encoding="utf-8")
    for field_names in SKILL_FIELD_TARGETS[skill_name].values():
        for field_name in field_names:
            assert f"`{field_name}`" in text


@pytest.mark.parametrize("skill_name", ("model-catalog", "model-activity"))
def test_model_skills_are_not_routed_into_chip_open_web_flow(skill_name: str) -> None:
    text = (_skill_path(skill_name)).read_text(encoding="utf-8")
    assert "当前芯片开放互联网流程不得调用" in text
    assert "API" in text


def test_project_specific_field_formats_are_explicit() -> None:
    compute = (SKILL_ROOT / "chip-compute" / "SKILL.md").read_text(encoding="utf-8")
    interconnect = (SKILL_ROOT / "chip-interconnect" / "SKILL.md").read_text(encoding="utf-8")
    ecosystem = (SKILL_ROOT / "chip-ecosystem" / "SKILL.md").read_text(encoding="utf-8")
    price = (SKILL_ROOT / "chip-price" / "SKILL.md").read_text(encoding="utf-8")
    benchmark = (SKILL_ROOT / "benchmark-ingest" / "SKILL.md").read_text(encoding="utf-8")

    assert "BF16=1980TF,FP8=3960TF,INT8=3960TOPS" in compute
    assert "`proposed_value` 只放纯数字" in interconnect
    assert '`"0"`/`"1"`' in ecosystem
    assert '`"0"`–`"5"`' in ecosystem
    assert "`2025 Q2`" in price
    assert "租金误当成硬件采购价" in price
    assert "云租赁价格：只生成 `price_notes`" in price
    for key in (
        "chip_model",
        "model_id",
        "workload_type",
        "suite_name",
        "chip_count",
        "precision",
    ):
        assert f"`{key}`" in benchmark


def test_control_skills_do_not_fake_success_without_required_inputs() -> None:
    quality = (SKILL_ROOT / "data-quality-review" / "SKILL.md").read_text(encoding="utf-8")
    catalog = (SKILL_ROOT / "chip-catalog" / "SKILL.md").read_text(encoding="utf-8")

    assert "review_bundle" in quality
    assert "缺少 review_bundle" in quality
    assert "facts: []" in quality
    assert "`scope`" in catalog
    assert "`max_entities`" in catalog
    assert "没有搜索能力就 `rejected`" in catalog


def test_deployment_guide_preserves_scope_and_table_source_type() -> None:
    text = (SKILL_ROOT / "deployment-ingest" / "SKILL.md").read_text(encoding="utf-8")
    assert '"chip_model":"H100"' in text
    assert '"model_id":"org/model"' in text
    assert '"source_type":"official_doc"' in text
    assert "不会自动填入指南表" in text
