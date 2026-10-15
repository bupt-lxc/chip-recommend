import pytest

from chip_model.pipeline.open_web_test import OpenAICompatibleExtractor, TEST_SKILL_REGISTRY


@pytest.mark.parametrize("configured, expected", [(None, 0.6), ("1", 1), ("2", 2)])
def test_extractor_sends_configured_temperature(monkeypatch, configured, expected):
    monkeypatch.delenv("DATA_AGENT_LLM_TEMPERATURE", raising=False)
    if configured is not None:
        monkeypatch.setenv("DATA_AGENT_LLM_TEMPERATURE", configured)
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"relevant":false,"facts":[]}'}}]}

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("chip_model.pipeline.open_web_test.requests.post", post)
    client = OpenAICompatibleExtractor(base_url="https://example.com", api_key="test", model="test")
    client.extract(text="test", url="https://example.com/spec", chip_hints=["test"],
                   skill=TEST_SKILL_REGISTRY["chip-specs"])
    assert requests[0]["temperature"] == expected


@pytest.mark.parametrize("value", ["oops", "nan", "inf", "-0.1", "2.1"])
def test_extractor_rejects_invalid_temperature(monkeypatch, value):
    monkeypatch.setenv("DATA_AGENT_LLM_TEMPERATURE", value)
    with pytest.raises(ValueError, match="DATA_AGENT_LLM_TEMPERATURE"):
        OpenAICompatibleExtractor(base_url="https://example.com", api_key="test")
