"""
scrapers/lawsafrica.py — Laws.Africa Content API scraper.

Uses the Laws.Africa Content API v3 to fetch recently updated Zimbabwe
judgments and legislation. Runs weekly on Sundays.

Free tier: requires signup at laws.africa — set LAWS_AFRICA_TOKEN env var.
Endpoint: https://api.laws.africa/v3/akn/zw/

The API returns paginated results ordered by last updated date.
We check for items updated since the last scrape date.

Schedule: Sundays at 06:00 UTC (08:00 CAT)
Credit budget: REST API calls, not Firecrawl — no credit cost.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx

import state

logger = logging.getLogger(__name__)

LAWS_AFRICA_TOKEN = os.environ.get("LAWS_AFRICA_TOKEN", "")
BASE_URL = "https://api.laws.africa/v3"
TIMEOUT  = 30


@dataclass
class LawsAfricaItem:
    url: str
    title: str
    frbr_uri: str
    doc_date: Optional[str]
    source_type: str  # "legislation" or "case_law"
    court: Optional[str] = None
    citation: Optional[str] = None
    pdf_url: Optional[str] = None
    pdf_path: Optional[Path] = None
    source: str = "Laws.Africa"
    markdown_summary: str = ""


async def _fetch_recent(endpoint: str, since_days: int = 7) -> list[dict]:
    """Fetch recently updated items from the Laws.Africa API."""
    if not LAWS_AFRICA_TOKEN:
        logger.error("[lawsafrica] LAWS_AFRICA_TOKEN not set — cannot use Laws.Africa API")
        return []

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d")
    headers = {"Authorization": f"Token {LAWS_AFRICA_TOKEN}"}
    results = []

    url = f"{BASE_URL}{endpoint}?updated_at__gte={since}&page_size=20"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        while url:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 401:
                    logger.error("[lawsafrica] Invalid LAWS_AFRICA_TOKEN (401)")
                    break
                if resp.status_code == 403:
                    logger.error("[lawsafrica] Access forbidden (403) — check your plan")
                    break
                if resp.status_code != 200:
                    logger.error(f"[lawsafrica] API error {resp.status_code}: {resp.text[:200]}")
                    break

                data = resp.json()
                results.extend(data.get("results", []))
                url = data.get("next")  # pagination

            except Exception as e:
                logger.error(f"[lawsafrica] Request failed: {e}")
                break

    logger.info(f"[lawsafrica] Fetched {len(results)} items from {endpoint}")
    return results


def _parse_judgment(item: dict) -> Optional[LawsAfricaItem]:
    """Parse a judgment result from the Laws.Africa API."""
    frbr_uri = item.get("frbr_uri", "")
    title = item.get("title", "Unknown")
    doc_date = item.get("date", "")
    url = f"https://zimlii.org{frbr_uri}" if frbr_uri else item.get("url", "")

    # Extract citation from title or FRBR URI
    citation = None
    import re
    citation_m = re.search(
        r"\b(ZWSC|ZWCC|ZWHHC|ZWBHC|ZWLC|SC|HH|HC|HB)\s*\d+[-/]\d{2,4}\b",
        title, re.IGNORECASE
    )
    if citation_m:
        citation = citation_m.group(0)

    # Extract court from FRBR URI: /akn/zw/judgment/{court}/...
    court = None
    court_m = re.search(r"/akn/zw/judgment/([a-z]+)/", frbr_uri)
    if court_m:
        court_map = {
            "zwhhc": "Harare High Court",
            "zwbhc": "Bulawayo High Court",
            "zwsc": "Supreme Court",
            "zwcc": "Constitutional Court",
            "zwlc": "Labour Court",
        }
        court = court_map.get(court_m.group(1), court_m.group(1).upper())

    summary = f"TITLE: {title}\nFRBR URI: {frbr_uri}\nDATE: {doc_date}\nCOURT: {court or ''}\nCITATION: {citation or ''}"

    return LawsAfricaItem(
        url=url,
        title=title,
        frbr_uri=frbr_uri,
        doc_date=doc_date,
        source_type="case_law",
        court=court,
        citation=citation,
        markdown_summary=summary,
    )


def _parse_legislation(item: dict) -> Optional[LawsAfricaItem]:
    """Parse a legislation result from the Laws.Africa API."""
    frbr_uri = item.get("frbr_uri", "")
    title = item.get("title", "Unknown")
    doc_date = item.get("date", "")
    url = f"https://zimlii.org{frbr_uri}" if frbr_uri else item.get("url", "")

    summary = f"TITLE: {title}\nFRBR URI: {frbr_uri}\nDATE: {doc_date}\nTYPE: legislation"

    return LawsAfricaItem(
        url=url,
        title=title,
        frbr_uri=frbr_uri,
        doc_date=doc_date,
        source_type="legislation",
        markdown_summary=summary,
    )


async def run(dry_run: bool = False) -> list[LawsAfricaItem]:
    logger.info(f"[lawsafrica] Starting weekly scrape (dry_run={dry_run})")

    if not LAWS_AFRICA_TOKEN:
        logger.warning("[lawsafrica] LAWS_AFRICA_TOKEN not set — skipping")
        state.set_last_scraped("lawsafrica")
        return []

    new_items: list[LawsAfricaItem] = []
    max_new = state.get_max_new_per_run()

    # Fetch recent judgments
    judgment_results = await _fetch_recent("/akn/zw/judgment/", since_days=7)
    for result in judgment_results:
        if len(new_items) >= max_new:
            break
        frbr_uri = result.get("frbr_uri", "")
        if state.is_seen(frbr_uri):
            state.increment_skipped()
            continue
        item = _parse_judgment(result)
        if item:
            if dry_run:
                logger.info(f"[DRY RUN] Would push judgment: {item.title}")
            else:
                state.mark_seen(frbr_uri)
            new_items.append(item)

    # Fetch recent legislation
    if len(new_items) < max_new:
        leg_results = await _fetch_recent("/akn/zw/act/", since_days=7)
        for result in leg_results:
            if len(new_items) >= max_new:
                break
            frbr_uri = result.get("frbr_uri", "")
            if state.is_seen(frbr_uri):
                state.increment_skipped()
                continue
            item = _parse_legislation(result)
            if item:
                if dry_run:
                    logger.info(f"[DRY RUN] Would push legislation: {item.title}")
                else:
                    state.mark_seen(frbr_uri)
                new_items.append(item)

    state.set_last_scraped("lawsafrica")
    logger.info(f"[lawsafrica] Done. {len(new_items)} new items.")
    return new_items
