-- Fixture: models/provenance columns from repository schema.sql, no business rows.
-- evidence_ref_json is an explicit test-only archive-reference column.
CREATE TABLE models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT, author TEXT, pipeline_tag TEXT, library_name TEXT, tags TEXT,
    downloads TEXT, likes TEXT, last_modified TEXT, private TEXT, gated TEXT,
    architecture_family TEXT, total_params_b TEXT, config_json TEXT,
    card_data_json TEXT, api_response_json TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE field_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT, row_id TEXT, field_name TEXT, field_label TEXT,
    old_value TEXT, new_value TEXT, source_type TEXT, source_url TEXT,
    source_detail TEXT, confidence TEXT, is_official TEXT, updated_at TEXT,
    notes TEXT, evidence_ref_json TEXT
);
