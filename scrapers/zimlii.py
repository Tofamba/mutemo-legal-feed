"""
ZimLII scraper — Zimbabwe Legal Information Institute
Scrapes recent judgments and legislation from zimlii.org
Schedule: 04:00 UTC daily (~6am CAT)
"""
import re
from datetime import datetime
from firecrawl_client import crawl_url
from state import is_seen, mark_seen, mark_run
from pusher import push_legal_update, push_zlr_entry

ZIMLII_BASE = "https://zimlii.org"
ZIMLII_JUDGMENTS = f"https://zimlii.org/judgments/all/{datetime.utcnow().year}/"
ZIMLII_HH = f"https://zimlii.org/judgments/ZWHHC/{datetime.utcnow().year}/"
ZIMLII_LEGISLATION = "https://zimlii.org/legislation/"


def _extract_reference(url: str) -> str:
    """Extract a short reference from a ZimLII URL."""
    parts = url.rstrip("/").split("/")
    return parts[-1] if parts else url


def _is_valid_content(content: str, url: str) -> bool:
    """Return False if content is a 404 page or too short."""
    if not content or len(content) < 200:
        return False
    lower = content.lower()
    if "not found (error 404)" in lower or "error 404" in lower[:200]:
        print(f"[zimlii] skipping 404 page: {url}")
        return False
    if "page not found" in lower[:200]:
        print(f"[zimlii] skipping 'page not found': {url}")
        return False
    return True


def scrape_recent_judgments(limit: int = 5) -> int:
    """Scrape recent judgments from ZimLII and push to MutemoOS."""
    pushed = 0
    try:
        pages = crawl_url(ZIMLII_JUDGMENTS, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not _is_valid_content(content, url):
                continue

            title = page.get("metadata", {}).get("title", "") or _extract_reference(url)
            reference = _extract_reference(url)

            is_headnote = bool(re.search(
                r'\b(HH|SC|CCZ|LC|HB|HM|HMT)-?\d+[-/]\d+', content
            ))

            if is_headnote:
                push_zlr_entry(
                    content=content,
                    filename=f"{reference}.txt",
                    source="ZimLII",
                    zimlii_url=url,
                )
            else:
                push_legal_update(
                    content=content,
                    filename=f"{reference}.txt",
                    source_type="case_law",
                    source_name="ZimLII",
                    reference=title[:200],
                )

            mark_seen(url)
            pushed += 1
            print(f"[zimlii] pushed judgment: {title[:80]}")

    except Exception as e:
        print(f"[zimlii] judgments scrape failed: {e}")

    mark_run("zimlii_judgments")
    return pushed


def scrape_harare_high_court(limit: int = 5) -> int:
    """Scrape recent Harare High Court judgments specifically."""
    pushed = 0
    try:
        pages = crawl_url(ZIMLII_HH, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not _is_valid_content(content, url):
                continue

            title = page.get("metadata", {}).get("title", "") or _extract_reference(url)
            reference = _extract_reference(url)

            is_headnote = bool(re.search(
                r'\b(HH)-?\d+[-/]\d+', content
            ))

            if is_headnote:
                push_zlr_entry(
                    content=content,
                    filename=f"HH_{reference}.txt",
                    source="ZimLII",
                    zimlii_url=url,
                )
            else:
                push_legal_update(
                    content=content,
                    filename=f"HH_{reference}.txt",
                    source_type="case_law",
                    source_name="ZimLII",
                    reference=title[:200],
                )

            mark_seen(url)
            pushed += 1
            print(f"[zimlii] pushed HH judgment: {title[:80]}")

    except Exception as e:
        print(f"[zimlii] HH scrape failed: {e}")

    mark_run("zimlii_hh")
    return pushed


def scrape_recent_legislation(limit: int = 3) -> int:
    """Scrape recent legislation from ZimLII and push to MutemoOS."""
    pushed = 0
    try:
        pages = crawl_url(ZIMLII_LEGISLATION, limit=limit)
        for page in pages:
            url = page.get("metadata", {}).get("sourceURL") or page.get("url", "")
            if not url or is_seen(url):
                continue

            content = page.get("markdown", "") or page.get("content", "")
            if not _is_valid_content(content, url):
                continue

            title = page.get("metadata", {}).get("title", "") or _extract_reference(url)
            reference = title[:200] if title else _extract_reference(url)

            push_legal_update(
                content=content,
                filename=f"{_extract_reference(url)}.txt",
                source_type="legislation",
                source_name="ZimLII",
                reference=reference,
            )

            mark_seen(url)
            pushed += 1
            print(f"[zimlii] pushed legislation: {title[:80]}")

    except Exception as e:
        print(f"[zimlii] legislation scrape failed: {e}")

    mark_run("zimlii_legislation")
    return pushed


def run():
    print("[zimlii] starting scrape...")
    j = scrape_recent_judgments(limit=5)
    hh = scrape_harare_high_court(limit=5)
    l = scrape_recent_legislation(limit=3)
    print(f"[zimlii] done — {j} judgments, {hh} HH judgments, {l} legislation pushed")
    return j + hh + l
