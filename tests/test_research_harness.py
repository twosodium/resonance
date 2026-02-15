"""
Tests for research_harness: internet search parsing, unwrap, URL normalization, and optional integration.
Run from project root: python -m pytest tests/ -v
"""
import os
import sys

import pytest

# Run from project root so research_harness is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research_harness import (
    Paper,
    _normalize_search_url,
    _parse_search_results,
    _unwrap_extract_list,
    fetch_arxiv,
    fetch_openalex,
    fetch_semantic_scholar,
)


class TestUnwrapExtractList:
    def test_none_returns_empty(self):
        assert _unwrap_extract_list(None) == []

    def test_list_returned_as_is(self):
        data = [{"title": "A", "url": "https://a.com"}]
        assert _unwrap_extract_list(data) == data

    def test_dict_with_result_key(self):
        data = {"result": [{"title": "B", "url": "https://b.com"}]}
        assert _unwrap_extract_list(data) == [{"title": "B", "url": "https://b.com"}]

    def test_dict_with_items_key(self):
        data = {"items": [{"title": "C", "url": "https://c.com"}]}
        assert _unwrap_extract_list(data) == [{"title": "C", "url": "https://c.com"}]

    def test_dict_with_data_key(self):
        data = {"data": [{"title": "D", "url": "https://d.com"}]}
        assert _unwrap_extract_list(data) == [{"title": "D", "url": "https://d.com"}]

    def test_empty_dict_returns_empty(self):
        assert _unwrap_extract_list({}) == []

    def test_non_list_value_returns_empty(self):
        assert _unwrap_extract_list({"result": "not a list"}) == []


class TestNormalizeSearchUrl:
    def test_plain_https_passthrough(self):
        u = "https://example.com/paper"
        assert _normalize_search_url(u) == u

    def test_plain_http_passthrough(self):
        u = "http://example.org/page"
        assert _normalize_search_url(u) == u

    def test_short_prefixed(self):
        assert _normalize_search_url("https://x.co") == "https://x.co"

    def test_relative_becomes_https(self):
        assert _normalize_search_url("example.com/paper") == "https://example.com/paper"

    def test_google_redirect_unwrapped(self):
        u = "https://www.google.com/url?q=https://real.com/article&sa=U"
        assert _normalize_search_url(u) == "https://real.com/article"

    def test_empty_or_too_short_returns_none(self):
        assert _normalize_search_url("") is None
        assert _normalize_search_url("   ") is None
        assert _normalize_search_url("http://x") is None


class TestParseSearchResults:
    def test_empty_result_zero_papers(self):
        assert _parse_search_results(None, 10) == []
        assert _parse_search_results([], 10) == []
        assert _parse_search_results({}, 10) == []

    def test_wrapped_dict_parsed(self):
        result = {"result": [{"title": "My Paper", "url": "https://journal.org/paper"}]}
        papers = _parse_search_results(result, 10)
        assert len(papers) == 1
        assert papers[0].title == "My Paper"
        assert papers[0].url == "https://journal.org/paper"
        assert papers[0].source == "internet"

    def test_link_key_instead_of_url(self):
        result = [{"title": "Other", "link": "https://other.com/page"}]
        papers = _parse_search_results(result, 10)
        assert len(papers) == 1
        assert papers[0].url == "https://other.com/page"

    def test_href_key_instead_of_url(self):
        result = [{"title": "Href", "href": "https://href.org/x"}]
        papers = _parse_search_results(result, 10)
        assert len(papers) == 1
        assert papers[0].url == "https://href.org/x"

    def test_empty_title_uses_url_snippet(self):
        result = [{"url": "https://long.example.com/very/long/path/to/paper"}]
        papers = _parse_search_results(result, 10)
        assert len(papers) == 1
        assert "long.example.com" in papers[0].title or "https://" in papers[0].title

    def test_authors_parsed(self):
        result = [{"title": "T", "url": "https://a.com", "authors": "Alice, Bob"}]
        papers = _parse_search_results(result, 10)
        assert papers[0].authors == ["Alice", "Bob"]

    def test_max_results_cap(self):
        result = [
            {"title": f"Paper {i}", "url": f"https://example.com/p{i}"}
            for i in range(10)
        ]
        papers = _parse_search_results(result, 3)
        assert len(papers) == 3

    def test_dedupe_by_url(self):
        result = [
            {"title": "A", "url": "https://same.com"},
            {"title": "B", "url": "https://same.com"},
        ]
        papers = _parse_search_results(result, 10)
        assert len(papers) == 1


class TestFetchArxiv:
    """Quick sanity: arXiv API returns papers for a real query."""

    def test_fetch_arxiv_returns_list(self):
        papers = fetch_arxiv("machine learning", max_results=3)
        assert isinstance(papers, list)
        assert len(papers) <= 3

    def test_fetch_arxiv_papers_have_required_fields(self):
        papers = fetch_arxiv("neuroscience", max_results=2)
        for p in papers:
            assert isinstance(p, Paper)
            assert p.title
            assert p.url
            assert p.source == "arxiv"


class TestFetchOpenAlex:
    """OpenAlex API returns papers with title, url, source=openalex."""

    def test_fetch_openalex_returns_list(self):
        papers = fetch_openalex("machine learning", max_results=3)
        assert isinstance(papers, list)
        assert len(papers) <= 3

    def test_fetch_openalex_papers_have_required_fields(self):
        papers = fetch_openalex("deep learning", max_results=2)
        for p in papers:
            assert isinstance(p, Paper)
            assert p.title
            assert p.url
            assert p.source == "openalex"


class TestFetchSemanticScholar:
    """Semantic Scholar API returns papers with title, url, source=semantic_scholar."""

    def test_fetch_semantic_scholar_returns_list(self):
        papers = fetch_semantic_scholar("machine learning", max_results=3)
        assert isinstance(papers, list)
        assert len(papers) <= 3

    def test_fetch_semantic_scholar_papers_have_required_fields(self):
        papers = fetch_semantic_scholar("neural networks", max_results=2)
        for p in papers:
            assert isinstance(p, Paper)
            assert p.title
            assert p.url
            assert p.source == "semantic_scholar"


@pytest.mark.skipif(
    not os.environ.get("BROWSERBASE_API_KEY") or not os.environ.get("ANTHROPIC_API_KEY"),
    reason="BROWSERBASE_API_KEY and ANTHROPIC_API_KEY required for internet integration test",
)
class TestInternetFetchIntegration:
    """Run only when Browserbase + Anthropic are configured; actually hits Google."""

    @pytest.mark.asyncio
    async def test_internet_fetch_returns_some_papers(self):
        from research_harness import _fetch_internet_stagehand

        papers = await _fetch_internet_stagehand("machine learning", max_results=5)
        assert isinstance(papers, list)
        assert len(papers) > 0, "Internet fetch should return at least one paper when env is set"
        for p in papers:
            assert p.source == "internet"
            assert p.url.startswith("http")
            assert p.title
