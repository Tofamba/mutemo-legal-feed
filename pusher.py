"""
pusher.py — Push new legal content to MutemoOS via webhook.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, Union

import httpx

import state
from scrapers.zimlii import JudgmentItem
from scrapers.veritas import LegislationItem
from scrapers.lrf import DigestItem
from scrapers.news import NewsItem
from scrapers.zlhr import ZLHRItem
from scrapers.lawsafrica import LawsAfricaItem

logger = logging.getLogger(__name__)

MUTEMOS_BASE_URL    = os.environ.get("MUTEMOS_BASE_URL", "https://mutemoos-production.up.railway.app")
MUTEMOS_ADMIN_TOKEN = os.environ.get("MUTEMOS_ADMIN_TOKEN", "")
MUTEMOS_FIRM_ID     = os.environ.get("MUTEMOS_FIRM_ID", "")
CF_CLIENT_ID        = os.environ.get("CF_CLIENT_ID", "")
CF_CLIENT_SECRET    = os.environ.get("CF_CLIENT_SECRET", "")

MAX_RETRIES  = 3
RETRY_DELAYS = [5, 15, 45]
PUSH_TIMEOUT = 120

FeedItem = Union[JudgmentItem, LegislationItem, DigestItem, NewsItem, ZLHRItem, LawsAfricaItem]


def _build_headers() -> dict:
    headers = {}
    if MUTEMOS_ADMIN_TOKEN:
        headers["X-Admin-Token"] = MUTEMOS_ADMIN_TOKEN
    if MUTEMOS_FIRM_ID:
        headers["X-Firm-ID"] = MUTEMOS_FIRM_ID
    if CF_CLIENT_ID:
        headers["CF-Access-Client-Id"] = CF_CLIENT_ID
    if CF_CLIENT_SECRET:
        headers["CF-Access-Client-Secret"] = CF_CLIENT_SECRET
    return headers


def _make_text_file(item) -> tuple:
    name = getattr(item, 'case_name', None) or getattr(item, 'title', None) or 'document'
    content = "\n".join([
        f"URL: {item.url}",
        f"TITLE/CASE: {name}",
        f"CITATION: {getattr(item, 'citation', '') or ''}",
        f"COURT: {getattr(item, 'court', '') or ''}",
        f"JUDGE: {getattr(item, 'judge', '') or ''}",
        f"DATE: {getattr(item, 'judgment_date', '') or getattr(item, 'doc_date', '') or ''}",
        f"SOURCE: {item.source}",
        "",
        item.markdown_summary or "",
    ])
    filename = f"{name.replace(' ', '_')[:60]}.txt"
    return (filename, content.encode("utf-8"), "text/plain")


def _make_legal_text_file(item: LegislationItem) -> tuple:
    content = "\n".join([
        f"URL: {item.url}",
        f"TITLE: {item.title}",
        f"REFERENCE: {item.reference or ''}",
        f"DATE: {item.doc_date or ''}",
        f"SOURCE: {item.source}",
        f"TYPE: {item.source_type}",
        "",
        item.markdown_summary or "",
    ])
    filename = f"{item.title.replace(' ', '_')[:60]}.txt"
    return (filename, content.encode("utf-8"), "text/plain")


async def _push_zlr_entry(item, client: httpx.AsyncClient) -> bool:
    url = f"{MUTEMOS_BASE_URL}/api/zlr/upload"
    form_data = {
        "source": item.source,
        "zimlii_url": item.url,
    }
    pdf_path: Optional[Path] = getattr(item, 'pdf_path', None)
    opened_file = None
    try:
        if pdf_path and pdf_path.exists():
            opened_file = open(pdf_path, "rb")
            files = {"file": (pdf_path.name, opened_file, "application/pdf")}
        else:
            logger.warning(f"[pusher] No PDF for {item.url} — pushing markdown as text file")
            files = {"file": _make_text_file(item)}

        resp = await client.post(url, data=form_data, files=files, headers=_build_headers())
        if resp.status_code in (200, 201, 202):
            logger.info(f"[pusher] ✓ ZLR pushed: {item.url}")
            state.increment_pushed()
            return True
        else:
            logger.warning(f"[pusher] ZLR push failed {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        if opened_file:
            opened_file.close()
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


async def _push_legal_update(item, client: httpx.AsyncClient) -> bool:
    url = f"{MUTEMOS_BASE_URL}/api/legal-updates/upload"
    source_type = getattr(item, 'source_type', 'legislation')
    source_name = getattr(item, 'source', 'Unknown')
    reference = getattr(item, 'reference', None) or getattr(item, 'title', '')[:200]

    form_data = {
        "source_type": source_type,
        "source_name": source_name,
        "reference": reference,
    }

    pdf_path: Optional[Path] = getattr(item, 'pdf_path', None)
    opened_file = None
    try:
        if pdf_path and pdf_path.exists():
            opened_file = open(pdf_path, "rb")
            files = {"file": (pdf_path.name, opened_file, "application/pdf")}
        else:
            logger.warning(f"[pusher] No PDF for {item.url} — pushing markdown as text file")
            if isinstance(item, LegislationItem):
                files = {"file": _make_legal_text_file(item)}
            else:
                files = {"file": _make_text_file(item)}

        resp = await client.post(url, data=form_data, files=files, headers=_build_headers())
        if resp.status_code in (200, 201, 202):
            logger.info(f"[pusher] ✓ Legal update pushed: {item.url}")
            state.increment_pushed()
            return True
        else:
            logger.warning(f"[pusher] Legal update push failed {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        if opened_file:
            opened_file.close()
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


async def _push_news_item(item: NewsItem, client: httpx.AsyncClient) -> bool:
    url = f"{MUTEMOS_BASE_URL}/api/legal-updates/upload"
    form_data = {
        "source_type": "news",
        "source_name": item.source,
        "reference": item.title[:200],
    }
    content = "\n".join([
        f"SOURCE: {item.source}",
        f"URL: {item.url}",
        f"TITLE: {item.title}",
        f"DATE: {item.doc_date or ''}",
        "",
        item.markdown_summary or "",
    ])
    filename = f"{item.title.replace(' ', '_')[:60]}.txt"
    files = {"file": (filename, content.encode("utf-8"), "text/plain")}
    try:
        resp = await client.post(url, data=form_data, files=files, headers=_build_headers())
        if resp.status_code in (200, 201, 202):
            logger.info(f"[pusher] ✓ News pushed: {item.title[:60]}")
            state.increment_pushed()
            return True
        else:
            logger.warning(f"[pusher] News push failed {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception:
        raise


async def push_with_retry(item: FeedItem, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info(f"[DRY RUN] Would push to MutemoOS: {item.url}")
        return True

    if not MUTEMOS_ADMIN_TOKEN:
        logger.error("[pusher] MUTEMOS_ADMIN_TOKEN not set — cannot push to MutemoOS")
        return False

    async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=True) as client:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if isinstance(item, NewsItem):
                    success = await _push_news_item(item, client)
                elif isinstance(item, LegislationItem):
                    success = await _push_legal_update(item, client)
                elif isinstance(item, LawsAfricaItem):
                    if item.source_type == "legislation":
                        success = await _push_legal_update(item, client)
                    else:
                        success = await _push_zlr_entry(item, client)
                elif isinstance(item, ZLHRItem):
                    # ZLHR items are human rights case updates — push as legal updates
                    success = await _push_legal_update(item, client)
                else:
                    # JudgmentItem and DigestItem go to ZLR
                    success = await _push_zlr_entry(item, client)

                if success:
                    return True

            except httpx.TimeoutException:
                logger.warning(f"[pusher] Attempt {attempt}/{MAX_RETRIES} timed out for {item.url}")
            except httpx.RequestError as e:
                logger.warning(f"[pusher] Attempt {attempt}/{MAX_RETRIES} network error for {item.url}: {e}")
            except Exception as e:
                logger.error(f"[pusher] Attempt {attempt}/{MAX_RETRIES} unexpected error for {item.url}: {e}")

            if attempt < MAX_RETRIES:
                delay = RETRY_DELAYS[attempt - 1]
                logger.info(f"[pusher] Retrying in {delay}s...")
                await asyncio.sleep(delay)

    logger.error(f"[pusher] All {MAX_RETRIES} attempts failed for {item.url}")
    state.log_push_failure({
        "url": item.url,
        "source": item.source,
        "title": getattr(item, "title", None) or getattr(item, "case_name", "unknown"),
    })
    return False


async def push_batch(items: list[FeedItem], dry_run: bool = False) -> dict:
    pushed = 0
    failed = 0
    for item in items:
        success = await push_with_retry(item, dry_run=dry_run)
        if success:
            pushed += 1
        else:
            failed += 1
        await asyncio.sleep(1)
    return {"pushed": pushed, "failed": failed, "total": len(items)}
