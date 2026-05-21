#!/usr/bin/env python3
"""
Porsline Bulk Sync — Tek Seferlik Tarihsel Veri İndirme Scripti
================================================================
Tüm Porsline anketlerini rate-limit'e takılmadan (yavaş, ama garantili)
çekip PostgreSQL'e yazar.

KULLANIM:
    python porsline_bulk_sync.py                   # tümünü sync et
    python porsline_bulk_sync.py --force            # DB sayısı eşleşse bile yeniden çek
    python porsline_bulk_sync.py --survey 12345     # tek anket
    python porsline_bulk_sync.py --interval 8       # istek arası saniye (varsayılan: 5)
    python porsline_bulk_sync.py --dry-run          # DB'ye yazmadan raporla

ORTAM DEĞİŞKENLERİ:
    DATABASE_URL      — PostgreSQL bağlantı URL'si  (zorunlu)
    PORSLINE_API_KEY  — Porsline API anahtarı        (zorunlu)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("porsline_bulk_sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("bulk_sync")

# ── SQLAlchemy ────────────────────────────────────────────────────────────────
try:
    from sqlalchemy import create_engine, text
except ImportError:
    log.error("sqlalchemy kurulu değil: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)

# ── Porsline servis ───────────────────────────────────────────────────────────
# porsline_service.py'nin bu scriptle aynı klasörde olması gerekiyor
sys.path.insert(0, os.path.dirname(__file__))
try:
    import porsline_service as _ps
    from porsline_service import (
        list_surveys,
        get_survey_detail,
        get_all_responses,
        parse_survey_title,
        parse_response_row,
    )
except ImportError as e:
    log.error("porsline_service import edilemedi: %s", e)
    sys.exit(1)

# ── survey_matcher (opsiyonel — yoksa eşleşme atlanır) ───────────────────────
try:
    from survey_matcher import SurveyMatcher, SurveyRecord
    _MATCHER_OK = True
except ImportError:
    _MATCHER_OK = False
    log.warning("survey_matcher bulunamadı — tur eşleşmesi yapılmayacak")


# ─────────────────────────────────────────────────────────────────────────────
#  Yardımcılar
# ─────────────────────────────────────────────────────────────────────────────

def _load_tours(engine):
    """DB'den tur listesi çeker (survey_matcher için)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, jt_kodu, tur_adi FROM turlar WHERE jt_kodu IS NOT NULL"
            )).fetchall()
        return [{"id": r[0], "jt_kodu": r[1], "tur_adi": r[2]} for r in rows]
    except Exception as e:
        log.warning("Tur listesi alınamadı: %s", e)
        return []


def _db_survey_counts(engine) -> dict[str, int]:
    """porsline_surveys tablosundaki kayıtlı yanıt sayılarını döndürür."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT porsline_survey_id, response_count FROM porsline_surveys"
            )).fetchall()
        return {str(r[0]): (r[1] or 0) for r in rows}
    except Exception:
        return {}


def _db_synced_responses(engine, sid: str) -> set[str]:
    """Belirli bir anketin DB'deki porsline_response_id'lerini döndürür."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT porsline_response_id FROM historical_surveys "
                "WHERE porsline_survey_id = :sid AND porsline_response_id != ''"
            ), {"sid": sid}).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────────────
#  Ana sync fonksiyonu
# ─────────────────────────────────────────────────────────────────────────────

def sync_survey(engine, sid: str, survey_meta: dict,
                tours: list, force: bool, dry_run: bool) -> dict:
    """
    Tek bir anketi Porsline'dan çekip DB'ye yazar.
    Döndürür: {"yeni": int, "atla": int, "hata": str|None}
    """
    title        = survey_meta.get("title") or survey_meta.get("name") or f"survey_{sid}"
    created_date = survey_meta.get("created_date") or survey_meta.get("created_at") or ""
    parsed       = parse_survey_title(title, created_date)

    # Yanıtları çek (rate-limit koruması porsline_service içinde)
    resp = get_all_responses(sid)
    if not resp["ok"]:
        return {"yeni": 0, "atla": 0, "hata": str(resp.get("hata", "bilinmiyor"))}

    rows   = resp["body"]
    header = resp["header"]
    total  = len(rows)

    if total == 0:
        return {"yeni": 0, "atla": 0, "hata": None}

    # Tur eşleştirme
    matched_tur_id  = None
    matched_jt_kodu = ""
    match_status    = "no_match"
    match_conf      = 0.0
    match_method    = ""

    if _MATCHER_OK and tours:
        try:
            matcher = SurveyMatcher(tours)
            sr = SurveyRecord(
                tur_adi=parsed["tur_adi"],
                kalkis_tarihi=parsed.get("kalkis_str") or "",
                rehber_adi=parsed.get("rehber_adi") or "",
            )
            mr = matcher.match_one(sr)
            if mr.best_match:
                matched_tur_id  = mr.best_match.tour.id
                matched_jt_kodu = mr.best_match.tour.jt_kodu
            match_status = mr.status
            match_conf   = mr.confidence
            match_method = mr.method
        except Exception as me:
            log.debug("Eşleştirme hatası (%s): %s", sid, me)

    if dry_run:
        log.info("  [DRY-RUN] %s → %d yanıt bulundu, DB'ye yazılmadı", title[:50], total)
        return {"yeni": total, "atla": 0, "hata": None}

    # Mevcut resp_id'leri al (duplicate skip için)
    existing_ids = _db_synced_responses(engine, sid)

    yeni = 0
    atla = 0
    with engine.connect() as conn:
        # porsline_surveys tablosunu güncelle
        conn.execute(text("""
            INSERT INTO porsline_surveys
                (porsline_survey_id, survey_title, parsed_tur_adi, parsed_kalkis,
                 parsed_havayolu, parsed_gece, matched_tur_id, matched_jt_kodu,
                 match_status, match_confidence, response_count, last_synced_at)
            VALUES (:sid,:title,:tur_adi,:kalkis,:havayolu,:gece,:tur_id,:jt,
                    :status,:conf,:rc,NOW())
            ON CONFLICT (porsline_survey_id) DO UPDATE SET
                survey_title=EXCLUDED.survey_title,
                last_synced_at=NOW(),
                response_count=EXCLUDED.response_count,
                match_status=EXCLUDED.match_status,
                match_confidence=EXCLUDED.match_confidence,
                matched_jt_kodu=EXCLUDED.matched_jt_kodu,
                matched_tur_id=EXCLUDED.matched_tur_id
        """), {
            "sid": sid, "title": title, "tur_adi": parsed["tur_adi"],
            "kalkis": parsed.get("kalkis_str") or "",
            "havayolu": parsed.get("havayolu") or "",
            "gece": parsed.get("gece"),
            "tur_id": matched_tur_id, "jt": matched_jt_kodu,
            "status": match_status, "conf": match_conf, "rc": total,
        })

        # Yanıtları ekle
        for i, row in enumerate(rows):
            resp_id = f"porsline_{sid}_{i}"
            if resp_id in existing_ids:
                atla += 1
                continue
            pr = parse_response_row(header, row)
            try:
                res = conn.execute(text("""
                    INSERT INTO historical_surveys
                        (musteri_adi, rehber_adi, acente_adi, kalkis_tarihi,
                         genel_puan, rehber_puani, puan_detay, tur_adi_ham,
                         matched_tur_id, matched_jt_kodu, match_confidence,
                         match_method, match_status, import_batch,
                         porsline_response_id, porsline_survey_id)
                    VALUES
                        (:musteri,:rehber,:acente,:kalkis,
                         :genel,:rehber_p,:detay,:tur_adi,
                         :tur_id,:jt,:conf,:method,:status,:batch,:resp_id,:survey_id)
                    ON CONFLICT (porsline_response_id)
                    WHERE porsline_response_id IS NOT NULL AND porsline_response_id != ''
                    DO NOTHING
                """), {
                    "musteri":   pr.get("musteri_adi") or "",
                    "rehber":    pr.get("rehber_adi") or parsed.get("rehber_adi") or "",
                    "acente":    pr.get("acente_adi") or "",
                    "kalkis":    parsed.get("kalkis_str") or "",
                    "genel":     pr.get("genel_puan"),
                    "rehber_p":  pr.get("rehber_puani"),
                    "detay":     json.dumps(pr.get("puan_detay") or {}, ensure_ascii=False),
                    "tur_adi":   parsed["tur_adi"],
                    "tur_id":    matched_tur_id, "jt": matched_jt_kodu,
                    "conf":      match_conf, "method": match_method, "status": match_status,
                    "batch":     f"bulk_{sid}",
                    "resp_id":   resp_id, "survey_id": sid,
                })
                if res.rowcount > 0:
                    yeni += 1
                else:
                    atla += 1
            except Exception as row_err:
                log.debug("Satır yazılamadı (%s[%d]): %s", sid, i, row_err)
                atla += 1

        conn.commit()

    return {"yeni": yeni, "atla": atla, "hata": None}


# ─────────────────────────────────────────────────────────────────────────────
#  Ana program
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Porsline Bulk Sync")
    parser.add_argument("--force",    action="store_true",
                        help="DB sayısı eşleşse bile yeniden çek")
    parser.add_argument("--survey",   metavar="ID",
                        help="Sadece bu ID'li anketi sync et")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="İstekler arası minimum bekleme saniyesi (varsayılan: 5)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="DB'ye yazmadan kaç yanıt olduğunu raporla")
    args = parser.parse_args()

    # ── Env kontrol ──────────────────────────────────────────────────────────
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL tanımlı değil")
        sys.exit(1)

    if not os.getenv("PORSLINE_API_KEY"):
        log.error("PORSLINE_API_KEY tanımlı değil")
        sys.exit(1)

    # ── Rate limit interval'ı artır ───────────────────────────────────────────
    # porsline_service'deki global değişkeni doğrudan ayarla
    _ps._rl_interval      = args.interval
    _ps._rl_base_interval = args.interval
    log.info("Rate limit interval: %.1f saniye/istek", args.interval)

    # ── DB bağlantısı ─────────────────────────────────────────────────────────
    try:
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        log.info("Veritabanı bağlantısı OK")
    except Exception as e:
        log.error("DB bağlantı hatası: %s", e)
        sys.exit(1)

    # ── Anket listesi ─────────────────────────────────────────────────────────
    log.info("Porsline anket listesi alınıyor…")
    chunk = list_surveys()
    if not chunk["ok"]:
        log.error("Anket listesi alınamadı: %s", chunk.get("hata"))
        sys.exit(1)

    all_surveys = chunk["surveys"]
    log.info("Toplam %d anket bulundu", len(all_surveys))

    if args.survey:
        all_surveys = [s for s in all_surveys
                       if str(s.get("id") or s.get("uid") or "") == args.survey]
        if not all_surveys:
            log.error("--survey %s bulunamadı anket listesinde", args.survey)
            sys.exit(1)
        log.info("Tek anket modu: %s", args.survey)

    # ── DB sayıları & tur listesi ─────────────────────────────────────────────
    db_counts = _db_survey_counts(engine)
    tours     = _load_tours(engine) if _MATCHER_OK else []
    log.info("DB'de %d anket kaydı var, %d tur yüklendi", len(db_counts), len(tours))

    # ── Sync döngüsü ──────────────────────────────────────────────────────────
    basla = datetime.now()
    toplam_yeni  = 0
    toplam_atla  = 0
    toplam_hata  = 0
    islenen      = 0
    atlanan_kount = 0

    log.info("=" * 65)
    log.info("BULK SYNC BAŞLIYOR  (force=%s, dry_run=%s)", args.force, args.dry_run)
    log.info("=" * 65)

    for idx, s in enumerate(all_surveys, 1):
        sid   = str(s.get("id") or s.get("uid") or "")
        title = s.get("title") or s.get("name") or f"survey_{sid}"
        if not sid:
            continue

        # Porsline'ın bildirdiği yanıt sayısı
        porsline_count = int(
            s.get("responses_count") or s.get("respondents_count") or
            s.get("response_count") or s.get("total_responses") or -1
        )
        stored_count = db_counts.get(sid, -1)

        # Skip kontrolü (--force yoksa, sayılar eşleşiyorsa atla)
        if (not args.force
                and porsline_count != -1
                and stored_count != -1
                and porsline_count == stored_count):
            atlanan_kount += 1
            log.debug("[%d/%d] ATLA  %s (%d yanıt, güncel)",
                      idx, len(all_surveys), title[:40], porsline_count)
            continue

        pct = f"{idx}/{len(all_surveys)}"
        log.info("[%s] %-50s  ~%s yanıt",
                 pct, title[:50], porsline_count if porsline_count != -1 else "?")

        # Detay bilgisi — survey_meta
        detail = get_survey_detail(sid)
        survey_meta = detail["survey"] if detail["ok"] else s

        # Asıl sync
        result = sync_survey(engine, sid, survey_meta, tours,
                             force=args.force, dry_run=args.dry_run)

        if result["hata"]:
            log.warning("  ✗ HATA: %s", result["hata"])
            toplam_hata += 1
        else:
            log.info("  ✓ +%d yeni, %d zaten vardı", result["yeni"], result["atla"])
            toplam_yeni += result["yeni"]
            toplam_atla += result["atla"]
        islenen += 1

        # İki istek arasında bekle (porsline_service zaten bekliyor ama
        # get_all_responses çok sayıda iç çağrı yaptı; ek dinlenme süresi)
        if idx < len(all_surveys):
            time.sleep(max(0, args.interval - 1))  # servis kendi 1s'ini zaten bekliyor

    # ── Özet ──────────────────────────────────────────────────────────────────
    sure = (datetime.now() - basla).total_seconds()
    log.info("=" * 65)
    log.info("TAMAMLANDI  %.0f saniye (%.1f dakika)", sure, sure / 60)
    log.info("  İşlenen  : %d anket", islenen)
    log.info("  Atlanan  : %d anket (zaten güncel)", atlanan_kount)
    log.info("  Yeni yanıt: %d", toplam_yeni)
    log.info("  Atla/dup : %d", toplam_atla)
    log.info("  Hata     : %d", toplam_hata)
    log.info("=" * 65)


if __name__ == "__main__":
    main()
