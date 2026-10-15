import sqlite3
from pathlib import Path

import pytest

from chip_model.pipeline.hermes_open_web_orchestrator import (
    build_hermes_prompt,
    run_hermes_open_web_test,
)
from tests.test_domain_acceptance import logical_digest


SCHEMA = (Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8')


def _database(path: Path) -> Path:
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
    return path


def test_prompt_only_passes_run_context_and_leaves_process_to_skill() -> None:
    prompt = build_hermes_prompt(
        run_id="20260929-120000-abcdef12",
        unit_id="chip-nvidia-h100",
        scope_type="chip",
        skill="chip-specs",
        target_chip="NVIDIA H100",
        target_fields=["vram_gb", "vram_bw_gb_s"],
    )
    assert "chip-specs" in prompt
    assert "NVIDIA H100" in prompt
    assert "UNIT_ID=chip-nvidia-h100" in prompt
    assert "按照已加载 Skill" in prompt
    assert "open_web_search" not in prompt
    assert "10 条" not in prompt


def test_orchestrator_rejects_custom_fields_before_runner_or_store(tmp_path: Path) -> None:
    formal = _database(tmp_path / "data.db")
    before, _ = logical_digest(formal, tmp_path / 'Fixture-before-fields.db')
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        pytest.fail('Deprecated field overrides must not invoke a legacy runner')

    with pytest.raises(ValueError, match='字段范围已停用'):
        run_hermes_open_web_test(source_db=formal, skill='chip-specs', target_chip='NVIDIA H100',
                                target_fields=['vram_gb'], runner=runner, hermes_command=['hermes'])
    after, _ = logical_digest(formal, tmp_path / 'Fixture-after-fields.db')
    assert before == after and calls == []
    assert not (tmp_path / 'test_runs').exists()
    assert not (tmp_path / 'domain_runs').exists()


def test_subprocess_only_runner_cannot_create_per_chip_units_or_fake_v3_success(tmp_path: Path) -> None:
    formal = _database(tmp_path / "data.db")
    with sqlite3.connect(formal) as db:
        db.executemany(
            "INSERT INTO chips (chip_model,vendor) VALUES (?,?)",
            [("Chip A", "Vendor"), ("Chip B", "Vendor")],
        )
    calls = []
    before, _ = logical_digest(formal, tmp_path / 'Fixture-before-legacy-runner.db')

    def runner(command, **kwargs):
        calls.append(command)
        pytest.fail('Subprocess callbacks cannot replace the audited V3 Hermes adapter')

    with pytest.raises(ValueError, match='需要 Hermes preflight/run'):
        run_hermes_open_web_test(source_db=formal, skill='chip-specs', target_chip=None,
                                runner=runner, hermes_command=['hermes'])
    after, _ = logical_digest(formal, tmp_path / 'Fixture-after-legacy-runner.db')
    assert before == after and calls == []
    assert not (tmp_path / 'test_runs').exists()
    assert not (tmp_path / 'domain_runs').exists()
