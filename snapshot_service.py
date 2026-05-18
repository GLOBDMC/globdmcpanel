"""
snapshot_service.py
-------------------
Snapshot iş mantığı.
Mevcut turlar + jolly_sonuc verilerini birleştirip snapshot hazırlar.
DB'ye yazmaz — repository katmanını kullanır.
"""
import logging
from datetime import date, datetime
from sqlalchemy import text
from sqlalchemy.engine import Engine

from snapshot_repository import bulk_insert_snapshots

logger = logging.getLogger("globdmc.snapshot")


# ── Yardımcı fonksiyonlar ────────────────────────────────────────────────────

def _safe_int(val):
    """None veya dönüştürülemeyen değeri güvenle int'e çevirir."""
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _occupancy_rate(sales, quota):
    """Doluluk oranını hesaplar. Sıfır bölme ve None durumları güvenli."""
    try:
        s, q = int(sales), int(quota)
        if q > 0:
            return round(s / q * 100, 2)
    except (TypeError, ValueError):
        pass
    return None


# ── Veri çekme ───────────────────────────────────────────────────────────────

def _fetch_all_tours(engine: Engine) -> list:
    """turlar tablosundan tüm aktif tur verilerini çeker."""
    sql = text("""
        SELECT
            jt_kodu, tur_adi, kalkis_tarihi, havayolu,
            pax, satilan, kalan, guncel_fiyat, rehber
        FROM turlar
        WHERE jt_kodu IS NOT NULL AND jt_kodu != ''
        ORDER BY jt_kodu
    """)
    with engine.connect() as conn:
        return conn.execute(sql).fetchall()


def _fetch_jolly_index(engine: Engine) -> dict:
    """
    jolly_sonuc tablosunu (tur_adi.lower, kalkis_tarihi) → vitrin dict
    şeklinde index'ler. O(1) lookup için.
    """
    sql = text("""
        SELECT grup_adi, kalkis_tarihi, vitrinde, eslesen_jolly_tur
        FROM jolly_sonuc
        WHERE platform = 'jolly'
          AND grup_adi IS NOT NULL
    """)
    index = {}
    with engine.connect() as conn:
        for r in conn.execute(sql).fetchall():
            key = (
                r[0].strip().lower() if r[0] else "",
                r[1] or "",
            )
            index[key] = {
                "vitrinde": r[2],
                "eslesen":  r[3],
            }
    return index


# ── Ana snapshot fonksiyonu ──────────────────────────────────────────────────

def take_snapshot(engine: Engine, snap_date: date = None) -> dict:
    """
    Tüm aktif turların anlık görüntüsünü alır ve kaydeder.

    Args:
        engine:    SQLAlchemy engine
        snap_date: Snapshot tarihi (varsayılan: bugün). Test için farklı tarih verilebilir.

    Returns:
        {
            "inserted": int,   — yeni eklenen kayıt sayısı
            "skipped":  int,   — zaten mevcut (duplicate) sayısı
            "errors":   int,   — atlanan satır sayısı
            "date":     str,   — snapshot tarihi
        }
    """
    if snap_date is None:
        snap_date = date.today()

    snap_dt = datetime.now()
    status  = {"inserted": 0, "skipped": 0, "errors": 0, "date": str(snap_date)}

    # 1 — Kaynak veriyi çek
    try:
        tours = _fetch_all_tours(engine)
        logger.info("Snapshot: %d tur bulundu", len(tours))
    except Exception as exc:
        logger.error("Snapshot: tur verisi cekilemedi | %s", exc)
        status["errors"] = 1
        return status

    try:
        jolly_index = _fetch_jolly_index(engine)
        logger.info("Snapshot: %d jolly kaydı indekslendi", len(jolly_index))
    except Exception as exc:
        logger.warning("Snapshot: jolly index yüklenemedi, devam ediliyor | %s", exc)
        jolly_index = {}

    # 2 — Her tur için snapshot dict oluştur
    snaps = []
    for t in tours:
        try:
            jt_kodu  = t[0]
            tur_adi  = t[1] or ""
            kalkis   = t[2] or ""
            pax      = _safe_int(t[4])
            satilan  = _safe_int(t[5])
            kalan    = _safe_int(t[6])

            j_key  = (tur_adi.strip().lower(), kalkis)
            j_data = jolly_index.get(j_key, {})

            snaps.append({
                "tour_code":         jt_kodu,
                "snapshot_date":     snap_date,
                "snapshot_datetime": snap_dt,
                "tour_name":         tur_adi,
                "departure_date":    kalkis,
                "airline":           t[3] or "",
                "current_quota":     pax,
                "current_sales":     satilan,
                "current_remaining": kalan,
                "current_price":     t[7] or "",
                "occupancy_rate":    _occupancy_rate(satilan, pax),
                "guide_name":        t[8] or "",
                "jolly_vitrinde":    j_data.get("vitrinde"),
                "jolly_match":       j_data.get("eslesen"),
                "competitor_price":  None,   # gelecekteki scraper için
                "competitor_count":  None,   # gelecekteki scraper için
                "scrape_status":     "ok",
            })
        except Exception as exc:
            logger.warning("Snapshot: satir atlandi | tour=%s | %s", t[0], exc)
            status["errors"] += 1

    # 3 — Toplu kayıt
    if snaps:
        ins, skp = bulk_insert_snapshots(engine, snaps)
        status["inserted"] = ins
        status["skipped"]  = skp

    logger.info(
        "Snapshot tamamlandi | tarih=%s | inserted=%d | skipped=%d | errors=%d",
        snap_date, status["inserted"], status["skipped"], status["errors"],
    )
    return status
