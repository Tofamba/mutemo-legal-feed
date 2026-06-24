"""
LRF Zimbabwe scraper — Law Reports of Zimbabwe / legal resources
Schedule: 05:00 UTC daily (~7am CAT)
"""
from firecrawl_client import crawl_url
from state import is_seen, mark_seen, mark_run
from pusher import push_legal_update, push_zlr_entry
import re

LRF_BASE = "https://www.lrfzimbabwe.org"
LRF_CASES = "https://www.lrfzimbabwe.org/cases"
LRF_RESOURCES = "https://www.lrfzimbabwe.org/resources"


def _extract_reference(url: str, title: str = "") -> str:
    if title:
        return title[:200]
    parts = url.rstrip("/").split("/")
    return parts[-1].replace("-", " ").title() if parts else url


def scrape_cases(limit: int = 5) -> int:
    pushed = 0
    try:
        pages = crawl_url(LRF_CASES, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not content or len(content) < 200:
                continue

            title = page.get("metadata", {}).get("title", "") or ""
            reference = _extract_reference(url, title)

            is_headnote = bool(re.search(
                r'\b(HH|SC|CCZ|LC|HB|HM|HMT)-?\d+[-/]\d+', content
            ))

            if is_headnote:
                push_zlr_entry(
                    content=content,
                    filename=f"lrf_{reference[:80].replace(' ', '_')}.txt",
                    source="ZimLII",
                    zimlii_url=url,
                )
            else:
                push_legal_update(
                    content=content,
                    filename=f"lrf_{reference[:80].replace(' ', '_')}.txt",
                    source_type="case_law",
                    source_name="LRF Zimbabwe",
                    reference=reference,
                )

            mark_seen(url)
            pushed += 1
            print(f"[lrf] pushed case: {reference[:80]}")

    except Exception as e:
        print(f"[lrf] cases scrape failed: {e}")

    mark_run("lrf_cases")
    return pushed


def scrape_resources(limit: int = 3) -> int:
    pushed = 0
    try:
        pages = crawl_url(LRF_RESOURCES, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not content or len(content) < 200:
                continue

            title = page.get("metadata", {}).get("title", "") or ""
            reference = _extract_reference(url, title)

            push_legal_update(
                content=content,
                filename=f"lrf_resource_{reference[:80].replace(' ', '_')}.txt",
                source_type="legislation",
                source_name="LRF Zimbabwe",
                reference=reference,
            )

            mark_seen(url)
            pushed += 1
            print(f"[lrf] pushed resource: {reference[:80]}")

    except Exception as e:
        print(f"[lrf] resources scrape failed: {e}")

    mark_run("lrf_resources")
    return pushed


def run():
    print("[lrf] starting scrape...")
    c = scrape_cases(limit=5)
    r = scrape_resources(limit=3)
    print(f"[lrf] done — {c} cases, {r} resources pushed")
    return c + r