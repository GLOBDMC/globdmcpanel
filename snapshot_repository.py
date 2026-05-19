"""
snapshot_repository.py
----------------------
tour_snapshots tablosu için veritabanı katmanı.
Append-only tasarım: mevcut kayıtlar hiçbir zaman güncellenmez.
"""
import logging
from datetime import date, datetime
from typing import Optional
from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("globdmc.snapshot")

# ── DDL ─────────────────────────────────────────────────────────────────────

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tour_snapshots (
    id                SERIAL         PRIMARY KEY,
    tour_code         VARCHAR(50)    NOT NULL,
    snapshot_date     DATE           NOT NULL,
    snapshot_datetime TIMESTAMP      NOT NULL,
    tour_name         TEXT,
    departure_date    VARCHAR(50),
    airline           VARCHAR(100),
    current_quota     INTEGER,
    current_sales     INTEGER,
    current_remaining INTEGER,
    current_price     VARCHAR(50),
    occupancy_rate    NUMERIC(5,2),
    guide_name        VARCHAR(200),
    jolly_vitrinde    VARCHAR(10),
    jolly_match       TEXT,
    competitor_price  VARCHAR(50),
    competitor_count  INTEGER,
    scrape_status     VARCHAR(20)    DEFAULT 'ok',
    created_at        TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT tour_snapshots_uniq UNIQUE (tour_code, snapshot_date)
);
"""

_CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_snaps_tour_code     ON tour_snapshots (tour_code);",
    "CREATE INDEX IF NOT EXISTS idx_snaps_date          ON tour_snapshots (snapshot_date);",
    "CREATE INDEX IF NOT EXISTS idx_snaps_departure     ON tour_snapshots (departure_date);",
    "CREATE INDEX IF NOT EXISTS idx_snaps_tour_date     ON tour_snapshots (tour_code, snapshot_date DESC);",
]

_INSERT_SQL = text("""
    INSERT INTO tour_snapshots (
        tour_code, snapshot_date, snapshot_datetime,
        tour_name, departure_date, airline,
        current_quota, current_sales, current_remaining,
        current_price, occupancy_rate, guide_name,
        jolly_vitrinde, jolly_match,
        competitor_price, competitor_count, scrape_status
    ) VALUES (
        :tour_code, :snapshot_date, :snapshot_datetime,
        :tour_name, :departure_date, :airline,
        :current_quota, :current_sales, :current_remaining,
        :current_price, :occupancy_rate, :guide_name,
        :jolly_vitrinde, :jolly_match,
        :competitor_price, :competitor_count, :scrape_status
    )
    ON CONFLICT (tour_code, snapshot_date) DO NOTHING
""")


# ── Public API ───────────────────────────────────────────────────────────────

def create_snapshot_table(engine: Engine) -> None:
    """tour_snapshots tablosunu ve indekslerini oluşturur. Idempotent."""
    with engine.connect() as conn:
        conn.execute(text(_CREATE_TABLE_SQL))
        for idx_sql in _CREATE_INDEXES_SQL:
            conn.execute(text(idx_sql))
        conn.commit()
    logger.info("tour_snapshots tablosu hazir")


def bulk_insert_snapshots(engine: Engine, snaps: list) -> tuple:
    """
    Snapshot listesini toplu kaydeder.
    Aynı (tour_code, snapshot_date) varsa atlar — append-only garanti.
    Returns: (inserted_count, skipped_count)
    """
    inserted = skipped = 0
    with engine.connect() as conn:
        for snap in snaps:
            try:
                result = conn.execute(_INSERT_SQL, snap)
                if result.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning("Snapshot satir kayit hatasi | tour=%s | %s",
                               snap.get("tour_code"), exc)
        conn.commit()
    return inserted, skipped


def get_tour_history(engine: Engine, tour_code: str, limit: int = 90) -> list:
    """
    Bir turun geçmiş snapshot'larını döndürür (en yeni önce).
    Trend analizi ve grafik için kullanılır.
    """
    sql = text("""
        SELECT
            tour_code, snapshot_date, tour_name, departure_date, airline,
            current_quota, current_sales, current_remaining,
            current_price, occupancy_rate, guide_name,
            jolly_vitrinde, scrape_status, created_at
        FROM tour_snapshots
        WHERE tour_code = :tc
        ORDER BY snapshot_date DESC
        LIMIT :lim
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"tc": tour_code, "lim": limit}).fetchall()
    return [dict(r._mapping) for r in rows]


def get_snapshot_summary(engine: Engine, snap_date: date = None) -> dict:
    """
    Belirli bir gün (varsayılan: bugün) için özet istatistik döndürür.
    Dashboard widget'ı ve health check için kullanılır.
    """
    if snap_date is None:
        snap_date = date.today()
    sql = text("""
        SELECT
            COUNT(*)                                            AS total_tours,
            COALESCE(SUM(current_sales), 0)                    AS total_sales,
            COALESCE(SUM(current_quota), 0)                    AS total_quota,
            ROUND(COALESCE(AVG(occupancy_rate), 0), 1)         AS avg_occupancy,
            COUNT(*) FILTER (WHERE scrape_status != 'ok')      AS error_count,
            MIN(created_at)                                     AS first_snap,
            MAX(created_at)                                     AS last_snap
        FROM tour_snapshots
        WHERE snapshot_date = :d
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"d": snap_date}).fetchone()
    return dict(row._mapping) if row else {}


def get_sales_velocity(engine: Engine, tour_code: str, days: int = 7) -> list:
    """
    Bir turun son N günlük günlük satış artışını hesaplar.
    Satış hızı ve tahminleme için kullanılır.
    """
    sql = text("""
        SELECT
            snapshot_date,
            current_sales,
            current_remaining,
            occupancy_rate,
            current_sales - LAG(current_sales) OVER (
                PARTITION BY tour_code ORDER BY snapshot_date
            ) AS daily_sales_delta
        FROM tour_snapshots
        WHERE tour_code = :tc
          AND snapshot_date >= CURRENT_DATE - :d
        ORDER BY snapshot_date DESC
    """)
    with engine.connect() as conn:
        rows = conn.execute(sql, {"tc": tour_code, "d": days}).fetchall()
    return [dict(r._mapping) for r in rows]


def get_snapshot_count(engine: Engine) -> int:
    """Toplam snapshot kayıt sayısı."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM tour_snapshots")).scalar() or 0


def get_weekly_occupancy_change(engine: Engine) -> Optional[dict]:
    """
    Son 1 haftadaki doluluk değişimini hesaplar.
    En son snapshot günü ile 7 gün önceki snapshot gününü karşılaştırır.
    Returns:
        {"current_pct": float, "prev_pct": float, "delta": float, "current_date": date, "prev_date": date}
        ya da None (yeterli veri yoksa)
    """
    sql = text("""
        WITH ranked_dates AS (
            SELECT DISTINCT snapshot_date
            FROM tour_snapshots
            ORDER BY snapshot_date DESC
            LIMIT 14
        ),
        date_pairs AS (
            SELECT
                (SELECT snapshot_date FROM ranked_dates ORDER BY snapshot_date DESC OFFSET 0 LIMIT 1) AS current_date,
                (SELECT snapshot_date FROM ranked_dates ORDER BY snapshot_date DESC OFFSET 6 LIMIT 1) AS prev_date
        ),
        current_snap AS (
            SELECT
                SUM(current_sales)              AS total_sales,
                SUM(current_quota)              AS total_quota
            FROM tour_snapshots t, date_pairs
            WHERE t.snapshot_date = date_pairs.current_date
              AND current_quota > 0
        ),
        prev_snap AS (
            SELECT
                SUM(current_sales)              AS total_sales,
                SUM(current_quota)              AS total_quota
            FROM tour_snapshots t, date_pairs
            WHERE t.snapshot_date = date_pairs.prev_date
              AND current_quota > 0
        )
        SELECT
            date_pairs.current_date,
            date_pairs.prev_date,
            CASE WHEN current_snap.total_quota > 0
                 THEN ROUND(current_snap.total_sales::numeric / current_snap.total_quota * 100, 1)
                 ELSE NULL END AS current_pct,
            CASE WHEN prev_snap.total_quota > 0
                 THEN ROUND(prev_snap.total_sales::numeric / prev_snap.total_quota * 100, 1)
                 ELSE NULL END AS prev_pct
        FROM date_pairs, current_snap, prev_snap
    """)
    try:
        with engine.connect() as conn:
            row = conn.execute(sql).fetchone()
        if not row:
            return None
        r = dict(row._mapping)
        if r.get("current_pct") is None or r.get("prev_pct") is None:
            return None
        # Yeterince farklı tarih yoksa (aynı güne denk geldiyse) None dön
        if r["current_date"] == r["prev_date"]:
            return None
        delta = float(r["current_pct"]) - float(r["prev_pct"])
        return {
            "current_pct":  float(r["current_pct"]),
            "prev_pct":     float(r["prev_pct"]),
            "delta":        round(delta, 1),
            "current_date": r["current_date"],
            "prev_date":    r["prev_date"],
        }
    except Exception as exc:
        logger.warning("get_weekly_occupancy_change hata: %s", exc)
        return None
