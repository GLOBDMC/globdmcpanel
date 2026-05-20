"""
gordios_scraper.py
------------------
Gordios backoffice'ten tur detaylarını çeken Playwright scraper.
Akış: login → ara → tura tıkla → plan_id al → PDF indir → pdfplumber ile parse et.

PDF yapısı (Gordios ExportTourPlanPdf):
  Sayfa 1-2  : kapak / genel bilgiler
  Sayfa 3+   : günlük program
  Sayfa 5    : uçuş bilgileri (tablo)
"""
import io
import os
import re
import logging
from typing import Optional

logger = logging.getLogger("globdmc.gordios")

# ── Sabitler ────────────────────────────────────────────────────────────────
GORDIOS_LOGIN_URL = "https://identity.globdmc.com/"
GORDIOS_BO_BASE   = "https://backoffice.globdmc.com"
GORDIOS_TOUR_LIST = f"{GORDIOS_BO_BASE}/AbroadTourPlan"
GORDIOS_PDF_BASE  = f"{GORDIOS_BO_BASE}/AbroadTourPlan/ExportTourPlanPdf"

GORDIOS_INSTITUTION = os.getenv("GORDIOS_INSTITUTION", "KYR477")
GORDIOS_USERNAME    = os.getenv("GORDIOS_USERNAME", "")
GORDIOS_PASSWORD    = os.getenv("GORDIOS_PASSWORD", "")


# ── Ana scrape fonksiyonu ────────────────────────────────────────────────────

def scrape_tour_detail(jt_kodu: str) -> dict:
    """
    JT kodu için Gordios'tan uçuş + program bilgileri ve PDF URL döndürür.

    Returns:
        {
          "jt_kodu":        str,
          "plan_id":        int | None,
          "pdf_url":        str | None,
          "ucus_listesi":   list[dict],   # PDF sayfa 5'ten
          "program_gunler": list[dict],   # PDF sayfa 3+'dan
          "program_baslik": str,
          "hata":           str | None,
        }
    """
    result: dict = {
        "jt_kodu":        jt_kodu,
        "plan_id":        None,
        "pdf_url":        None,
        "ucus_listesi":   [],
        "program_gunler": [],
        "program_baslik": "",
        "hata":           None,
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["hata"] = "Gordios senkronizasyonu bu ortamda devre dışı (playwright kurulu değil)"
        return result

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        try:
            # ── 1. LOGIN ────────────────────────────────────────────────────
            logger.info("[gordios] login başlıyor → %s", GORDIOS_LOGIN_URL)
            page.goto(GORDIOS_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            page.fill('input[name="ScopeCode"]', GORDIOS_INSTITUTION)
            page.fill('input[name="Username"]', GORDIOS_USERNAME)
            page.fill('input[name="Password"]', GORDIOS_PASSWORD)
            page.click('input[type="submit"]')
            page.wait_for_load_state("networkidle", timeout=15_000)
            logger.info("[gordios] submit sonrası URL: %s", page.url)

            # ── 2. BACKOFFICE'E GİT ─────────────────────────────────────────
            page.goto(GORDIOS_TOUR_LIST, wait_until="networkidle", timeout=30_000)
            logger.info("[gordios] tour list URL: %s", page.url)

            if GORDIOS_BO_BASE not in page.url:
                page_text = ""
                try:
                    page_text = page.inner_text("body")[:500]
                except Exception:
                    pass
                logger.error("[gordios] backoffice'e ulaşılamadı: URL=%s text=%s",
                             page.url, page_text)
                result["hata"] = f"Backoffice'e erişilemedi: {page.url}"
                return result
            logger.info("[gordios] login + backoffice OK → %s", page.url)

            # ── 3. JT KODUNU GİR ────────────────────────────────────────────
            jt_filled = False
            for inp in page.query_selector_all('input[type="text"], input:not([type])'):
                ph   = (inp.get_attribute("placeholder") or "").lower()
                name = (inp.get_attribute("name") or "").lower()
                id_  = (inp.get_attribute("id") or "").lower()
                tag  = f"{ph} {name} {id_}"
                if any(x in tag for x in ["periyot", "period", "periodcode", "periyotkod"]):
                    inp.fill(jt_kodu)
                    jt_filled = True
                    logger.info("[gordios] Periyot kodu alanı bulundu: name=%s placeholder=%s", name, ph)
                    break

            if not jt_filled:
                labels = page.query_selector_all("label")
                for lbl in labels:
                    if "periyot" in (lbl.inner_text() or "").lower():
                        for_attr = lbl.get_attribute("for")
                        if for_attr:
                            inp = page.query_selector(f'#{for_attr}')
                            if inp:
                                inp.fill(jt_kodu)
                                jt_filled = True
                                logger.info("[gordios] Label ile input bulundu: #%s", for_attr)
                                break
                    if jt_filled:
                        break

            if not jt_filled:
                all_inputs = page.query_selector_all('input[type="text"]')
                if len(all_inputs) >= 2:
                    all_inputs[1].fill(jt_kodu)
                    logger.warning("[gordios] Fallback: ikinci input kullanıldı")
                elif all_inputs:
                    all_inputs[0].fill(jt_kodu)

            # ── 4. LİSTELE ──────────────────────────────────────────────────
            listele_clicked = False
            for sel in [
                'input[value="Listele"]', 'button:has-text("Listele")',
                'input[value="Ara"]',     'button:has-text("Ara")',
                'input[type="submit"]',   'button[type="submit"]',
            ]:
                try:
                    if page.is_visible(sel, timeout=1_000):
                        page.click(sel)
                        listele_clicked = True
                        logger.info("[gordios] listele tıklandı: %s", sel)
                        break
                except Exception:
                    pass
            if not listele_clicked:
                page.keyboard.press("Enter")
                logger.warning("[gordios] listele: Enter ile gönderildi")
            page.wait_for_load_state("networkidle", timeout=15_000)

            # ── 5. SONUÇTAN TUR LİNKİNE TIK ────────────────────────────────
            # BlockUI overlay kaybolana kadar bekle
            try:
                page.wait_for_selector('.blockUI', state='hidden', timeout=15_000)
                logger.info("[gordios] blockUI overlay kalktı")
            except Exception:
                logger.warning("[gordios] blockUI timeout — 2s beklenecek")
                page.wait_for_timeout(2_000)

            link_locator = page.locator(f'a:has-text("{jt_kodu}")')
            if link_locator.count() == 0:
                link_locator = page.locator("table tbody tr td a").first
            if link_locator.count() == 0:
                result["hata"] = f"Sonuç tablosunda tur linki bulunamadı: {jt_kodu}"
                return result

            link_locator.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            logger.info("[gordios] detail URL: %s", page.url)

            # ── 6. PLAN ID ──────────────────────────────────────────────────
            plan_id = _extract_plan_id(page)
            if not plan_id:
                logger.warning("[gordios] plan_id alınamadı, URL=%s", page.url)
                result["hata"] = "plan_id bulunamadı — PDF indirilemez"
                return result

            result["plan_id"] = plan_id
            result["pdf_url"] = f"{GORDIOS_PDF_BASE}?planId={plan_id}"
            logger.info("[gordios] plan_id=%s", plan_id)

            # ── 7. PDF İNDİR VE PARSE ET ────────────────────────────────────
            cookies = ctx.cookies()
            pdf_bytes = _download_pdf_bytes(plan_id, cookies)

            if pdf_bytes:
                logger.info("[gordios] PDF indirildi: %d bytes, parse ediliyor…", len(pdf_bytes))
                parsed = _parse_pdf(pdf_bytes)
                result["ucus_listesi"]   = parsed["ucus_listesi"]
                result["program_gunler"] = parsed["program_gunler"]
                result["program_baslik"] = parsed["program_baslik"]
                logger.info("[gordios] uçuş: %d satır, program: %d gün",
                            len(result["ucus_listesi"]), len(result["program_gunler"]))
            else:
                result["hata"] = "PDF indirilemedi"

        except Exception as exc:
            logger.error("[gordios] scrape hatası [%s]: %s", jt_kodu, exc, exc_info=True)
            result["hata"] = str(exc)
        finally:
            browser.close()

    return result


# ── PDF indirme ──────────────────────────────────────────────────────────────

def _download_pdf_bytes(plan_id: int, cookies: list) -> Optional[bytes]:
    """Playwright session cookie'leriyle PDF'i httpx ile indir."""
    try:
        import httpx
        jar = {c["name"]: c["value"] for c in cookies}
        url = f"{GORDIOS_PDF_BASE}?planId={plan_id}"
        r = httpx.get(url, cookies=jar, follow_redirects=True, timeout=60)
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            if "pdf" in ct or r.content[:4] == b"%PDF":
                logger.info("[gordios] PDF indirildi: %d bytes", len(r.content))
                return r.content
        logger.warning("[gordios] PDF indirilemedi: status=%s ct=%s",
                       r.status_code, r.headers.get("content-type"))
    except Exception as e:
        logger.error("[gordios] PDF indirme hatası: %s", e)
    return None


# Dışarıdan erişilebilir alias (tur_kart_routes.py kullanıyor)
def download_pdf(plan_id: int, cookies: list) -> Optional[bytes]:
    return _download_pdf_bytes(plan_id, cookies)


# ── PDF Parse ────────────────────────────────────────────────────────────────

def _parse_pdf(pdf_bytes: bytes) -> dict:
    """
    pdfplumber ile PDF'i parse eder.
    Sayfa 3+  → günlük program
    Sayfa 5   → uçuş tablosu

    Returns:
        {
          "ucus_listesi":   list[dict],
          "program_gunler": list[dict],  # [{"gun": int, "baslik": str, "icerik": str}, …]
          "program_baslik": str,
        }
    """
    out = {"ucus_listesi": [], "program_gunler": [], "program_baslik": ""}

    try:
        import pdfplumber
    except ImportError:
        logger.error("[gordios] pdfplumber kurulu değil")
        return out

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total = len(pdf.pages)
            logger.info("[gordios] PDF toplam sayfa: %d", total)

            # ── Uçuş tablosu: sayfa 5 (index 4) ────────────────────────────
            # Sayfa 5 yoksa son 2 sayfada da ara
            ucus_pages = []
            if total >= 5:
                ucus_pages.append(pdf.pages[4])   # 5. sayfa
            if total >= 4:
                ucus_pages.append(pdf.pages[3])   # fallback: 4. sayfa
            if total >= 6:
                ucus_pages.append(pdf.pages[5])   # fallback: 6. sayfa

            for p in ucus_pages:
                ucuslar = _extract_flights_from_page(p)
                if ucuslar:
                    out["ucus_listesi"] = ucuslar
                    logger.info("[gordios] uçuş tablosu bulundu sayfa %d", p.page_number)
                    break

            # ── Günlük program: sayfa 3'ten sona kadar ──────────────────────
            if total >= 3:
                program_text = ""
                for p in pdf.pages[2:]:  # sayfa 3 = index 2
                    t = p.extract_text() or ""
                    program_text += t + "\n"

                parsed_program = _extract_program_from_text(program_text)
                out["program_gunler"] = parsed_program["gunler"]
                out["program_baslik"] = parsed_program["baslik"]
                logger.info("[gordios] günlük program: %d gün parse edildi",
                            len(out["program_gunler"]))

    except Exception as e:
        logger.error("[gordios] PDF parse hatası: %s", e, exc_info=True)

    return out


def _extract_flights_from_page(page) -> list:
    """
    pdfplumber page nesnesinden uçuş tablosunu çıkar.
    Önce extract_tables, sonra metin parse fallback.
    """
    ucuslar = []

    # ── Tablo yöntemi ────────────────────────────────────────────────────────
    try:
        tables = page.extract_tables()
        for tbl in tables:
            if not tbl or len(tbl) < 2:
                continue
            header = [str(c or "").lower() for c in tbl[0]]
            # Uçuş tablosunu tanı: "yön", "uçuş", "kalkış", "varış" gibi sütunlar
            if not any(k in " ".join(header) for k in
                       ["uçuş", "ucus", "yön", "yon", "kalkış", "kalkis", "varış", "varis"]):
                continue
            for row in tbl[1:]:
                if not row or not any(row):
                    continue
                vals = [str(c or "").strip() for c in row]
                if not vals[0]:
                    continue
                ucuslar.append({
                    "yon":         vals[0]  if len(vals) > 0 else "",
                    "ucus_no":     vals[1]  if len(vals) > 1 else "",
                    "pnr":         vals[2]  if len(vals) > 2 else "",
                    "havayolu":    vals[3]  if len(vals) > 3 else "",
                    "kalkis_saat": vals[4]  if len(vals) > 4 else "",
                    "kalkis_yeri": vals[5]  if len(vals) > 5 else "",
                    "varis_yeri":  vals[6]  if len(vals) > 6 else "",
                    "varis_saat":  vals[7]  if len(vals) > 7 else "",
                    "aktarimli":   vals[8]  if len(vals) > 8 else "",
                    "ertesi_gun":  vals[9]  if len(vals) > 9 else "",
                })
            if ucuslar:
                return ucuslar
    except Exception as e:
        logger.warning("[gordios] tablo parse hatası: %s", e)

    # ── Metin yöntemi (fallback) ─────────────────────────────────────────────
    try:
        text = page.extract_text() or ""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        # "GİDİŞ" veya "DÖNÜŞ" ile başlayan satırları bul
        for line in lines:
            parts = re.split(r'\s{2,}|\t', line)
            if not parts:
                continue
            yon = parts[0].strip()
            if yon.upper() in ("GİDİŞ", "GİDİS", "DÖNÜŞ", "DONUS", "GİDİŞ/DÖNÜŞ"):
                ucuslar.append({
                    "yon":         yon,
                    "ucus_no":     parts[1]  if len(parts) > 1 else "",
                    "pnr":         parts[2]  if len(parts) > 2 else "",
                    "havayolu":    parts[3]  if len(parts) > 3 else "",
                    "kalkis_saat": parts[4]  if len(parts) > 4 else "",
                    "kalkis_yeri": parts[5]  if len(parts) > 5 else "",
                    "varis_yeri":  parts[6]  if len(parts) > 6 else "",
                    "varis_saat":  parts[7]  if len(parts) > 7 else "",
                    "aktarimli":   "",
                    "ertesi_gun":  "",
                })
    except Exception as e:
        logger.warning("[gordios] metin uçuş parse hatası: %s", e)

    return ucuslar


def _extract_program_from_text(text: str) -> dict:
    """
    PDF metin bloğundan günlük programı parse eder.

    Desteklenen gün başlığı formatları:
      "1. GÜN"  "1.GÜN"  "GÜN 1"  "1. GÜN - LONDRA"  "DAY 1"  vb.

    Returns:
        {"baslik": str, "gunler": [{"gun": int, "baslik": str, "icerik": str}, …]}
    """
    out = {"baslik": "", "gunler": []}

    # Tur başlığını ilk satırdan al (bos olmayan ilk satır)
    lines = text.splitlines()
    for ln in lines:
        ln = ln.strip()
        if ln:
            out["baslik"] = ln
            break

    # Gün başlığı regex — Türkçe ve İngilizce
    GUN_PATTERN = re.compile(
        r'(?:^|\n)\s*'
        r'(?:'
        r'(\d{1,2})\s*[.·]\s*G[ÜU]N'   # "1. GÜN" veya "1.GUN"
        r'|G[ÜU]N\s*(\d{1,2})'          # "GÜN 1"
        r'|DAY\s*(\d{1,2})'             # "DAY 1"
        r')'
        r'(?:\s*[-–:]\s*(.*))?',         # opsiyonel " - BAŞLIK"
        re.IGNORECASE | re.MULTILINE,
    )

    matches = list(GUN_PATTERN.finditer(text))
    if not matches:
        # Hiç gün bulunamadıysa tüm metni tek blok olarak döndür
        icerik = text.strip()
        if icerik:
            out["gunler"].append({"gun": 0, "baslik": "", "icerik": icerik})
        return out

    for i, m in enumerate(matches):
        gun_no = int(m.group(1) or m.group(2) or m.group(3) or 0)
        gun_baslik = (m.group(4) or "").strip()

        # İçerik: bu eşleşmenin sonu ile bir sonraki eşleşmenin başı arası
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        icerik = text[start:end].strip()

        out["gunler"].append({
            "gun":    gun_no,
            "baslik": gun_baslik,
            "icerik": icerik,
        })

    return out


# ── Diğer yardımcılar ────────────────────────────────────────────────────────

def _extract_plan_id(page) -> Optional[int]:
    """URL veya PDF linkinden plan ID'sini çıkar."""
    url = page.url
    for pattern in [r'/Detail/(\d+)', r'[?&]planId=(\d+)', r'[?&]id=(\d+)']:
        m = re.search(pattern, url, re.I)
        if m:
            return int(m.group(1))
    pdf_el = page.query_selector('a[href*="ExportTourPlanPdf"], a:has-text("Tur Planı PDF")')
    if pdf_el:
        href = pdf_el.get_attribute("href") or ""
        m = re.search(r'planId=(\d+)', href, re.I)
        if m:
            return int(m.group(1))
    return None
