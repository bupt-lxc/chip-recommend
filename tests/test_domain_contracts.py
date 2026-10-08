"""The discovery bundles own fields; the runtime only validates their contracts."""

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from chip_model.pipeline.domain_discovery.contracts import (
    DOMAINS,
    DomainContract,
    load_domain,
    validate_domain_schema,
)


PROJECT = Path(__file__).resolve().parents[1]
BUNDLES = PROJECT / ".hermes-skills"
EXPECTED_FIELDS = {
    "identity": {"chips": "vendor vendor_display vendor_region chip_series chip_model chip_type usage tier release_date production_status eol_date target_market is_released expected_release_date".split()},
    "specs": {"chips": "architecture arch_codename generation process_node_nm foundry die_size_mm2 transistors_b package_type is_chiplet vram_gb vram_type vram_bus_bit vram_bw_gb_s vram_clock_mhz l1_cache_kb l2_cache_mb on_chip_sram_mb base_clock_mhz boost_clock_mhz tdp_w max_power_w psu_w power_connector board_length_mm board_width_mm slot_width form_factor bus_interface interconnect_bw_gb_s interconnect_tech network_interface".split()},
    "compute": {"chips": "precision_support precision_perf compute_units tensor_cores rt_cores shading_units sm_count".split()},
    "compatibility": {
        "chips": "software_stack compatible_frameworks framework_compat sw_stack".split(),
        "chip_model_compatibility": "compat_status framework precision verified_at notes".split(),
    },
    "benchmark": {"chip_model_benchmarks": "scenario task hardware_config framework batch_size input_seq_length output_seq_length concurrency prefill_throughput decode_throughput time_to_first_token_ms inter_token_latency_ms memory_peak_mb throughput_tok_s throughput_samples_s tpot_ms mfu_pct gpu_hours training_tokens_T training_gpu_count training_workload_type test_date notes citation evidence_level methodology reference_url".split()},
    "deployment": {"deployment_guides": ["backend", "notes"]},
}
EXPECTED_KEYS = {
    "identity": {"chips": ["vendor", "chip_model"]},
    "specs": {"chips": ["vendor", "chip_model"]},
    "compute": {"chips": ["vendor", "chip_model"]},
    "compatibility": {"chips": ["vendor", "chip_model"], "chip_model_compatibility": ["chip_model", "model_id"]},
    "benchmark": {"chip_model_benchmarks": "chip_model model_id suite_name workload_type chip_count precision".split()},
    "deployment": {"deployment_guides": "url title chip_model model_id source_type".split()},
}


def raw_contract(domain="identity"):
    return json.loads((BUNDLES / f"chip-{domain}" / "domain.json").read_text(encoding="utf-8"))


def write_contract(root, payload, domain="identity"):
    path = root / f"chip-{domain}" / "domain.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("domain", EXPECTED_FIELDS)
def test_complete_fields_and_entity_keys_are_owned_by_bundle(domain):
    contract = load_domain(BUNDLES, domain)
    assert contract.domain_id == domain
    assert contract.skill_name == f"chip-{domain}"
    assert contract.rule_version == "3.0.0"
    assert contract.label.strip()
    assert {t: set(v) for t, v in contract.fields.items()} == {t: set(v) for t, v in EXPECTED_FIELDS[domain].items()}
    assert contract.entity_keys == EXPECTED_KEYS[domain]
    for table in contract.fields:
        if table != "chips":
            assert not set(contract.entity_keys[table]) & set(contract.fields[table])
            assert "model_id" in contract.entity_keys[table]
            assert "model_id" not in contract.fields[table]
    assert json.loads(contract.model_dump_json())["conditions"] == raw_contract(domain)["conditions"]
    assert json.loads(contract.model_dump_json())["units"] == raw_contract(domain)["units"]


def test_six_domains_have_no_duplicate_chip_field_ownership_or_out_of_scope_facts():
    assert DOMAINS == ("identity", "specs", "compute", "compatibility", "benchmark", "deployment")
    chip_fields = [field for domain in DOMAINS for field in load_domain(BUNDLES, domain).fields.get("chips", [])]
    assert len(chip_fields) == len(set(chip_fields))
    assert {"psu_w", "board_length_mm", "foundry", "architecture"} <= set(load_domain(BUNDLES, "specs").fields["chips"])
    identity = set(load_domain(BUNDLES, "identity").fields["chips"])
    assert {"eol_date", "expected_release_date", "production_status"} <= identity
    assert "architecture" not in identity
    assert set(load_domain(BUNDLES, "compatibility").fields["chips"]) == {"software_stack", "compatible_frameworks", "framework_compat", "sw_stack"}
    assert not {"price_usd", "price_cny_wan", "cloud_available", "maturity_level", "source_url", "source_type", "confidence"} & set(chip_fields)
    assert all("models" not in load_domain(BUNDLES, domain).fields for domain in DOMAINS)


@pytest.mark.parametrize("domain", ["price", "", "../identity", "identity/../../outside", "IDENTITY", "C:/outside", "identity\\..\\specs"])
def test_invalid_domain_is_rejected_before_reading_any_path(tmp_path, domain):
    with pytest.raises(ValueError, match="domain"):
        load_domain(tmp_path / "nonexistent", domain)


@pytest.mark.parametrize("change", [
    {"domain_id": "specs"}, {"skill_name": "chip-price"}, {"rule_version": "2.0.0"},
    {"unexpected": "value"}, {"label": " "},
    {"fields": {"chips": ["vendor", "vendor"]}},
    {"entity_keys": {"chips": ["chip_model", "chip_model"]}},
    {"entity_keys": {"models": ["model_id"]}},
    {"fields": {"chips": []}}, {"fields": {"chips; DROP TABLE chips": ["vendor"]}},
    {"references": ["../secret.md"]}, {"references": ["C:/secret.md"]},
    {"references": ["/secret.md"]}, {"references": ["chip-identity/../../secret.md"]},
    {"references": ["https://example.com/rules"]}, {"references": ["_shared\\..\\secret.md"]},
    {"conditions": {"bad": object()}},
])
def test_invalid_contract_rejected(tmp_path, change):
    payload = raw_contract()
    payload.update(change)
    if "conditions" in change:
        with pytest.raises(ValidationError):
            DomainContract.model_validate(payload)
    else:
        write_contract(tmp_path, payload)
        with pytest.raises(ValueError):
            load_domain(tmp_path, "identity")


def test_relation_keys_cannot_be_claimed_as_scalar_fields(tmp_path):
    payload = raw_contract("benchmark")
    payload["fields"]["chip_model_benchmarks"].append("model_id")
    write_contract(tmp_path, payload, "benchmark")
    with pytest.raises(ValueError, match="entity"):
        load_domain(tmp_path, "benchmark")


@pytest.mark.parametrize("duplicate_members, duplicate_key", [
    ('"rule_version": "2.0.0", "rule_version": "3.0.0", "conditions": {}', "rule_version"),
    ('"rule_version": "3.0.0", "conditions": {"nested": [{"overwrite": true, "overwrite": false}]}', "overwrite"),
])
def test_load_domain_rejects_duplicate_json_keys_at_any_depth(tmp_path, duplicate_members, duplicate_key):
    path = tmp_path / "chip-identity" / "domain.json"
    path.parent.mkdir()
    # Raw JSON is essential: constructing a dict would discard the duplicate.
    path.write_text(
        '{"domain_id": "identity", "label": "Identity", "skill_name": "chip-identity", '
        + duplicate_members
        + ', "fields": {"chips": ["vendor"]}, "entity_keys": {"chips": ["vendor", "chip_model"]}, '
        '"units": {}, "references": ["chip-identity/rules.md"]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=f"Duplicate JSON key: '{duplicate_key}'"):
        load_domain(tmp_path, "identity")


def test_frozen_contract_rejects_attribute_assignment():
    contract = load_domain(BUNDLES, "identity")
    with pytest.raises(ValidationError):
        contract.label = "changed"


def test_bundle_path_cannot_escape_root_through_symlink(tmp_path):
    outside = tmp_path / "outside"
    write_contract(outside, raw_contract())
    root = tmp_path / "approved"
    root.mkdir()
    try:
        (root / "chip-identity").symlink_to(outside / "chip-identity", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires OS privileges")
    with pytest.raises(ValueError, match="root"):
        load_domain(root, "identity")


def test_contract_file_symlink_cannot_escape_root(tmp_path):
    outside = write_contract(tmp_path / "outside", raw_contract())
    root = tmp_path / "approved"
    (root / "chip-identity").mkdir(parents=True)
    try:
        (root / "chip-identity" / "domain.json").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires OS privileges")
    with pytest.raises(ValueError, match="root"):
        load_domain(root, "identity")


def test_schema_validation_uses_real_snapshot_and_is_read_only(tmp_path):
    """The real snapshot has benchmark evidence columns absent in base DDL."""
    target = tmp_path / "snapshot.db"
    fixture = PROJECT / "tests" / "fixtures" / "domain_discovery" / "business_schema.sql"
    with sqlite3.connect(target) as copied:
        copied.executescript(fixture.read_text(encoding="utf-8"))
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as db:
        statements = []
        db.set_trace_callback(statements.append)
        for domain in DOMAINS:
            validate_domain_schema(db, load_domain(BUNDLES, domain))
        assert statements
        assert all(statement.upper().startswith("PRAGMA TABLE_INFO(") for statement in statements)
        assert db.total_changes == 0


def test_schema_validation_reports_missing_real_extension_columns():
    with sqlite3.connect(":memory:") as db:
        db.executescript((PROJECT / "schema.sql").read_text(encoding="utf-8"))
        for domain in DOMAINS:
            if domain != "benchmark":
                validate_domain_schema(db, load_domain(BUNDLES, domain))
        with pytest.raises(ValueError) as failure:
            validate_domain_schema(db, load_domain(BUNDLES, "benchmark"))
        assert all(field in str(failure.value) for field in ("citation", "evidence_level", "methodology", "reference_url"))


@pytest.mark.parametrize("section", ["fields", "entity_keys"])
def test_schema_validation_checks_both_owned_fields_and_entity_keys(section):
    payload = raw_contract("deployment")
    payload[section]["deployment_guides"].append("missing_real_column")
    contract = DomainContract.model_validate(payload)
    with sqlite3.connect(":memory:") as db:
        db.executescript((PROJECT / "schema.sql").read_text(encoding="utf-8"))
        with pytest.raises(ValueError, match="missing_real_column"):
            validate_domain_schema(db, contract)


def test_schema_validation_rejects_missing_table():
    with sqlite3.connect(":memory:") as db:
        with pytest.raises(ValueError, match="chips"):
            validate_domain_schema(db, load_domain(BUNDLES, "identity"))


def test_schema_validation_rejects_injected_table_even_for_unvalidated_model():
    contract = load_domain(BUNDLES, "identity").model_copy(update={"fields": {"chips); DROP TABLE chips;--": ["vendor"]}})
    with sqlite3.connect(":memory:") as db:
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(ValueError, match="table"):
            validate_domain_schema(db, contract)
        assert not statements


def test_runtime_reads_ownership_from_json_without_a_second_field_registry(tmp_path):
    payload = raw_contract("specs")
    payload["fields"]["chips"].append("future_field")
    write_contract(tmp_path, payload, "specs")
    contract = load_domain(tmp_path, "specs")
    assert "future_field" in contract.fields["chips"]
    with sqlite3.connect(":memory:") as db:
        db.executescript((PROJECT / "schema.sql").read_text(encoding="utf-8"))
        with pytest.raises(ValueError, match="future_field"):
            validate_domain_schema(db, contract)


def test_identity_and_specs_conditions_preserve_sku_and_psu_semantics():
    identity = load_domain(BUNDLES, "identity")
    assert identity.conditions["entity_scope"]["include"] == ["chip", "board_sku"]
    assert identity.conditions["entity_scope"]["exclude"] == ["series_as_sku", "server", "cluster"]
    assert identity.conditions["entity_matching"]["preserve"] == ["sku_suffix", "capacity", "form_factor"]
    assert identity.conditions["release_date"]["disallowed_inference"] == ["copyright_year", "article_date"]
    assert identity.conditions["unannounced"]["values"] == {"production_status": "未公开发布", "is_released": "0"}
    assert identity.conditions["unannounced"]["requires_explicit_evidence"] is True
    specs = load_domain(BUNDLES, "specs")
    assert specs.conditions["entity_scope"]["default"] == ["chip", "single_card"]
    assert specs.conditions["psu_w"]["meaning"] == "recommended_system_psu_for_card_or_configuration"
    assert specs.conditions["psu_w"]["exclude"] == ["installed_server_psu", "tdp_w"]
    assert specs.units["bandwidth"]["preserve"] == ["bit_or_byte", "direction", "per_link_or_aggregate"]


def test_compute_conditions_preserve_formats_and_sparse_dense_metrics():
    contract = load_domain(BUNDLES, "compute")
    assert contract.conditions["precision_formats"] == ["FP64", "FP32", "TF32", "BF16", "FP16", "FP8", "FP4", "INT8", "INT4"]
    assert contract.conditions["cross_format_inference"] is False
    assert contract.conditions["business_format"]["lossless_metadata"] is True
    assert contract.units["precision_perf"]["preserve"] == ["precision", "dense_or_sparse", "TFLOPS_or_TOPS"]


def test_relation_conditions_preserve_evidence_requirements_and_ambiguity():
    compatibility = load_domain(BUNDLES, "compatibility")
    assert compatibility.conditions["compat_status"]["verified"] == "actual_verification"
    assert compatibility.conditions["compat_status"]["vendor_claimed"] == "vendor_statement"
    assert compatibility.conditions["compat_status"]["community"] == "community_report"
    assert compatibility.conditions["vendor_statement_is_verified"] is False
    benchmark = load_domain(BUNDLES, "benchmark")
    assert benchmark.conditions["minimum_evidence"] == ["explicit_sku", "full_model", "workload", "card_count", "precision", "absolute_metric_and_unit", "distinguishable_report_suite_configuration"]
    assert benchmark.conditions["generative_metrics"]["required"] == ["input_seq_length", "output_seq_length", "batch_size_or_concurrency"]
    assert benchmark.conditions["training_metrics"]["required"] == ["training_workload", "training_data", "hardware_configuration"]
    assert benchmark.conditions["optional_or_not_applicable"] == "unknown_allowed"
    for domain in DOMAINS:
        contract = load_domain(BUNDLES, domain)
        assert contract.conditions["ambiguous_history"] == {"candidate_status": "validated", "comparison_status": "comparison_unresolved", "overwrite": False}
        assert contract.conditions["candidate_metadata"] == ["source_url", "source_type", "confidence"]
    deployment = load_domain(BUNDLES, "deployment")
    assert deployment.conditions["commands"] == "display_only"
    assert deployment.conditions["details_storage"] == {"fields": ["notes"], "metadata": ["version", "commands", "dependencies", "topology"]}


def test_references_are_explicit_bundle_relative_paths_without_existence_requirement(tmp_path):
    payload = raw_contract()
    assert payload["references"]
    payload["references"] = ["_shared/not-created-yet.md", "chip-identity/references/not-created-yet.md"]
    write_contract(tmp_path, payload)
    assert load_domain(tmp_path, "identity").references == payload["references"]
