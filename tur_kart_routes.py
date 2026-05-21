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

    # ── API: Toplu Gordios Sync ───────────────────────────────────────────────

    @router.post("/api/gordios/sync-all")
    async def api_gordios_sync_all(request: Request):
        """Tüm turları arka planda Gordios'tan senkronize eder (admin)."""
        from main import oturum_kullanicisi
        kullanici = oturum_kullanicisi(request)
        if not kullanici or kullanici["rol"] != "admin":
            return JSONResponse({"hata": "Yetkisiz"}, status_code=403)

        try:
            with db_engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT t.jt_kodu FROM turlar t
                    WHERE t.jt_kodu IS NOT NULL AND t.jt_kodu != ''
                    ORDER BY t.kalkis_tarihi
                """)).fetchall()
            jt_kodlari = [r[0] for r in rows]
        except Exception as e:
            return JSONResponse({"hata": str(e)}, status_code=500)

        if not jt_kodlari:
            return JSONResponse({"ok": True, "mesaj": "Sync edilecek tur yok", "toplam": 0})

        import asyncio

        async def _do_sync_all():
            loop = asyncio.get_event_loop()
            basarili = hata = 0
            for jt_kodu in jt_kodlari:
                try:
                    _upsert_tur_detay(db_engine, jt_kodu, {}, "syncing")
                    data = await loop.run_in_executor(
                        _gordios_executor, _run_gordios_sync, jt_kodu
                    )
                    status = "error" if data.get("hata") else "ok"
                    _upsert_tur_detay(db_engine, jt_kodu, data, status)
                    if status == "ok":
                        basarili += 1
                        logger.info("[gordios-all] OK: %s", jt_kodu)
                    else:
                        hata += 1
                        logger.warning("[gordios-all] hata: %s -> %s", jt_kodu, data.get("hata"))
                except Exception as e:
                    hata += 1
                    logger.error("[gordios-all] exception [%s]: %s", jt_kodu, e)
                    _upsert_tur_detay(db_engine, jt_kodu, {"hata": str(e)}, "error")
            logger.info("[gordios-all] tamamlandi: %d basarili, %d hata", basarili, hata)

        asyncio.create_task(_do_sync_all())
        return JSONResponse({
            "ok": True,
            "mesaj": f"{len(jt_kodlari)} tur için sync başlatıldı",
            "toplam": len(jt_kodlari),
        })

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

        # ── Önce: Gordios'tan kaydedilen orijinal PDF ────────────────────────
        raw_pdf = bytes(detay_row[0]) if detay_row[0] is not None else None
        if raw_pdf and raw_pdf[:4] == b"%PDF":
            return Response(
                content=raw_pdf,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="tur-programi-{safe_ad}.pdf"'},
            )

        # ── Fallback: reportlab ile DB verisinden üret ───────────────────────
        program_gunler = _json.loads(detay_row[1] or "[]")
        ucus_listesi   = _json.loads(detay_row[2] or "[]")

        if not program_gunler and not ucus_listesi:
            return JSONResponse({"hata": "Program verisi henüz çekilmemiş"}, status_code=404)

        tur_adi       = tur_row[0] if tur_row else jt_kodu
        kalkis_tarihi = tur_row[1] if tur_row else ""
        havayolu      = tur_row[2] if tur_row else ""

        try:
            pdf_bytes = _build_program_pdf(
                jt_kodu, tur_adi, kalkis_tarihi, havayolu,
                program_gunler, ucus_listesi,
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


def gordios_sync_all_tours(db_engine):
    """
    Tüm aktif turları Gordios'tan senkronize eder.
    Scheduler tarafından günlük çalıştırılır.
    Son 24 saatte sync edilmişleri atlar.
    """
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT t.jt_kodu FROM turlar t
                LEFT JOIN tur_detaylar d ON t.jt_kodu = d.jt_kodu
                WHERE t.jt_kodu IS NOT NULL AND t.jt_kodu != ''
                  AND (d.gordios_sync_at IS NULL
                       OR d.gordios_sync_at < NOW() - INTERVAL '23 hours')
                ORDER BY t.kalkis_tarihi
                LIMIT 50
            """)).fetchall()
        jt_kodlari = [r[0] for r in rows]
        logger.info("[gordios-auto] %d tur sync edilecek", len(jt_kodlari))
        for jt_kodu in jt_kodlari:
            try:
                data = _run_gordios_sync(jt_kodu)
                status = "error" if data.get("hata") else "ok"
                _upsert_tur_detay(db_engine, jt_kodu, data, status)
                logger.info("[gordios-auto] %s → %s", jt_kodu, status)
            except Exception as e:
                logger.error("[gordios-auto] %s hata: %s", jt_kodu, e)
    except Exception as e:
        logger.error("[gordios-auto] genel hata: %s", e)


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

def _build_program_pdf(jt_kodu: str, tur_adi: str, kalkis_tarihi: str,
                       havayolu: str, program_gunler: list, ucus_listesi: list) -> bytes:
    """
    DB'deki program ve uçuş verisinden reportlab ile PDF üretir.
    Gordios auth gerektirmez — panele erişimi olan herkes indirebilir.
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
        fontName="Helvetica-Bold", fontSize=15,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=4,
    )
    s_altyazi = ParagraphStyle(
        "altyazi",
        fontName="Helvetica", fontSize=9,
        textColor=colors.HexColor("#64748b"),
        spaceAfter=2,
    )
    s_bolum = ParagraphStyle(
        "bolum",
        fontName="Helvetica-Bold", fontSize=10,
        textColor=colors.HexColor("#1e40af"),
        spaceBefore=14, spaceAfter=6,
    )
    s_gun_no = ParagraphStyle(
        "gun_no",
        fontName="Helvetica-Bold", fontSize=9,
        textColor=colors.HexColor("#ffffff"),
    )
    s_gun_baslik = ParagraphStyle(
        "gun_baslik",
        fontName="Helvetica-Bold", fontSize=10,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=3,
    )
    s_gun_icerik = ParagraphStyle(
        "gun_icerik",
        fontName="Helvetica", fontSize=9,
        textColor=colors.HexColor("#374151"),
        leading=13, spaceAfter=4,
    )
    s_tablo_baslik = ParagraphStyle(
        "tablo_baslik",
        fontName="Helvetica-Bold", fontSize=8,
        textColor=colors.white,
    )
    s_tablo_hucre = ParagraphStyle(
        "tablo_hucre",
        fontName="Helvetica", fontSize=8,
        textColor=colors.HexColor("#1e293b"),
        leading=11,
    )
    s_footer = ParagraphStyle(
        "footer",
        fontName="Helvetica", fontSize=7,
        textColor=colors.HexColor("#94a3b8"),
        alignment=TA_CENTER,
    )

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
