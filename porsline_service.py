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


# ── Yardımcı: Porsline lokalize alanları dict döndürebilir ───────────────────

def _str_field(val) -> str:
    """
    Porsline API bazen title/label alanlarını lokalize dict olarak döner:
      {"fa": "متن", "en": "Text"}
    Bu fonksiyon her türlü değeri güvenli şekilde str'ye çevirir.
    """
    if val is None:
        return ""
    if isinstance(val, dict):
        # Önce İngilizce, sonra Farsça, sonra ilk değer
        for key in ("en", "fa", "tr"):
            if val.get(key):
                return str(val[key])
        for v in val.values():
            if v:
                return str(v)
        return ""
    if isinstance(val, list):
        return " ".join(_str_field(v) for v in val if v)
    return str(val)


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
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        retry_after = None
        try:
            body = e.read().decode()
            retry_after = e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Reset")
        except Exception:
            pass
        return {"error": e.code, "detail": body, "retry_after": retry_after}
    except Exception as e:
        return {"error": str(e)}


def _get_with_retry(path: str, params: dict = None,
                    max_retries: int = 4, _delay: float = 0.8) -> dict:
    """
    _get'i çağırır; 429 (rate-limit) gelirse Retry-After kadar bekler ve tekrar dener.
    Her denemeden önce _delay saniye uyur (Porsline'ın burst sınırı için).
    """
    import time as _t
    last = {}
    for attempt in range(max_retries):
        if attempt > 0 or _delay > 0:
            _t.sleep(_delay)
        last = _get(path, params)
        if "error" not in last:
            return last
        if last["error"] == 429:
            wait = 60.0
            ra = last.get("retry_after")
            if ra:
                try:
                    wait = min(float(ra), 300)  # en fazla 5 dk bekle
                except (ValueError, TypeError):
                    pass
            import logging as _log
            _log.getLogger(__name__).warning(
                "Porsline 429 rate-limit — %s sn bekleniyor (deneme %d/%d)",
                wait, attempt + 1, max_retries,
            )
            _t.sleep(wait)
            continue
        # 429 dışında hata → tekrar deneme
        break
    return last


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


# Folders cache — /api/folders/ çok çağrılmasın diye
_folders_cache: list = []
_folders_cache_ts: float = 0.0


def _get_survey_from_folders(survey_id: str) -> Optional[dict]:
    """Folders listesinden survey objesi döndürür (5 dak. cache)."""
    import time as _time
    global _folders_cache, _folders_cache_ts
    if not _folders_cache or (_time.time() - _folders_cache_ts) > 300:
        res = _get("/api/folders/")
        if "error" not in res:
            folders = res if isinstance(res, list) else res.get("results", [])
            _folders_cache = []
            for folder in folders:
                for s in folder.get("surveys", []):
                    _folders_cache.append(s)
            _folders_cache_ts = _time.time()
    for s in _folders_cache:
        if str(s.get("id")) == str(survey_id):
            return s
    return None


def get_survey_detail(survey_id: str) -> dict:
    """
    Bir anketin detaylarını getirir.
    /api/v2/surveys/{id}/ rate-limit'e çok takılıyor —
    direkt folders cache'i kullan (zaten 150 anketi içeriyor).
    Sadece folders'da bulunamazsa v2'yi dene.
    """
    # Önce cache'den bak (API çağrısı yok)
    s = _get_survey_from_folders(survey_id)
    if s:
        return {"ok": True, "survey": s}

    # Cache boş veya anket bulunamadı → v2 dene
    result = _get(f"/api/v2/surveys/{survey_id}/")
    if "error" not in result:
        return {"ok": True, "survey": result}

    return {"ok": False, "hata": result.get("error", "survey bulunamadı")}


def get_responses(survey_id: str, page: int = 1, page_size: int = 100) -> dict:
    """Bir anketin yanıtlarını getirir. results-table → yoksa responses/ dener."""
    params = {"page": page, "page_size": page_size}

    endpoints = [
        f"/api/v2/surveys/{survey_id}/responses/results-table/",
        f"/api/surveys/{survey_id}/responses/results-table/",
        f"/api/v2/surveys/{survey_id}/responses/",
    ]

    last_error = None
    retry_after = None
    for ep in endpoints:
        result = _get_with_retry(ep, params)
        if "error" in result:
            last_error = result["error"]
            retry_after = result.get("retry_after")
            if result["error"] == 404:
                continue  # sonraki URL'yi dene
            break  # diğer hata → dur
        # Başarılı
        header = result.get("header") or result.get("headers") or []
        body   = (result.get("body") or result.get("results")
                  or result.get("responses") or [])
        count  = (result.get("responders_count") or result.get("count")
                  or result.get("total") or len(body))
        return {"ok": True, "header": header, "body": body,
                "count": count, "_endpoint": ep}

    return {"ok": False, "hata": last_error or "responses endpoint bulunamadı",
            "retry_after": retry_after}


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


def build_header_from_questions(questions: list) -> list:
    """
    Survey detayındaki questions listesinden header oluşturur.
    results-table endpoint'i çalışmadığında kullanılır.
    Sadece type=7 (yıldız) ve type=2/3 (metin/seçim) sorularını alır.
    """
    return [_str_field(q.get("title")) for q in questions if q.get("type") in (2, 3, 7)]


def _all_title_variants(q) -> list[str]:
    """
    Soru objesinden TÜM dil varyantlarını küçük harfle döndürür.
    Porsline bazen {"en": "Guide Rating", "tr": "Rehber Puanı", "fa": "..."} döner.
    Anahtar kelime aramasında herhangi bir varyantta eşleşmesi yeterli.
    """
    raw = q.get("title")
    if isinstance(raw, dict):
        return [str(v).lower() for v in raw.values() if v]
    elif raw:
        return [str(raw).lower()]
    return [""]


def _unwrap_answer(ans: dict):
    """
    Porsline cevap objesinden ham değeri güvenli şekilde çıkarır.
    Farklı formatları destekler:
      {"answer": "3"}
      {"answer": {"value": 3, "label": "3 yıldız"}}
      {"answer": null, "value": "3"}
      {"choices": [{"id": 5, "text": "Madrid"}]}
    """
    raw = ans.get("answer")
    # None ise diğer alanlara bak (0 değerini kaybetme: sadece None'a bak)
    if raw is None:
        raw = ans.get("value")
    if raw is None:
        raw = ans.get("text")
    if raw is None:
        # choices formatı: seçim sorusu
        choices = ans.get("choices") or ans.get("selected_choices") or []
        if choices:
            # Seçilen seçeneğin text veya label'ını al
            texts = [str(c.get("text") or c.get("label") or c.get("value") or "")
                     for c in choices if isinstance(c, dict)]
            raw = ", ".join(t for t in texts if t) or None
    # Dict cevap (ör. {"value": 3, "label": "3 yıldız"})
    if isinstance(raw, dict):
        raw = (raw.get("value") if raw.get("value") is not None else
               raw.get("rating") if raw.get("rating") is not None else
               raw.get("score") if raw.get("score") is not None else
               raw.get("answer") if raw.get("answer") is not None else
               raw.get("text") or "")
    # Tek elemanlı liste
    if isinstance(raw, list):
        raw = raw[0] if len(raw) == 1 else (", ".join(str(v) for v in raw if v) or None)
    return raw


def parse_response_from_questions(questions: list, response: dict) -> dict:
    """
    /api/v2/surveys/{id}/responses/ endpoint'inden gelen tek yanıtı parse eder.
    response örneği: {"id": 123, "answers": [{"question": 456, "answer": "3"}, ...]}
    questions: survey detayındaki sorular listesi.

    Dil-bağımsız eşleştirme: soru başlığının TÜM dil varyantlarında (tr/en/fa) arama yapılır.
    """
    # Cevapları soru ID'ye göre indeksle
    answers_by_q = {}
    for ans in (response.get("answers") or response.get("answer_list") or []):
        q_id = ans.get("question") or ans.get("question_id")
        val  = _unwrap_answer(ans)
        if q_id is not None and val is not None:
            answers_by_q[q_id] = val

    musteri_adi       = ""
    acente_adi        = ""
    rehber_puani      = None
    otobus_puani      = None
    sofor_puani       = None
    program_puani     = None
    operasyon_puani   = None
    transfer_puani    = None
    ekstra_tur_puani  = None
    genel_memnuniyet  = None
    otel_puanlari     = {}
    tavsiye_puan      = None

    for q in questions:
        qid   = q.get("id")
        if qid is None:
            continue
        qtype = q.get("type")
        val   = answers_by_q.get(qid)

        if val is None:
            continue

        # Tüm dil varyantlarını al
        variants = _all_title_variants(q)

        def in_title(*keywords):
            """Herhangi bir başlık varyantında herhangi bir anahtar kelime geçiyor mu?"""
            return any(any(k in t for k in keywords) for t in variants)

        # Soru tipi belirlenemiyorsa (None) — sayısal değerse puan, değilse metin say
        if qtype in (2,) or (qtype is None and not _safe_float(val)):
            # Metin sorusu
            if in_title("isim", "soyisim", "ad soyad", "adınız", "name", "müşteri", "musteri"):
                musteri_adi = str(val).strip()
        elif qtype in (3,) or (qtype is None and not _safe_float(val)):
            # Seçim sorusu
            if in_title("acente", "agency", "aracı"):
                acente_adi = str(val).strip()
        else:
            # Yıldız/puan sorusu (type==7) veya sayısal
            if qtype == 7 or _safe_float(val) is not None:
                v = _safe_float(val)
                if in_title("genel memnuniyet", "genel olarak memnun", "genel değerlendirme",
                            "genel izlenim", "genel puan", "overall satisfaction",
                            "overall", "genel olarak", "general satisfaction", "satisfaction"):
                    genel_memnuniyet = v
                elif in_title("rehber", "guide", "tour guide", "tur rehber", "tur rehberi"):
                    rehber_puani = v
                elif in_title("otobüs", "otobus", "araç konfor", "arac konfor",
                              "bus", "coach", "vehicle comfort", "vehicle"):
                    otobus_puani = v
                elif in_title("şoför", "sofor", "şofor", "sürücü", "surucu",
                              "driver", "chauffeur"):
                    sofor_puani = v
                elif in_title("operasyon", "organizasyon", "örgütlen", "orgutlen",
                              "operation", "organization", "organisation"):
                    operasyon_puani = v
                elif in_title("transfer", "havaalani", "havaalanı", "havalimanı",
                              "airport", "airport transfer", "shuttle"):
                    transfer_puani = v
                elif in_title("ekstra tur", "isteğe bağlı", "isteğe bagli",
                              "optional", "seçmeli", "extra tour", "optional tour"):
                    ekstra_tur_puani = v
                elif in_title("program", "tur programı", "tur programi",
                              "itinerary", "tour program"):
                    program_puani = v
                elif in_title("tavsiye", "öneri", "önerir", "onerir",
                              "recommend", "would you recommend"):
                    tavsiye_puan = v
                elif in_title("otel", "hotel", "accommodation", "konaklama",
                             "otelinden memnun", "memnun kaldınız"):
                    raw_t = _str_field(q.get("title"))
                    _, _, display_key = _parse_hotel_label(raw_t)
                    if v is not None and display_key:
                        otel_puanlari[display_key] = v
            # qtype==2 veya 3 ama sayısal değer içerebilir — acente/ad kontrolü
            if qtype == 2:
                if in_title("isim", "soyisim", "ad soyad", "adınız", "name", "müşteri", "musteri"):
                    musteri_adi = str(val).strip()
            elif qtype == 3:
                if in_title("acente", "agency", "aracı"):
                    acente_adi = str(val).strip()

    # Genel puan: adanmış soru varsa onu kullan
    if genel_memnuniyet is not None:
        genel_puan = genel_memnuniyet
    else:
        sub_scores = [v for v in [
            rehber_puani, otobus_puani, sofor_puani, program_puani,
            operasyon_puani, transfer_puani, ekstra_tur_puani,
        ] + list(otel_puanlari.values()) if v is not None]
        genel_puan = round(sum(sub_scores) / len(sub_scores), 2) if sub_scores else None

    return {
        "musteri_adi":  musteri_adi,
        "acente_adi":   acente_adi,
        "rehber_adi":   "",
        "genel_puan":   genel_puan,
        "rehber_puani": rehber_puani,
        "puan_detay": {
            "oteller":          otel_puanlari,
            "otobus":           otobus_puani,
            "sofor":            sofor_puani,
            "program":          program_puani,
            "operasyon":        operasyon_puani,
            "transfer":         transfer_puani,
            "ekstra_tur":       ekstra_tur_puani,
            "genel_memnuniyet": genel_memnuniyet,
            "tavsiye":          tavsiye_puan,
        },
    }


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
        # Kalkış, anket oluşturmadan en fazla 14 gün sonra ya da 365 gün önce olabilir
        # (anket tur çıkışından birkaç gün önce oluşturulabiliyor)
        if -14 <= diff <= 365:
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

def _header_title(h) -> str:
    """
    Porsline header objesinden görünen başlık metnini çıkarır.

    Results-table header item formatı:
      {"id":3185441, "title":"Tur rehberimizin...", "col_type":1, "cell_type":"int", ...}
    Plain string header da desteklenir (geriye dönük uyumluluk).

    DİKKAT: _str_field() direkt dict üzerinde çağrılırsa dict'in ilk değerini
    (genellikle sayısal id'yi) döndürür — bu fonksiyon bunun önüne geçer.
    """
    if isinstance(h, dict):
        raw = h.get("title") or h.get("label") or h.get("name") or ""
        return _str_field(raw)
    return _str_field(h)


def _find_col(header: list, keywords: list[str]) -> Optional[int]:
    """Header listesinde anahtar kelime içeren ilk sütun indeksini döndürür."""
    for kw in keywords:
        for i, h in enumerate(header):
            if kw.lower() in _header_title(h).lower():
                return i
    return None


def _safe_float(val) -> Optional[float]:
    if val is None or str(val).strip() in ("", "-", "N/A"):
        return None
    try:
        return float(str(val).replace(",", "."))
    except ValueError:
        return None


def _parse_hotel_label(raw_title: str) -> tuple:
    """
    Otel sorusunu parse eder.

    Desteklenen format:
      "Osaka otelinden memnun kaldınız mı? APA Hotel Osaka"
       ^^^^^^^^ city                        ^^^^^^^^^^^^^^^^ hotel_label

    Döner: (city, hotel_label, display_key)
      - city        : "Osaka"
      - hotel_label : "APA Hotel Osaka"  (soru işaretinden sonraki kısım)
      - display_key : hotel_label dolu ise hotel_label, değilse city adı

    Eğer format eşleşmezse ham başlığı display_key olarak kullanır.
    """
    raw = (raw_title or "").strip()

    # "X otelinden memnun kaldınız mı? Hotel Adı" — Türkçe ana format
    m = re.match(
        r'^(.+?)\s+otelinden\s+memnun\s+kaldınız\s+mı\??\s*(.*)',
        raw, re.IGNORECASE
    )
    if m:
        city       = m.group(1).strip()
        hotel_lbl  = m.group(2).strip()
        display    = hotel_lbl if hotel_lbl else city
        return city, hotel_lbl, display[:80]

    # Genel fallback: "?" varsa sonrası hotel adı, öncesi başlık
    if "?" in raw:
        parts      = raw.split("?", 1)
        hotel_lbl  = parts[1].strip()
        city       = re.sub(r'\s+otelinden.*$', '', parts[0], flags=re.IGNORECASE).strip()
        display    = hotel_lbl if hotel_lbl else city
        return city, hotel_lbl, display[:80]

    # Son çare: ham başlığın ilk 60 karakteri
    return "", "", raw[:60]


def _extract_guide_name(header: list) -> Optional[str]:
    """
    Rehber sorusunun başlığından rehber adını çıkarır.
    Örn: "Tur rehberimizin bilgi ve ilgisinden memnun kaldınız mı?  (Derya Iberi)"
    → "Derya Iberi"
    """
    for h in header:
        h_str = _header_title(h)
        if "rehber" in h_str.lower():
            m = re.search(r'\(([^)]+)\)', h_str)
            if m:
                return m.group(1).strip()
    return None


def parse_response_row(header: list, row) -> dict:
    """
    Tek bir yanıt satırını alanlarına göre parse eder.
    Porsline results-table'dan gelen iki formatı destekler:
      - Liste formatı: ["val1", "val2", ...]
      - Dict formatı: {"responder_id": 123, "responder_code": "abc", "data": ["val1", ...]}
    Header item'lar plain string veya {"id":...,"title":"...","col_type":...} dict olabilir.
    """
    # Dict formatında data listesini çıkar
    if isinstance(row, dict):
        actual_row = row.get("data") or []
    else:
        actual_row = list(row) if row else []

    def get(idx):
        if idx is None or idx >= len(actual_row):
            return None
        return actual_row[idx]

    # Sütun indeksleri
    i_musteri     = _find_col(header, ["isim", "ad soyad", "ad-soyad", "name"])
    i_acente      = _find_col(header, ["acente"])
    i_rehber      = _find_col(header, ["rehber"])
    i_otobus      = _find_col(header, ["otobüs", "otobus", "araç konfor", "arac konfor"])
    i_sofor       = _find_col(header, ["şoför", "sofor", "şofor", "sürücü", "surucu"])
    i_program     = _find_col(header, ["program", "tur programı", "tur programi"])
    i_operasyon   = _find_col(header, ["operasyon", "organizasyon", "örgütlen", "orgutlen"])
    i_transfer    = _find_col(header, ["transfer", "havaalani", "havaalanı", "havalimanı", "havalimanı servis"])
    i_ekstra_tur  = _find_col(header, ["ekstra tur", "isteğe bağlı", "isteğe bagli", "optional", "seçmeli"])
    i_genel_memnuniyet = _find_col(header, [
        "genel memnuniyet", "genel olarak memnun", "genel değerlendirme",
        "genel izlenim", "genel puan", "overall", "genel olarak",
    ])
    i_tavsiye = _find_col(header, ["tavsiye", "öneri", "önerir misiniz", "onerir misiniz"])

    # Otel puanları: "otel", "hotel" veya şehir/ülke adı içeren tüm sütunlar
    otel_puanlari: dict = {}
    otel_keywords = [
        "otel", "hotel",
        "barcelona", "barselona", "valencia", "granada", "sevilla", "madrid",
        "roma", "floransa", "venedik", "napoli", "milano", "sicilya",
        "paris", "nice", "lyon",
        "amsterdam", "bruksel",
        "berlin", "frankfurt", "munih", "münchen",
        "zurich", "zürih", "cenevre", "interlaken",
        "viyana", "salzburg",
        "prag", "budapes", "varso",
        "lizbon", "porto",
        "londra",
        "atina", "selanik", "santorini",
        "tiflis", "baku", "bakü", "erivan",
        "tokyo", "osaka", "bangkok", "bali", "singapur",
        "dubai", "kahire", "marakes",
        "istanbul", "ankara", "bodrum", "antalya", "kapadokya",
    ]
    for i, h in enumerate(header):
        h_str = _header_title(h).lower()
        if any(kw in h_str for kw in otel_keywords):
            # rehber, program, operasyon sütunlarıyla çakışmayı önle
            if any(kw in h_str for kw in ["rehber", "program", "operasyon", "transfer"]):
                continue
            v = _safe_float(get(i))
            if v is not None:
                raw_t = _header_title(header[i])
                _, _, display_key = _parse_hotel_label(raw_t)
                if display_key:
                    otel_puanlari[display_key] = v

    # Rehber adı header'dan
    rehber_adi = _extract_guide_name(header)

    # Sayısal puanlar
    rehber_puani   = _safe_float(get(i_rehber))
    otobus_puani   = _safe_float(get(i_otobus))
    sofor_puani    = _safe_float(get(i_sofor))
    program_puani  = _safe_float(get(i_program))
    operasyon_puani= _safe_float(get(i_operasyon))
    transfer_puani = _safe_float(get(i_transfer))
    ekstra_tur_puani = _safe_float(get(i_ekstra_tur))
    genel_memnuniyet = _safe_float(get(i_genel_memnuniyet))
    tavsiye_puani  = _safe_float(get(i_tavsiye))   # skala ise float, yoksa metin

    # Genel puan: adanmış "genel memnuniyet" sorusu varsa onu kullan
    # yoksa tüm sayısal alt puanların ortalamasını al
    if genel_memnuniyet is not None:
        genel_puan = genel_memnuniyet
    else:
        sub_scores = [v for v in [
            rehber_puani, otobus_puani, sofor_puani, program_puani,
            operasyon_puani, transfer_puani, ekstra_tur_puani,
        ] + list(otel_puanlari.values()) if v is not None]
        genel_puan = round(sum(sub_scores) / len(sub_scores), 2) if sub_scores else None

    return {
        "musteri_adi":  str(get(i_musteri) or "").strip(),
        "acente_adi":   str(get(i_acente)  or "").strip(),
        "rehber_adi":   rehber_adi or "",
        "genel_puan":   genel_puan,
        "rehber_puani": rehber_puani,
        "puan_detay": {
            "oteller":          otel_puanlari,
            "otobus":           otobus_puani,
            "sofor":            sofor_puani,
            "program":          program_puani,
            "operasyon":        operasyon_puani,
            "transfer":         transfer_puani,
            "ekstra_tur":       ekstra_tur_puani,
            "genel_memnuniyet": genel_memnuniyet,
            "tavsiye":          tavsiye_puani,
        },
    }


# ── Otel adı çıkarma ─────────────────────────────────────────────────────────

# Soru metni → temiz şehir/otel adı eşleştirmesi
_OTEL_SEHIR_MAP: list[tuple[str, str]] = [
    # İspanya
    ("barcelona", "Barcelona"), ("barselona", "Barcelona"),
    ("valencia", "Valencia"), ("granada", "Granada"),
    ("sevilla", "Sevilla"), ("madrid", "Madrid"),
    ("endulus", "Endülüs"), ("endülüs", "Endülüs"),
    # İtalya
    ("roma", "Roma"), ("floransa", "Floransa"),
    ("venedik", "Venedik"), ("napoli", "Napoli"),
    ("milano", "Milano"), ("sicilya", "Sicilya"),
    # Fransa
    ("paris", "Paris"), ("nice", "Nice"), ("lyon", "Lyon"),
    # Benelux
    ("amsterdam", "Amsterdam"), ("bruksel", "Brüksel"),
    # Almanya
    ("berlin", "Berlin"), ("frankfurt", "Frankfurt"),
    ("munih", "Münih"), ("münchen", "Münih"), ("hamburg", "Hamburg"),
    # İsviçre
    ("zurich", "Zürih"), ("zürih", "Zürih"), ("cenevre", "Cenevre"),
    ("interlaken", "Interlaken"), ("luzern", "Luzern"),
    # Avusturya
    ("viyana", "Viyana"), ("salzburg", "Salzburg"),
    # Doğu Avrupa
    ("prag", "Prag"), ("budapes", "Budapeşte"),
    ("varso", "Varşova"), ("varşo", "Varşova"),
    ("bratislava", "Bratislava"), ("krakow", "Krakow"),
    # Portekiz
    ("lizbon", "Lizbon"), ("porto", "Porto"),
    # İngiltere
    ("londra", "Londra"), ("edinburgh", "Edinburgh"),
    # Balkanlar
    ("belgrad", "Belgrad"), ("dubrovnik", "Dubrovnik"),
    ("zagreb", "Zagreb"), ("budva", "Budva"),
    ("tiran", "Tiran"), ("skopye", "Üsküp"),
    # Yunanistan
    ("atina", "Atina"), ("selanik", "Selanik"),
    ("santorini", "Santorini"), ("rodos", "Rodos"),
    # Kafkasya
    ("tiflis", "Tiflis"), ("baku", "Bakü"), ("bakü", "Bakü"),
    ("erivan", "Erivan"),
    # Uzak Doğu
    ("tokyo", "Tokyo"), ("osaka", "Osaka"), ("kyoto", "Kyoto"),
    ("bangkok", "Bangkok"), ("bali", "Bali"),
    ("singapur", "Singapur"), ("vietnam", "Vietnam"),
    # Orta Doğu / Afrika
    ("dubai", "Dubai"), ("abu dabi", "Abu Dabi"),
    ("kahire", "Kahire"), ("luksor", "Luksor"),
    ("marakes", "Marakeş"), ("marakeş", "Marakeş"),
    # Amerika
    ("new york", "New York"), ("los angeles", "Los Angeles"),
    ("miami", "Miami"), ("toronto", "Toronto"),
    # Türkiye
    ("istanbul", "İstanbul"), ("ankara", "Ankara"),
    ("bodrum", "Bodrum"), ("antalya", "Antalya"),
    ("kapadokya", "Kapadokya"), ("pamukkale", "Pamukkale"),
    # Fas
    ("kazablanka", "Kazablanka"), ("fes", "Fes"), ("rabat", "Rabat"),
    # İskandinav
    ("oslo", "Oslo"), ("stockholm", "Stockholm"),
    ("kopenhag", "Kopenhag"), ("helsinki", "Helsinki"),
]


def extract_otel_adi(puan_detay: dict) -> str:
    """
    puan_detay['oteller'] dict'inden temiz şehir/otel isim listesi çıkarır.

    Örn:  {"Barselona otelinizden memnun kaldınız mı": 4.0, "Madrid Oteli": 3.5}
          → "Barcelona, Madrid"

    Dönen string DB'ye `otel_adi` kolonuna yazılır;  ILIKE ile aranır.
    """
    oteller: dict = puan_detay.get("oteller") or {}
    if not oteller:
        return ""

    found: list[str] = []
    for key in oteller.keys():
        key_low = key.lower()
        matched = False
        for kw, city in _OTEL_SEHIR_MAP:
            if kw in key_low:
                if city not in found:
                    found.append(city)
                matched = True
                break
        if not matched:
            # Şehir bulunamadı — ilk 3 kelimeyi al (temiz görünüm)
            words = key.split()[:3]
            short = " ".join(words).strip("?").strip()[:30]
            if short and short not in found:
                found.append(short)
    return ", ".join(found)


# ── Bölge tespiti ─────────────────────────────────────────────────────────────

_BOLGELER: dict[str, list[str]] = {
    "Japonya":      ["japon", "tokyo", "osaka", "kyoto"],
    "İtalya":       ["italya", "roma", "venedik", "floransa", "napoli", "milano", "sicilya"],
    "İspanya":      ["ispanya", "madrid", "barselona", "sevilla", "granada", "endulus", "endülüs", "katalonya"],
    "Fransa":       ["fransa", "paris", "nice", "lyon", "strasbourg", "versay", "normandiya"],
    "Benelux":      ["benelux", "amsterdam", "bruksel", "brüssel", "belcika", "belçika", "hollanda", "luksemburg", "lüksemburg"],
    "Rusya":        ["rusya", "moskova", "petersburg", "st.pete"],
    "Fas":          ["fas", "marakes", "marakeş", "kazablanka", "fes", "agadir", "rabat"],
    "Yunanistan":   ["yunanistan", "atina", "selanik", "rodos", "girit", "santorini", "mikonos"],
    "Portekiz":     ["portekiz", "lizbon", "porto"],
    "İngiltere":    ["ingiltere", "londra", "manchester", "edinburgh"],
    "Almanya":      ["almanya", "berlin", "frankfurt", "munih", "münchen", "hamburg", "dusseldorf"],
    "İsviçre":      ["isviçre", "svicre", "zurich", "zürih", "cenevre", "bern", "interlaken", "luzern"],
    "Avusturya":    ["avusturya", "viyana", "salzburg", "innsbruck"],
    "Balkanlar":    ["balkan", "belgrad", "zagreb", "dubrovnik", "budva", "karadag", "karadağ", "bosna", "hersek", "makedonya", "arnavutluk", "slovenya"],
    "Doğu Avrupa":  ["prag", "budapeşte", "budapes", "varsova", "varşova", "bratislava", "cek", "çek", "polonya", "macaristan", "slovakya", "romanya", "bulgaristan"],
    "İskandinav":   ["norvec", "norveç", "isvec", "isveç", "danimarka", "finlandiya", "oslo", "stockholm", "kopenhag", "helsinki", "bergen", "fjord"],
    "Uzak Doğu":    ["tayland", "bali", "singapur", "vietnam", "endonezya", "kamboçya", "kambocya", "malezya", "filipin", "uzakdogu", "uzakdoğu"],
    "Mısır":        ["misir", "mısır", "kahire", "luksor", "hurgada", "sharm", "nil"],
    "Dubai":        ["dubai", "abu dabi", "bae", "katar", "kuvait", "bahreyn", "umman"],
    "Amerika":      ["amerika", "new york", "los angeles", "miami", "kanada", "toronto", "las vegas", "chicago"],
    "Güney Amerika":["brezilya", "arjantin", "peru", "kolombiya", "şili", "santiago", "buenos aires", "rio"],
    "Afrika":       ["kenya", "tanzanya", "güney afrika", "cape town", "safarı", "safari", "zanzibar"],
    "Orta Asya":    ["orta asya", "özbekistan", "kazakistan", "türkmenistan", "tacikistan", "semerkant", "buhara"],
    "Kafkasya":     ["gürcistan", "ermeni", "azerbaycan", "tiflis", "bakü", "erivan", "kazbek"],
    "Türkiye İç":   ["kapadokya", "efes", "pamukkale", "antalya", "bodrum", "fethiye", "ege", "karadeniz", "dogu anadolu", "doğu anadolu"],
}


def detect_bolge(tur_adi: str, tags: list = None) -> str:
    """
    Tur adı ve/veya Porsline tag listesinden bölge tespit eder.
    Tags: [{"id": 210, "label": "Japonya"}, ...] formatında.
    """
    # Önce tag'lardan bak (en güvenilir)
    if tags:
        for tag in tags:
            label = str(tag.get("label") or "").strip()
            if not label:
                continue
            label_norm = label.lower()
            for bolge, keywords in _BOLGELER.items():
                bolge_norm = bolge.lower()
                if label_norm == bolge_norm or label_norm in keywords:
                    return bolge

    # Tur adından keyword tarama
    if tur_adi:
        tur_norm = tur_adi.lower()
        # Aksanları kaldır
        import unicodedata
        tur_norm_ascii = "".join(
            c for c in unicodedata.normalize("NFKD", tur_norm)
            if not unicodedata.combining(c)
        )
        for bolge, keywords in _BOLGELER.items():
            for kw in keywords:
                kw_ascii = "".join(
                    c for c in unicodedata.normalize("NFKD", kw.lower())
                    if not unicodedata.combining(c)
                )
                if kw_ascii in tur_norm_ascii or kw.lower() in tur_norm:
                    return bolge

    return ""
