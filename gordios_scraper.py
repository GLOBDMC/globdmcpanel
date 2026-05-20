"""
gordios_scraper.py
------------------
Gordios backoffice'ten tur detaylarını çeken Playwright scraper.
Her scrape çağrısı: login → ara → detail → uçuş tablosunu parse → PDF URL döndür.
"""
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
    JT kodu için Gordios'tan uçuş bilgileri ve PDF URL döndürür.

    Returns:
        {
          "jt_kodu":      str,
          "plan_id":      int | None,
          "pdf_url":      str | None,
          "ucus_listesi": list[dict],   # Gidiş + Dönüş satırları
          "hata":         str | None,
        }
    """
    result: dict = {
        "jt_kodu":      jt_kodu,
        "plan_id":      None,
        "pdf_url":      None,
        "ucus_listesi": [],
        "hata":         None,
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        result["hata"] = "playwright kurulu değil — pip install playwright && playwright install chromium"
        return result

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx  = browser.new_context(
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

            # Kurum kodu — farklı uygulamalarda field adı değişebilir
            _fill_first_visible(page, [
                'input[name*="nstitution"]',
                'input[name*="Kurum"]',
                'input[placeholder*="Kurum"]',
                '#InstitutionCode',
                'input[id*="nstitution"]',
            ], GORDIOS_INSTITUTION)

            # Kullanıcı adı / e-posta
            _fill_first_visible(page, [
                'input[name="Input.Username"]',
                'input[name="Username"]',
                'input[name="username"]',
                'input[type="email"]',
                '#username', '#Username',
            ], GORDIOS_USERNAME)

            # Şifre
            page.fill('input[type="password"]', GORDIOS_PASSWORD)

            # Submit
            page.click('button[type="submit"], input[type="submit"]')

            # Backoffice'e yönlendirmeyi bekle
            try:
                page.wait_for_url(f"{GORDIOS_BO_BASE}/**", timeout=20_000)
            except Exception:
                page.wait_for_load_state("networkidle", timeout=15_000)

            if GORDIOS_BO_BASE not in page.url:
                result["hata"] = f"Login sonrası beklenmedik URL: {page.url}"
                return result
            logger.info("[gordios] login OK → %s", page.url)

            # ── 2. ARAMA SAYFASI ─────────────────────────────────────────────
            page.goto(GORDIOS_TOUR_LIST, wait_until="networkidle", timeout=30_000)

            # "Periyot Kodu" alanına JT kodunu yaz
            # Önce name/id/placeholder'da "periyot" veya "period" geçen input'u ara
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
                # Fallback: sayfadaki label metinlerine bak
                labels = page.query_selector_all("label")
                for lbl in labels:
                    if "periyot" in (lbl.inner_text() or "").lower():
                        for_attr = lbl.get_attribute("for")
                        if for_attr:
                            inp = page.query_selector(f'#{for_attr}')
                            if inp:
                                inp.fill(jt_kodu)
                                jt_filled = True
                                logger.info("[gordios] Label 'periyot' ile input bulundu: #%s", for_attr)
                                break
                    if jt_filled:
                        break

            if not jt_filled:
                # Son çare: ikinci text input (ekran görüntüsünde JT kodu 2. inputta)
                all_inputs = page.query_selector_all('input[type="text"]')
                if len(all_inputs) >= 2:
                    all_inputs[1].fill(jt_kodu)
                    logger.warning("[gordios] Fallback: ikinci input kullanıldı")
                elif all_inputs:
                    all_inputs[0].fill(jt_kodu)

            # Listele
            page.click('button:has-text("Listele"), input[value="Listele"]')
            page.wait_for_load_state("networkidle", timeout=15_000)

            # ── 3. SONUÇTAN TUR LİNKİNE TIK ─────────────────────────────────
            link = page.query_selector(f'a:has-text("{jt_kodu}")')
            if not link:
                # İlk satır linkini dene
                link = page.query_selector("table tbody tr td a")
            if not link:
                result["hata"] = f"Sonuç tablosunda tur linki bulunamadı: {jt_kodu}"
                return result

            link.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
            logger.info("[gordios] detail URL: %s", page.url)

            # ── 4. PLAN ID ───────────────────────────────────────────────────
            plan_id = _extract_plan_id(page)
            if plan_id:
                result["plan_id"] = plan_id
                result["pdf_url"] = f"{GORDIOS_PDF_BASE}?planId={plan_id}"
                logger.info("[gordios] plan_id=%s", plan_id)
            else:
                logger.warning("[gordios] plan_id alınamadı, URL=%s", page.url)

            # ── 5. UÇUŞ TABLOSU ─────────────────────────────────────────────
            result["ucus_listesi"] = _parse_flight_table(page)
            logger.info("[gordios] uçuş satır sayısı: %d", len(result["ucus_listesi"]))

        except Exception as exc:
            logger.error("[gordios] scrape hatası [%s]: %s", jt_kodu, exc, exc_info=True)
            result["hata"] = str(exc)
        finally:
            browser.close()

    return result


def download_pdf(plan_id: int, cookies: list) -> Optional[bytes]:
    """
    Playwright context cookie'leriyle PDF indir.
    cookies: Playwright ctx.cookies() listesi
    """
    try:
        import httpx
        jar = {c["name"]: c["value"] for c in cookies}
        url = f"{GORDIOS_PDF_BASE}?planId={plan_id}"
        r = httpx.get(url, cookies=jar, follow_redirects=True, timeout=30)
        if r.status_code == 200:
            ct = r.headers.get("content-type", "")
            if "pdf" in ct or r.content[:4] == b"%PDF":
                logger.info("[gordios] PDF indirildi: %d bytes", len(r.content))
                return r.content
        logger.warning("[gordios] PDF indirilemedi: status=%s", r.status_code)
    except Exception as e:
        logger.error("[gordios] PDF indirme hatası: %s", e)
    return None


# ── Yardımcılar ──────────────────────────────────────────────────────────────

def _fill_first_visible(page, selectors: list, value: str) -> bool:
    for sel in selectors:
        try:
            if page.is_visible(sel, timeout=1_000):
                page.fill(sel, value)
                return True
        except Exception:
            pass
    return False


def _extract_plan_id(page) -> Optional[int]:
    """URL veya PDF linkinden plan ID'sini çıkar."""
    # 1. Mevcut URL'den
    url = page.url
    for pattern in [r'/Detail/(\d+)', r'[?&]planId=(\d+)', r'[?&]id=(\d+)']:
        m = re.search(pattern, url, re.I)
        if m:
            return int(m.group(1))
    # 2. PDF linkinden
    pdf_el = page.query_selector('a[href*="ExportTourPlanPdf"], a:has-text("Tur Planı PDF")')
    if pdf_el:
        href = pdf_el.get_attribute("href") or ""
        m = re.search(r'planId=(\d+)', href, re.I)
        if m:
            return int(m.group(1))
    return None


def _cell_text(cell) -> str:
    """
    TD hücresinden metin çıkar.
    Önce time input, sonra select seçili option, sonra inner_text.
    """
    # time veya text input
    inp = cell.query_selector('input')
    if inp:
        v = inp.get_attribute("value") or ""
        if v.strip():
            return v.strip()
        return inp.inner_text().strip()

    # select dropdown
    select = cell.query_selector("select")
    if select:
        try:
            return select.evaluate(
                "el => el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : ''"
            ).strip()
        except Exception:
            pass

    return cell.inner_text().strip()


def _parse_flight_table(page) -> list:
    """
    Detail sayfasındaki uçuş tablosunu parse eder.
    Döndürür: [{"yon", "ucus_no", "pnr", "havayolu",
                 "kalkis_saat", "kalkis_yeri", "varis_yeri", "varis_saat",
                 "aktarimli", "ertesi_gun"}, ...]
    """
    ucuslar = []
    # İlk tablo uçuş tablosu (birden fazla tablo varsa "Uçuş" başlığının yakınındakini bul)
    tables = page.query_selector_all("table")
    target = None
    for tbl in tables:
        header_text = tbl.inner_text()[:200].lower()
        if any(k in header_text for k in ["uçuş", "ucus", "yön", "yon", "havayolu"]):
            target = tbl
            break
    if not target and tables:
        target = tables[0]
    if not target:
        return ucuslar

    rows = target.query_selector_all("tbody tr")
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 4:
            continue
        vals = [_cell_text(c) for c in cells]
        yon = vals[0] if len(vals) > 0 else ""
        if not yon:
            continue
        ucus = {
            "yon":         yon,
            "ucus_no":     vals[1] if len(vals) > 1 else "",
            "pnr":         vals[2] if len(vals) > 2 else "",
            "havayolu":    vals[3] if len(vals) > 3 else "",
            "kalkis_saat": vals[4] if len(vals) > 4 else "",
            "kalkis_yeri": vals[5] if len(vals) > 5 else "",
            "varis_yeri":  vals[6] if len(vals) > 6 else "",
            "varis_saat":  vals[7] if len(vals) > 7 else "",
            "aktarimli":   vals[8] if len(vals) > 8 else "",
            "ertesi_gun":  vals[9] if len(vals) > 9 else "",
        }
        ucuslar.append(ucus)

    return ucuslar
