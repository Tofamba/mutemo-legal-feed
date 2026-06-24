"""
Thin wrapper around the Firecrawl API for scraping legal sources.
"""
import os
from firecrawl import FirecrawlApp

_client = None

def get_client() -> FirecrawlApp:
    global _client
    if _client is None:
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            raise RuntimeError("FIRECRAWL_API_KEY not set")
        _client = FirecrawlApp(api_key=api_key)
    return _client

def scrape_url(url: str, formats: list = None) -> dict:
    """Scrape a single URL and return the result dict."""
    client = get_client()
    result = client.scrape_url(url, params={
        "formats": formats or ["markdown"],
    })
    return result

def crawl_url(url: str, limit: int = 10, formats: list = None) -> list:
    """Crawl a URL up to `limit` pages and return list of result dicts."""
    client = get_client()
    result = client.crawl_url(url, params={
        "limit": limit,
        "scrapeOptions": {"formats": formats or ["markdown"]},
    })
    return result.get("data", [])