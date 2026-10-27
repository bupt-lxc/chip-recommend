import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / ".hermes-plugins" / "aishperf-open-web" / "__init__.py"


def _module():
    spec = importlib.util.spec_from_file_location("aishperf_open_web_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


def test_plugin_registers_exactly_three_tools_in_dedicated_toolset() -> None:
    module = _module()
    context = FakeContext()
    module.register(context)

    assert set(context.tools) == {
        "open_web_search", "open_web_preview", "open_web_submit_selection"
    }
    assert {item["toolset"] for item in context.tools.values()} == {
        "aishperf_open_web"
    }
    assert all(item['schema']['strict'] is False for item in context.tools.values())


def test_plugin_handler_calls_only_configured_loopback_service(monkeypatch) -> None:
    module = _module()
    calls = []

    def fake_post(path, payload):
        calls.append((path, payload))
        return {"ok": True, "path": path}

    monkeypatch.setattr(module, "_post", fake_post)
    result = json.loads(module._handle_search({
        "run_id": "20260929-120000-abcdef12",
        "skill": "chip-specs",
        "target_chip": "TestChip X1",
        "queries": [{"query": str(index), "reason": "r"} for index in range(10)],
    }))
    assert result["ok"] is True
    assert calls[0][0] == "/v1/search"


def test_plugin_schemas_are_generated_strict_v3_contract() -> None:
    module = _module()
    from chip_model.domain_tool_api import tool_contract
    assert [module.SEARCH_SCHEMA, module.PREVIEW_SCHEMA, module.SUBMIT_SCHEMA] == tool_contract()['tools']
    for schema in (module.SEARCH_SCHEMA, module.PREVIEW_SCHEMA, module.SUBMIT_SCHEMA):
        assert schema['parameters']['type'] == 'object'
        assert schema['parameters']['additionalProperties'] is False
        assert {'run_id', 'scope_type', 'domain', 'contract_version', 'pipeline', 'chip'} <= set(schema['parameters']['required'])
        for model in schema['parameters']['$defs'].values():
            if 'run_id' in model.get('properties', {}):
                assert {'run_id', 'scope_type', 'domain', 'contract_version', 'pipeline', 'chip'} <= set(model['required'])
                assert 'unit_id' not in model['properties']
                assert model['additionalProperties'] is False


def test_selection_handler_uses_v3_route(monkeypatch):
    module = _module()
    calls = []
    monkeypatch.setattr(module, '_post', lambda path, args: calls.append(path) or {'ok': True})
    module._handle_selection({})
    assert calls == ['/v1/selection']


def test_model_facing_object_schema_accepts_every_action_sample():
    from jsonschema import Draft202012Validator
    from tests.test_domain_tool_api import request, plan
    module = _module()
    cases = [(module.SEARCH_SCHEMA, request(action='execute', request_id='a', plan=plan())),
             (module.SEARCH_SCHEMA, request(action='results', list_version='v')),
             (module.SEARCH_SCHEMA, request(action='complete_intents', request_id='b', completions=[
                 {'failed_query_id':'q1','replacement_query_id':'q2','reason':'success read'}])),
             (module.PREVIEW_SCHEMA, request(mode='initial', request_id='c', list_version='v')),
             (module.PREVIEW_SCHEMA, request(mode='detail', request_id='d', snapshot_id='s', offset=0, length=100)),
             (module.SUBMIT_SCHEMA, request(action='append', request_id='e', list_version='v', decisions=[
                 {'candidate_id':'candidate','decision':'reject','reason':'no evidence','expected_info':'','linked_domains':[]}])),
             (module.SUBMIT_SCHEMA, request(action='finalize', request_id='f', list_version='v'))]
    for schema, payload in cases:
        assert schema['parameters'].get('type') == 'object'
        Draft202012Validator.check_schema(schema['parameters'])
        Draft202012Validator(schema['parameters']).validate(payload)
