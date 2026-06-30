"""
scheduler.py — Daily scrape scheduler for the Legal Intelligence Feed.

Schedule (all times UTC, which is CAT - 2h):
  WEEKDAYS (Mon-Fri):
    04:00 UTC (06:00 CAT) — ZimLII judgments
    04:30 UTC (06:30 CAT) — Veritas Zimbabwe legislation
    05:00 UTC (07:00 CAT) — LRF case digests (Mondays only)
    05:30 UTC (07:30 CAT) — Legal news (6 sources)
    06:00 UTC (08:00 CAT) — ZLHR press statements

  WEEKLY (Sundays):
    06:00 UTC (08:00 CAT) — Laws.Africa Knowledge Base API
    06:30 UTC (08:30 CAT) — Zimbabwe Electronic Law Journal

Runs as a background asyncio task inside the FastAPI app.
Uses a simple loop with asyncio.sleep — no external task queue needed.
"""

import asyncio
import calendar
import logging
from datetime import datetime, timezone, time as dtime

import state
import pusher
from scrapers import zimlii, veritas, lrf, news, zlhr

logger = logging.getLogger(__name__)

MAX_NEW_PER_RUN = 10

# Schedule entries:
# (hour_utc, minute_utc, scraper_name, run_on_weekdays, run_on_weekends, mondays_only)
SCHEDULE = [
    (4,  0,  "zimlii",     True,  False, False),   # Mon-Fri
    (4, 30,  "veritas",    True,  False, False),   # Mon-Fri
    (5,  0,  "lrf",        True,  False, True),    # Mondays only
    (5, 30,  "news",       True,  False, False),   # Mon-Fri
    (6,  0,  "zlhr",       True,  False, False),   # Mon-Fri
    (6,  0,  "lawsafrica", False, True,  False),   # Sundays only
    (6, 30,  "zelj",       False, True,  False),   # Sundays only (future scraper)
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


async def _run_zlhr(dry_run: bool = False) -> dict:
    logger.info("[scheduler] Running ZLHR scrape...")
    try:
        items = await zlhr.run(dry_run=dry_run)
        items = items[:MAX_NEW_PER_RUN]
        result = await pusher.push_batch(items, dry_run=dry_run)
        logger.info(f"[scheduler] ZLHR complete: {result}")
        return result
    except Exception as e:
        logger.error(f"[scheduler] ZLHR scrape failed: {e}", exc_info=True)
        return {"pushed": 0, "failed": 0, "total": 0, "error": str(e)}


async def _run_lawsafrica(dry_run: bool = False) -> dict:
    """Laws.Africa API scraper — placeholder until scrapers/lawsafrica.py is built."""
    logger.info("[scheduler] Laws.Africa scraper not yet implemented — skipping")
    return {"pushed": 0, "failed": 0, "total": 0, "skipped": True}


async def _run_zelj(dry_run: bool = False) -> dict:
    """Zimbabwe Electronic Law Journal scraper — placeholder."""
    logger.info("[scheduler] ZELJ scraper not yet implemented — skipping")
    return {"pushed": 0, "failed": 0, "total": 0, "skipped": True}


SCRAPER_MAP = {
    "zimlii":     _run_zimlii,
    "veritas":    _run_veritas,
    "lrf":        _run_lrf,
    "news":       _run_news,
    "zlhr":       _run_zlhr,
    "lawsafrica": _run_lawsafrica,
    "zelj":       _run_zelj,
}


async def run_all(dry_run: bool = False) -> dict:
    """Run all active scrapers sequentially. Used by the manual trigger endpoint."""
    logger.info(f"[scheduler] Running all scrapers (dry_run={dry_run})")
    results = {}
    for name, fn in SCRAPER_MAP.items():
        results[name] = await fn(dry_run=dry_run)
    return results


async def run_single(source: str, dry_run: bool = False) -> dict:
    if source not in SCRAPER_MAP:
        raise ValueError(f"Unknown source: {source}. Valid: {list(SCRAPER_MAP.keys())}")
    return await SCRAPER_MAP[source](dry_run=dry_run)


def _should_run(hour: int, minute: int, name: str,
                run_weekdays: bool, run_weekends: bool, mondays_only: bool,
                now: datetime) -> bool:
    """Return True if this scraper should run at the current time."""
    current_time = now.time().replace(second=0, microsecond=0)
    weekday = now.weekday()  # 0=Monday, 6=Sunday
    is_weekday = weekday < 5
    is_weekend = weekday >= 5
    is_monday = weekday == calendar.MONDAY

    if current_time.hour != hour or current_time.minute != minute:
        return False

    if mondays_only and not is_monday:
        return False
    if run_weekdays and not run_weekends and is_weekend:
        return False
    if run_weekends and not run_weekdays and is_weekday:
        return False

    return True


async def scheduler_loop(dry_run: bool = False) -> None:
    """
    Background task that runs the scrapers on schedule.
    Designed to run forever inside the FastAPI lifespan.
    """
    logger.info(f"[scheduler] Scheduler started (dry_run={dry_run})")
    logger.info("[scheduler] Schedule:")
    logger.info("[scheduler]   Mon-Fri: ZimLII 04:00 | Veritas 04:30 | News 05:30 | ZLHR 06:00 UTC")
    logger.info("[scheduler]   Mondays: LRF 05:00 UTC")
    logger.info("[scheduler]   Sundays: Laws.Africa 06:00 | ZELJ 06:30 UTC")

    pending: dict[str, asyncio.Task] = {}

    while True:
        now = datetime.now(timezone.utc)

        for hour, minute, name, run_weekdays, run_weekends, mondays_only in SCHEDULE:
            if name in pending:
                continue  # already running

            if _should_run(hour, minute, name, run_weekdays, run_weekends, mondays_only, now):
                logger.info(
                    f"[scheduler] Triggering {name} at {now.strftime('%H:%M UTC')} "
                    f"(weekday={now.weekday()})"
                )
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

        await asyncio.sleep(55)
