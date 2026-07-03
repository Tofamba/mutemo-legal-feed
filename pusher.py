"""
pusher.py — Push new legal content to all registered MutemoOS instances.

Firm Registry:
  Each firm has its own MutemoOS instance, admin token, and firm_id.
  The pusher reads firm config from environment variables and pushes
  every scraped item to ALL registered firms simultaneously.

  To add a new firm:
    1. Deploy a new MutemoOS instance on Railway
    2. Add FIRM_{N}_BASE_URL, FIRM_{N}_ADMIN_TOKEN, FIRM_{N}_ID to env vars
    3. The pusher picks them up automatically at startup

  Environment variable pattern:
    FIRM_1_BASE_URL=https://mutemo-sawyer.up.railway.app
    FIRM_1_ADMIN_TOKEN=144d3459...
    FIRM_1_ID=a1b2c3d4-0000-0000-0000-000000000001
    FIRM_1_NAME=Sawyer & Mkushi

    FIRM_2_BASE_URL=https://mutemo-legalcorner.up.railway.app
    FIRM_2_ADMIN_TOKEN=0190cf0a...
    FIRM_2_ID=b2c3d4e5-1111-1111-1111-111111111112
    FIRM_2_NAME=Legal Corner

  Fallback (single-firm legacy mode):
    If no FIRM_1_* vars set, falls back to MUTEMOS_BASE_URL / MUTEMOS_ADMIN_TOKEN / MUTEMOS_FIRM_ID

Validation Gate:
  Every item is validated before push. Items that fail validation are
  logged and skipped — they never enter MutemoOS's database.

Retry policy:
  3 attempts with exponential backoff: 5s, 15s, 45s per firm.
  On final failure, logs to state.push_failures for audit.
  Never silently drops an item.
"""

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import httpx

import state
from scrapers.zimlii import JudgmentItem
from scrapers.veritas import LegislationItem
from scrapers.lrf import DigestItem
from scrapers.news import NewsItem
from scrapers.zlhr import ZLHRItem

logger = logging.getLogger(__name__)

MAX_RETRIES  = 3
RETRY_DELAYS = [5, 15, 45]   # seconds between attempts
PUSH_TIMEOUT = 120            # seconds — PDF uploads can be slow

FeedItem = Union[JudgmentItem, LegislationItem, DigestItem, NewsItem, ZLHRItem]


# ── Firm Registry ─────────────────────────────────────────────────────────────

@dataclass
class FirmConfig:
    name: str
    base_url: str
    admin_token: str
    firm_id: str


def _load_firms() -> list[FirmConfig]:
    """
    Load firm configs from environment variables.
    Supports up to 10 firms via FIRM_1_* through FIRM_10_* pattern.
    Falls back to legacy single-firm MUTEMOS_* vars if no FIRM_* vars set.
    """
    firms = []

    for n in range(1, 11):
        base_url = os.environ.get(f"FIRM_{n}_BASE_URL", "").strip()
        token    = os.environ.get(f"FIRM_{n}_ADMIN_TOKEN", "").strip()
        firm_id  = os.environ.get(f"FIRM_{n}_ID", "").strip()
        name     = os.environ.get(f"FIRM_{n}_NAME", f"Firm {n}").strip()

        if not base_url:
            break  # no more firms configured

        if not token:
            logger.warning(f"[pusher] FIRM_{n}_ADMIN_TOKEN not set — skipping {name}")
            continue

        firms.append(FirmConfig(
            name=name,
            base_url=base_url.rstrip("/"),
            admin_token=token,
            firm_id=firm_id,
        ))
        logger.info(f"[pusher] Registered firm: {name} → {base_url}")

    # Legacy fallback — single firm via MUTEMOS_* vars
    if not firms:
        base_url = os.environ.get("MUTEMOS_BASE_URL", "").strip()
        token    = os.environ.get("MUTEMOS_ADMIN_TOKEN", "").strip()
        firm_id  = os.environ.get("MUTEMOS_FIRM_ID", "").strip()
        if base_url and token:
            firms.append(FirmConfig(
                name="Default Firm",
                base_url=base_url.rstrip("/"),
                admin_token=token,
                firm_id=firm_id,
            ))
            logger.info(f"[pusher] Using legacy single-firm config: {base_url}")
        else:
            logger.error("[pusher] No firm config found. Set FIRM_1_BASE_URL / FIRM_1_ADMIN_TOKEN or MUTEMOS_BASE_URL / MUTEMOS_ADMIN_TOKEN.")

    return firms


# Load at module startup
FIRMS: list[FirmConfig] = _load_firms()


# ── Validation Gate ───────────────────────────────────────────────────────────

ERROR_PHRASES = [
    "page not found", "404 not found", "access denied",
    "cloudflare", "just a moment", "enable javascript",
    "error 403", "error 500", "service unavailable",
    "bandwidth limit exceeded", "too many requests",
]

def validate_item(item: FeedItem) -> tuple[bool, str]:
    """
    Deterministic validation gate — runs before any push attempt.
    Returns (is_valid, reason).
    """
    text = getattr(item, 'markdown_summary', '') or ''
    url  = getattr(item, 'url', '') or ''

    if not url:
        return False, "no source URL"

    if len(text.strip()) < 150:
        return False, f"content too short ({len(text.strip())} chars) — likely truncated or empty"

    text_lower = text.lower()
    for phrase in ERROR_PHRASES:
        if phrase in text_lower:
            return False, f"error page detected: '{phrase}'"

    # For judgments, require at least a case name or citation
    if isinstance(item, JudgmentItem):
        if not item.case_name and not item.citation:
            return False, "judgment has no case name or citation"

    return True, "ok"


# ── Push Functions ────────────────────────────────────────────────────────────

def _build_headers(firm: FirmConfig) -> dict:
    headers = {"X-Admin-Token": firm.admin_token}
    if firm.firm_id:
        headers["X-Firm-ID"] = firm.firm_id
    return headers


async def _push_zlr_entry(item: Union[JudgmentItem, DigestItem],
                          client: httpx.AsyncClient,
                          firm: FirmConfig) -> bool:
    """Push a judgment or LRF digest to /api/zlr/upload."""
    url = f"{firm.base_url}/api/zlr/upload"

    form_data = {
        "source":         item.source,
        "case_name":      item.case_name or getattr(item, "title", "") or "Unknown",
        "citation":       item.citation or "",
        "court":          getattr(item, "court", "") or "",
        "judge":          getattr(item, "judge", "") or "",
        "judgment_date":  getattr(item, "judgment_date", "") or getattr(item, "doc_date", "") or "",
        "zimlii_url":     item.url,
        "source_url":     item.url,
        "summary":        (item.markdown_summary or "")[:500],
        "scraped_at":     getattr(item, "scraped_at", None) and item.scraped_at.isoformat() or "",
    }

    pdf_path: Optional[Path] = getattr(item, "pdf_path", None)
    files = None

    if pdf_path and pdf_path.exists():
        files = {"file": (pdf_path.name, open(pdf_path, "rb"), "application/pdf")}
    else:
        logger.warning(f"[pusher/{firm.name}] No PDF for {item.url} — pushing metadata only")

    try:
        if files:
            resp = await client.post(url, data=form_data, files=files, headers=_build_headers(firm))
        else:
            resp = await client.post(url, data=form_data, headers=_build_headers(firm))

        if resp.status_code in (200, 201):
            logger.info(f"[pusher/{firm.name}] ✓ ZLR pushed: {item.url}")
            return True
        else:
            logger.warning(f"[pusher/{firm.name}] ZLR push failed {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        if files:
            files["file"][1].close()
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


async def _push_news_item(item: NewsItem,
                          client: httpx.AsyncClient,
                          firm: FirmConfig) -> bool:
    """Push a news article to /api/legal-updates/upload."""
    url = f"{firm.base_url}/api/legal-updates/upload"

    form_data = {
        "source_type": "news",
        "source_name": item.source,
        "reference":   getattr(item, "reference", "") or "",
        "doc_date":    item.doc_date or "",
        "title":       item.title,
        "summary":     (item.markdown_summary or "")[:500],
        "source_url":  item.url,
        "scraped_at":  getattr(item, "scraped_at", None) and item.scraped_at.isoformat() or "",
    }

    try:
        resp = await client.post(url, data=form_data, headers=_build_headers(firm))
        if resp.status_code in (200, 201):
            logger.info(f"[pusher/{firm.name}] ✓ News pushed: {item.url}")
            return True
        else:
            logger.warning(f"[pusher/{firm.name}] News push failed {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[pusher/{firm.name}] News push error: {e}")
        return False


async def _push_legal_update(item: Union[LegislationItem, ZLHRItem],
                             client: httpx.AsyncClient,
                             firm: FirmConfig) -> bool:
    """Push legislation or ZLHR update to /api/legal-updates/upload."""
    url = f"{firm.base_url}/api/legal-updates/upload"

    form_data = {
        "source_type": getattr(item, "source_type", "legal_update"),
        "source_name": item.source,
        "reference":   getattr(item, "reference", "") or getattr(item, "title", "") or "",
        "doc_date":    getattr(item, "doc_date", "") or "",
        "title":       getattr(item, "title", "") or getattr(item, "reference", "") or "",
        "summary":     (item.markdown_summary or "")[:500],
        "source_url":  item.url,
        "scraped_at":  getattr(item, "scraped_at", None) and item.scraped_at.isoformat() or "",
    }

    pdf_path: Optional[Path] = getattr(item, "pdf_path", None)
    files = None

    if pdf_path and pdf_path.exists():
        files = {"file": (pdf_path.name, open(pdf_path, "rb"), "application/pdf")}
    else:
        logger.warning(f"[pusher/{firm.name}] No PDF for {item.url} — pushing metadata only")

    try:
        if files:
            resp = await client.post(url, data=form_data, files=files, headers=_build_headers(firm))
        else:
            resp = await client.post(url, data=form_data, headers=_build_headers(firm))

        if resp.status_code in (200, 201):
            logger.info(f"[pusher/{firm.name}] ✓ Legal update pushed: {item.url}")
            return True
        else:
            logger.warning(f"[pusher/{firm.name}] Legal update push failed {resp.status_code}: {resp.text[:200]}")
            return False
    finally:
        if files:
            files["file"][1].close()
        if pdf_path and pdf_path.exists():
            try:
                pdf_path.unlink()
            except Exception:
                pass


async def _push_to_firm(item: FeedItem,
                        firm: FirmConfig,
                        client: httpx.AsyncClient) -> bool:
    """Push a single item to a single firm with retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if isinstance(item, NewsItem):
                success = await _push_news_item(item, client, firm)
            elif isinstance(item, (LegislationItem, ZLHRItem)):
                success = await _push_legal_update(item, client, firm)
            else:
                success = await _push_zlr_entry(item, client, firm)

            if success:
                return True

        except httpx.TimeoutException:
            logger.warning(f"[pusher/{firm.name}] Attempt {attempt}/{MAX_RETRIES} timed out for {item.url}")
        except httpx.RequestError as e:
            logger.warning(f"[pusher/{firm.name}] Attempt {attempt}/{MAX_RETRIES} network error: {e}")
        except Exception as e:
            logger.error(f"[pusher/{firm.name}] Attempt {attempt}/{MAX_RETRIES} unexpected error: {e}")

        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[attempt - 1]
            logger.info(f"[pusher/{firm.name}] Retrying in {delay}s...")
            await asyncio.sleep(delay)

    logger.error(f"[pusher/{firm.name}] All {MAX_RETRIES} attempts failed for {item.url}")
    return False


async def push_with_retry(item: FeedItem, dry_run: bool = False) -> bool:
    """
    Validate and push a feed item to ALL registered firms.

    Validation runs first — invalid items are rejected before any network call.
    Each firm gets independent retry logic — one firm being down doesn't
    prevent push to other firms.

    Returns True if pushed successfully to at least one firm.
    """
    # Validation gate
    valid, reason = validate_item(item)
    if not valid:
        logger.warning(f"[pusher] ✗ Rejected: {getattr(item, 'url', '?')} — {reason}")
        state.increment_skipped()
        return False

    if dry_run:
        logger.info(f"[DRY RUN] Would push to {len(FIRMS)} firm(s): {item.url}")
        return True

    if not FIRMS:
        logger.error("[pusher] No firms configured — cannot push")
        return False

    any_success = False
    async with httpx.AsyncClient(timeout=PUSH_TIMEOUT, follow_redirects=True) as client:
        for firm in FIRMS:
            success = await _push_to_firm(item, firm, client)
            if success:
                any_success = True
                state.increment_pushed()
            else:
                state.log_push_failure({
                    "url": item.url,
                    "source": getattr(item, "source", "unknown"),
                    "title": getattr(item, "case_name", None) or getattr(item, "title", "unknown"),
                    "firm": firm.name,
                })

        # Small delay between firms
        await asyncio.sleep(0.5)

    return any_success


async def push_batch(items: list[FeedItem], dry_run: bool = False) -> dict:
    """Push a batch of items to all firms. Returns summary stats."""
    pushed  = 0
    failed  = 0
    skipped = 0

    for item in items:
        valid, reason = validate_item(item)
        if not valid:
            logger.warning(f"[pusher] Skipping invalid item: {reason}")
            skipped += 1
            continue

        success = await push_with_retry(item, dry_run=dry_run)
        if success:
            pushed += 1
        else:
            failed += 1

        await asyncio.sleep(1)

    logger.info(f"[pusher] Batch complete: {pushed} pushed, {failed} failed, {skipped} skipped (validation)")
    return {"pushed": pushed, "failed": failed, "skipped": skipped, "total": len(items)}
