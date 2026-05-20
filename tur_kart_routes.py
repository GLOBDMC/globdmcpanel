"""
tur_kart_routes.py
------------------
Tur Kartı sayfası ve Gordios API endpoint'leri.
APIRouter kullanır — main.py'de modül seviyesinde include_router ile eklenir.
"""
import json as _json
import logging
import concurrent.futures as _futures
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

logger = logging.getLogger("globdmc.turkart")

_gordios_executor = _futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="gordios")


def create_tur_kart_router(db_engine, templates) -> APIRouter:
    """
    FastAPI APIRouter döndürür.
    main.py'de: app.include_router(create_tur_kart_router(db_engine, templates))
    """
    router = APIRouter()

    # DB tablosunu garantile
    _ensure_tur_detaylar_table(db_engine)

    # ── Tur Kartı HTML Sayfası ────────────────────────────────────────────────

    @router.get("/turlar/{jt_kodu}")
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

    @router.get("/api/tur/{jt_kodu}")
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

    @router.post("/api/tur/{jt_kodu}/sync")
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

    # ── Gordios Login Debug ───────────────────────────────────────────────────

    @router.get("/api/gordios/debug-login")
    def gordios_debug_login(request: Request):
        """Login'i dener, sonucu ve hata mesajını döndürür."""
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici or kullanici["rol"] != "admin":
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return JSONResponse({"hata": "playwright kurulu değil"})
        try:
            from gordios_scraper import (GORDIOS_LOGIN_URL, GORDIOS_INSTITUTION,
                                         GORDIOS_USERNAME, GORDIOS_PASSWORD,
                                         GORDIOS_BO_BASE)
            import base64
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page()
                page.goto(GORDIOS_LOGIN_URL, wait_until="networkidle", timeout=30_000)
                url_before = page.url

                # Doldur
                page.fill('input[name="ScopeCode"]', GORDIOS_INSTITUTION)
                page.fill('input[name="Username"]', GORDIOS_USERNAME)
                page.fill('input[name="Password"]', GORDIOS_PASSWORD)

                # Submit + navigation bekle
                page.click('input[type="submit"]')
                try:
                    page.wait_for_url(f"{GORDIOS_BO_BASE}/**", timeout=20_000)
                except Exception:
                    page.wait_for_load_state("networkidle", timeout=15_000)

                url_after = page.url
                # Sayfadaki hata mesajını al
                error_text = ""
                for sel in [".error", ".alert", ".validation-summary-errors",
                            '[class*="error"]', '[class*="alert"]', '.text-danger']:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            error_text = el.inner_text()[:300]
                            break
                    except Exception:
                        pass
                # Screenshot
                ss_b64 = base64.b64encode(page.screenshot()).decode()
                # Page text snippet
                page_text = page.inner_text("body")[:500] if page.query_selector("body") else ""
                browser.close()

            return JSONResponse({
                "url_before": url_before,
                "url_after":  url_after,
                "login_ok":   GORDIOS_BO_BASE in url_after,
                "error_text": error_text,
                "page_text":  page_text,
                "screenshot_base64": ss_b64[:500] + "...",
                "env": {
                    "institution": GORDIOS_INSTITUTION,
                    "username":    GORDIOS_USERNAME,
                    "password_len": len(GORDIOS_PASSWORD),
                }
            })
        except Exception as e:
            return JSONResponse({"hata": str(e)}, status_code=500)

    # ── API: Snapshot Geçmişi ─────────────────────────────────────────────────

    @router.get("/api/tur/{jt_kodu}/snapshots")
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

    return router


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
