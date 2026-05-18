"""
snapshot_scheduler.py
---------------------
Snapshot job'larını mevcut APScheduler instance'ına ekler.
main.py'nin lifespan fonksiyonundan çağrılır.
"""
import os
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.engine import Engine

from snapshot_service import take_snapshot

logger = logging.getLogger("globdmc.snapshot")

# Türkiye saati UTC+3 → 02:00 TRT = 23:00 UTC önceki gün
# Örnek: Türkiye'de 02:00 = UTC 23:00 (önceki gün) → hour=23, minute=0
_SNAPSHOT_HOUR   = int(os.environ.get("SNAPSHOT_HOUR",   "23"))   # UTC saat
_SNAPSHOT_MINUTE = int(os.environ.get("SNAPSHOT_MINUTE", "0"))    # UTC dakika


def _run_daily_snapshot(engine: Engine) -> None:
    """APScheduler tarafından çağrılır. Hataları yutmaz, loglar."""
    logger.info("Gece snapshot job basladi")
    try:
        result = take_snapshot(engine)
        logger.info("Gece snapshot job bitti | %s", result)
    except Exception as exc:
        logger.error("Gece snapshot job hatasi: %s", exc, exc_info=True)


def setup_snapshot_scheduler(scheduler: BackgroundScheduler, engine: Engine) -> None:
    """
    Mevcut APScheduler instance'ına günlük snapshot job'ını ekler.
    Duplicate job eklenmemesi için replace_existing=True kullanılır.

    Çağrı yeri: main.py lifespan() içinde, scheduler.start()'tan önce.
    """
    scheduler.add_job(
        _run_daily_snapshot,
        trigger="cron",
        hour=_SNAPSHOT_HOUR,
        minute=_SNAPSHOT_MINUTE,
        kwargs={"engine": engine},
        id="daily_tour_snapshot",
        replace_existing=True,
        misfire_grace_time=3600,   # 1 saat içinde kaçırılan job yine çalışır
    )
    logger.info(
        "Snapshot scheduler kuruldu: her gun %02d:%02d UTC'de calisir (02:00 TRT)",
        _SNAPSHOT_HOUR, _SNAPSHOT_MINUTE,
    )
