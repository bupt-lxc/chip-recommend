import pytest

from chip_model.pipeline.open_web_test import FallbackSearch, SearchResult


class FixtureProvider:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows
        self.calls = []

    def search(self, query, limit):
        self.calls.append(query)
        return [SearchResult(url=url, title=title, query=query, provider=self.name)
                for url, title in self.rows][:limit]


def make_search(*providers):
    search = FallbackSearch.__new__(FallbackSearch)
    search.providers = providers
    return search


def test_nonempty_unrelated_search_batch_does_not_block_fallback():
    wrong = FixtureProvider("bing", [("https://www.zhihu.com/douyin", "如何卸载抖音")])
    empty = FixtureProvider("so360", [])
    good = FixtureProvider("fallback", [("https://www.nvidia.com/h100/", "H100 GPU specs")])
    rows = make_search(wrong, empty, good).search("NVIDIA H100 memory specifications", 10)
    assert [row.provider for row in rows] == ["fallback"]
    assert good.calls == ["NVIDIA H100 memory specifications"]


def test_vendor_homepages_do_not_count_as_target_model_matches():
    wrong = FixtureProvider("bing", [("https://www.nvidia.com/", "NVIDIA GeForce drivers")])
    good = FixtureProvider("so360", [("https://specs.example/h100", "NVIDIA H100 specifications")])
    rows = make_search(wrong, good).search("NVIDIA H100 SXM5 80GB HBM3 specs", 10)
    assert [row.provider for row in rows] == ["so360"]


def test_all_search_sources_unrelated_are_reported_as_failure():
    wrong = FixtureProvider("bing", [("https://example.com/music", "Music downloads")])
    with pytest.raises(RuntimeError, match="H100"):
        make_search(wrong).search("NVIDIA H100 memory", 10)


def test_site_scope_rejects_outside_domains_and_domain_lookalikes():
    outside = FixtureProvider("bing", [
        ("https://www.zhihu.com/h100", "H100 GPU specs"),
        ("https://nvidia.com.attacker.example/h100", "H100 GPU specs"),
    ])
    inside = FixtureProvider("so360", [("https://docs.nvidia.com/h100", "H100 GPU specs")])
    rows = make_search(outside, inside).search('"H100" memory site:nvidia.com', 10)
    assert [row.url for row in rows] == ["https://docs.nvidia.com/h100"]


def test_transport_health_check_keeps_semantic_selection_with_hermes():
    provider = FixtureProvider("bing", [
        ("https://specs.example/h100", "H100 GPU memory"),
        ("https://vendor.example/architecture", "GPU architecture documentation"),
    ])
    rows = make_search(provider).search("NVIDIA H100 memory", 10)
    assert len(rows) == 2


def test_model_token_does_not_match_a_different_model_with_same_prefix():
    wrong = FixtureProvider("bing", [("https://example.com/h1000", "H1000 specifications")])
    good = FixtureProvider("so360", [("https://example.com/spec", "H-100 GPU memory")])
    rows = make_search(wrong, good).search("NVIDIA H100 memory", 10)
    assert [row.provider for row in rows] == ["so360"]


def test_open_discovery_without_model_anchor_keeps_results_for_hermes():
    provider = FixtureProvider("bing", [("https://vendor.example/new", "New accelerator catalog")])
    rows = make_search(provider).search("new AI accelerator hardware specifications", 10)
    assert len(rows) == 1


def test_hyphenated_chip_model_matches_its_exact_code_but_not_another_suffix():
    wrong = FixtureProvider("bing", [
        ("https://www.cambricon.com/mlu370-s8", "MLU370-S8 specifications"),
    ])
    good = FixtureProvider("so360", [
        ("https://www.cambricon.com/mlu370-s4", "MLU370-S4 24GB specifications"),
    ])
    rows = make_search(wrong, good).search("MLU370-S4 memory specifications", 10)
    assert [row.provider for row in rows] == ["so360"]
