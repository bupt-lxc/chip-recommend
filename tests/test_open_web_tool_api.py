"""Historical V1/V2 artifacts stay readable; old writes never run providers."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chip_model.open_web_tool_server import create_open_web_tool_app
from chip_model.pipeline.hermes_open_web_state import create_hermes_open_web_run, resolve_run_dir
from chip_model.pipeline.open_web_test import database_fingerprints

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('version', [1, 2])
def test_historical_get_is_readonly_and_old_posts_refuse_all_writes(tmp_path, version):
    source = tmp_path / 'data.db'
    with sqlite3.connect(source) as connection:
        connection.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        connection.execute("INSERT INTO chips(id,chip_model,vendor,vram_gb) VALUES (1,'Test X1','Vendor','64')")
    before = database_fingerprints(source)
    run = create_hermes_open_web_run(source_db=source, skill='chip-specs', target_chip='Test X1', target_fields=['vram_gb'])
    folder = resolve_run_dir(source, run['session_id'])
    if version == 1:
        path = folder / 'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['schema_version'] = 'hermes-open-web-v1'
        path.write_text(json.dumps(manifest), encoding='utf-8')
    frozen_files = {path: path.read_bytes() for path in folder.rglob('*') if path.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail('historical POST executed provider/refresher/extractor')
    app = create_open_web_tool_app(source_db=source, token='local-test-token', search_provider_factory=forbidden,
                                   refresher_factory=forbidden, extractor_factory=forbidden)
    client = TestClient(app, client=('127.0.0.1', 50000))
    headers = {'Authorization': 'Bearer local-test-token'}
    response = client.get(f'/v1/runs/{run["session_id"]}', headers=headers, params={'contract_version': version})
    assert response.status_code == 200, response.text
    assert response.json()['read_only'] is True
    assert response.json()['contract_version'] == version
    for endpoint in ('search', 'preview', 'submit-selection', 'selection'):
        for marker in (None, version):
            payload = {'run_id': run['session_id'], 'unit_id': run['units'][0]['unit_id'], 'scope_type': 'chip',
                       'skill': 'chip-specs', 'target_chip': 'Test X1', 'queries': [], 'decisions': []}
            if marker is not None:
                payload['contract_version'] = marker
            rejected = client.post('/v1/' + endpoint, headers=headers, json=payload)
            assert rejected.status_code == 409
            assert 'version' in rejected.json()['detail']
    assert database_fingerprints(source) == before
    assert {path: path.read_bytes() for path in folder.rglob('*') if path.is_file()} == frozen_files


def test_historical_auth_and_path_limits_do_not_create_artifacts(tmp_path):
    source = tmp_path / 'missing.db'
    app = create_open_web_tool_app(source_db=source, token='test')
    client = TestClient(app, client=('127.0.0.1', 50000))
    assert client.get('/v1/runs/not-found').status_code == 401
    assert client.get('/v1/runs/not-found', headers={'Authorization':'Bearer bad'}).status_code == 401
    assert client.get('/v1/runs/not-found', headers={'Authorization':'Bearer test'}).status_code == 404
    assert not source.exists()
