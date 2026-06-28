"""
scheduler.py — Daily scrape scheduler for the Legal Intelligence Feed.

Schedule (all times UTC, which is CAT - 2h):
  04:00 UTC (06:00 CAT) — ZimLII judgments
  04:30 UTC (06:30 CAT) — Veritas Zimbabwe legislation
  05:00 UTC (07:00 CAT) — LRF case digests

Runs as a background asyncio task inside the FastAPI app.
Uses a simple loop with asyncio.sleep — no external task queue needed.

Credit budget (Firecrawl free tier: 500/month):
  ZimLII:  ~2 pages × 5 credits (stealth) × 30 days = 300 credits
  Veritas: ~2 pages × 5 credits (stealth) × 30 days = 300 credits
  LRF:     ~2 pages × 1 credit  (basic)   × 30 days =  60 credits
  Individual judgment pages: ~2/day × 5 credits × 30 days = 300 credits
  ──────────────────────────────────────────────────────────────────
  Estimated total: ~460 credits/month (within 500 free tier)

  Note: If ZimLII publishes many new judgments in a single day, the
  per-judgment scrapes could push over the limit. The scraper caps at
  MAX_NEW_PER_RUN new items per run to stay within budget.
"""

import asyncio
import logging
from datetime import datetime, timezone, time as dtime

import state
import pusher
from scrapers import zimlii, veritas, lrf, news

logger = logging.getLogger(__name__)

# Maximum new items to process per scraper run (credit budget guard)
MAX_NEW_PER_RUN = 10

# Schedule: (hour_utc, minute_utc, scraper_name)
SCHEDULE = [
    (4,  0,  "zimlii"),
    (4, 30,  "veritas"),
    (5,  0,  "lrf"),
    (5, 30,  "news"),
]


async def _run_zimlii(dry_run: bool = False) -> dict:
    logger.info("[scheduler] Running ZimLII scrape...")
    try:
        items = await zimlii.run(dry_run=dry_run)
        items = items[:MAX_NEW_PER_RUN]
        result = await pusher.push_batch(items, dry_run=dry_run)
        logger.info(f"[scheduler] ZimLII complete: {result}")
        return result
    except Exception as e:
        logger.error(f"[scheduler] ZimLII scrape failed: {e}", exc_info=True)
        return {"pushed": 0, "failed": 0, "total": 0, "error": str(e)}


async def _run_veritas(dry_run: bool = False) -> dict:
    logger.info("[scheduler] Running Veritas scrape...")
    try:
        items = await veritas.run(dry_run=dry_run)
        items = items[:MAX_NEW_PER_RUN]
        result = await pusher.push_batch(items, dry_run=dry_run)
        logger.info(f"[scheduler] Veritas complete: {result}")
        return result
    except Exception as e:
        logger.error(f"[scheduler] Veritas scrape failed: {e}", exc_info=True)
        return {"pushed": 0, "failed": 0, "total": 0, "error": str(e)}


async def _run_news(dry_run: bool = False) -> dict:
    logger.info("[scheduler] Running legal news scrape...")
    try:
        items = await news.run(dry_run=dry_run)
        items = items[:MAX_NEW_PER_RUN]
        result = await pusher.push_batch(items, dry_run=dry_run)
        logger.info(f"[scheduler] News complete: {result}")
        return result
    except Exception as e:
        logger.error(f"[scheduler] News scrape failed: {e}", exc_info=True)
        return {"pushed": 0, "failed": 0, "total": 0, "error": str(e)}


async def _run_lrf(dry_run: bool = False) -> dict:
    logger.info("[scheduler] Running LRF scrape...")
    try:
        items = await lrf.run(dry_run=dry_run)
        items = items[:MAX_NEW_PER_RUN]
        result = await pusher.push_batch(items, dry_run=dry_run)
        logger.info(f"[scheduler] LRF complete: {result}")
        return result
    except Exception as e:
        logger.error(f"[scheduler] LRF scrape failed: {e}", exc_info=True)
        return {"pushed": 0, "failed": 0, "total": 0, "error": str(e)}


SCRAPER_MAP = {
    "zimlii":  _run_zimlii,
    "veritas": _run_veritas,
    "lrf":     _run_lrf,
    "news":    _run_news,
}


async def run_all(dry_run: bool = False) -> dict:
    """Run all three scrapers sequentially. Used by the manual trigger endpoint."""
    logger.info(f"[scheduler] Running all scrapers (dry_run={dry_run})")
    results = {}
    for name, fn in SCRAPER_MAP.items():
        results[name] = await fn(dry_run=dry_run)
    return results


async def run_single(source: str, dry_run: bool = False) -> dict:
    """Run a single scraper by name. Used by the manual trigger endpoint."""
    if source not in SCRAPER_MAP:
        raise ValueError(f"Unknown source: {source}. Valid: {list(SCRAPER_MAP.keys())}")
    return await SCRAPER_MAP[source](dry_run=dry_run)


def _seconds_until(hour: int, minute: int) -> float:
    """Return seconds until the next occurrence of HH:MM UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        # Already past today — schedule for tomorrow
        from datetime import timedelta
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def scheduler_loop(dry_run: bool = False) -> None:
    """
    Background task that runs the scrapers on schedule.
    Designed to run forever inside the FastAPI lifespan.
    """
    logger.info(f"[scheduler] Scheduler started (dry_run={dry_run})")
    logger.info(f"[scheduler] Schedule: ZimLII 04:00 UTC | Veritas 04:30 UTC | LRF 05:00 UTC | News 05:30 UTC")

    # Track which jobs have been scheduled
    pending: dict[str, asyncio.Task] = {}

    while True:
        now = datetime.now(timezone.utc)

        for hour, minute, name in SCHEDULE:
            # Check if it's time to run this scraper
            target_time = dtime(hour, minute)
            current_time = now.time().replace(second=0, microsecond=0)

            # Run if within the current minute and not already running
            if (
                current_time.hour == target_time.hour
                and current_time.minute == target_time.minute
                and name not in pending
            ):
                logger.info(f"[scheduler] Triggering {name} at {now.strftime('%H:%M UTC')}")
                task = asyncio.create_task(SCRAPER_MAP[name](dry_run=dry_run))
                pending[name] = task

        # Clean up completed tasks
        done = [name for name, task in pending.items() if task.done()]
        for name in done:
            task = pending.pop(name)
            try:
                result = task.result()
                logger.info(f"[scheduler] {name} completed: {result}")
            except Exception as e:
                logger.error(f"[scheduler] {name} task raised: {e}")

        # Sleep 55 seconds between checks (avoids double-triggering within same minute)
        await asyncio.sleep(55)
