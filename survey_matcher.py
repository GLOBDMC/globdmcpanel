"""
Historical Survey Matching Engine
──────────────────────────────────
Sıfır dış bağımlılık — sadece Python stdlib kullanır.

Puanlama sistemi (toplam 100):
  • Tur adı benzerliği  : 0-50 puan  (difflib SequenceMatcher + token overlap)
  • Kalkış tarihi       : 0-25 puan  (gün farkı bazlı)
  • Rehber adı          : 0-15 puan  (token eşleşmesi)
  • Destinasyon         : 0-10 puan  (anahtar kelime eşleşmesi)

Karar eşikleri:
  ≥ 80 → otomatik eşleşme  (match_status = 'matched')
  50-79 → manuel inceleme   (match_status = 'review')
  < 50 → eşleşmedi          (match_status = 'review', düşük skor)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, date
from difflib import SequenceMatcher
from typing import List, Optional


# ── Eşik sabitleri ────────────────────────────────────────────────────────────
THRESHOLD_AUTO   = 80   # Bu ve üzeri → otomatik eşleşme
THRESHOLD_REVIEW = 50   # Bu ve üzeri → manuel inceleme gerekli
# Altı → unmatched olarak işaretlenir ama yine review queue'ya düşer


# ── Normalize yardımcıları ────────────────────────────────────────────────────

def _remove_accents(text: str) -> str:
    """Türkçe karakterleri ve aksanları ASCII'ye dönüştürür."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


# Tur adlarında genel geçer "gürültü" kelimeleri
_NOISE_WORDS = {
    "tur", "turu", "turu,", "seyahat", "tatil", "gece", "gun", "gunluk",
    "comfort", "classic", "lux", "deluxe", "ozel", "super", "premium",
    "paket", "package", "holiday", "tour",
}

# Destinasyon anahtar kelimeleri (kaba bir harita)
_DEST_KEYWORDS: dict[str, list[str]] = {
    "benelux":    ["belcika", "hollanda", "amsterdam", "bruksel", "luksemburg"],
    "italya":     ["roma", "venedik", "floransa", "napoli", "milano"],
    "ispanya":    ["madrid", "barselona", "sevilla", "granada"],
    "fransa":     ["paris", "nice", "lyon", "strasbourg"],
    "yunanistan": ["atina", "selanik", "rodos", "girit"],
    "portekiz":   ["lizbon", "porto"],
    "ingiltere":  ["londra", "manchester"],
    "almanya":    ["berlin", "frankfurt", "munih", "hamburg"],
    "avusturya":  ["viyana", "salzburg", "innsbruck"],
    "iskandinavya": ["oslo", "kopenhag", "stockholm", "helsinki"],
    "balkanlar":  ["belgrad", "zagreb", "ljubljana", "sofya", "dubrovnik", "budva"],
    "dogu_avrupa": ["prag", "varşova", "budapeşte", "viyana", "bratislava"],
    "uzakdogu":   ["japonya", "tokyo", "tayland", "bali", "singapur", "vietnam"],
    "misir":      ["kahire", "luksor", "hurgada"],
    "fas":        ["kazablanka", "marakes", "fes"],
}


def normalize(text) -> str:
    """Metni karşılaştırma için normalleştirir."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    t = text.lower().strip()
    t = _remove_accents(t)
    t = re.sub(r"[^\w\s]", " ", t)   # noktalama → boşluk
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokenize(text: str) -> set[str]:
    """Normalize edilmiş metni anlamlı token setine çevirir."""
    tokens = set(normalize(text).split())
    return tokens - _NOISE_WORDS


def _parse_date(s: str) -> Optional[date]:
    """Çeşitli tarih formatlarını parse eder."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


# ── Puan hesaplayıcılar ───────────────────────────────────────────────────────

def name_score(survey_name: str, tour_name: str) -> int:
    """
    Tur adı benzerlik puanı (0-50).
    İki metrik kombinasyonu:
      • SequenceMatcher oranı (karakter düzeyinde)
      • Token örtüşmesi (kelime düzeyinde)
    """
    if not survey_name or not tour_name:
        return 0

    a = normalize(survey_name)
    b = normalize(tour_name)

    # Karakter düzeyinde benzerlik
    char_ratio = SequenceMatcher(None, a, b).ratio()

    # Token örtüşmesi
    ta = tokenize(survey_name)
    tb = tokenize(tour_name)
    if ta and tb:
        overlap = len(ta & tb) / max(len(ta), len(tb))
    else:
        overlap = 0.0

    # Ağırlıklı kombinasyon: 60% karakter + 40% token
    combined = (char_ratio * 0.60) + (overlap * 0.40)
    return round(combined * 50)


def date_score(survey_date: str, tour_date: str) -> int:
    """
    Kalkış tarihi yakınlık puanı (0-25).
    Gün farkına göre azalan puan.
    """
    d1 = _parse_date(survey_date)
    d2 = _parse_date(tour_date)

    if not d1 or not d2:
        return 0

    diff = abs((d1 - d2).days)
    if diff == 0:    return 25
    if diff <= 2:    return 22
    if diff <= 5:    return 18
    if diff <= 10:   return 12
    if diff <= 20:   return 6
    if diff <= 30:   return 2
    return 0


def guide_score(survey_guide: str, tour_guide: str) -> int:
    """
    Rehber adı eşleşme puanı (0-15).
    Token bazlı: soyadı veya adı eşleşirse puan verir.
    """
    if not survey_guide or not tour_guide:
        return 0

    sg = tokenize(survey_guide)
    tg = tokenize(tour_guide)

    if not sg or not tg:
        return 0

    overlap = len(sg & tg)
    if overlap >= 2:   return 15  # Hem ad hem soyad eşleşti
    if overlap == 1:   return 8   # Sadece bir token eşleşti

    # Token eşleşmese bile karakter benzerliği dene
    char_ratio = SequenceMatcher(None, normalize(survey_guide), normalize(tour_guide)).ratio()
    if char_ratio >= 0.85:
        return 12
    if char_ratio >= 0.70:
        return 6

    return 0


def destination_score(survey_dest: str, tour_name: str) -> int:
    """
    Destinasyon eşleşme puanı (0-10).
    Survey destinasyonu ile tur adındaki destinasyon kelimelerini karşılaştırır.
    """
    if not survey_dest or not tour_name:
        return 0

    dest_norm = normalize(survey_dest)
    tour_norm = normalize(tour_name)

    # Doğrudan token eşleşmesi
    dest_tokens = tokenize(survey_dest)
    tour_tokens = tokenize(tour_name)
    if dest_tokens & tour_tokens:
        return 10

    # Bilinen destinasyon eş anlamlıları
    for dest_key, synonyms in _DEST_KEYWORDS.items():
        key_norm = normalize(dest_key)
        if key_norm in dest_norm or any(s in dest_norm for s in synonyms):
            # Bu destinasyon tur adında geçiyor mu?
            if key_norm in tour_norm or any(s in tour_norm for s in synonyms):
                return 10
            # Yakın ama tam değil
            if any(s in tour_norm for s in [dest_key[:4]]):
                return 5

    # Karakter benzerliği fallback
    ratio = SequenceMatcher(None, dest_norm, tour_norm).ratio()
    if ratio >= 0.6:
        return 5

    return 0


# ── Ana veri yapıları ─────────────────────────────────────────────────────────

@dataclass
class SurveyRecord:
    """Import edilecek ham anket verisi."""
    tur_adi:       str
    kalkis_tarihi: str
    rehber_adi:    str     = ""
    destinasyon:   str     = ""
    musteri_adi:   str     = ""
    acente_adi:    str     = ""
    survey_date:   str     = ""
    genel_puan:    Optional[float] = None
    rehber_puani:  Optional[float] = None
    yorum:         str     = ""
    # Ham kayıt referans alanları (import'a yardımcı)
    kaynak_satir:  int     = 0


@dataclass
class TourRecord:
    """Mevcut turlar tablosundan gelen tur verisi."""
    id:            int
    jt_kodu:       str
    tur_adi:       str
    kalkis_tarihi: str
    rehber:        str     = ""


@dataclass
class MatchCandidate:
    """Bir anket için tek bir tur adayı ve puanı."""
    tour:            TourRecord
    total_score:     int
    name_s:          int
    date_s:          int
    guide_s:         int
    destination_s:   int


@dataclass
class MatchResult:
    """Bir anketin eşleştirme sonucu."""
    survey:          SurveyRecord
    best_match:      Optional[MatchCandidate] = None
    all_candidates:  List[MatchCandidate] = field(default_factory=list)

    @property
    def confidence(self) -> int:
        return self.best_match.total_score if self.best_match else 0

    @property
    def status(self) -> str:
        if not self.best_match:
            return "review"
        if self.confidence >= THRESHOLD_AUTO:
            return "matched"
        return "review"

    @property
    def method(self) -> str:
        if not self.best_match:
            return "unmatched"
        if self.confidence >= THRESHOLD_AUTO:
            return "auto_high"
        if self.confidence >= THRESHOLD_REVIEW:
            return "auto_medium"
        return "auto_low"


# ── Ana eşleştirme motoru ─────────────────────────────────────────────────────

class SurveyMatcher:
    """
    Verilen anket listesini mevcut turlarla eşleştirir.
    """

    def __init__(self, tours: List[TourRecord]):
        self.tours = tours

    def match_one(self, survey: SurveyRecord) -> MatchResult:
        """Tek bir anketi tüm turlarla karşılaştırır, en iyi adayı döndürür."""
        candidates: List[MatchCandidate] = []

        for tour in self.tours:
            n_s = name_score(survey.tur_adi, tour.tur_adi)
            d_s = date_score(survey.kalkis_tarihi, tour.kalkis_tarihi)
            g_s = guide_score(survey.rehber_adi, tour.rehber)
            x_s = destination_score(survey.destinasyon, tour.tur_adi)

            total = n_s + d_s + g_s + x_s

            # Çok düşük skorları listenin kirlenmemesi için elerle
            if total >= 15:
                candidates.append(MatchCandidate(
                    tour=tour,
                    total_score=total,
                    name_s=n_s,
                    date_s=d_s,
                    guide_s=g_s,
                    destination_s=x_s,
                ))

        # En yüksek skora göre sırala
        candidates.sort(key=lambda c: c.total_score, reverse=True)

        best = candidates[0] if candidates else None

        # Eğer ikinci en iyi ile fark < 5 ise belirsizlik var → review
        # (çok benzer iki tur varsa otomatik eşleşme yapma)
        if best and len(candidates) >= 2:
            second = candidates[1]
            if (best.total_score - second.total_score) < 5 and best.total_score < THRESHOLD_AUTO:
                best = best  # Yine de geç ama status review olacak

        return MatchResult(
            survey=survey,
            best_match=best,
            all_candidates=candidates[:5],  # En fazla 5 aday sakla
        )

    def match_all(self, surveys: List[SurveyRecord]) -> List[MatchResult]:
        """Tüm anket listesini eşleştirir."""
        return [self.match_one(s) for s in surveys]


# ── CSV parse yardımcısı ──────────────────────────────────────────────────────

# Beklenen CSV kolon adları (case-insensitive, çoklu alternatif)
_COL_TOUR    = ["tur adi", "tur adı", "grup adi", "grup adı", "tur", "tour"]
_COL_DATE    = ["kalkis tarihi", "kalkış tarihi", "hareket tarihi", "tarih", "departure"]
_COL_GUIDE   = ["rehber", "rehber adi", "rehber adı", "guide"]
_COL_DEST    = ["destinasyon", "bolge", "bölge", "destination", "ulke", "ülke"]
_COL_CUST    = ["musteri", "müşteri", "musteri adi", "müşteri adı", "customer"]
_COL_AGENCY  = ["acente", "agency", "acente adi", "acente adı"]
_COL_SDATE   = ["anket tarihi", "survey date", "kayit tarihi", "kayıt tarihi"]
_COL_SCORE   = ["puan", "skor", "genel puan", "score", "rating", "not", "değerlendirme"]
_COL_GUIDESC = ["rehber puani", "rehber puanı", "rehber notu", "guide score"]
_COL_COMMENT = ["yorum", "comment", "aciklama", "açıklama"]


def _find_col(headers_lower: dict, candidates: list) -> Optional[str]:
    for c in candidates:
        if c in headers_lower:
            return headers_lower[c]
    return None


def parse_csv_rows(rows: list[dict]) -> List[SurveyRecord]:
    """
    gspread.get_all_records() veya csv.DictReader çıktısını parse eder.
    Eksik alanlar None/boş kalır, zorunlu alan (tur_adi) olmayan satırlar atlanır.
    """
    if not rows:
        return []

    headers_lower = {k.lower().strip(): k for k in rows[0].keys()}

    col_tour   = _find_col(headers_lower, _COL_TOUR)
    col_date   = _find_col(headers_lower, _COL_DATE)
    col_guide  = _find_col(headers_lower, _COL_GUIDE)
    col_dest   = _find_col(headers_lower, _COL_DEST)
    col_cust   = _find_col(headers_lower, _COL_CUST)
    col_agency = _find_col(headers_lower, _COL_AGENCY)
    col_sdate  = _find_col(headers_lower, _COL_SDATE)
    col_score  = _find_col(headers_lower, _COL_SCORE)
    col_guidesc= _find_col(headers_lower, _COL_GUIDESC)
    col_comment= _find_col(headers_lower, _COL_COMMENT)

    if not col_tour:
        raise ValueError(
            f"CSV'de tur adı kolonu bulunamadı. "
            f"Beklenen kolon adları: {_COL_TOUR}. "
            f"Bulunan: {list(headers_lower.values())}"
        )

    records: List[SurveyRecord] = []
    for i, row in enumerate(rows, start=2):
        tur_adi = str(row.get(col_tour, "")).strip()
        if not tur_adi:
            continue

        def get(col):
            if col is None:
                return ""
            return str(row.get(col, "")).strip()

        def get_float(col):
            v = get(col)
            if not v:
                return None
            try:
                return float(v.replace(",", "."))
            except ValueError:
                return None

        records.append(SurveyRecord(
            tur_adi=tur_adi,
            kalkis_tarihi=get(col_date),
            rehber_adi=get(col_guide),
            destinasyon=get(col_dest),
            musteri_adi=get(col_cust),
            acente_adi=get(col_agency),
            survey_date=get(col_sdate),
            genel_puan=get_float(col_score),
            rehber_puani=get_float(col_guidesc),
            yorum=get(col_comment),
            kaynak_satir=i,
        ))

    return records
