"""
gordios_parser.py
-----------------
Gordios'tan indirilen Tur Planı PDF'ini parse eder.
pdfplumber kullanır.
"""
import re
import logging
from typing import Optional

logger = logging.getLogger("globdmc.gordios.parser")


def parse_tour_program(pdf_bytes: bytes) -> dict:
    """
    PDF bytes'ından günlük tur programını çıkarır.

    Returns:
        {
          "baslik":   str,           # Tur başlığı (ilk satırdan)
          "gunler":   list[dict],    # [{gun_no, baslik, detay}, ...]
          "raw_text": str,           # Ham metin (debug için)
        }
    """
    result = {"baslik": "", "gunler": [], "raw_text": ""}
    if not pdf_bytes:
        return result

    try:
        import pdfplumber
        import io

        full_text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ""
                if t.strip():
                    full_text_parts.append(t)

        full_text = "\n".join(full_text_parts)
        result["raw_text"] = full_text

        # Başlık: ilk anlamlı satır
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        if lines:
            result["baslik"] = lines[0]

        # Günlük program satırlarını çıkar
        # Pattern: "1. GÜN", "2. Gün", "GÜN 1", "DAY 1" vs.
        result["gunler"] = _extract_days(full_text)

        logger.info("[gordios.parser] %d gün çıkarıldı", len(result["gunler"]))

    except ImportError:
        logger.error("pdfplumber kurulu değil")
        result["raw_text"] = "(pdfplumber yok)"
    except Exception as e:
        logger.error("[gordios.parser] parse hatası: %s", e, exc_info=True)
        result["raw_text"] = f"(hata: {e})"

    return result


def _extract_days(text: str) -> list:
    """
    Metinden gün gün programı çıkarır.
    Türkçe PDF formatları: "1. GÜN", "GÜN 1 –", "1. GÜN –", "1.GÜN"
    """
    # Gün başlıkları için regex
    day_pattern = re.compile(
        r'(?:^|\n)\s*'
        r'(?:'
        r'(\d{1,2})\s*[.\-–:)]\s*GÜN'    # "1. GÜN"
        r'|GÜN\s*(\d{1,2})'               # "GÜN 1"
        r'|(\d{1,2})\s*\.\s*DAY'          # "1. DAY"
        r')',
        re.IGNORECASE | re.MULTILINE
    )

    matches = list(day_pattern.finditer(text))
    if not matches:
        # Alternatif: sadece sayısal prefix ile başlayan bölümler
        day_pattern2 = re.compile(
            r'(?:^|\n)\s*(\d{1,2})[.)\-]\s+[A-ZÇĞİÖŞÜa-zçğışöşü]',
            re.MULTILINE
        )
        matches = list(day_pattern2.finditer(text))

    if not matches:
        return []

    gunler = []
    for i, m in enumerate(matches):
        # Gün numarası
        gun_no = int(m.group(1) or m.group(2) or m.group(3) or i + 1)

        # Başlık satırı (match'in bulunduğu satır)
        start = m.start()
        line_end = text.find("\n", m.end())
        baslik_line = text[start:line_end].strip() if line_end > 0 else text[start:].strip()
        baslik_line = re.sub(r'^\s*\d+\s*[.\-–:)]\s*GÜN\s*[–-]?\s*', '', baslik_line, flags=re.I).strip()
        baslik_line = re.sub(r'^GÜN\s*\d+\s*[–-]?\s*', '', baslik_line, flags=re.I).strip()

        # Detay: bu gün başlığı ile bir sonraki gün başlığı arasındaki metin
        detail_start = line_end + 1 if line_end > 0 else m.end()
        detail_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        detay = text[detail_start:detail_end].strip()

        # Çok uzun detayları kırp (görüntü için)
        if len(detay) > 2000:
            detay = detay[:2000] + "…"

        gunler.append({
            "gun_no": gun_no,
            "baslik": baslik_line or f"{gun_no}. Gün",
            "detay":  detay,
        })

    return gunler
