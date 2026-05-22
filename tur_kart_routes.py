"""
tur_kart_routes.py
------------------
Tur Kartı sayfası ve Gordios API endpoint'leri.
APIRouter kullanır — main.py'de modül seviyesinde include_router ile eklenir.
"""
import json as _json
import logging
import os
import threading as _threading
import concurrent.futures as _futures
from datetime import datetime as _dt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

logger = logging.getLogger("globdmc.turkart")

_gordios_executor = _futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="gordios")

# ── Sync ilerleme durumu (process-global, thread-safe okuma) ─────────────────
_sync_lock: _threading.Lock = _threading.Lock()
_sync_state: dict = {
    "running":     False,
    "total":       0,
    "done":        0,
    "basarili":    0,
    "errors":      0,
    "current":     "",
    "started_at":  None,
    "finished_at": None,
    "mode":        "",   # "manual" | "auto"
}


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

    # ── API: Toplu Gordios Sync ───────────────────────────────────────────────

    @router.post("/api/gordios/sync-all")
    def api_gordios_sync_all(request: Request):
        """Bekleyen/hatalı turları arka planda Gordios'tan senkronize eder (admin)."""
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici or kullanici["rol"] != "admin":
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)

        # Halihazırda çalışıyor mu?
        with _sync_lock:
            if _sync_state["running"]:
                return JSONResponse({
                    "ok": True, "zaten_calisiyor": True,
                    "mesaj": "Sync zaten devam ediyor",
                    "toplam": _sync_state["total"],
                })

        # Bekleyen + hatalı + hiç sync edilmemiş turlar
        try:
            with db_engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT t.jt_kodu FROM turlar t
                    LEFT JOIN tur_detaylar d ON t.jt_kodu = d.jt_kodu
                    WHERE t.jt_kodu IS NOT NULL AND t.jt_kodu != ''
                      AND (d.jt_kodu IS NULL
                           OR d.sync_status IN ('pending', 'error')
                           OR d.gordios_sync_at < NOW() - INTERVAL '7 days')
                    ORDER BY t.kalkis_tarihi
                """)).fetchall()
            jt_kodlari = [r[0] for r in rows]
        except Exception as e:
            return JSONResponse({"hata": str(e)}, status_code=500)

        if not jt_kodlari:
            return JSONResponse({"ok": True, "mesaj": "Sync edilecek tur yok", "toplam": 0})

        # Thread'de başlat — ana event loop'u bloklamaz
        _gordios_executor.submit(_gordios_sync_run, jt_kodlari, db_engine, "manual")

        return JSONResponse({
            "ok": True, "zaten_calisiyor": False,
            "mesaj": f"{len(jt_kodlari)} tur için sync başlatıldı",
            "toplam": len(jt_kodlari),
        })

    # ── API: Sync İlerlemesi ──────────────────────────────────────────────────

    @router.get("/api/gordios/sync-progress")
    def api_gordios_sync_progress(request: Request):
        """Anlık sync ilerleme durumunu döndürür. UI tarafından her 3s poll edilir."""
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici:
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
        with _sync_lock:
            state = dict(_sync_state)
        pct = int(state["done"] / state["total"] * 100) if state["total"] > 0 else 0
        return JSONResponse({**state, "pct": pct})

    # ── Gordios Sync Status ───────────────────────────────────────────────────

    @router.get("/api/gordios/sync-status")
    def gordios_sync_status(request: Request):
        """Tüm turların Gordios sync durumunu döndürür."""
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici or kullanici["rol"] != "admin":
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
        try:
            with db_engine.connect() as conn:
                # Özet istatistikler
                ozet = conn.execute(text("""
                    SELECT sync_status, COUNT(*) as adet
                    FROM tur_detaylar
                    GROUP BY sync_status
                """)).fetchall()

                # Hatalı kayıtlar
                hatali = conn.execute(text("""
                    SELECT td.jt_kodu, t.tur_adi, td.hata_mesaj, td.gordios_sync_at
                    FROM tur_detaylar td
                    LEFT JOIN turlar t ON t.jt_kodu = td.jt_kodu
                    WHERE td.sync_status = 'error'
                    ORDER BY td.gordios_sync_at DESC NULLS LAST
                    LIMIT 30
                """)).fetchall()

                # Bekleyen / hiç çekilmemiş turlar
                bekleyen = conn.execute(text("""
                    SELECT t.jt_kodu, t.tur_adi, t.kalkis_tarihi
                    FROM turlar t
                    LEFT JOIN tur_detaylar td ON td.jt_kodu = t.jt_kodu
                    WHERE t.jt_kodu IS NOT NULL AND t.jt_kodu != ''
                      AND (td.jt_kodu IS NULL OR td.sync_status IN ('pending','error'))
                    ORDER BY t.kalkis_tarihi DESC NULLS LAST
                    LIMIT 20
                """)).fetchall()

                # Başarılı son 5
                basarili = conn.execute(text("""
                    SELECT td.jt_kodu, t.tur_adi, td.gordios_sync_at,
                           td.program_baslik,
                           (SELECT COUNT(*) FROM json_array_elements(td.program_json::json)) AS gun_sayisi
                    FROM tur_detaylar td
                    LEFT JOIN turlar t ON t.jt_kodu = td.jt_kodu
                    WHERE td.sync_status = 'ok'
                    ORDER BY td.gordios_sync_at DESC NULLS LAST
                    LIMIT 5
                """)).fetchall()

            def _d(r): return {k: (v.isoformat() if hasattr(v,'isoformat') else v) for k,v in r._mapping.items()}

            return JSONResponse({
                "ok": True,
                "ozet":     [_d(r) for r in ozet],
                "hatali":   [_d(r) for r in hatali],
                "bekleyen": [_d(r) for r in bekleyen],
                "basarili": [_d(r) for r in basarili],
                "env": {
                    "playwright": _check_playwright(),
                    "gordios_user": bool(os.getenv("GORDIOS_USERNAME")),
                    "gordios_pass": bool(os.getenv("GORDIOS_PASSWORD")),
                    "gordios_inst": os.getenv("GORDIOS_INSTITUTION",""),
                },
            })
        except Exception as e:
            return JSONResponse({"hata": str(e)}, status_code=500)

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

                # Backoffice'e git
                from gordios_scraper import GORDIOS_TOUR_LIST
                page.goto(GORDIOS_TOUR_LIST, wait_until="networkidle", timeout=30_000)
                url_bo = page.url

                # Tüm input ve button'ları topla
                inputs_bo, buttons_bo = [], []
                for inp in page.query_selector_all("input"):
                    inputs_bo.append({
                        "type": inp.get_attribute("type"),
                        "name": inp.get_attribute("name"),
                        "id":   inp.get_attribute("id"),
                        "value": inp.get_attribute("value"),
                        "visible": inp.is_visible(),
                    })
                for btn in page.query_selector_all("button, input[type=submit], input[type=button]"):
                    try:
                        buttons_bo.append({
                            "tag":  btn.evaluate("el => el.tagName"),
                            "type": btn.get_attribute("type"),
                            "text": (btn.inner_text() or btn.get_attribute("value") or "")[:60],
                            "visible": btn.is_visible(),
                        })
                    except Exception:
                        pass
                page_text = ""
                try:
                    page_text = page.inner_text("body")[:300]
                except Exception:
                    pass
                browser.close()

            return JSONResponse({
                "login_url_after": url_after,
                "login_ok": GORDIOS_BO_BASE in url_after or GORDIOS_BO_BASE in url_bo,
                "backoffice_url": url_bo,
                "backoffice_inputs":  inputs_bo,
                "backoffice_buttons": buttons_bo,
                "page_text": page_text,
                "env": {
                    "institution": GORDIOS_INSTITUTION,
                    "username":    GORDIOS_USERNAME,
                    "password_len": len(GORDIOS_PASSWORD),
                }
            })
        except Exception as e:
            return JSONResponse({"hata": str(e)}, status_code=500)

    # ── API: Program PDF Dışa Aktar ───────────────────────────────────────────

    @router.get("/api/tur/{jt_kodu}/export-pdf")
    def api_tur_export_pdf(request: Request, jt_kodu: str):
        """
        DB'deki program ve uçuş verisinden PDF üretir.
        Gordios auth gerektirmez — panele erişimi olan herkes indirebilir.
        """
        from main import oturum_kullanicisi
        from fastapi.responses import Response
        kullanici = oturum_kullanicisi(request)
        if not kullanici:
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)

        # Tur temel bilgileri + ham PDF bytes
        try:
            with db_engine.connect() as conn:
                tur_row = conn.execute(text(
                    "SELECT tur_adi, kalkis_tarihi, havayolu FROM turlar WHERE jt_kodu = :jt"
                ), {"jt": jt_kodu}).fetchone()
                detay_row = conn.execute(text(
                    "SELECT pdf_data, program_json, ucus_json FROM tur_detaylar WHERE jt_kodu = :jt"
                ), {"jt": jt_kodu}).fetchone()
        except Exception as e:
            return JSONResponse({"hata": str(e)}, status_code=500)

        if not detay_row:
            return JSONResponse({"hata": "Tur programı bulunamadı — önce Gordios sync yapın"}, status_code=404)

        safe_ad = "".join(c if c.isalnum() or c in "-_" else "_" for c in jt_kodu)

        raw_pdf        = bytes(detay_row[0]) if detay_row[0] is not None else None
        program_gunler = _json.loads(detay_row[1] or "[]")
        ucus_listesi   = _json.loads(detay_row[2] or "[]")

        if not program_gunler and not ucus_listesi:
            return JSONResponse({"hata": "Program verisi henüz çekilmemiş"}, status_code=404)

        tur_adi       = tur_row[0] if tur_row else jt_kodu
        kalkis_tarihi = tur_row[1] if tur_row else ""
        havayolu      = tur_row[2] if tur_row else ""

        # ── Gordios ham PDF'den ek bölümleri parse et ───────────────────────
        dahil_hizmetler: list = []
        haric_hizmetler: list = []
        notlar: str = ""

        if raw_pdf and raw_pdf[:4] == b"%PDF":
            try:
                from gordios_scraper import _parse_pdf_extra
                extra = _parse_pdf_extra(raw_pdf)
                dahil_hizmetler = extra.get("dahil_hizmetler", [])
                haric_hizmetler = extra.get("haric_hizmetler", [])
                notlar          = extra.get("notlar", "")
            except Exception as _e:
                logger.warning("export-pdf extra parse [%s]: %s", jt_kodu, _e)

        try:
            pdf_bytes = _build_program_pdf(
                jt_kodu, tur_adi, kalkis_tarihi, havayolu,
                program_gunler, ucus_listesi,
                dahil_hizmetler, haric_hizmetler, notlar,
            )
        except Exception as e:
            logger.error("export-pdf [%s]: %s", jt_kodu, e, exc_info=True)
            return JSONResponse({"hata": f"PDF oluşturulamadı: {e}"}, status_code=500)

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="tur-programi-{safe_ad}.pdf"'},
        )

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
        # ham PDF bytes — sync sırasında Gordios'tan çekilir, sonra auth gerektirmeden sunulur
        "ALTER TABLE tur_detaylar ADD COLUMN IF NOT EXISTS pdf_data BYTEA",
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
        pdf_data = data.get("pdf_bytes")   # bytes | None — scraper'dan gelen ham PDF
        with db_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO tur_detaylar
                    (jt_kodu, plan_id, pdf_url, pdf_data, ucus_json, program_json,
                     program_baslik, sync_status, hata_mesaj, gordios_sync_at, updated_at)
                VALUES
                    (:jt, :pid, :purl, :pdata, :ucus, :prog, :pbaslik,
                     :status, :hata, NOW(), NOW())
                ON CONFLICT (jt_kodu) DO UPDATE SET
                    plan_id         = EXCLUDED.plan_id,
                    pdf_url         = EXCLUDED.pdf_url,
                    pdf_data        = COALESCE(EXCLUDED.pdf_data, tur_detaylar.pdf_data),
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
                "pdata":   pdf_data,
                "ucus":    data.get("ucus_json", "[]"),
                "prog":    data.get("program_json", "[]"),
                "pbaslik": data.get("program_baslik", ""),
                "status":  status,
                "hata":    data.get("hata"),
            })
            conn.commit()
    except Exception as e:
        logger.error("_upsert_tur_detay [%s]: %s", jt_kodu, e)


def _gordios_sync_run(jt_kodlari: list, db_engine, mode: str = "auto") -> None:
    """
    Verilen JT kodları listesini sırayla sync eder.
    İlerlemeyi _sync_state'e yazar — API ve UI tarafından okunabilir.
    Thread-safe, yeniden girişe karşı korumalı.
    """
    global _sync_state
    with _sync_lock:
        if _sync_state["running"]:
            logger.warning("[gordios-sync] zaten çalışıyor, atlanıyor")
            return
        _sync_state.update({
            "running": True, "total": len(jt_kodlari),
            "done": 0, "basarili": 0, "errors": 0,
            "current": "", "started_at": _dt.now().isoformat(),
            "finished_at": None, "mode": mode,
        })

    logger.info("[gordios-%s] başlıyor: %d tur", mode, len(jt_kodlari))
    try:
        for jt_kodu in jt_kodlari:
            with _sync_lock:
                _sync_state["current"] = jt_kodu
            try:
                _upsert_tur_detay(db_engine, jt_kodu, {}, "syncing")
                data = _run_gordios_sync(jt_kodu)
                status = "error" if data.get("hata") else "ok"
                _upsert_tur_detay(db_engine, jt_kodu, data, status)
                with _sync_lock:
                    _sync_state["done"] += 1
                    if status == "ok":
                        _sync_state["basarili"] += 1
                        logger.info("[gordios-%s] OK: %s", mode, jt_kodu)
                    else:
                        _sync_state["errors"] += 1
                        logger.warning("[gordios-%s] hata: %s → %s", mode, jt_kodu, data.get("hata"))
            except Exception as e:
                with _sync_lock:
                    _sync_state["done"]   += 1
                    _sync_state["errors"] += 1
                logger.error("[gordios-%s] exception [%s]: %s", mode, jt_kodu, e)
                _upsert_tur_detay(db_engine, jt_kodu, {"hata": str(e)}, "error")
    finally:
        with _sync_lock:
            _sync_state["running"]     = False
            _sync_state["current"]     = ""
            _sync_state["finished_at"] = _dt.now().isoformat()
        logger.info("[gordios-%s] tamamlandı: %d/%d başarılı, %d hata",
                    mode, _sync_state["basarili"], _sync_state["total"], _sync_state["errors"])


def gordios_sync_all_tours(db_engine):
    """
    Scheduler tarafından günlük çağrılır (03:00 TRT).
    Sadece 11+ gün sonraki turları sync eder:
      - Tarihi geçmiş turlar → Gordios'ta değişmez, gereksiz
      - Önümüzdeki 10 gün içindeki turlar → artık son halinde, dokunma
    """
    global _sync_state
    with _sync_lock:
        if _sync_state["running"]:
            logger.info("[gordios-auto] manuel sync devam ediyor, otomatik atlandi")
            return
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT t.jt_kodu FROM turlar t
                LEFT JOIN tur_detaylar d ON t.jt_kodu = d.jt_kodu
                WHERE t.jt_kodu IS NOT NULL AND t.jt_kodu != ''
                  AND t.kalkis_tarihi > CURRENT_DATE + INTERVAL '10 days'
                  AND (d.gordios_sync_at IS NULL
                       OR d.gordios_sync_at < NOW() - INTERVAL '23 hours')
                ORDER BY t.kalkis_tarihi
            """)).fetchall()
        jt_kodlari = [r[0] for r in rows]
    except Exception as e:
        logger.error("[gordios-auto] tur listesi alinamadi: %s", e)
        return

    if not jt_kodlari:
        logger.info("[gordios-auto] sync edilecek tur yok (tarih filtresi uygulandı)")
        return

    logger.info("[gordios-auto] %d tur sync edilecek (10+ gün sonrası)", len(jt_kodlari))
    _gordios_sync_run(jt_kodlari, db_engine, mode="auto")


def _check_playwright() -> str:
    try:
        from playwright.sync_api import sync_playwright  # noqa
        return "ok"
    except ImportError:
        return "kurulu_degil"
    except Exception as e:
        return str(e)[:80]


def _run_gordios_sync(jt_kodu: str) -> dict:
    """Playwright scraper'ı thread'de çalıştırır.
    Gordios AbroadTourPlan → Periyot Kodu alanına JT kodu → Listele → tura tıkla → PDF parse.
    """
    from gordios_scraper import scrape_tour_detail
    raw = scrape_tour_detail(jt_kodu)
    return {
        "plan_id":        raw.get("plan_id"),
        "pdf_url":        raw.get("pdf_url"),
        "pdf_bytes":      raw.get("pdf_bytes"),   # ham bytes — DB'ye BYTEA olarak kaydedilir
        "ucus_json":      _json.dumps(raw.get("ucus_listesi", []), ensure_ascii=False),
        "program_json":   _json.dumps(raw.get("program_gunler", []), ensure_ascii=False),
        "program_baslik": raw.get("program_baslik", ""),
        "hata":           raw.get("hata"),
    }


# ── PDF Üretimi ──────────────────────────────────────────────────────────────

# Modül seviyesinde bir kez hesaplanır
_PDF_FONT_REGULAR: str | None = None
_PDF_FONT_BOLD:    str | None = None


def _get_unicode_fonts() -> tuple[str, str]:
    """
    Türkçe karakter destekli (ş ı ğ ü ö ç) TTF fontunu ReportLab'e kaydeder.
    Döner: (regular_font_name, bold_font_name)

    Öncelik sırası:
      1. Liberation Sans  — Dockerfile'da fonts-liberation paketiyle kurulu (Linux/Railway)
      2. DejaVu Sans      — python:slim imajlarda bazen mevcut
      3. Arial            — Windows geliştirme ortamı
      4. Helvetica        — yedek (Türkçe desteği yok ama çökmez)
    """
    global _PDF_FONT_REGULAR, _PDF_FONT_BOLD
    if _PDF_FONT_REGULAR is not None:
        return _PDF_FONT_REGULAR, _PDF_FONT_BOLD

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        # (regular_path, bold_path, regular_name, bold_name)
        (
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "LiberationSans", "LiberationSans-Bold",
        ),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans", "DejaVuSans-Bold",
        ),
        (
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "Arial", "Arial-Bold",
        ),
    ]

    for reg_path, bold_path, reg_name, bold_name in candidates:
        if os.path.exists(reg_path) and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont(reg_name, reg_path))
                pdfmetrics.registerFont(TTFont(bold_name, bold_path))
                logger.info("PDF Türkçe font: %s / %s", reg_name, bold_name)
                _PDF_FONT_REGULAR = reg_name
                _PDF_FONT_BOLD    = bold_name
                return reg_name, bold_name
            except Exception as exc:
                logger.warning("Font kayıt hatası (%s): %s", reg_name, exc)

    # Hiç TTF bulunamadı — Helvetica (Türkçe kutuları gösterir ama çökmez)
    logger.warning("PDF Türkçe font bulunamadı, Helvetica kullanılıyor")
    _PDF_FONT_REGULAR = "Helvetica"
    _PDF_FONT_BOLD    = "Helvetica-Bold"
    return "Helvetica", "Helvetica-Bold"


def _build_program_pdf(
    jt_kodu: str,
    tur_adi: str,
    kalkis_tarihi: str,
    havayolu: str,
    program_gunler: list,
    ucus_listesi: list,
    dahil_hizmetler: list = None,
    haric_hizmetler: list = None,
    notlar: str = "",
) -> bytes:
    """
    DB'deki program + uçuş + hizmet/notlar verisinden reportlab ile PDF üretir.
    Gordios auth gerektirmez — panele erişimi olan herkes indirebilir.
    Dahil/hariç hizmetler ve notlar Gordios ham PDF'inden parse edilir;
    yolcu listesi ve konaklama dahil edilmez.
    """
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether,
    )
    from reportlab.lib.enums import TA_LEFT, TA_CENTER

    F  = _get_unicode_fonts()[0]   # regular
    FB = _get_unicode_fonts()[1]   # bold

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title=f"Tur Programı — {jt_kodu}",
    )

    W = A4[0] - 4*cm  # kullanılabilir genişlik

    # ── Stiller ──────────────────────────────────────────────────────────────
    base = getSampleStyleSheet()

    s_baslik = ParagraphStyle(
        "baslik",
        fontName=FB, fontSize=15,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=4,
    )
    s_altyazi = ParagraphStyle(
        "altyazi",
        fontName=F, fontSize=9,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=2,
    )
    s_bolum = ParagraphStyle(
        "bolum",
        fontName=FB, fontSize=10,
        textColor=colors.HexColor("#1e40af"),
        spaceBefore=14, spaceAfter=6,
    )
    s_gun_no = ParagraphStyle(
        "gun_no",
        fontName=FB, fontSize=9,
        textColor=colors.HexColor("#ffffff"),
    )
    s_gun_baslik = ParagraphStyle(
        "gun_baslik",
        fontName=FB, fontSize=10,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=3,
    )
    s_gun_icerik = ParagraphStyle(
        "gun_icerik",
        fontName=F, fontSize=9,
        textColor=colors.HexColor("#374151"),
        leading=13, spaceAfter=4,
    )
    s_tablo_baslik = ParagraphStyle(
        "tablo_baslik",
        fontName=FB, fontSize=8,
        textColor=colors.white,
    )
    s_tablo_hucre = ParagraphStyle(
        "tablo_hucre",
        fontName=F, fontSize=8,
        textColor=colors.HexColor("#1e293b"),
        leading=11,
    )
    s_footer = ParagraphStyle(
        "footer",
        fontName=F, fontSize=7,
        textColor=colors.HexColor("#94a3b8"),
        alignment=TA_CENTER,
    )
    s_hizmet_baslik = ParagraphStyle(
        "hizmet_baslik",
        fontName=FB, fontSize=9,
        textColor=colors.HexColor("#ffffff"),
        spaceAfter=4,
    )
    s_hizmet = ParagraphStyle(
        "hizmet",
        fontName=F, fontSize=8,
        textColor=colors.HexColor("#1e293b"),
        leading=12, spaceAfter=2,
    )
    s_not = ParagraphStyle(
        "not",
        fontName=F, fontSize=8,
        textColor=colors.HexColor("#374151"),
        leading=12, spaceAfter=3,
    )

    # Eksik parametreleri normalize et
    dahil_hizmetler = dahil_hizmetler or []
    haric_hizmetler = haric_hizmetler or []
    notlar          = notlar or ""

    story = []

    # ── Başlık bloğu ─────────────────────────────────────────────────────────
    story.append(Paragraph(tur_adi or jt_kodu, s_baslik))
    meta_parts = [f"Kod: {jt_kodu}"]
    if kalkis_tarihi: meta_parts.append(f"Kalkış: {kalkis_tarihi}")
    if havayolu:      meta_parts.append(f"Havayolu: {havayolu}")
    story.append(Paragraph("  ·  ".join(meta_parts), s_altyazi))
    story.append(HRFlowable(width=W, thickness=1, color=colors.HexColor("#e2e8f0"),
                             spaceAfter=10))

    # ── Uçuş Bilgileri ───────────────────────────────────────────────────────
    if ucus_listesi:
        story.append(Paragraph("UÇUŞ BİLGİLERİ", s_bolum))

        tbl_data = [[
            Paragraph("Yön", s_tablo_baslik),
            Paragraph("Uçuş No", s_tablo_baslik),
            Paragraph("Kalkış", s_tablo_baslik),
            Paragraph("Varış", s_tablo_baslik),
            Paragraph("Havayolu", s_tablo_baslik),
        ]]
        for u in ucus_listesi:
            tbl_data.append([
                Paragraph(u.get("yon", ""), s_tablo_hucre),
                Paragraph(u.get("ucus_no", ""), s_tablo_hucre),
                Paragraph(
                    f"{u.get('kalkis_saat','')}\n{u.get('kalkis_yeri','')}", s_tablo_hucre
                ),
                Paragraph(
                    f"{u.get('varis_saat','')}\n{u.get('varis_yeri','')}", s_tablo_hucre
                ),
                Paragraph(u.get("havayolu", ""), s_tablo_hucre),
            ])

        col_w = [W*0.12, W*0.13, W*0.28, W*0.28, W*0.19]
        tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#1e40af")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#f8fafc"), colors.white]),
            ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#cbd5e1")),
            ("VALIGN",      (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING",  (0,0), (-1,-1), 5),
            ("RIGHTPADDING", (0,0), (-1,-1), 5),
            ("TOPPADDING",   (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ]))
        story.append(tbl)

    # ── Günlük Program ────────────────────────────────────────────────────────
    if program_gunler:
        story.append(Paragraph("GÜNLÜK PROGRAM", s_bolum))

        for gun in program_gunler:
            gun_no   = str(gun.get("gun", ""))
            baslik   = gun.get("baslik", "")
            icerik   = gun.get("icerik", "").replace("\n", "<br/>")

            # Gün numarası baloncuğu + başlık yan yana tablo olarak
            badge = Table(
                [[Paragraph(gun_no, s_gun_no)]],
                colWidths=[0.65*cm],
                rowHeights=[0.65*cm],
            )
            badge.setStyle(TableStyle([
                ("BACKGROUND",   (0,0), (0,0), colors.HexColor("#1e40af")),
                ("ALIGN",        (0,0), (0,0), "CENTER"),
                ("VALIGN",       (0,0), (0,0), "MIDDLE"),
                ("ROUNDEDCORNERS", [3]),
                ("LEFTPADDING",  (0,0), (0,0), 2),
                ("RIGHTPADDING", (0,0), (0,0), 2),
            ]))

            header_row = Table(
                [[badge, Paragraph(baslik, s_gun_baslik)]],
                colWidths=[0.9*cm, W - 0.9*cm],
            )
            header_row.setStyle(TableStyle([
                ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
                ("LEFTPADDING",  (0,0), (-1,-1), 0),
                ("RIGHTPADDING", (0,0), (-1,-1), 0),
                ("TOPPADDING",   (0,0), (-1,-1), 0),
                ("BOTTOMPADDING",(0,0), (-1,-1), 4),
            ]))

            block = [header_row]
            if icerik:
                block.append(Paragraph(icerik, s_gun_icerik))
            block.append(HRFlowable(width=W, thickness=0.4,
                                    color=colors.HexColor("#e2e8f0"), spaceAfter=6))

            story.append(KeepTogether(block))

    # ── Dahil / Hariç Hizmetler ───────────────────────────────────────────────
    if dahil_hizmetler or haric_hizmetler:
        def _hizmet_sutun(baslik_text, items, bg):
            """Tek sütun için iç içe tablo döndürür."""
            col_rows = [[Paragraph(baslik_text, s_hizmet_baslik)]]
            for item in items:
                col_rows.append([Paragraph(f"• {item}", s_hizmet)])
            inner = Table(col_rows, colWidths=[W * 0.47])
            inner.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor(bg)),
                ("LEFTPADDING",   (0, 0), (-1, -1), 7),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 7),
                ("TOPPADDING",    (0, 0), (-1,  0), 5),
                ("BOTTOMPADDING", (0, 0), (-1,  0), 5),
                ("TOPPADDING",    (0, 1), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ]))
            return inner

        sol = _hizmet_sutun("DAHİL OLAN HİZMETLER",    dahil_hizmetler, "#166534")
        sag = _hizmet_sutun("DAHİL OLMAYAN HİZMETLER", haric_hizmetler, "#991b1b")

        hizmet_tbl = Table(
            [[sol, Spacer(W * 0.06, 1), sag]],
            colWidths=[W * 0.47, W * 0.06, W * 0.47],
        )
        hizmet_tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(hizmet_tbl)
        story.append(Spacer(1, 0.3 * cm))

    # ── Önemli Notlar / Katılım Koşulları ────────────────────────────────────
    if notlar and notlar.strip():
        story.append(Paragraph("ÖNEMLİ NOTLAR", s_bolum))
        story.append(HRFlowable(width=W, thickness=0.4, color=colors.HexColor("#fbbf24"),
                                 spaceAfter=6))
        for para in notlar.split("\n\n"):
            para = para.strip()
            if not para:
                continue
            # Çok satırlı paragrafı <br/> ile birleştir
            para_html = para.replace("\n", "<br/>")
            story.append(Paragraph(para_html, s_not))
        story.append(Spacer(1, 0.2 * cm))

    # ── Footer ────────────────────────────────────────────────────────────────
    from datetime import datetime as _dt
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width=W, thickness=0.5, color=colors.HexColor("#e2e8f0"),
                             spaceAfter=4))
    story.append(Paragraph(
        f"Glob DMC Panel · {jt_kodu} · {_dt.now().strftime('%d.%m.%Y')} tarihinde oluşturuldu",
        s_footer,
    ))

    doc.build(story)
    return buf.getvalue()
