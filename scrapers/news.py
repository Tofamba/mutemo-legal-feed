"""
scrapers/news.py — Zimbabwe legal news scraper (Phase 3).

Monitors 6 Zimbabwean news sources for legal, court, and regulatory stories.
Schedule: 05:30 UTC (07:30 CAT) — runs after ZimLII, Veritas, LRF.

Sources:
  - NewsDay         (newsday.co.zw)
  - The Herald      (herald.co.zw)
  - Financial Gazette (fingaz.co.zw)
  - Zimbabwe Independent (theindependent.co.zw)
  - Chronicle       (chronicle.co.zw)
  - Business Weekly (businessweekly.co.zw)

Strategy:
1. Scrape the news listing/homepage of each source via Firecrawl basic proxy
2. Extract article URLs from the markdown
3. Filter by legal keywords — only articles relevant to legal practice
4. For each new URL, scrape the full article
5. Return NewsItem objects for pushing to MutemoOS as Legal Updates
   (source_type: "news")

Credit budget: 1 credit/listing × 6 sources + 1 credit/article × up to 10 = ~16 credits/run
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, FirecrawlError
import state

logger = logging.getLogger(__name__)

# News sources — listing URLs that return the most recent articles
NEWS_SOURCES = [
    {
        "name": "NewsDay",
        "listing_url": "https://www.newsday.co.zw/local-news/",
        "base_url": "https://www.newsday.co.zw",
        "article_pattern": re.compile(r"https://www\.newsday\.co\.zw/[a-z0-9\-]+/article/\d+/[a-z0-9\-]+/?"),
    },
    {
        "name": "The Herald",
        "listing_url": "https://www.herald.co.zw/",
        "base_url": "https://www.herald.co.zw",
        "article_pattern": re.compile(r"https://www\.herald\.co\.zw/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/?"),
    },
    {
        "name": "Financial Gazette",
        "listing_url": "https://www.fingaz.co.zw/",
        "base_url": "https://www.fingaz.co.zw",
        "article_pattern": re.compile(r"https://www\.fingaz\.co\.zw/[a-z0-9\-/]+/?"),
    },
    {
        "name": "Zimbabwe Independent",
        "listing_url": "https://www.theindependent.co.zw/",
        "base_url": "https://www.theindependent.co.zw",
        "article_pattern": re.compile(r"https://www\.theindependent\.co\.zw/[a-z0-9\-/]+/?"),
    },
    {
        "name": "Chronicle",
        "listing_url": "https://www.chronicle.co.zw/",
        "base_url": "https://www.chronicle.co.zw",
        "article_pattern": re.compile(r"https://www\.chronicle\.co\.zw/\d{4}/\d{2}/\d{2}/[a-z0-9\-]+/?"),
    },
    {
        "name": "Business Weekly",
        "listing_url": "https://businessweekly.co.zw/",
        "base_url": "https://businessweekly.co.zw",
        "article_pattern": re.compile(r"https://businessweekly\.co\.zw/[a-z0-9\-/]+/?"),
    },
]

# Legal keywords — article must contain at least one to be included
LEGAL_KEYWORDS = [
    "court", "judge", "judgment", "magistrate", "high court", "supreme court",
    "constitutional court", "labour court", "appeal", "sentence", "convicted",
    "acquitted", "interdict", "injunction", "lawsuit", "litigation",
    "attorney", "advocate", "lawyer", "prosecution", "accused", "defendant",
    "plaintiff", "applicant", "respondent", "verdict",
    "zimra", "revenue authority",
    "reserve bank", "financial intelligence", "aml",
    "companies act", "companies registry", "liquidation", "winding up",
    "eviction", "ejectment", "spoliation", "rei vindicatio",
    "bill of rights", "fundamental rights",
    "statutory instrument", "law society", "legal practitioners",
]

RE_DATE  = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Exclude navigation/utility links
EXCLUDE_PATTERNS = [
    "/category/", "/tag/", "/author/", "/page/", "/wp-content/",
    "/wp-admin/", "/feed/", "#", "mailto:", "tel:", "/about",
    "/contact", "/advertise", "/subscribe", "/privacy", "/terms",
    "/sport/", "/entertainment/", "/lifestyle/", "/technology/",
]


def _is_excluded(url: str) -> bool:
    return any(pat in url for pat in EXCLUDE_PATTERNS)


def _is_legal_content(text: str) -> bool:
    """Return True if the text contains at least 2 legal keywords."""
    text_lower = text.lower()
    matches = sum(1 for kw in LEGAL_KEYWORDS if kw in text_lower)
    return matches >= 2


@dataclass
class NewsItem:
    url: str
    title: str
    source: str                  # e.g. "NewsDay", "The Herald"
    doc_date: Optional[str]
    markdown_summary: str
    pdf_url: Optional[str] = None
    pdf_path: Optional[Path] = None
    source_type: str = "news"    # for MutemoOS legal updates
    scraped_at: Optional[object] = None  # datetime set at scrape time


async def _scrape_source(source: dict, dry_run: bool = False) -> list[NewsItem]:
    """Scrape a single news source and return relevant NewsItems."""
    name = source["name"]
    listing_url = source["listing_url"]
    article_pattern = source["article_pattern"]

    logger.info(f"[news/{name}] Scraping listing: {listing_url}")
    try:
        md = await scrape_markdown(listing_url, proxy="basic", wait_ms=1000)
    except FirecrawlError as e:
        logger.error(f"[news/{name}] Failed to scrape listing: {e}")
        return []

    if not md:
        logger.warning(f"[news/{name}] Empty markdown from listing")
        return []

    # Extract article URLs
    article_urls = article_pattern.findall(md)
    article_urls = [u for u in article_urls if not _is_excluded(u)]

    # Deduplicate
    seen: set[str] = set()
    unique_urls: list[str] = []
    for u in article_urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    logger.info(f"[news/{name}] Found {len(unique_urls)} article URLs")

    items: list[NewsItem] = []
    max_new = state.get_max_new_per_run()

    for url in unique_urls:
        if len(items) >= max_new:
            break

        if state.is_seen(url):
            state.increment_skipped()
            continue

        # Scrape the article
        try:
            article_md = await scrape_markdown(url, proxy="basic", wait_ms=500)
        except FirecrawlError as e:
            logger.warning(f"[news/{name}] Failed to scrape {url}: {e}")
            continue

        if not article_md or len(article_md) < 100:
            continue

        # Filter by legal keywords
        if not _is_legal_content(article_md):
            logger.debug(f"[news/{name}] Skipping non-legal article: {url}")
            if not dry_run:
                state.mark_seen(url)  # mark as seen so we don't check again
            continue

        title_m = RE_TITLE.search(article_md)
        title = title_m.group(1).strip() if title_m else url.split("/")[-2].replace("-", " ").title()

        date_m = RE_DATE.search(article_md)
        doc_date = date_m.group(1).strip() if date_m else None

        summary = article_md[:800].strip()

        item = NewsItem(
            url=url,
            title=title,
            source=name,
            doc_date=doc_date,
            markdown_summary=summary,
            scraped_at=datetime.now(timezone.utc),
        )

        if dry_run:
            logger.info(f"[DRY RUN] Would push news: {title} — {url}")
        else:
            state.mark_seen(url)

        items.append(item)

    return items


async def run(dry_run: bool = False) -> list[NewsItem]:
    """
    Main entry point. Scrapes all news sources and returns new legal NewsItems.
    Each source is scraped sequentially to avoid rate-limiting.
    """
    logger.info(f"[news] Starting scrape (dry_run={dry_run})")
    all_items: list[NewsItem] = []

    for source in NEWS_SOURCES:
        items = await _scrape_source(source, dry_run=dry_run)
        all_items.extend(items)

    state.set_last_scraped("news")
    logger.info(f"[news] Done. {len(all_items)} new legal news items.")
    return all_items
