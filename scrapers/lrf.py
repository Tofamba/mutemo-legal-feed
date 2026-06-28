"""
scrapers/lrf.py — Legal Resources Foundation (LRF) case digest scraper.

CORRECT URL STRUCTURE (verified 2026-06-28):
  lrfzim.com is a WordPress site. Direct URL checks showed:
    /lrf-in-action/news-articles/  → 200 OK (correct)
    /resources/publications/       → 404 (broken — page does not exist)
    /resources/                    → 301 redirect to #  (broken)
    /case-law/                     → 404
    /category/publications/        → 404

  The WordPress REST API is available at /wp-json/wp/v2/
  but returns empty arrays for posts — the site may have REST API disabled
  or posts are in a custom post type.

  Strategy:
  1. Use Firecrawl basic proxy to scrape /lrf-in-action/news-articles/ (200 OK)
  2. Parse article URLs from the listing markdown
  3. For each new URL, scrape the individual article page
  4. Extract title, date, case citation if present, PDF link if present
  5. Push to MutemoOS as ZLR entries (source: "LRF")

  The /resources/publications/ URL has been removed — it 404s.
  Only /lrf-in-action/news-articles/ is used as the listing source.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, download_pdf, FirecrawlError
import state

logger = logging.getLogger(__name__)

# Only the working listing URL
LISTING_URLS = [
    "https://lrfzim.com/lrf-in-action/news-articles/",
]
BASE_URL = "https://lrfzim.com"

# Article URL pattern — match lrfzim.com article paths
RE_ARTICLE_URL = re.compile(
    r"https://lrfzim\.com/(?!wp-|xmlrpc|feed|sitemap|category|tag|author|page)"
    r"[a-z0-9\-]+/[a-z0-9\-/]+"
)
RE_DATE        = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_PDF_LINK    = re.compile(r"\[.*?\]\((https?://[^\)]+\.pdf[^\)]*)\)", re.IGNORECASE)
RE_TITLE       = re.compile(r"^#\s+(.+)$", re.MULTILINE)
RE_CITATION    = re.compile(
    r"\b(ZWSC|ZWCC|ZWHHC|ZWBHC|ZWLC|SC|HH|HC|HB)\s*\d+[-/]\d{2,4}\b",
    re.IGNORECASE,
)
RE_CASE_NAME   = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+v\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b"
)

# Exclude navigation/utility links
EXCLUDE_PATTERNS = [
    "/wp-content/", "/wp-admin/", "/wp-json/", "/feed/", "/xmlrpc",
    "#", "mailto:", "tel:", "/about", "/contact", "/donate",
    "/lrf-in-action/news-articles/",  # exclude the listing page itself
]


def _is_excluded(url: str) -> bool:
    return any(pat in url for pat in EXCLUDE_PATTERNS)


@dataclass
class DigestItem:
    url: str
    title: str
    case_name: Optional[str]
    citation: Optional[str]
    doc_date: Optional[str]
    pdf_url: Optional[str]
    pdf_path: Optional[Path]
    source: str = "LRF"
    markdown_summary: str = ""


async def _get_listing_urls() -> list[str]:
    """Scrape LRF listing pages and return article URLs."""
    all_urls: list[str] = []

    for listing_url in LISTING_URLS:
        logger.info(f"[lrf] Scraping listing: {listing_url}")
        try:
            md = await scrape_markdown(listing_url, proxy="basic", wait_ms=1000)
        except FirecrawlError as e:
            logger.error(f"[lrf] Failed to scrape {listing_url}: {e}")
            continue

        if not md:
            logger.warning(f"[lrf] Empty markdown from {listing_url}")
            continue

        # Extract all lrfzim.com article URLs from markdown
        found = RE_ARTICLE_URL.findall(md)
        for url in found:
            if not _is_excluded(url) and url not in all_urls:
                all_urls.append(url)

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in all_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    logger.info(f"[lrf] Found {len(unique)} article URLs")
    return unique


async def _scrape_article(url: str, dry_run: bool = False) -> Optional[DigestItem]:
    """Scrape a single LRF article and return a DigestItem."""
    logger.info(f"[lrf] Scraping article: {url}")
    try:
        md = await scrape_markdown(url, proxy="basic", wait_ms=1000)
    except FirecrawlError as e:
        logger.error(f"[lrf] Failed to scrape {url}: {e}")
        return None

    if not md or len(md) < 80:
        logger.warning(f"[lrf] Empty or very short markdown for {url}")
        return None

    # Skip pages that are clearly not articles (404 pages, login walls, etc.)
    if any(phrase in md.lower() for phrase in ["page not found", "404", "access denied"]):
        logger.warning(f"[lrf] Skipping non-article page: {url}")
        return None

    title_m = RE_TITLE.search(md)
    title = title_m.group(1).strip() if title_m else "LRF Case Digest"

    citation_m = RE_CITATION.search(md)
    citation = citation_m.group(0).strip() if citation_m else None

    case_name_m = RE_CASE_NAME.search(md)
    case_name = case_name_m.group(0).strip() if case_name_m else None

    date_m = RE_DATE.search(md)
    doc_date = date_m.group(1).strip() if date_m else None

    pdf_m = RE_PDF_LINK.search(md)
    pdf_url = pdf_m.group(1).strip() if pdf_m else None

    summary = md[:800].strip()

    item = DigestItem(
        url=url,
        title=title,
        case_name=case_name,
        citation=citation,
        doc_date=doc_date,
        pdf_url=pdf_url,
        pdf_path=None,
        markdown_summary=summary,
    )

    if not dry_run and pdf_url:
        try:
            item.pdf_path = await download_pdf(pdf_url)
        except Exception as e:
            logger.warning(f"[lrf] PDF download failed for {url}: {e}")
            item.pdf_path = None

    return item


async def run(dry_run: bool = False) -> list[DigestItem]:
    """Main entry point. Returns new DigestItems not previously seen."""
    logger.info(f"[lrf] Starting scrape (dry_run={dry_run})")
    urls = await _get_listing_urls()

    new_items: list[DigestItem] = []
    max_new = state.get_max_new_per_run()

    for url in urls:
        if len(new_items) >= max_new:
            logger.info(f"[lrf] Reached MAX_NEW_PER_RUN={max_new}, stopping")
            break

        if state.is_seen(url):
            state.increment_skipped()
            logger.debug(f"[lrf] Already seen: {url}")
            continue

        item = await _scrape_article(url, dry_run=dry_run)
        if item is None:
            continue

        if dry_run:
            logger.info(
                f"[DRY RUN] Would push: {item.title} ({item.citation}) — {url}"
            )
        else:
            state.mark_seen(url)

        new_items.append(item)

    state.set_last_scraped("lrf")
    logger.info(f"[lrf] Done. {len(new_items)} new items.")
    return new_items
