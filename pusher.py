"""
pusher.py — Push new legal content to MutemoOS via webhook.

Pushes:
  - ZimLII judgments → POST /api/zlr/upload  (multipart with PDF)
  - Veritas legislation → POST /api/legal-updates/upload  (multipart with PDF)
  - LRF digests → POST /api/zlr/upload  (multipart with PDF, source=LRF)

Retry policy:
  - 3 attempts with exponential backoff: 5s, 15s, 45s
  - On final failure, logs to state.push_failures for audit
  - Never silently drops an item

Authentication:
  MutemoOS v2 uses OTP-based sessions. For the feed service we use the
  MUTEMOS_ADMIN_TOKEN header (X-Admin-Token) which bypasses OTP for
  machine-to-machine calls. Set MUTEMOS_ADMIN_TOKEN in env vars.
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

logger = logging.getLogger(__name__)

MUTEMOS_BASE_URL   = os.environ.get("MUTEMOS_BASE_URL", "https://mutemoos-production.up.railway.app")
MUTEMOS_ADMIN_TOKEN = os.environ.get("MUTEMOS_ADMIN_TOKEN", "")
MUTEMOS_FIRM_ID    = os.environ.get("MUTEMOS_FIRM_ID", "")

MAX_RETRIES   = 3
RETRY_DELAYS  = [5, 15, 45]   # seconds between attempts
PUSH_TIMEOUT  = 120            # seconds — PDF uploads can be slow

FeedItem = Union[JudgmentItem, LegislationItem, DigestItem]


def _build_headers() -> dict:
    headers = {}
    if MUTEMOS_ADMIN_TOKEN:
        headers["X-Admin-Token"] = MUTEMOS_ADMIN_TOKEN
    if MUTEMOS_FIRM_ID:
        headers["X-Firm-ID"] = MUTEMOS_FIRM_ID
    return headers


async def _push_zlr_entry(item: Union[JudgmentItem, DigestItem], client: httpx.AsyncClient) -> bool:
    """Push a judgment or LRF digest to /api/zlr/upload."""
    url = f"{MUTEMOS_BASE_URL}/api/zlr/upload"

    # Build multipart form data
    form_data = {
        "source": item.source,
        "case_name": item.case_name or item.title if hasattr(item, "title") else (item.case_name or "Unknown"),
        "citation": item.citation or "",
        "court": getattr(item, "court", "") or "",
        "judge": getattr(item, "judge", "") or "",
        "judgment_date": item.judgment_date if hasattr(item, "judgment_date") else (getattr(item, "doc_date", "") or ""),
        "zimlii_url": item.url,
        "summary": item.markdown_summary[:500] if item.markdown_summary else "",
    }

    files = None
    pdf_path: Optional[Path] = item.pdf_path

    if pdf_path and pdf_path.exists():
        files = {"file": (pdf_path.name, open(pdf_path, "rb"), "application/pdf")}
    else:
        # No PDF — push metadata only (the backend will create a stub entry)
        logger.warning(f"[pusher] No PDF for {item.url} — pushing metadata only")

    try:
        if files:
            resp = await client.post(url, data=form_data, files=files, headers=_build_headers())
        else:
            resp = await client.post(url, data=form_data, headers=_build_headers())

        if resp.status_code in (200, 201):
            logger.info(f"[pusher] ✓ ZLR pushed: {item.url}")
            state.increment_pushed()
            return True
        else:
            logger.warning(f"[pusher] ZLR push failed {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        if files:
            files["file"][1].close()
        # Clean up downloaded PDF
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


async def _push_legal_update(item: LegislationItem, client: httpx.AsyncClient) -> bool:
    """Push a Veritas legislation item to /api/legal-updates/upload."""
    url = f"{MUTEMOS_BASE_URL}/api/legal-updates/upload"

    form_data = {
        "source_type": item.source_type,
        "source_name": item.source,
        "reference": item.reference or "",
        "doc_date": item.doc_date or "",
        "title": item.title,
        "summary": item.markdown_summary[:500] if item.markdown_summary else "",
        "source_url": item.url,
    }

    files = None
    pdf_path: Optional[Path] = item.pdf_path

    if pdf_path and pdf_path.exists():
        files = {"file": (pdf_path.name, open(pdf_path, "rb"), "application/pdf")}
    else:
        logger.warning(f"[pusher] No PDF for {item.url} — pushing metadata only")

    try:
        if files:
            resp = await client.post(url, data=form_data, files=files, headers=_build_headers())
        else:
            resp = await client.post(url, data=form_data, headers=_build_headers())

        if resp.status_code in (200, 201):
            logger.info(f"[pusher] ✓ Legal update pushed: {item.url}")
            state.increment_pushed()
            return True
        else:
            logger.warning(f"[pusher] Legal update push failed {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        if files:
            files["file"][1].close()
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


async def push_with_retry(item: FeedItem, dry_run: bool = False) -> bool:
    """
    Push a feed item to MutemoOS with exponential backoff retry.

    Returns True if pushed successfully, False if all retries exhausted.
    On final failure, logs to state.push_failures.
    """
    if dry_run:
        logger.info(f"[DRY RUN] Would push to MutemoOS: {item.url}")
        return True

    if not MUTEMOS_ADMIN_TOKEN:
        logger.error("[pusher] MUTEMOS_ADMIN_TOKEN not set — cannot push to MutemoOS")
        return False

    async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=True) as client:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if isinstance(item, LegislationItem):
                    success = await _push_legal_update(item, client)
                else:
                    # JudgmentItem and DigestItem both go to ZLR
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

    # All retries exhausted
    logger.error(f"[pusher] All {MAX_RETRIES} attempts failed for {item.url}")
    state.log_push_failure({
        "url": item.url,
        "source": item.source,
        "title": getattr(item, "case_name", None) or getattr(item, "title", "unknown"),
    })
    return False


async def push_batch(items: list[FeedItem], dry_run: bool = False) -> dict:
    """Push a batch of items. Returns summary stats."""
    pushed = 0
    failed = 0

    for item in items:
        success = await push_with_retry(item, dry_run=dry_run)
        if success:
            pushed += 1
        else:
            failed += 1
        # Small delay between pushes to avoid hammering MutemoOS
        await asyncio.sleep(1)

    return {"pushed": pushed, "failed": failed, "total": len(items)}
