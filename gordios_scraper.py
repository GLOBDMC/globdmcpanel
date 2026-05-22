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
        "pdf_bytes":      None,   # ham PDF — DB'de saklanır, Gordios auth gerektirmez
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
            # blockUI: önce 500ms bekle (overlay henüz gelmemiş olabilir),
            # sonra görünmesini ve kaybolmasını sıraylı bekle.
            page.wait_for_timeout(500)
            try:
                page.wait_for_selector('.blockUI', state='attached', timeout=4_000)
                page.wait_for_selector('.blockUI', state='hidden',   timeout=20_000)
                logger.info("[gordios] blockUI overlay kalktı")
            except Exception:
                # blockUI hiç gelmedi veya zaten kalktı — ekstra 1.5s ver
                page.wait_for_timeout(1_500)

            # Önce JT kodunu içeren linki DOM'a gelene kadar bekle
            link_locator = None
            try:
                page.wait_for_selector(f'a:has-text("{jt_kodu}")', timeout=12_000)
                link_locator = page.locator(f'a:has-text("{jt_kodu}")')
                logger.info("[gordios] JT linki bulundu (has-text)")
            except Exception:
                # Fallback: tablo satırındaki herhangi bir link bekle
                try:
                    page.wait_for_selector('table tbody tr td a', timeout=8_000)
                    all_links = page.locator('table tbody tr td a')
                    if all_links.count() > 0:
                        link_locator = all_links.first
                        logger.info("[gordios] Fallback: tablo linki kullanılıyor")
                except Exception:
                    pass

            if link_locator is None:
                # Gerçekten sonuç yok — page text logla
                try:
                    page_text = page.inner_text("body")[:300]
                except Exception:
                    page_text = "(okunamadı)"
                logger.warning("[gordios] sonuç bulunamadı [%s] page=%s", jt_kodu, page_text[:150])
                result["hata"] = f"Gordios'ta tur bulunamadı: {jt_kodu}"
                return result

            link_locator.click()
            page.wait_for_load_state("networkidle", timeout=20_000)
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
                result["pdf_bytes"] = pdf_bytes   # ham bytes — DB'ye kaydedilecek
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

    PDF yapısı (Gordios ExportTourPlanPdf):
      Sayfa 1    : Glob DMC başlık + dahil/hariç hizmetler
      Sayfa 2    : Katılım koşulları / notlar
      Sayfa 3-N  : Günlük program  (her gün "X . GÜN\\n<lokasyon>\\n<açıklama>")
      Sayfa N+1  : Uçak Bilgileri tablosu  ("Yön / Uçuş Num." başlıklı tablo)
      Sayfa N+2  : Konaklama Bilgileri
      Sayfa N+3+ : Yolcu listesi

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

            # ── Tur başlığı: sayfa 1'den al ─────────────────────────────────
            if total >= 1:
                out["program_baslik"] = _extract_tour_title(
                    pdf.pages[0].extract_text() or ""
                )

            # ── Sayfaları tara: program / uçuş bölümlerini ayırt et ─────────
            program_text = ""
            ucus_page    = None

            UCUS_KEYWORDS  = ["uçak bilgi", "ucak bilgi", "yön\t", "yön ", "kalkış saati",
                               "kalkis saati", "uçuş num", "ucus num"]
            STOP_KEYWORDS  = ["konaklama bilgi", "yolcu liste", "konaklama\n"]

            for i in range(2, total):          # sayfa 3 = index 2
                page = pdf.pages[i]
                raw  = page.extract_text() or ""
                preview = raw[:400].lower()

                if any(k in preview for k in UCUS_KEYWORDS):
                    ucus_page = page
                    logger.info("[gordios] uçuş sayfası: %d", i + 1)
                    break

                if any(k in preview for k in STOP_KEYWORDS):
                    logger.info("[gordios] program sonu sayfada: %d", i + 1)
                    break

                program_text += raw + "\n"

            # Fallback: uçuş sayfası bulunamadıysa sayfa 5'i dene
            if not ucus_page:
                for idx in [4, 3, 5]:          # 5. sayfa = index 4
                    if total > idx:
                        candidate = pdf.pages[idx]
                        if _extract_flights_from_page(candidate):
                            ucus_page = candidate
                            logger.info("[gordios] uçuş fallback sayfa %d", idx + 1)
                            break

            # ── Uçuş tablosunu parse et ──────────────────────────────────────
            if ucus_page:
                out["ucus_listesi"] = _extract_flights_from_page(ucus_page)
                logger.info("[gordios] uçuş satır sayısı: %d", len(out["ucus_listesi"]))

            # ── Günlük programı parse et ─────────────────────────────────────
            if program_text.strip():
                parsed = _extract_program_from_text(program_text)
                out["program_gunler"] = parsed["gunler"]
                if not out["program_baslik"] and parsed["baslik"]:
                    out["program_baslik"] = parsed["baslik"]
                logger.info("[gordios] günlük program: %d gün", len(out["program_gunler"]))

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


def _extract_tour_title(page1_text: str) -> str:
    """
    Sayfa 1 metninden tur başlığını çıkar.
    Glob DMC kapak sayfasında başlık, web sitesi satırından sonra gelir.
    """
    lines = [ln.strip() for ln in page1_text.splitlines() if ln.strip()]
    # Web sitesi / telefon satırından sonraki ilk uzun satırı al
    for i, line in enumerate(lines):
        low = line.lower()
        if "globdmc.com" in low or ("www." in low and "glob" in low):
            if i + 1 < len(lines):
                return lines[i + 1]
    # Fallback: adres/telefon olmayan, 20+ karakter olan ilk satır
    skip = {"mah.", "sk.", "no:", "telefon", "www.", "@", "glob dmc"}
    for line in lines[2:10]:
        if len(line) > 20 and not any(k in line.lower() for k in skip):
            return line
    return ""


def _extract_program_from_text(text: str) -> dict:
    """
    PDF metin bloğundan günlük programı parse eder.

    Gordios PDF gün başlığı formatı (tam satır olarak):
        "1 . GÜN"   veya   "1. GÜN"   (GÜN yerine GüN de gelebilir — encoding)
    Bir sonraki satır: lokasyon/başlık  (ör. "ANKARA – MİLANO – COMO GÖLÜ")
    Ardından: açıklama metni

    Returns:
        {"baslik": str, "gunler": [{"gun": int, "baslik": str, "icerik": str}, …]}
    """
    out = {"baslik": "", "gunler": []}

    # Gün başlığı: satırın tamamı "1 . GÜN" veya "1. GÜN" şeklinde
    # GÜN harfi bazen GüN olarak çıkabiliyor (pdfplumber encoding farklılığı)
    GUN_PATTERN = re.compile(
        r'^(\d{1,2})\s*\.\s*G[ÜüUu]N\s*$',
        re.IGNORECASE | re.MULTILINE,
    )

    matches = list(GUN_PATTERN.finditer(text))

    if not matches:
        # Hiç gün bulunamadıysa tüm metni tek blok olarak döndür
        icerik = text.strip()
        # "Tur Programı" gibi başlık satırını çıkar
        lines = icerik.splitlines()
        for ln in lines:
            if ln.strip():
                out["baslik"] = ln.strip()
                break
        if icerik:
            out["gunler"].append({"gun": 0, "baslik": "", "icerik": icerik})
        return out

    for i, m in enumerate(matches):
        gun_no = int(m.group(1))

        # Bu eşleşmenin bitişinden sonraki metin bloğu
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()

        # Blokun ilk satırı = lokasyon başlığı, geri kalanı = açıklama
        block_lines = [ln for ln in block.splitlines() if ln.strip()]
        gun_baslik  = block_lines[0].strip() if block_lines else ""
        icerik      = "\n".join(block_lines[1:]).strip() if len(block_lines) > 1 else ""

        out["gunler"].append({
            "gun":    gun_no,
            "baslik": gun_baslik,
            "icerik": icerik,
        })

    return out


# ── Ek bölüm parse (export-pdf için) ─────────────────────────────────────────

def _parse_pdf_extra(pdf_bytes: bytes) -> dict:
    """
    Gordios PDF'den dahil/hariç hizmetler ve notları çıkarır.
    Konaklama + yolcu listesi atlanır.

    Döner:
        dahil_hizmetler : list[str]
        haric_hizmetler : list[str]
        notlar          : str
    """
    out: dict = {"dahil_hizmetler": [], "haric_hizmetler": [], "notlar": ""}
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            total = len(pdf.pages)
            # Sayfa 1 → dahil/hariç hizmetler (sayfayı sol/sağ bölerek)
            if total >= 1:
                out["dahil_hizmetler"], out["haric_hizmetler"] = \
                    _extract_services_from_page(pdf.pages[0])
                logger.info("[gordios-extra] dahil: %d, hariç: %d",
                            len(out["dahil_hizmetler"]), len(out["haric_hizmetler"]))
            # Sayfa 2 → notlar / katılım koşulları
            if total >= 2:
                t2 = pdf.pages[1].extract_text() or ""
                out["notlar"] = _extract_notes(t2)
                logger.info("[gordios-extra] notlar: %d karakter", len(out["notlar"]))
    except Exception as e:
        logger.warning("[gordios] _parse_pdf_extra hatası: %s", e)
    return out


def _extract_services_from_page(page) -> tuple:
    """
    Gordios sayfa 1'den dahil / hariç hizmetleri çıkarır.

    extract_words() ile her kelimenin gerçek x0 koordinatını alır.
    'DAHİL OLMAYAN' başlığının x0'ı sütun sınırı (col_x) olarak kullanılır;
    bu değer pw/2'den farklı olabilir — tam konum PDF'den okunur.

    col_x solunda kalan kelimeler → dahil (sol sütun)
    col_x sağında kalan kelimeler → hariç (sağ sütun)
    """
    try:
        words = page.extract_words(x_tolerance=5, y_tolerance=5)
        if not words:
            return [], []

        # ── 1. 'DAHİL OLMAYAN' başlığının x0 konumunu bul ──────────────────
        # Doğru yöntem: önce 'OLMAYAN' kelimesini bul, sonra hemen öncesindeki
        # 'DAHİL' kelimesinin x0'ını al.  İleri arama yanlış — sol başlıktaki
        # 'DAHİL OLAN HİZMETLER DAHİL OLMAYAN HİZMETLER' satırında soldaki
        # 'DAHİL' (x0≈50) 4 kelime sonra 'OLMAYAN'ı buluyor ve col_x≈50 veriyor.
        col_x: float | None = None
        for i, w in enumerate(words):
            if re.search(r'olmayan', w['text'], re.I):
                # Hemen öncesindeki 'dahil' kelimesini ara (en fazla 5 geri)
                for back in range(1, min(6, i + 1)):
                    prev = words[i - back]
                    if re.search(r'dah[iı]l', prev['text'], re.I):
                        col_x = float(prev['x0'])
                        break
                if col_x is not None:
                    break

        if col_x is None:
            col_x = float(page.width) / 2
            logger.warning("[gordios] col_x bulunamadı, pw/2=%.1f kullanılıyor", col_x)
        else:
            logger.info("[gordios] col_x=%.1f (pw=%.1f)", col_x, float(page.width))

        # ── 2. Başlık satırının en alt kenarını bul ─────────────────────────
        HDR_RE = re.compile(r'dah[iı]l', re.I)
        header_bottom = 0.0
        for w in words:
            if HDR_RE.search(w['text']):
                header_bottom = max(header_bottom, float(w['bottom']))

        # ── 3. Başlık altındaki kelimeleri sol/sağ sütuna ata ───────────────
        from collections import defaultdict
        left_lines: dict  = defaultdict(list)
        right_lines: dict = defaultdict(list)

        for w in words:
            if float(w['top']) <= header_bottom:
                continue   # başlık satırı veya üstü — atla
            y_key = round(float(w['top']))
            if float(w['x0']) < col_x:
                left_lines[y_key].append(w['text'])
            else:
                right_lines[y_key].append(w['text'])

        def _reconstruct(lines_dict: dict) -> str:
            return '\n'.join(
                ' '.join(lines_dict[y]) for y in sorted(lines_dict.keys())
            )

        dahil = _parse_service_items(_reconstruct(left_lines))
        haric = _parse_service_items(_reconstruct(right_lines))
        logger.info("[gordios] word-x sonuç: dahil=%d haric=%d", len(dahil), len(haric))
        return dahil, haric

    except Exception as e:
        logger.warning("[gordios] _extract_services_from_page hatası: %s", e)
        return [], []


def _parse_service_items(text: str) -> list:
    """
    Madde listesi metninden hizmet maddelerini çıkarır.
    - Bullet ile başlayan satırlar yeni madde başlatır
    - Bullet'sız devam satırları önceki maddeye eklenir
    - 'X/Y' sayfa numaraları ve çok kısa satırlar atlanır
    """
    BULLET   = re.compile(r'^[•·▪▸▶\-\*✓✗–—►]+\s*')
    PAGE_NUM = re.compile(r'^\d+/\d+$')

    items:   list = []
    current: str | None = None

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if PAGE_NUM.match(ln):
            continue
        if BULLET.match(ln):           # yeni madde
            if current:
                items.append(current)
            current = BULLET.sub('', ln).strip()
        elif current is not None:      # önceki maddenin devamı
            current += ' ' + ln
        # bullet olmayan, current=None → başlık / gürültü → atla

    if current:
        items.append(current)

    return [i for i in items if len(i) > 3]


def _extract_notes(text: str) -> str:
    """Sayfa 2'den notlar / katılım koşullarını düz metin olarak döndürür."""
    SKIP = re.compile(
        r'glob\s*dmc|globdmc\.com|www\.|'
        r'kat[iı]l[iı]m\s+ko[sş]ul|önemli\s+not|[oö]nemli\s+bil|'
        r'tel[efo]*n\s*:|\b\+?\d[\d\s\-\(\)]{7,}\b',
        re.I,
    )
    lines = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or SKIP.search(ln):
            continue
        lines.append(ln)
    out = "\n".join(lines).strip()
    out = re.sub(r'\n{3,}', '\n\n', out)
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
