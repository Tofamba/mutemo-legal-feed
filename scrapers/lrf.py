"""
scrapers/lrf.py — Legal Resources Foundation (LRF) case digest scraper.

CORRECT URL STRUCTURE (verified 2026-06-28):
  lrfzim.com is a WordPress site.
    /lrf-in-action/news-articles/  → 200 OK (listing page)
    Article URLs: /lrf-in-action/{article-slug}/

  Strategy:
  1. Scrape /lrf-in-action/news-articles/ via Firecrawl basic proxy
  2. Extract only /lrf-in-action/ article URLs (not thematic areas, not press statements)
  3. Apply ARTICLE_KEYWORDS check on slug — must look like a real article
  4. Scrape each article, extract metadata
  5. Push to MutemoOS via _push_zlr_entry (source: "LRF")

Schedule: Mondays at 05:00 UTC (07:00 CAT)
Credit budget: 1 credit/listing + 1 credit/article × up to 10 = ~11 credits/run
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, download_pdf, FirecrawlError
import state

logger = logging.getLogger(__name__)

LISTING_URLS = [
    "https://lrfzim.com/lrf-in-action/news-articles/",
]
BASE_URL = "https://lrfzim.com"

# Only match article URLs under /lrf-in-action/ with at least 10-char slugs
# Excludes the listing page itself and short nav slugs
RE_ARTICLE_URL = re.compile(
    r"https://lrfzim\.com/lrf-in-action/[a-z0-9][a-z0-9\-]{9,}/?"
)

# Keywords that indicate a real article slug vs a navigation/category page
ARTICLE_KEYWORDS = [
    "court", "high-court", "judgment", "ruling", "case", "appeal",
    "rights", "human-rights", "legal", "law", "justice", "lawyer",
    "advocate", "prosecution", "accused", "bail", "sentence", "convicted",
    "acquitted", "constitution", "constitutional", "tribunal", "magistrate",
    "digest", "update", "report", "bulletin", "newsletter",
    "parliament", "legislation", "statutory", "regulation",
    "police", "arrest", "detention", "prison", "release", "trial",
]

RE_DATE     = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_PDF_LINK = re.compile(r"\[.*?\]\((https?://[^\)]+\.pdf[^\)]*)\)", re.IGNORECASE)
RE_TITLE    = re.compile(r"^#\s+(.+)$", re.MULTILINE)
RE_CITATION = re.compile(
    r"\b(ZWSC|ZWCC|ZWHHC|ZWBHC|ZWLC|SC|HH|HC|HB)\s*\d+[-/]\d{2,4}\b",
    re.IGNORECASE,
)
RE_CASE_NAME = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+v\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b"
)

EXCLUDE_PATTERNS = [
    "/wp-content/", "/wp-admin/", "/wp-json/", "/feed/", "/xmlrpc",
    "#", "mailto:", "tel:",
    "/thematic-areas/", "/about", "/contact", "/donate",
    "/press-statements", "/events", "/resources",
    "/lrf-in-action/news-articles/",
    "/strengthening-justice", "/legal-education",
    "/legal-services", "/research-and-advocacy",
]


def _is_excluded(url: str) -> bool:
    return any(pat in url for pat in EXCLUDE_PATTERNS)


def _is_article(url: str) -> bool:
    """Return True if the URL slug looks like a real article."""
    slug = url.rstrip("/").split("/")[-1].lower()
    return any(kw in slug for kw in ARTICLE_KEYWORDS)


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
    scraped_at: Optional[object] = None  # datetime, set at scrape time


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

        found = RE_ARTICLE_URL.findall(md)
        for url in found:
            if not _is_excluded(url) and _is_article(url) and url not in all_urls:
                all_urls.append(url)

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

    if any(phrase in md.lower() for phrase in ["page not found", "404", "access denied"]):
        logger.warning(f"[lrf] Skipping non-article page: {url}")
        return None

    title_m = RE_TITLE.search(md)
    title = title_m.group(1).strip() if title_m else \
        url.rstrip("/").split("/")[-1].replace("-", " ").title()

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
        scraped_at=datetime.now(timezone.utc),
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
            logger.info(f"[DRY RUN] Would push: {item.title} ({item.citation}) — {url}")
        else:
            state.mark_seen(url)

        new_items.append(item)

    state.set_last_scraped("lrf")
    logger.info(f"[lrf] Done. {len(new_items)} new items.")
    return new_items
