"""
Porsline Survey Platform entegrasyonu.
Base URL : https://survey.porsline.ir/api/
Auth     : Authorization: API-Key <token>
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, date
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
_BASE = "https://survey.porsline.com"
_TOKEN = os.environ.get("PORSLINE_API_KEY", "")

# Ay adları → sayı (Türkçe)
_MONTHS_TR = {
    "ocak": 1, "şubat": 2, "mart": 3, "nisan": 4,
    "mayıs": 5, "haziran": 6, "temmuz": 7, "ağustos": 8,
    "eylül": 9, "ekim": 10, "kasım": 11, "aralık": 12,
    # aksansız
    "subat": 2, "mayis": 5, "haziran": 6, "agustos": 8,
    "eylul": 9, "kasim": 11, "aralik": 12,
}


# ── HTTP yardımcıları ─────────────────────────────────────────────────────────

def _get(path: str, params: dict = None) -> dict:
    if not _TOKEN:
        return {"error": "PORSLINE_API_KEY tanımlı değil"}
    url = f"{_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    _auth_prefix = os.environ.get("PORSLINE_AUTH_PREFIX", "API-Key")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"{_auth_prefix} {_TOKEN}",
            "Content-Type":  "application/json",
            "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept":        "application/json",
        },
    )
    import time as _time
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            if e.code == 429:
                # Rate limit — bekle ve tekrar dene
                wait = (attempt + 1) * 5  # 5s, 10s, 15s
                _time.sleep(wait)
                continue
            return {"error": e.code, "detail": body}
        except Exception as e:
            return {"error": str(e)}
    return {"error": 429, "detail": "Rate limit: 3 denemede geçilemedi"}


# ── API çağrıları ─────────────────────────────────────────────────────────────

def test_connection() -> dict:
    """API anahtarının çalışıp çalışmadığını test eder."""
    result = _get("/api/folders/")
    if "error" in result:
        return {"ok": False, "hata": result["error"], "detay": result.get("detail", "")}
    # Kaç anket var?
    surveys = []
    for folder in (result if isinstance(result, list) else result.get("results", [])):
        surveys.extend(folder.get("surveys", []))
    return {"ok": True, "klasor_sayisi": len(result) if isinstance(result, list) else result.get("count", "?"), "anket_sayisi": len(surveys)}


def list_surveys(page: int = 1, page_size: int = 50) -> dict:
    """
    Folders endpoint üzerinden tüm anketleri toplar.
    Porsline'da survey listeleme endpoint'i yok; klasörler içinden çıkarılır.
    """
    result = _get("/api/folders/")
    if "error" in result:
        return {"ok": False, "hata": result["error"]}

    folders = result if isinstance(result, list) else result.get("results", [])
    surveys = []
    for folder in folders:
        for s in folder.get("surveys", []):
            s["_folder"] = folder.get("title", "")
            surveys.append(s)

    return {
        "ok":      True,
        "count":   len(surveys),
        "surveys": surveys,
        "next":    None,
    }


def get_survey_detail(survey_id: str) -> dict:
    """Bir anketin detaylarını getirir."""
    result = _get(f"/api/v2/surveys/{survey_id}/")
    if "error" in result:
        return {"ok": False, "hata": result["error"]}
    return {"ok": True, "survey": result}


def get_responses(survey_id: str, page: int = 1, page_size: int = 100) -> dict:
    """Bir anketin yanıtlarını getirir."""
    result = _get(
        f"/api/v2/surveys/{survey_id}/responses/results-table/",
        {"page": page, "page_size": page_size},
    )
    if "error" in result:
        return {"ok": False, "hata": result["error"]}
    return {
        "ok":      True,
        "header":  result.get("header", []),
        "body":    result.get("body", []),
        "count":   result.get("responders_count", 0),
    }


def get_all_responses(survey_id: str) -> dict:
    """Bir anketin TÜM yanıtlarını sayfalı olarak çeker."""
    all_rows = []
    header   = []
    page     = 1

    while True:
        chunk = get_responses(survey_id, page=page, page_size=100)
        if not chunk["ok"]:
            return chunk
        if not header:
            header = chunk["header"]
        rows = chunk["body"]
        all_rows.extend(rows)
        if len(rows) < 100:
            break
        page += 1

    return {"ok": True, "header": header, "body": all_rows, "count": len(all_rows)}


# ── Survey başlığı parse ──────────────────────────────────────────────────────

def _infer_year(gun: int, ay: int, created_date_str: str) -> Optional[int]:
    """
    Anket oluşturma tarihinden kalkış yılını tahmin eder.

    Kural: kalkış tarihi oluşturma tarihinden ≤ 180 gün ÖNCESİNDE olmalı.
    Örn: Anket 2024-08-15'te oluşturulmuş, kalkış "23 Temmuz" → 2024.
         Anket 2024-01-10'da oluşturulmuş, kalkış "23 Temmuz" → muhtemelen 2023.
    """
    if not created_date_str:
        return None
    created = None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            created = datetime.strptime(created_date_str[:19], fmt[:len(created_date_str[:19])]).date()
            break
        except Exception:
            pass
    if not isinstance(created, date):
        # Fallback: try just parsing the first 10 chars
        try:
            created = datetime.strptime(created_date_str[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    # Aynı yıl içinde bu gün-ay created'dan önce mi?
    from datetime import date as _date
    for year_offset in (0, -1, 1):
        try:
            candidate = _date(created.year + year_offset, ay, gun)
        except ValueError:
            continue
        diff = (created - candidate).days
        # Kalkış, anket oluşturmadan 0-365 gün önce olabilir
        if 0 <= diff <= 365:
            return candidate.year

    return created.year  # son çare: created yılını kullan


def parse_survey_title(title: str, created_date: str = "") -> dict:
    """
    Porsline anket başlığından tur bilgilerini çıkarır.

    Örnek:
      "14 Mayıs Comfort İspanya & Endülüs Pegasus Hava Yolları ile 7 Gece
       Ekstra Turlar Dahil Memnuniyet Anketi"
    →
      {
        "kalkis_gun":  14,
        "kalkis_ay":   5,
        "kalkis_str":  "14-05-2024",   # yıl created_date'den tahmin edilir
        "tur_adi":     "Comfort İspanya & Endülüs",
        "havayolu":    "Pegasus",
        "gece":        7,
      }

    created_date: Porsline'dan gelen anket oluşturma tarihi (yıl tahmini için).
    """
    result = {
        "kalkis_gun":  None,
        "kalkis_ay":   None,
        "kalkis_str":  None,
        "tur_adi":     title,
        "havayolu":    None,
        "gece":        None,
        "rehber_adi":  None,
    }

    t = title.strip()

    # Rehber adı: başlığın sonunda " - Ad Soyad" kalıbı
    rehber_match = re.search(r'\s*[-–]\s*([A-ZÇĞIİŞÖÜa-zçğışöü][a-zçğışöüA-ZÇĞIİŞÖÜ]+(?:\s+[A-ZÇĞIİŞÖÜa-zçğışöü][a-zçğışöüA-ZÇĞIİŞÖÜ]+)+)\s*$', t)
    if rehber_match:
        result["rehber_adi"] = rehber_match.group(1).strip()
        t = t[:rehber_match.start()].strip()

    # "Memnuniyet Anketi" ve benzeri sonekler temizle
    t = re.sub(r'\s*(memnuniyet\s*)?anketi?\s*$', '', t, flags=re.IGNORECASE).strip()

    # Gece sayısı: "7 Gece", "10 Gece Ekstra Turlar Dahil" vb.
    m = re.search(r'(\d+)\s*gece', t, re.IGNORECASE)
    if m:
        result["gece"] = int(m.group(1))
        # " ... ile X Gece ..." kısmını temizle
        t = t[:m.start()].strip().rstrip("ile").strip()

    # Havayolu: "Pegasus Hava Yolları", "THY ile", "Turkish Airlines ile"
    havayolu_patterns = [
        r'pegasus(?:\s+hava\s+yollar[ıi])?',
        r'thy(?:\s+türk\s+hava\s+yollar[ıi])?',
        r'türk(?:ish)?\s+(?:hava\s+yollar[ıi]|airlines)',
        r'sunexpress(?:\s+hava\s+yollar[ıi])?',
        r'atlas(?:\s+global)?(?:\s+hava\s+yollar[ıi])?',
        r'anadolu\s+jet',
        r'flydubai',
        r'wizz\s*air',
        r'(\w+\s+hava\s+yollar[ıi])',
    ]
    for pat in havayolu_patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            result["havayolu"] = m.group(0).strip()
            # Temizle
            t = t[:m.start()].strip().rstrip("ile").strip()
            break

    # Tarih: başta "14 Mayıs", "14 Mayıs 2024" gibi
    date_match = re.match(
        r'^(\d{1,2})\s+([a-zçğışöüA-ZÇĞIİŞÖÜ]+)\s*(?:(\d{4})\s*)?',
        t.strip()
    )
    if date_match:
        gun  = int(date_match.group(1))
        ay_s = date_match.group(2).lower().strip()
        yil  = int(date_match.group(3)) if date_match.group(3) else None
        ay   = _MONTHS_TR.get(ay_s)
        if ay:
            result["kalkis_gun"] = gun
            result["kalkis_ay"]  = ay
            if not yil:
                yil = _infer_year(gun, ay, created_date)
            if yil:
                try:
                    result["kalkis_str"] = f"{gun:02d}-{ay:02d}-{yil}"
                except Exception:
                    pass
            t = t[date_match.end():].strip()

    # Geriye kalan = tur adı
    result["tur_adi"] = t.strip().strip("-").strip() if t.strip() else title

    return result


# ── Yanıt parse ───────────────────────────────────────────────────────────────

def _find_col(header: list[str], keywords: list[str]) -> Optional[int]:
    """Header listesinde anahtar kelime içeren ilk sütun indeksini döndürür."""
    for kw in keywords:
        for i, h in enumerate(header):
            if kw.lower() in h.lower():
                return i
    return None


def _safe_float(val) -> Optional[float]:
    if val is None or str(val).strip() in ("", "-", "N/A"):
        return None
    try:
        return float(str(val).replace(",", "."))
    except ValueError:
        return None


def _extract_guide_name(header: list[str]) -> Optional[str]:
    """
    Rehber sorusunun başlığından rehber adını çıkarır.
    Örn: "Tur rehberimizin bilgi ve ilgisinden memnun kaldınız mı?  (Derya Iberi)"
    → "Derya Iberi"
    """
    for h in header:
        if "rehber" in h.lower():
            m = re.search(r'\(([^)]+)\)', h)
            if m:
                return m.group(1).strip()
    return None


def parse_response_row(header: list[str], row: list) -> dict:
    """
    Tek bir yanıt satırını alanlarına göre parse eder.
    Header ile row aynı uzunlukta olmak zorunda.
    """
    def get(idx):
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    # Sütun indeksleri
    i_musteri = _find_col(header, ["isim", "ad soyad", "ad-soyad", "name"])
    i_acente  = _find_col(header, ["acente"])
    i_rehber  = _find_col(header, ["rehber"])
    i_otobus  = _find_col(header, ["otobüs", "otobus"])
    i_sofor   = _find_col(header, ["şoför", "sofor", "şofor"])
    i_program = _find_col(header, ["program"])
    i_tavsiye = _find_col(header, ["tavsiye", "öneri"])

    # Otel puanları: "otel" veya şehir adı içeren tüm sütunlar
    otel_puanlari = {}
    otel_keywords = ["otel", "hotel", "barcelona", "valencia", "granada",
                     "sevilla", "madrid", "roma", "paris", "amsterdam",
                     "venedik", "istanbul", "ankara", "bodrum"]
    for i, h in enumerate(header):
        if any(kw in h.lower() for kw in otel_keywords):
            v = _safe_float(get(i))
            if v is not None:
                # Başlıktan kısa isim çıkar
                short = h.split("?")[0].strip()[:60]
                otel_puanlari[short] = v

    # Rehber adı header'dan
    rehber_adi = _extract_guide_name(header)

    # Puanlar
    rehber_puani = _safe_float(get(i_rehber))
    otobus_puani = _safe_float(get(i_otobus))
    sofor_puani  = _safe_float(get(i_sofor))
    program_puani= _safe_float(get(i_program))

    # Genel puan = tüm sayısal puanların ortalaması
    all_scores = [v for v in [rehber_puani, otobus_puani, sofor_puani, program_puani]
                  + list(otel_puanlari.values()) if v is not None]
    genel_puan = round(sum(all_scores) / len(all_scores), 2) if all_scores else None

    return {
        "musteri_adi":  str(get(i_musteri) or "").strip(),
        "acente_adi":   str(get(i_acente)  or "").strip(),
        "rehber_adi":   rehber_adi or "",
        "genel_puan":   genel_puan,
        "rehber_puani": rehber_puani,
        "puan_detay": {
            "oteller":  otel_puanlari,
            "otobus":   otobus_puani,
            "sofor":    sofor_puani,
            "program":  program_puani,
            "tavsiye":  str(get(i_tavsiye) or ""),
        },
    }
