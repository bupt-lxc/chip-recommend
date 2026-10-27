from pathlib import Path
import re

import pytest

from chip_model.pipeline.open_web_test import TEST_SKILL_REGISTRY


ROOT = Path(__file__).resolve().parents[1]
# Preserve V2 full-chip behavior as history, not as active V3 requirements.
SKILL_ROOT = ROOT / "docs" / "archive" / "codex-domain-skills"
OPEN_WEB_SKILLS = tuple(TEST_SKILL_REGISTRY)

REQUIRED_SECTIONS = (
    "## 何时使用",
    "## 输入与运行入口",
    "## 负责字段",
    "## 执行流程",
    "## 输出与留痕",
    "## 常见错误",
    "## 完成标准",
)

HERMES_TOOL_SEQUENCE = (
    "open_web_search",
    "open_web_preview",
    "open_web_submit_selection",
)


@pytest.mark.parametrize("skill_name", OPEN_WEB_SKILLS)
def test_open_web_skills_are_self_contained_operational_contracts(
    skill_name: str,
) -> None:
    config = TEST_SKILL_REGISTRY[skill_name]
    text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert f"name: {skill_name}" in text
    description = next(
        line.removeprefix("description:").strip()
        for line in text.splitlines()
        if line.startswith("description:")
    )
    assert description.startswith("用于")
    assert re.search(r"[\u4e00-\u9fff]", description)
    assert f"version: {config['version']}" in text
    assert f"`{config['target_table']}`" in text
    assert f"--skill {skill_name}" in text
    for section in REQUIRED_SECTIONS:
        assert section in text


@pytest.mark.parametrize("skill_name", OPEN_WEB_SKILLS)
def test_open_web_skills_name_every_owned_field(skill_name: str) -> None:
    config = TEST_SKILL_REGISTRY[skill_name]
    text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    for field_name in config["fields"]:
        assert f"`{field_name}`" in text


@pytest.mark.parametrize("skill_name", OPEN_WEB_SKILLS)
def test_open_web_skills_explain_the_shared_safety_and_evidence_contract(
    skill_name: str,
) -> None:
    text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    for term in (
        "粗筛",
        "精确复核",
        "网页快照",
        "逐字证据",
        "information_categories",
        "extracted_fields",
        "url_assets.jsonl",
        "extracted_facts.jsonl",
        "正式数据库",
    ):
        assert term in text


@pytest.mark.parametrize("skill_name", OPEN_WEB_SKILLS)
def test_open_web_skills_define_the_hermes_tool_contract(skill_name: str) -> None:
    text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert "10 条" in text
    assert "不可信" in text
    assert "candidate_id" in text
    assert "suggested_skills" in text
    assert "不得包含当前 Skill" in text
    assert "没有其他类别时使用空数组" in text
    assert "只创建一层关联任务" in text
    positions = [text.index(tool) for tool in HERMES_TOOL_SEQUENCE]
    assert positions == sorted(positions)


@pytest.mark.parametrize("skill_name", OPEN_WEB_SKILLS)
def test_open_web_skills_define_full_scope_and_exact_decision_contract(
    skill_name: str,
) -> None:
    text = (SKILL_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")

    for term in (
        "芯片可选",
        "开放发现单元",
        "每个芯片单元",
        "不得使用全局前 10",
        '"unit_id"',
        '"scope_type"',
        '"candidate_id"',
        '"matched_categories"',
        '"suggested_skills"',
        "不回填本轮芯片队列",
        "重试",
        "零候选",
    ):
        assert term in text
