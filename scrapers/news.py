"""
scrapers/news.py — Zimbabwe legal news scraper.

Monitors 6 Zimbabwean news sources for legal, court, and regulatory stories.
Schedule: 05:30 UTC (07:30 CAT) — runs after ZimLII, Veritas, LRF.

Sources:
  - NewsDay         (newsday.co.zw)
  - The Herald      (herald.co.zw)
  - Financial Gazette (fingaz.co.zw)
  - Zimbabwe Independent (theindependent.co.zw)
  - Chronicle       (chronicle.co.zw)
  - Business Weekly (businessweekly.co.zw)

URL structures (verified 2026-06-29):
  NewsDay:     /local-news/article/200057756/article-slug
  Herald:      /category/article/ID/slug or /YYYY/MM/DD/slug
  FinGaz:      /YYYY/MM/DD/slug or /category/slug
  Independent: /category/article/ID/slug
  Chronicle:   /YYYY/MM/DD/slug
  BizWeekly:   /slug or /category/slug
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from firecrawl_client import scrape_markdown, FirecrawlError
import state

logger = logging.getLogger(__name__)

NEWS_SOURCES = [
    {
        "name": "NewsDay",
        "listing_url": "https://www.newsday.co.zw/local-news/",
        "base_url": "https://www.newsday.co.zw",
        "article_pattern": re.compile(
            r"https://www\.newsday\.co\.zw/[a-z0-9\-]+/article/\d+/[a-z0-9\-]+/?"
        ),
    },
    {
        "name": "The Herald",
        "listing_url": "https://www.herald.co.zw/",
        "base_url": "https://www.herald.co.zw",
        "article_pattern": re.compile(
            r"https://www\.herald\.co\.zw/(?:\d{4}/\d{2}/\d{2}/[a-z0-9\-]+|[a-z0-9\-]+/article/\d+/[a-z0-9\-]+)/?",
        ),
    },
    {
        "name": "Financial Gazette",
        "listing_url": "https://www.fingaz.co.zw/",
        "base_url": "https://www.fingaz.co.zw",
        "article_pattern": re.compile(
            r"https://www\.fingaz\.co\.zw/(?:\d{4}/\d{2}/\d{2}/[a-z0-9\-]+|[a-z0-9\-]+/[a-z0-9\-]+)/?",
        ),
    },
    {
        "name": "Zimbabwe Independent",
        "listing_url": "https://www.theindependent.co.zw/",
        "base_url": "https://www.theindependent.co.zw",
        "article_pattern": re.compile(
            r"https://www\.theindependent\.co\.zw/[a-z0-9\-]+/article/\d+/[a-z0-9\-]+/?",
        ),
    },
    {
        "name": "Chronicle",
        "listing_url": "https://www.chronicle.co.zw/",
        "base_url": "https://www.chronicle.co.zw",
        "article_pattern": re.compile(
            r"https://www\.chronicle\.co\.zw/(?:\d{4}/\d{2}/\d{2}/[a-z0-9\-]+|[a-z0-9\-]+/article/\d+/[a-z0-9\-]+)/?",
        ),
    },
    {
        "name": "Business Weekly",
        "listing_url": "https://businessweekly.co.zw/",
        "base_url": "https://businessweekly.co.zw",
        "article_pattern": re.compile(
            r"https://businessweekly\.co\.zw/(?:\d{4}/\d{2}/\d{2}/[a-z0-9\-]+|[a-z0-9\-]+/[a-z0-9\-]+)/?",
        ),
    },
]

LEGAL_KEYWORDS = [
    "court", "judge", "judgment", "magistrate", "high court", "supreme court",
    "constitutional court", "labour court", "appeal", "sentence", "convicted",
    "acquitted", "interdict", "injunction", "lawsuit", "litigation", "legal",
    "attorney", "advocate", "lawyer", "prosecution", "accused", "defendant",
    "plaintiff", "applicant", "respondent", "verdict", "ruling", "order",
    "zimra", "revenue authority", "tax", "vat", "customs",
    "reserve bank", "financial intelligence", "aml",
    "securities", "stock exchange", "zse",
    "companies act", "companies registry", "liquidation", "winding up",
    "eviction", "ejectment", "spoliation", "rei vindicatio",
    "constitution", "constitutional", "bill of rights", "fundamental rights",
    "parliament", "legislation", "statutory instrument", "gazette",
    "law society", "legal practitioners", "bar association",
]

RE_DATE  = re.compile(
    r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|"
    r"August|September|October|November|December)\s+\d{4})\b"
)
RE_TITLE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

EXCLUDE_PATTERNS = [
    "/category/", "/tag/", "/author/", "/page/", "/wp-content/",
    "/wp-admin/", "/feed/", "#", "mailto:", "tel:", "/about",
    "/contact", "/advertise", "/subscribe", "/privacy", "/terms",
    "/sport/", "/entertainment/", "/lifestyle/", "/technology/",
]


def _is_excluded(url: str) -> bool:
    return any(pat in url for pat in EXCLUDE_PATTERNS)


def _is_legal_content(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in LEGAL_KEYWORDS)


@dataclass
class NewsItem:
    url: str
    title: str
    source: str
    doc_date: Optional[str]
    markdown_summary: str
    pdf_url: Optional[str] = None
    pdf_path: Optional[Path] = None
    source_type: str = "news"


async def _scrape_source(source: dict, dry_run: bool = False) -> list[NewsItem]:
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

    article_urls = article_pattern.findall(md)
    article_urls = [u for u in article_urls if not _is_excluded(u)]

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

        try:
            article_md = await scrape_markdown(url, proxy="basic", wait_ms=500)
        except FirecrawlError as e:
            logger.warning(f"[news/{name}] Failed to scrape {url}: {e}")
            continue

        if not article_md or len(article_md) < 100:
            continue

        if not _is_legal_content(article_md):
            logger.debug(f"[news/{name}] Skipping non-legal article: {url}")
            if not dry_run:
                state.mark_seen(url)
            continue

        title_m = RE_TITLE.search(article_md)
        title = title_m.group(1).strip() if title_m else url.rstrip("/").split("/")[-1].replace("-", " ").title()

        date_m = RE_DATE.search(article_md)
        doc_date = date_m.group(1).strip() if date_m else None

        summary = article_md[:800].strip()

        item = NewsItem(
            url=url,
            title=title,
            source=name,
            doc_date=doc_date,
            markdown_summary=summary,
        )

        if dry_run:
            logger.info(f"[DRY RUN] Would push news: {title} — {url}")
        else:
            state.mark_seen(url)

        items.append(item)

    return items


async def run(dry_run: bool = False) -> list[NewsItem]:
    logger.info(f"[news] Starting scrape (dry_run={dry_run})")
    all_items: list[NewsItem] = []

    for source in NEWS_SOURCES:
        items = await _scrape_source(source, dry_run=dry_run)
        all_items.extend(items)

    state.set_last_scraped("news")
    logger.info(f"[news] Done. {len(all_items)} new legal news items.")
    return all_items
