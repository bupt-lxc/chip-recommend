"""CLI and API share one strict domain start contract, without database lookups."""

import copy
import importlib
import json
import sys
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

from chip_model.pipeline.domain_discovery import contracts
from scripts import run_hermes_open_web_test as cli


def normalize(payload, **kwargs):
    normalizer = getattr(contracts, "normalize_start", None)
    assert callable(normalizer), "normalize_start must provide the shared start contract"
    return normalizer(payload, **kwargs)


def expected(domain="specs", chip=None, resume=None):
    return {"domain": domain, "chip": chip, "scope_type": "chip" if chip else "discovery", "resume_run_id": resume}


@pytest.mark.parametrize("domain", contracts.DOMAINS)
def test_default_start_is_exact_discovery_payload(domain):
    assert normalize({"domain": domain}) == expected(domain)


def test_accepts_read_only_mapping_without_mutating_input():
    payload = {"skill": "chip-specs", "chips": ["  Novel-X 64GB  "], "request_id": "request-1", "mode": "test", "pipeline": "open_web"}
    original = copy.deepcopy(payload)
    assert normalize(MappingProxyType(payload)) == expected(chip="Novel-X 64GB")
    assert payload == original


@pytest.mark.parametrize("payload", [
    {"domain": "specs", "chips": []},
    {"domain": "specs", "chip": None},
    {"domain": "specs", "chip": None, "chips": []},
    {"domain": "specs", "chip": None, "chips": ["A"]},
    {"domain": "specs", "chip": "A", "chips": []},
    {"domain": "specs", "chip": " A ", "chips": ["A"]},
    {"domain": "specs", "skill": "chip-specs", "chip": "A"},
    {"skill": "chip-specs", "chips": ["A"]},
    {"domain": "specs", "chip": "A" * 160},
])
def test_aliases_and_single_chip_are_normalized(payload):
    chip = payload.get("chip") or next(iter(payload.get("chips", [])), None)
    chip = chip.strip() if chip is not None else None
    assert normalize(payload) == expected(chip=chip)


@pytest.mark.parametrize("payload", [
    None, [], "specs", 1, {}, {"domain": None}, {"domain": []},
    {"domain": "SPECS"}, {"domain": " specs "}, {"domain": "price"},
    {"skill": None}, {"skill": []}, {"skill": "specs"}, {"skill": "chip-price"},
    {"domain": "specs", "skill": "chip-compute"},
    {"domain": "specs", "skill": None},
    {"domain": "specs", "target_fields": ["tdp_w"]},
    {"domain": "specs", "queries": ["fixed query"]},
    {"domain": "specs", "timeout": 900}, {"domain": "specs", "unexpected": True},
    {"domain": "specs", "mode": "formal"}, {"domain": "specs", "mode": None},
    {"domain": "specs", "pipeline": "formal"}, {"domain": "specs", "pipeline": None},
    {"domain": "specs", "chip": 0},
    {"domain": "specs", "chip": " "}, {"domain": "specs", "chip": "A" * 161},
    {"domain": "specs", "chips": None}, {"domain": "specs", "chips": "A"},
    {"domain": "specs", "chips": ("A",)}, {"domain": "specs", "chips": [None]},
    {"domain": "specs", "chips": [" "]}, {"domain": "specs", "chips": ["A", "B"]},
    {"domain": "specs", "chip": "A", "chips": ["B"]},
    {"domain": "specs", "chip": "A", "chips": None},
    {"domain": "specs", "request_id": " "},
    {"domain": "specs", "request_id": 1},
    {"domain": "specs", "request_id": []},
    {"domain": "specs", "request_id": "a" * 201},
    {"domain": "specs", "request_id": "a\nb"},
    {"domain": "specs", "request_id": "\tvalid"},
    {"domain": "specs", "request_id": "a\u0085b"},
    {"domain": "specs", 1: True},
])
def test_invalid_requests_are_rejected_with_clear_chinese_error(payload):
    with pytest.raises(ValueError) as failure:
        normalize(payload)
    assert any("\u4e00" <= char <= "\u9fff" for char in str(failure.value))


@pytest.mark.parametrize("resume", [None, "run-abc_123", "a" * 128])
def test_resume_stays_separate_from_discovery_scope(resume):
    assert normalize({"domain": "specs", "resume_run_id": resume}) == expected(resume=resume)


@pytest.mark.parametrize("resume", ["", " ", 1, [], "../run", "run/child", r"run\child", "C:run", "/run", "..", "a\x00b", "a*b", "CON", "NUL", "com1", "LPT9", "_run", "-run", "a" * 129, "芯片"])
def test_resume_rejects_nonportable_path_ids(resume):
    with pytest.raises(ValueError, match="续跑|运行"):
        normalize({"domain": "specs", "resume_run_id": resume})


@pytest.mark.parametrize("payload, header", [
    ({"domain": "specs"}, None),
    ({"domain": "specs", "request_id": None}, None),
    ({"domain": "specs", "request_id": None}, "request-1"),
    ({"domain": "specs", "request_id": "request-1"}, None),
    ({"domain": "specs"}, "request-1"),
    ({"domain": "specs", "request_id": "request-1"}, "request-1"),
    ({"domain": "specs", "request_id": " request-1 "}, "request-1"),
])
def test_idempotency_keys_are_validated_without_entering_business_payload(payload, header):
    assert normalize(payload, idempotency_key=header) == expected()


@pytest.mark.parametrize("header", ["", " ", 1, [], "a" * 201, "a\nb", "a\u0085b"])
def test_invalid_idempotency_header_is_never_ignored(header):
    with pytest.raises(ValueError, match="幂等|请求"):
        normalize({"domain": "specs"}, idempotency_key=header)


def test_body_and_header_keys_must_match():
    with pytest.raises(ValueError, match="一致"):
        normalize({"domain": "specs", "request_id": "one"}, idempotency_key="two")


def capturing_executor(calls, status="success"):
    def execute(request, dependencies):
        calls.append((request, dependencies))
        return {"run_id": "run-1", "request_id": dependencies["request_id"], "status": status, "artifact_path": "结果/run-1"}
    return execute


def test_cli_normalizes_discovery_and_separates_runtime_options(capsys):
    calls = []
    assert cli.main(["--domain", "specs", "--db", "not-opened.db"], executor=capturing_executor(calls)) == 0
    request, dependencies = calls[0]
    assert request == expected()
    assert dependencies["source_db"] == Path("not-opened.db")
    assert dependencies["timeout"] == 1800
    assert isinstance(dependencies["request_id"], str) and dependencies["request_id"].strip()
    assert set(dependencies) == {"source_db", "timeout", "request_id"}
    output = capsys.readouterr().out
    assert "结果" in output
    assert json.loads(output) == {"run_id": "run-1", "request_id": dependencies["request_id"], "status": "success", "artifact_path": "结果/run-1"}


def test_cli_old_skill_is_only_a_domain_alias(capsys):
    calls = []
    argv = ["--skill", "chip-specs", "--domain", "specs", "--chip", "  Novel-X 64GB  ", "--resume", "run-old", "--request-id", " request-1 ", "--timeout", "1"]
    assert cli.main(argv, executor=capturing_executor(calls)) == 0
    assert calls[0][0] == expected(chip="Novel-X 64GB", resume="run-old")
    assert calls[0][1]["request_id"] == "request-1"
    assert calls[0][1]["timeout"] == 1
    assert json.loads(capsys.readouterr().out)["request_id"] == "request-1"


@pytest.mark.parametrize("status, code", [("success", 0), ("no_findings", 0), ("partial", 2), ("failed", 2), ("interrupted", 2), ("error", 2)])
def test_cli_exit_code_reflects_executor_status(capsys, status, code):
    assert cli.main(["--domain", "specs"], executor=capturing_executor([], status)) == code
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize("provided_key", [None, "wrong-key"])
def test_cli_preserves_its_request_key_when_executor_omits_or_changes_it(capsys, provided_key):
    response = {"run_id": "run-1", "status": "success", "artifact_path": "run-1"}
    if provided_key is not None:
        response["request_id"] = provided_key
    original = response.copy()
    assert cli.main(["--domain", "specs", "--request-id", "request-1"], executor=lambda request, dependencies: response) == 0
    assert json.loads(capsys.readouterr().out)["request_id"] == "request-1"
    assert response == original


def test_cli_generated_request_key_is_preserved_when_executor_omits_it(capsys):
    keys = []
    def execute(request, dependencies):
        keys.append(dependencies["request_id"])
        return {"status": "no_findings"}
    assert cli.main(["--domain", "specs"], executor=execute) == 0
    assert json.loads(capsys.readouterr().out) == {"run_id": None, "request_id": keys[0], "status": "no_findings", "artifact_path": None}


def test_cli_malformed_executor_result_returns_structured_error(capsys):
    assert cli.main(["--domain", "specs", "--request-id", "request-1"], executor=lambda request, dependencies: None) == 2
    response = json.loads(capsys.readouterr().out)
    assert response["status"] == "error"
    assert response["request_id"] == "request-1"


@pytest.mark.parametrize("argv, message", [
    ([], "数据域"), (["--domain", "price"], "数据域"),
    (["--skill", "chip-price"], "Skill"),
    (["--domain", "specs", "--skill", "chip-compute"], "一致"),
    (["--domain", "specs", "--chip", " "], "芯片"),
    (["--domain", "specs", "--field", "tdp_w"], "请按数据域运行"),
    (["--domain", "specs", "--workers", "2"], "unrecognized arguments"),
    (["--domain", "specs", "--timeout", "0"], "1800"),
    (["--domain", "specs", "--timeout", "1801"], "1800"),
    (["--domain", "specs", "--timeout", "-1"], "1800"),
    (["--domain", "specs", "--timeout", "bad"], "1800"),
    (["--domain", "specs", "--request-id", " "], "请求"),
    (["--domain", "specs", "--resume", "../run"], "续跑"),
])
def test_parser_errors_exit_two_before_executor(argv, message, capsys):
    calls = []
    with pytest.raises(SystemExit) as failure:
        cli.main(argv, executor=capturing_executor(calls))
    assert failure.value.code == 2
    assert calls == []
    assert message in capsys.readouterr().err


def test_cli_help_advertises_open_web_discovery(capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main(["--help"], executor=capturing_executor([]))
    assert failure.value.code == 0
    help_text = capsys.readouterr().out
    assert "开放互联网发现" in help_text
    assert "全部芯片" not in help_text
    assert "--workers" not in help_text


def test_cli_executor_exception_is_structured_json(capsys):
    def failing_executor(request, dependencies):
        raise RuntimeError("执行失败")
    assert cli.main(["--domain", "specs", "--request-id", "request-1"], executor=failing_executor) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "error"
    assert result["request_id"] == "request-1"
    assert result["run_id"] is None
    assert result["artifact_path"] is None
    assert "执行失败" in result["error"]


def test_production_default_is_structured_unavailable_until_v3_runtime_exists(monkeypatch, capsys):
    runtime_name = "chip_model.pipeline.domain_discovery.runtime"
    monkeypatch.setitem(sys.modules, runtime_name, None)
    assert cli.main(["--domain", "specs", "--request-id", "request-1"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "runtime_unavailable"
    assert result["request_id"] == "request-1"
    assert result["run_id"] is None and result["artifact_path"] is None
    assert "Task 12" not in result["error"]


def test_production_default_delegates_only_to_v3_runtime(monkeypatch, capsys):
    calls = []
    module = ModuleType("chip_model.pipeline.domain_discovery.runtime")
    module.run_domain = capturing_executor(calls)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert cli.main(["--domain", "compute", "--request-id", "request-1"]) == 0
    assert calls[0][0] == expected(domain="compute")
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-1"


def test_cli_import_does_not_import_legacy_orchestrator(monkeypatch):
    legacy = "chip_model.pipeline.hermes_open_web_orchestrator"
    monkeypatch.setitem(sys.modules, legacy, None)
    importlib.reload(cli)
    assert not hasattr(cli, "run_hermes_open_web_test")
