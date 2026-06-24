"""
APScheduler-based scheduler for the legal feed.
Staggered UTC times to spread Firecrawl credit usage:
  04:00 UTC — ZimLII
  04:30 UTC — Veritas
  05:00 UTC — LRF
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from scrapers.zimlii import run as run_zimlii
from scrapers.veritas import run as run_veritas
from scrapers.lrf import run as run_lrf

CAT = pytz.timezone("Africa/Harare")

def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=pytz.utc)

    scheduler.add_job(
        run_zimlii,
        CronTrigger(hour=4, minute=0, timezone=pytz.utc),
        id="zimlii",
        name="ZimLII scrape",
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        run_veritas,
        CronTrigger(hour=4, minute=30, timezone=pytz.utc),
        id="veritas",
        name="Veritas scrape",
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        run_lrf,
        CronTrigger(hour=5, minute=0, timezone=pytz.utc),
        id="lrf",
        name="LRF scrape",
        misfire_grace_time=1800,
    )

    return scheduler