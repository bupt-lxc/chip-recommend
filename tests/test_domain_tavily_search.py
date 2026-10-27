import pytest
import requests
from chip_model.pipeline.domain_discovery import search

class Session:
    def __init__(self, data=None, status=200, error=None):
        self.data, self.status, self.error, self.calls = data, status, error, []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error: raise self.error
        outer = self
        class Response:
            status_code = outer.status
            def json(self): return outer.data
        return Response()

def test_tavily_preserves_query_and_maps_only_search_results():
    session = Session({'results':[{'url':'https://vendor.example/product','title':'新品','content':'发布信息'}]})
    result = search.TavilySearchProvider(api_key='test-secret', session=session).search('国产 新芯片',5,language='zh')
    assert result.outcome == 'success'
    assert result.rows == [{'url':'https://vendor.example/product','title':'新品','snippet':'发布信息','rank':1}]
    payload = session.calls[0][1]['json']
    assert payload['query'] == '国产 新芯片'
    assert payload['include_answer'] is False and payload['include_raw_content'] is False
    assert payload['search_depth'] == 'basic' and payload['max_results'] == 5
    assert session.calls[0][1]['allow_redirects'] is False

@pytest.mark.parametrize('data,status,outcome',[({'results':[]},200,'healthy_empty'),({},200,'parse_error'),({'results':'invalid'},200,'parse_error'),({},401,'http_error'),({},429,'http_error')])
def test_tavily_empty_and_failure_are_distinct(data,status,outcome):
    assert search.TavilySearchProvider(api_key='test-secret',session=Session(data,status)).search('q',5,language='en').outcome == outcome

def test_tavily_does_not_expose_key_in_network_error():
    result = search.TavilySearchProvider(api_key='test-secret',session=Session(error=requests.Timeout('test-secret'))).search('q',5,language='en')
    assert result.outcome == 'network_error' and 'test-secret' not in result.error

def test_tavily_explicit_selection_and_missing_key(monkeypatch):
    monkeypatch.setenv('DOMAIN_SEARCH_BACKEND','tavily')
    monkeypatch.delenv('TAVILY_API_KEY',raising=False)
    with pytest.raises(ValueError,match='TAVILY_API_KEY'): search.default_providers()
    monkeypatch.setenv('TAVILY_API_KEY','test-secret')
    assert [p.name for p in search.default_providers()] == ['tavily']
    monkeypatch.setenv('DOMAIN_SEARCH_BACKEND','typo')
    with pytest.raises(ValueError,match='unsupported'): search.default_providers()

def test_tavily_uses_existing_executor_dedup_budget_and_replay(tmp_path):
    from chip_model.pipeline.domain_discovery.store import RunStore
    from chip_model.pipeline.domain_discovery.budget import RunBudget
    store = RunStore(tmp_path / 'runs')
    store.start('start', {'domain':'identity','scope_type':'discovery','chip':None}, 'tavily-run')
    generation = store.claim('tavily-run','worker',100000)
    budget = RunBudget(store,'tavily-run',generation)
    session = Session({'results':[{'url':'https://vendor.example/product','title':'New chip','content':'Launch'}]})
    provider = search.TavilySearchProvider(api_key='test-secret',session=session)
    executor = search.SearchExecutor(store,budget,providers=[provider],backoff=lambda _:None)
    plan = {'round_number':0,'queries':[{'query_id':f'q{i}','text':f'new chip launch {i}','language':'zh' if i < 4 else 'en','reason':'Discover chip','page_type':'product'} for i in range(10)]}
    result = executor.execute('initial',plan)
    assert result['total'] == 1
    assert len(session.calls) == 10
    assert executor.execute('initial',plan) == result
    assert len(session.calls) == 10
