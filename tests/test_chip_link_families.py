"""The parent-URL catalogue is a bounded chip-only test selector."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chip_model.database import get_db_path, set_db_path
from chip_model.pipeline.data_agent import DataAgentOrchestrator, finish_agent_job
from chip_model.pipeline.link_families import (
    read_chip_families, save_family_selection, selected_family_rows,
)
from chip_model.pipeline.test_workspace import (
    _apply_selected_family, _reset_test_source_baseline, start_test_cycle,
)
from chip_model.server import app
from scripts.run_data_agent import main as data_agent_main


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "data.db"
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parent.parent / "schema.sql").read_text(encoding="utf-8"))
    return path


def test_catalogue_ignores_hf_model_and_self_parent(tmp_path: Path):
    csv_path = tmp_path / "links.csv"
    csv_path.write_text(
        "序号,父URL,URL,描述,涉及厂商,分类\n"
        "1,https://chips.example,https://chips.example,首页,厂商,芯片信息综合\n"
        "2,https://chips.example,https://chips.example/spec/gpu,规格,厂商,芯片硬件参数\n"
        "3,https://chips.example,https://chips.example/models/a,模型,厂商,模型信息\n"
        "4,https://huggingface.co,https://huggingface.co,HF首页,,芯片硬件参数\n"
        "5,https://huggingface.co,https://huggingface.co/org/model,模型,,芯片硬件参数\n",
        encoding="utf-8-sig",
    )
    families = read_chip_families(csv_path)
    assert len(families) == 1
    assert families[0]["child_count"] == 1
    assert families[0]["children"][0]["url"] == "https://chips.example/spec/gpu"
    source = _db(tmp_path)
    selection = save_family_selection(
        "https://chips.example", ["https://chips.example/spec/gpu"],
        source_db=source, csv_path=csv_path,
    )
    rows = selected_family_rows(selection, csv_path=csv_path)
    assert rows[0]["role"] == "listing"
    assert rows[0]["parent_url"] == ""
    assert rows[1]["parent_url"] == rows[0]["url"]
    with pytest.raises(ValueError):
        save_family_selection(
            "https://chips.example", ["https://chips.example/models/a"],
            source_db=source, csv_path=csv_path,
        )


def test_family_edges_and_chip_only_planning_stay_in_clone(tmp_path: Path):
    formal = _db(tmp_path)
    clone = tmp_path / "clone.db"
    with sqlite3.connect(formal) as original, sqlite3.connect(clone) as target:
        original.backup(target)
    rows = [
        {"url": "https://chips.example", "description": "厂商官网", "category": "芯片产品列表",
         "vendor": "厂商", "role": "listing", "parent_url": ""},
        {"url": "https://chips.example/spec/gpu", "description": "GPU规格", "category": "芯片硬件参数",
         "vendor": "厂商", "role": "detail", "parent_url": "https://chips.example"},
    ]
    ids = _apply_selected_family(clone, rows)
    with sqlite3.connect(clone) as db:
        root = db.execute("SELECT parent_link_id,url_role FROM source_registry WHERE link_id=?", (ids[0],)).fetchone()
        child = db.execute("SELECT parent_link_id,crawl_depth FROM source_registry WHERE link_id=?", (ids[1],)).fetchone()
        assert root == (None, "listing")
        assert child == (ids[0], 1)
        db.execute("INSERT INTO source_monitor_state (link_id,content_hash) VALUES (?, 'formal-hash')", (ids[1],))
        db.execute(
            "INSERT INTO link_library (url,description,category) "
            "VALUES ('https://huggingface.co/org/model','伪装芯片来源','芯片硬件参数')"
        )
        hf_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    _reset_test_source_baseline(clone, ids)
    with sqlite3.connect(clone) as db:
        assert db.execute("SELECT COUNT(*) FROM source_monitor_state WHERE link_id=?", (ids[1],)).fetchone()[0] == 0
    agent = DataAgentOrchestrator(db_path=clone, snapshot_dir=tmp_path / "snapshots")
    cycle = agent.start_cycle(mode="test")
    assert agent.plan(cycle, limit=5, force=True, selected_link_ids=[*ids, hf_id], chip_only=True) == 2
    with sqlite3.connect(clone) as db:
        jobs = db.execute("SELECT job_type,skill_name,link_id FROM data_agent_jobs WHERE cycle_run_id=?", (cycle,)).fetchall()
    assert len(jobs) == 2
    assert all(job[0] == "source_refresh" and job[1] == "source-refresh" for job in jobs)
    assert hf_id not in [job[2] for job in jobs]
    with sqlite3.connect(formal) as db:
        assert db.execute("SELECT COUNT(*) FROM link_library").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM source_registry").fetchone()[0] == 0


def test_test_run_requires_explicit_chip_sources(tmp_path: Path):
    formal = _db(tmp_path)
    with pytest.raises(ValueError, match="不会从全库随机抽取"):
        start_test_cycle(source_db=formal, limit=5)


def test_direct_cli_formal_run_is_paused(tmp_path: Path, capsys):
    formal = _db(tmp_path)
    assert data_agent_main(["--db", str(formal), "run", "--limit", "1"]) == 0
    assert '"status": "paused"' in capsys.readouterr().out
    with sqlite3.connect(formal) as db:
        assert db.execute("SELECT COUNT(*) FROM update_runs").fetchone()[0] == 0


def test_discovered_catalogue_child_reuses_this_cycles_visit(tmp_path: Path):
    db_path = _db(tmp_path)
    with sqlite3.connect(db_path) as db:
        root_id = db.execute(
            "INSERT INTO link_library (url,description,category) "
            "VALUES ('https://chips.example','厂商官网','芯片产品列表')"
        ).lastrowid
        child_id = db.execute(
            "INSERT INTO link_library (url,description,category) "
            "VALUES ('https://chips.example/spec/gpu','GPU规格','芯片硬件参数')"
        ).lastrowid
        cycle = db.execute(
            "INSERT INTO update_runs (run_type,mode,status,started_at,created_at) "
            "VALUES ('data_agent_cycle','test','awaiting_agent','now','now')"
        ).lastrowid
        db.execute(
            "INSERT INTO data_agent_jobs "
            "(cycle_run_id,dedupe_key,job_type,skill_name,link_id,status,scheduled_for,created_at,updated_at) "
            "VALUES (?, 'visited-child', 'source_refresh', 'source-refresh', ?, 'succeeded', 'now','now','now')",
            (cycle, child_id),
        )
        discovery = db.execute(
            "INSERT INTO data_agent_jobs "
            "(cycle_run_id,dedupe_key,job_type,skill_name,link_id,status,worker_id,"
            "scheduled_for,input_json,created_at,updated_at) "
            "VALUES (?, 'discover-root', 'agent_url_discovery', 'url-discovery', ?, "
            "'running', 'test-worker','now',?,'now','now')",
            (cycle, root_id,
             '{"url":"https://chips.example","crawl_depth":0,'
             '"outbound_links":["https://chips.example/spec/gpu"]}'),
        ).lastrowid
    result = finish_agent_job(
        discovery, "test-worker", status="succeeded", db_path=db_path,
        output={"discovered_sources": [{"url": "https://chips.example/spec/gpu",
                                        "description": "GPU规格", "category": "芯片硬件参数",
                                        "role": "detail"}]},
    )
    assert result["detail_job_ids"] == []
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM data_agent_jobs WHERE job_type='source_target_refresh'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT status,reason FROM source_discovery_edges WHERE cycle_run_id=?", (cycle,)
        ).fetchone() == ("skipped", "本轮已访问，复用已有快照")


def test_family_api_is_admin_only_and_selection_does_not_write_formal_db(tmp_path: Path):
    from tests.test_domain_acceptance import logical_digest
    formal = _db(tmp_path)
    before, _ = logical_digest(formal, tmp_path / 'Fixture-family-before.db')
    original = get_db_path()
    set_db_path(formal)
    try:
        client = TestClient(app)
        assert client.get("/api/v1/data-agent/families").status_code == 403
        token = formal.parent / "manual_control" / "admin_token"
        token.parent.mkdir()
        token.write_text("test-only-token", encoding="utf-8")
        headers = {"X-Data-Agent-Admin": "test-only-token"}
        formal_run = client.post("/api/v1/data-agent/manual-run", headers=headers,
                                 json={"mode": "formal", "limit": 5})
        # Raw JSON validation now returns the explicit 400 rejection contract.
        assert formal_run.status_code == 400
        assert '正式巡检已暂停' in formal_run.json()['detail']
        assert not list((formal.parent / 'manual_control/requests').glob('*.json'))
        assert not (formal.parent / 'domain_runs').exists()
        rejected, _ = logical_digest(formal, tmp_path / 'Fixture-family-rejected.db')
        assert rejected == before
        families = client.get("/api/v1/data-agent/families", headers=headers).json()["families"]
        assert families
        family = families[0]
        selected = client.post(
            "/api/v1/data-agent/family-selection", headers=headers,
            json={"parent_url": family["parent_url"],
                  "child_urls": [family["children"][0]["url"]]},
        )
        assert selected.status_code == 200
        assert client.get("/api/v1/data-agent/families", headers=headers).json()["selection"]["parent_url"] == family["parent_url"]
        with sqlite3.connect(formal) as db:
            assert db.execute("SELECT COUNT(*) FROM link_library").fetchone()[0] == 0
        selected_digest, _ = logical_digest(formal, tmp_path / 'Fixture-family-selected.db')
        assert selected_digest == before
    finally:
        set_db_path(original)
