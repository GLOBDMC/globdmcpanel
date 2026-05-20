"""
tur_kart_routes.py
------------------
Tur Kartı sayfası ve Gordios API endpoint'leri.
main.py'de `from tur_kart_routes import register_tur_kart_routes` ile eklenir.
"""
import json as _json
import logging
import concurrent.futures as _futures
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

logger = logging.getLogger("globdmc.turkart")

_gordios_executor = _futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="gordios")


def register_tur_kart_routes(app, db_engine, templates):

    # ── DB Migration ──────────────────────────────────────────────────────────
    _ensure_tur_detaylar_table(db_engine)

    # ── Tur Kartı HTML Sayfası ────────────────────────────────────────────────

    @app.get("/turlar/{jt_kodu}")
    def tur_karti_sayfasi(request: Request, jt_kodu: str):
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici:
            return RedirectResponse("/login", status_code=302)

        tur = _get_tur(db_engine, jt_kodu)
        if not tur:
            return JSONResponse({"hata": f"Tur bulunamadı: {jt_kodu}"}, status_code=404)

        detay = _get_detay(db_engine, jt_kodu)

        return templates.TemplateResponse(
            request=request,
            name="tur_kart.html",
            context={
                "kullanici":   kullanici,
                "aktif_sayfa": "turlar",
                "tur":         tur,
                "detay":       detay,
            },
        )

    # ── API: Tur Detay JSON ───────────────────────────────────────────────────

    @app.get("/api/tur/{jt_kodu}")
    def api_tur_detay(request: Request, jt_kodu: str):
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici:
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)

        tur = _get_tur(db_engine, jt_kodu)
        if not tur:
            return JSONResponse({"hata": "Tur bulunamadı"}, status_code=404)

        detay = _get_detay(db_engine, jt_kodu)
        return JSONResponse({"ok": True, "tur": tur, "detay": detay or {}})

    # ── API: Gordios Sync ─────────────────────────────────────────────────────

    @app.post("/api/tur/{jt_kodu}/sync")
    async def api_tur_sync(request: Request, jt_kodu: str):
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici or kullanici["rol"] != "admin":
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)

        _upsert_tur_detay(db_engine, jt_kodu, {}, "syncing")

        import asyncio

        async def _do_sync():
            loop = asyncio.get_event_loop()
            try:
                data = await loop.run_in_executor(
                    _gordios_executor, _run_gordios_sync, jt_kodu
                )
                status = "error" if data.get("hata") else "ok"
                _upsert_tur_detay(db_engine, jt_kodu, data, status)
                logger.info("Gordios sync OK [%s]", jt_kodu)
            except Exception as e:
                logger.error("Gordios sync hata [%s]: %s", jt_kodu, e)
                _upsert_tur_detay(db_engine, jt_kodu, {"hata": str(e)}, "error")

        asyncio.create_task(_do_sync())
        return JSONResponse({"ok": True, "mesaj": f"{jt_kodu} sync başlatıldı"})

    # ── API: Snapshot Geçmişi ─────────────────────────────────────────────────

    @app.get("/api/tur/{jt_kodu}/snapshots")
    def api_tur_snapshots(request: Request, jt_kodu: str, limit: int = 60):
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici:
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
        try:
            from snapshot_repository import get_tour_history
            rows = get_tour_history(db_engine, jt_kodu, limit=limit)
            for r in rows:
                for k, v in r.items():
                    if hasattr(v, "isoformat"):
                        r[k] = v.isoformat()
            return JSONResponse({"ok": True, "snapshots": rows})
        except Exception as e:
            logger.error("api_tur_snapshots [%s]: %s", jt_kodu, e)
            return JSONResponse({"hata": str(e)}, status_code=500)


# ── İç yardımcılar ────────────────────────────────────────────────────────────

def _ensure_tur_detaylar_table(db_engine):
    ddl_steps = [
        """CREATE TABLE IF NOT EXISTS tur_detaylar (
            id              SERIAL PRIMARY KEY,
            jt_kodu         VARCHAR(50) UNIQUE NOT NULL,
            plan_id         INTEGER,
            pdf_url         TEXT,
            ucus_json       TEXT DEFAULT '[]',
            program_json    TEXT DEFAULT '[]',
            program_baslik  TEXT DEFAULT '',
            sync_status     VARCHAR(20) DEFAULT 'pending',
            hata_mesaj      TEXT,
            gordios_sync_at TIMESTAMP,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tur_detaylar_jt ON tur_detaylar(jt_kodu)",
    ]
    for i, ddl in enumerate(ddl_steps):
        try:
            with db_engine.connect() as conn:
                conn.execute(text(ddl))
                conn.commit()
        except Exception as e:
            logger.warning("tur_detaylar migration step %d: %s", i, e)


def _get_tur(db_engine, jt_kodu: str):
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT jt_kodu, tur_adi, kalkis_tarihi, bitis_tarihi,
                       havayolu, pax, satilan, kalan, guncel_fiyat, rehber
                FROM turlar WHERE jt_kodu = :jt
            """), {"jt": jt_kodu}).fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("_get_tur [%s]: %s", jt_kodu, e)
        return None


def _get_detay(db_engine, jt_kodu: str):
    try:
        with db_engine.connect() as conn:
            row = conn.execute(text("""
                SELECT plan_id, pdf_url, ucus_json, program_json,
                       program_baslik, sync_status, hata_mesaj, gordios_sync_at
                FROM tur_detaylar WHERE jt_kodu = :jt
            """), {"jt": jt_kodu}).fetchone()
            if not row:
                return None
            d = dict(row._mapping)
            d["ucus_listesi"]   = _json.loads(d.get("ucus_json") or "[]")
            d["program_gunler"] = _json.loads(d.get("program_json") or "[]")
            if d.get("gordios_sync_at") and hasattr(d["gordios_sync_at"], "isoformat"):
                d["gordios_sync_at"] = d["gordios_sync_at"].isoformat()
            return d
    except Exception as e:
        logger.error("_get_detay [%s]: %s", jt_kodu, e)
        return None


def _upsert_tur_detay(db_engine, jt_kodu: str, data: dict, status: str):
    try:
        with db_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO tur_detaylar
                    (jt_kodu, plan_id, pdf_url, ucus_json, program_json,
                     program_baslik, sync_status, hata_mesaj, gordios_sync_at, updated_at)
                VALUES
                    (:jt, :pid, :purl, :ucus, :prog, :pbaslik,
                     :status, :hata, NOW(), NOW())
                ON CONFLICT (jt_kodu) DO UPDATE SET
                    plan_id         = EXCLUDED.plan_id,
                    pdf_url         = EXCLUDED.pdf_url,
                    ucus_json       = EXCLUDED.ucus_json,
                    program_json    = EXCLUDED.program_json,
                    program_baslik  = EXCLUDED.program_baslik,
                    sync_status     = EXCLUDED.sync_status,
                    hata_mesaj      = EXCLUDED.hata_mesaj,
                    gordios_sync_at = EXCLUDED.gordios_sync_at,
                    updated_at      = NOW()
            """), {
                "jt":      jt_kodu,
                "pid":     data.get("plan_id"),
                "purl":    data.get("pdf_url"),
                "ucus":    data.get("ucus_json", "[]"),
                "prog":    data.get("program_json", "[]"),
                "pbaslik": data.get("program_baslik", ""),
                "status":  status,
                "hata":    data.get("hata"),
            })
            conn.commit()
    except Exception as e:
        logger.error("_upsert_tur_detay [%s]: %s", jt_kodu, e)


def _run_gordios_sync(jt_kodu: str) -> dict:
    """Playwright scraper'ı thread'de çalıştırır.
    Gordios AbroadTourPlan → Periyot Kodu alanına JT kodu → Listele → tura tıkla.
    """
    from gordios_scraper import scrape_tour_detail
    raw = scrape_tour_detail(jt_kodu)
    return {
        "plan_id":       raw.get("plan_id"),
        "pdf_url":       raw.get("pdf_url"),
        "ucus_json":     _json.dumps(raw.get("ucus_listesi", []), ensure_ascii=False),
        "program_json":  "[]",
        "program_baslik": "",
        "hata":          raw.get("hata"),
    }
