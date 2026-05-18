import os
import time
import secrets
import logging
import logging.handlers
from collections import defaultdict
from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.exception_handlers import http_exception_handler
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
from datetime import datetime
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from passlib.context import CryptContext
import gspread
from oauth2client.service_account import ServiceAccountCredentials

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Production config ────────────────────────────────────────────────────────
_CANONICAL_DOMAIN = os.environ.get("CANONICAL_DOMAIN", "").strip()
_ENFORCE_HTTPS    = os.environ.get("ENFORCE_HTTPS", "1") == "1"
_SECRET_KEY       = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# ── Logging setup ────────────────────────────────────────────────────────────
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_DIR   = os.environ.get("LOG_DIR", "/tmp/globdmc_logs")

_log_fmt = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

def _build_logger(name: str, log_file: str,
                  level=logging.INFO, max_bytes=10_485_760, backup_count=5) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.setLevel(level)
    lg.propagate = False

    # Console (Railway stdout'a yazar)
    ch = logging.StreamHandler()
    ch.setFormatter(_log_fmt)
    lg.addHandler(ch)

    # Rotating file — hata varsa sadece console'a düşer
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(_LOG_DIR, log_file),
            maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
        )
        fh.setFormatter(_log_fmt)
        lg.addHandler(fh)
    except Exception:
        pass

    return lg

logger       = _build_logger("globdmc",       "app.log",   level=getattr(logging, _LOG_LEVEL, logging.INFO))
audit_logger = _build_logger("globdmc.audit", "audit.log", level=logging.INFO, backup_count=10)

# Harici kütüphanelerin gürültüsünü kıs
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("gspread").setLevel(logging.WARNING)

# ── Security Headers Middleware ──────────────────────────────────────────────
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' cdn.tailwindcss.com; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    _SKIP = {"/health", "/robots.txt"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1 — HTTP → HTTPS redirect (Cloudflare / Railway proxy)
        if _ENFORCE_HTTPS and path not in self._SKIP:
            if request.headers.get("x-forwarded-proto") == "http":
                url = str(request.url).replace("http://", "https://", 1)
                return RedirectResponse(url=url, status_code=301)

        # 2 — Canonical domain redirect (Railway raw domain → custom domain)
        if _CANONICAL_DOMAIN and path not in self._SKIP:
            host = request.headers.get("host", "").split(":")[0]
            if host and host != _CANONICAL_DOMAIN:
                target = f"https://{_CANONICAL_DOMAIN}{request.url.path}"
                if request.url.query:
                    target += f"?{request.url.query}"
                return RedirectResponse(url=target, status_code=301)

        response = await call_next(request)

        # 3 — Security headers
        h = response.headers
        h["X-Frame-Options"]           = "DENY"
        h["X-Content-Type-Options"]    = "nosniff"
        h["X-XSS-Protection"]          = "1; mode=block"
        h["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        h["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        h["Content-Security-Policy"]   = _CSP

        # 4 — Hide server fingerprint
        if "server" in h:
            del h["server"]
        if "x-powered-by" in h:
            del h["x-powered-by"]

        return response

# ── Login rate limiter (in-memory, resets on restart) ───────────────────────
_login_attempts: dict = defaultdict(list)
_LOGIN_MAX    = int(os.environ.get("LOGIN_MAX_ATTEMPTS",   "10"))
_LOGIN_WINDOW = int(os.environ.get("LOGIN_WINDOW_SECONDS", "300"))  # 5 dakika

def _client_ip(request: Request) -> str:
    """
    Gerçek istemci IP'sini proxy zincirinden güvenli şekilde çıkarır.
    Öncelik: CF-Connecting-IP (Cloudflare) → X-Real-IP → X-Forwarded-For[0] → doğrudan bağlantı
    """
    # Cloudflare — en güvenilir kaynak, proxy'nin set ettiği
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    # Tek reverse proxy (Railway veya nginx)
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    # Çoklu proxy zinciri — en soldaki (orijinal istemci)
    fwd = request.headers.get("x-forwarded-for", "").strip()
    if fwd:
        return fwd.split(",")[0].strip()
    # Doğrudan bağlantı (local dev)
    return request.client.host if request.client else "unknown"

def _is_rate_limited(ip: str) -> bool:
    now  = time.time()
    hits = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = hits
    return len(hits) >= _LOGIN_MAX

def _record_login_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.time())


db_engine = create_engine(
    os.getenv("DATABASE_URL")
)


def sheets_baglan():
    import json as _json
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise Exception("SERVICE_ACCOUNT_JSON env var bulunamadi")
    try:
        info = _json.loads(sa_json)
    except _json.JSONDecodeError as e:
        raise Exception(f"SERVICE_ACCOUNT_JSON gecersiz JSON: {e}")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
    return gspread.authorize(creds)


def tablo_olustur():
    sql_turlar = """
        CREATE TABLE IF NOT EXISTS turlar (
            id SERIAL PRIMARY KEY,
            jt_kodu VARCHAR(50) UNIQUE,
            tur_adi TEXT,
            kalkis_tarihi VARCHAR(50),
            havayolu VARCHAR(100),
            pax INTEGER,
            satilan INTEGER,
            kalan INTEGER,
            guncel_fiyat VARCHAR(50),
            rehber VARCHAR(200) DEFAULT '',
            guncelleme_zamani TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    sql_kullanicilar = """
        CREATE TABLE IF NOT EXISTS kullanicilar (
            id SERIAL PRIMARY KEY,
            kullanici_adi VARCHAR(50) UNIQUE NOT NULL,
            ad_soyad VARCHAR(100) NOT NULL,
            pozisyon VARCHAR(100),
            email VARCHAR(100),
            rol VARCHAR(50) DEFAULT 'kullanici',
            aktif BOOLEAN DEFAULT TRUE,
            sifre_hash VARCHAR(200),
            kayit_tarihi TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    with db_engine.connect() as conn:
        conn.execute(text(sql_turlar))
        conn.execute(text(sql_kullanicilar))
        conn.execute(text("ALTER TABLE turlar ADD COLUMN IF NOT EXISTS rehber VARCHAR(200) DEFAULT ''"))
        conn.execute(text("ALTER TABLE turlar ADD COLUMN IF NOT EXISTS bitis_tarihi VARCHAR(50) DEFAULT ''"))
        conn.execute(text("ALTER TABLE kullanicilar ADD COLUMN IF NOT EXISTS sifre_hash VARCHAR(200)"))
        conn.execute(text("ALTER TABLE kullanicilar ADD COLUMN IF NOT EXISTS sifre_degistir BOOLEAN DEFAULT FALSE"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS jolly_sonuc (
                id SERIAL PRIMARY KEY,
                grup_adi TEXT,
                kalkis_tarihi VARCHAR(50) DEFAULT '',
                vitrinde VARCHAR(10),
                eslesen_jolly_tur TEXT,
                jt_kodu_jolly VARCHAR(50),
                skor VARCHAR(20),
                kontrol_tarihi VARCHAR(50)
            )
        """))
        # Mevcut tabloya kalkis_tarihi ekle (ilk kurulumda yoksa)
        conn.execute(text(
            "ALTER TABLE jolly_sonuc ADD COLUMN IF NOT EXISTS kalkis_tarihi VARCHAR(50) DEFAULT ''"
        ))
        # Platform kolonu ekle (çok-platform desteği)
        conn.execute(text(
            "ALTER TABLE jolly_sonuc ADD COLUMN IF NOT EXISTS platform VARCHAR(50) DEFAULT 'jolly'"
        ))
        # Durum değişim takibi kolonları
        conn.execute(text(
            "ALTER TABLE jolly_sonuc ADD COLUMN IF NOT EXISTS onceki_vitrinde VARCHAR(10)"
        ))
        conn.execute(text(
            "ALTER TABLE jolly_sonuc ADD COLUMN IF NOT EXISTS degisim_tarihi VARCHAR(50)"
        ))
        # Unique constraint migrasyonu: (grup_adi, kalkis_tarihi, platform)
        conn.execute(text("""
            DO $$
            BEGIN
                -- Eski constraint'leri düşür
                BEGIN ALTER TABLE jolly_sonuc DROP CONSTRAINT jolly_sonuc_grup_adi_key;
                EXCEPTION WHEN undefined_object THEN NULL; END;
                BEGIN ALTER TABLE jolly_sonuc DROP CONSTRAINT jolly_sonuc_grup_tarih_uniq;
                EXCEPTION WHEN undefined_object THEN NULL; END;
                -- Yeni composite unique
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'jolly_sonuc_platform_uniq'
                ) THEN
                    ALTER TABLE jolly_sonuc
                        ADD CONSTRAINT jolly_sonuc_platform_uniq
                        UNIQUE (grup_adi, kalkis_tarihi, platform);
                END IF;
            END $$;
        """))
        # Admin hesabı — ilk kurulumda varsayılan şifre: Glob2025!
        default_hash = pwd.hash("Glob2025!")
        conn.execute(text("""
            INSERT INTO kullanicilar (kullanici_adi, ad_soyad, pozisyon, email, rol, sifre_hash)
            VALUES ('gokhan', 'Gokhan Kaya', 'Cruise Operation Manager', 'gokhan.kaya@globdmc.com', 'admin', :h)
            ON CONFLICT (kullanici_adi) DO UPDATE SET
                sifre_hash = CASE WHEN kullanicilar.sifre_hash IS NULL THEN :h ELSE kullanicilar.sifre_hash END
        """), {"h": default_hash})
        conn.commit()
    # Snapshot tablosu
    try:
        from snapshot_repository import create_snapshot_table
        create_snapshot_table(db_engine)
    except Exception as e:
        logger.error("Snapshot tablosu olusturulamadi: %s", e)
    logger.info("Tablolar hazir")


def bitis_tarihi_hesapla(bitis_raw: str, tur_adi: str, kalkis: str) -> str:
    """Sheet'ten gelen bitiş tarihini döndürür.
    Boşsa tur adındaki gece sayısından hesaplar."""
    import re as _re
    from datetime import timedelta as _td

    if bitis_raw:
        return bitis_raw

    # Tur adından gece sayısını çıkar: "7 Gece", "10gece", "14 GECE" vb.
    m = _re.search(r'(\d+)\s*[Gg][Ee][Cc][Ee]', tur_adi)
    if not m or not kalkis:
        return ""

    geceler = int(m.group(1))
    for fmt in ("%d-%m-%Y", "%d.%m.%Y"):
        try:
            kalkis_dt = datetime.strptime(kalkis.strip(), fmt)
            bitis_dt  = kalkis_dt + _td(days=geceler)
            return bitis_dt.strftime(fmt)
        except ValueError:
            continue
    return ""


def sheets_den_postgresql_kopyala():
    client = sheets_baglan()
    sheet = client.open("TUR KONTENJANLARI").sheet1
    veriler = sheet.get_all_values()
    veri_satirlari = veriler[1:]

    eklenen = 0
    guncellenen = 0

    insert_sql = """
        INSERT INTO turlar (jt_kodu, tur_adi, kalkis_tarihi, bitis_tarihi, havayolu, pax, satilan, kalan, guncel_fiyat)
        VALUES (:jt, :tur_adi, :kalkis, :bitis, :havayolu, :pax, :satilan, :kalan, :fiyat)
        ON CONFLICT (jt_kodu) DO UPDATE SET
            tur_adi       = EXCLUDED.tur_adi,
            kalkis_tarihi = EXCLUDED.kalkis_tarihi,
            bitis_tarihi  = EXCLUDED.bitis_tarihi,
            havayolu      = EXCLUDED.havayolu,
            pax           = EXCLUDED.pax,
            satilan       = EXCLUDED.satilan,
            kalan         = EXCLUDED.kalan,
            guncel_fiyat  = EXCLUDED.guncel_fiyat,
            guncelleme_zamani = CURRENT_TIMESTAMP
        RETURNING (xmax = 0) AS yeni_mi
    """

    with db_engine.connect() as conn:
        for satir in veri_satirlari:
            if not satir or len(satir) < 17:
                continue
            jt = satir[16].strip() if len(satir) > 16 else ""
            if not jt:
                continue
            tur_adi  = satir[1] if len(satir) > 1 else ""
            kalkis   = satir[3] if len(satir) > 3 else ""
            havayolu = satir[2] if len(satir) > 2 else ""
            # I sütunu = index 8 → bitiş tarihi
            bitis_raw = satir[8].strip() if len(satir) > 8 else ""
            bitis = bitis_tarihi_hesapla(bitis_raw, tur_adi, kalkis)
            try:
                pax = int(satir[13]) if satir[13].strip() else 0
            except:
                pax = 0
            try:
                satilan = int(satir[14]) if satir[14].strip() else 0
            except:
                satilan = 0
            try:
                kalan = int(satir[15]) if satir[15].strip() else 0
            except:
                kalan = 0
            fiyat = satir[20].strip() if len(satir) > 20 else ""

            sonuc = conn.execute(text(insert_sql), {
                "jt": jt, "tur_adi": tur_adi, "kalkis": kalkis, "bitis": bitis,
                "havayolu": havayolu, "pax": pax, "satilan": satilan,
                "kalan": kalan, "fiyat": fiyat
            })
            yeni_mi = sonuc.scalar()
            if yeni_mi:
                eklenen += 1
            else:
                guncellenen += 1
        conn.commit()
    logger.info(f"Sheets sync: {eklenen} eklendi, {guncellenen} guncellendi")


def jolly_sonuc_kopyala():
    """Jolly Sonuc worksheet'inden PostgreSQL jolly_sonuc tablosuna senkronize eder."""
    try:
        client = sheets_baglan()
        # TUR KONTENJANLARI ile aynı spreadsheet içinde "Jolly Sonuc" sekmesini ara
        sheet_name = os.environ.get("JOLLY_SPREADSHEET_NAME", "TUR KONTENJANLARI")
        try:
            spreadsheet = client.open(sheet_name)
        except Exception:
            try:
                spreadsheet = client.open("fiyat test")
            except Exception as e2:
                logger.error(f"Jolly Sonuc: spreadsheet acilamadi: {e2}")
                return

        ws = None
        for sheet in spreadsheet.worksheets():
            if sheet.title.strip().lower() == "jolly sonuc":
                ws = sheet
                break
        if ws is None:
            logger.warning("Jolly Sonuc worksheet bulunamadi")
            return

        # get_all_values() kolon adı encoding sorununu bypass eder (pozisyon bazlı)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            logger.warning("Jolly Sonuc sayfasi bos")
            return

        # Beklenen sütun sırası (jolly_matcher _OUTPUT_HEADERS ile eşleşmeli):
        # 0:Grup Adı  1:Gidiş Tarihi  2:Vitrinde  3:Eşleşen Jolly Tur
        # 4:JT Kodu   5:Skor          6:Sebep      7:Kontrol Tarihi
        header = all_values[0]
        logger.debug(f"Jolly Sonuc header: {header}")

        data_rows = all_values[1:]
        with db_engine.connect() as conn:
            # Önce mevcut durumu kaydet (değişim tespiti için)
            mevcut = {}
            for r in conn.execute(text(
                "SELECT grup_adi, kalkis_tarihi, platform, vitrinde, onceki_vitrinde, degisim_tarihi "
                "FROM jolly_sonuc"
            )).fetchall():
                mevcut[(r[0], r[1], r[2])] = {
                    "vitrinde": r[3], "onceki": r[4], "degisim": r[5]
                }

            simdi = datetime.now().strftime("%Y-%m-%d %H:%M")
            islenen_keyler = set()

            for row in data_rows:
                if len(row) < 3:
                    continue
                grup_adi = str(row[0]).strip()
                if not grup_adi:
                    continue
                kalkis_tarihi = str(row[1]).strip() if len(row) > 1 else ""
                vitrinde      = str(row[2]).strip() if len(row) > 2 else ""
                eslesen       = str(row[3]).strip() if len(row) > 3 else ""
                jt_kodu       = str(row[4]).strip() if len(row) > 4 else ""
                skor          = str(row[5]).strip() if len(row) > 5 else ""
                kontrol       = str(row[7]).strip() if len(row) > 7 else ""

                key = (grup_adi, kalkis_tarihi, "jolly")
                islenen_keyler.add(key)
                eski = mevcut.get(key)

                # Değişim tespiti
                if eski and eski["vitrinde"] and eski["vitrinde"] != vitrinde:
                    yeni_onceki   = eski["vitrinde"]
                    yeni_degisim  = simdi
                elif eski:
                    # Değişim yok — önceki bilgileri koru
                    yeni_onceki  = eski["onceki"]
                    yeni_degisim = eski["degisim"]
                else:
                    yeni_onceki  = None
                    yeni_degisim = None

                conn.execute(text("""
                    INSERT INTO jolly_sonuc
                        (grup_adi, kalkis_tarihi, platform, vitrinde, eslesen_jolly_tur,
                         jt_kodu_jolly, skor, kontrol_tarihi, onceki_vitrinde, degisim_tarihi)
                    VALUES (:g, :kt, 'jolly', :v, :e, :j, :s, :k, :ov, :dt)
                    ON CONFLICT (grup_adi, kalkis_tarihi, platform) DO UPDATE SET
                        onceki_vitrinde   = EXCLUDED.onceki_vitrinde,
                        degisim_tarihi    = EXCLUDED.degisim_tarihi,
                        vitrinde          = EXCLUDED.vitrinde,
                        eslesen_jolly_tur = EXCLUDED.eslesen_jolly_tur,
                        jt_kodu_jolly     = EXCLUDED.jt_kodu_jolly,
                        skor              = EXCLUDED.skor,
                        kontrol_tarihi    = EXCLUDED.kontrol_tarihi
                """), {
                    "g": grup_adi, "kt": kalkis_tarihi,
                    "v": vitrinde, "e": eslesen, "j": jt_kodu,
                    "s": skor, "k": kontrol,
                    "ov": yeni_onceki, "dt": yeni_degisim,
                })

            # Artık listede olmayan kayıtları YOK yap (siteden kaldırıldı)
            for key, eski in mevcut.items():
                if key not in islenen_keyler and eski["vitrinde"] == "VAR":
                    conn.execute(text("""
                        UPDATE jolly_sonuc SET
                            onceki_vitrinde = vitrinde,
                            degisim_tarihi  = :simdi,
                            vitrinde        = 'YOK'
                        WHERE grup_adi=:g AND kalkis_tarihi=:kt AND platform=:p
                    """), {"simdi": simdi, "g": key[0], "kt": key[1], "p": key[2]})
            conn.commit()
        logger.info(f"Jolly Sonuc sync: {len(data_rows)} kayit islendi")
    except Exception as e:
        logger.error(f"Jolly Sonuc sync hatasi: {e}")


def kullanici_getir(kullanici_adi: str):
    with db_engine.connect() as conn:
        row = conn.execute(text("""
            SELECT kullanici_adi, ad_soyad, pozisyon, email, rol, sifre_hash, sifre_degistir
            FROM kullanicilar WHERE kullanici_adi = :k AND aktif = TRUE LIMIT 1
        """), {"k": kullanici_adi}).fetchone()
        if row:
            ad = row[1]
            bas_harfler = ''.join([p[0].upper() for p in ad.split() if p])[:2]
            return {
                "kullanici_adi": row[0],
                "ad_soyad": row[1],
                "pozisyon": row[2],
                "email": row[3],
                "rol": row[4],
                "sifre_hash": row[5],
                "sifre_degistir": row[6] if len(row) > 6 else False,
                "bas_harfler": bas_harfler,
            }
    return None


def oturum_kullanicisi(request: Request):
    k = request.session.get("kullanici_adi")
    if not k:
        return None
    return kullanici_getir(k)


def satis_aleri_getir():
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT jt_kodu, tur_adi, kalkis_tarihi, pax, satilan, havayolu FROM turlar WHERE pax > 0"
        )).fetchall()

    today = datetime.today()
    alerts = []

    for row in rows:
        try:
            for fmt in ("%d-%m-%Y", "%d.%m.%Y"):
                try:
                    tour_date = datetime.strptime(row[2], fmt)
                    break
                except ValueError:
                    continue
            else:
                continue

            days_left = (tour_date - today).days
            if not (35 <= days_left <= 60):
                continue

            pax = row[3] or 0
            satilan = row[4] or 0
            if pax == 0 or satilan / pax >= 0.5:
                continue

            alerts.append({
                "jt_kodu": row[0],
                "tur_adi": row[1],
                "kalkis": row[2],
                "days_left": days_left,
                "pax": pax,
                "satilan": satilan,
                "doluluk": round(satilan / pax * 100, 1),
                "havayolu": row[5] or "",
            })
        except Exception:
            continue

    return sorted(alerts, key=lambda x: x["days_left"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        tablo_olustur()
    except Exception as e:
        logger.error(f"Tablo olusturma hatasi: {e}")
    try:
        sheets_den_postgresql_kopyala()
        logger.info("Baslangic Sheets sync tamamlandi")
    except Exception as e:
        logger.error(f"Baslangic Sheets sync hatasi: {e}")
    try:
        jolly_sonuc_kopyala()
        logger.info("Baslangic Jolly sync tamamlandi")
    except Exception as e:
        logger.error(f"Baslangic Jolly sync hatasi: {e}")
    scheduler = None
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(sheets_den_postgresql_kopyala, 'interval', hours=1)
        scheduler.add_job(jolly_sonuc_kopyala, 'interval', hours=1)
        # Günlük snapshot job (02:00 TRT = 23:00 UTC)
        from snapshot_scheduler import setup_snapshot_scheduler
        setup_snapshot_scheduler(scheduler, db_engine)
        scheduler.start()
        logger.info("Otomatik senkronizasyon aktif: 1 saatte bir")
    except Exception as e:
        logger.error(f"Scheduler baslatılamadi: {e}")
    yield
    if scheduler:
        try:
            scheduler.shutdown()
        except Exception:
            pass


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,        # Swagger UI kapalı
    redoc_url=None,       # ReDoc kapalı
    openapi_url=None,     # OpenAPI schema kapalı
)

# Middleware sırası: son eklenen = en dışta (request'i ilk karşılar)
app.add_middleware(
    SessionMiddleware,
    secret_key=_SECRET_KEY,
    https_only=_ENFORCE_HTTPS,
    same_site="lax",
    max_age=28800,        # 8 saat
)
app.add_middleware(SecurityHeadersMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Exception handlers ───────────────────────────────────────────────────────
@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        kullanici = oturum_kullanicisi(request)
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"kullanici": kullanici},
            status_code=404,
        )
    # Diğer HTTP hatalar için default davranış
    return await http_exception_handler(request, exc)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception | path=%s | %s: %s",
                 request.url.path, type(exc).__name__, exc)
    kullanici = oturum_kullanicisi(request)
    return templates.TemplateResponse(
        request=request,
        name="404.html",
        context={"kullanici": kullanici, "status_code": 500},
        status_code=500,
    )


class RehberGuncelle(BaseModel):
    rehber: str


@app.get("/login")
def login_sayfasi(request: Request):
    if request.session.get("kullanici_adi"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"hata": None})


@app.post("/login")
def login_yap(request: Request, kullanici_adi: str = Form(...), sifre: str = Form(...)):
    ip = _client_ip(request)
    if _is_rate_limited(ip):
        audit_logger.warning(
            "LOGIN_BLOCKED | ip=%s | user=%.30s | reason=rate_limit", ip, kullanici_adi
        )
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"hata": "Çok fazla hatalı deneme. Lütfen birkaç dakika bekleyin."},
            status_code=429,
        )
    k = kullanici_getir(kullanici_adi)
    if not k or not k["sifre_hash"] or not pwd.verify(sifre, k["sifre_hash"]):
        _record_login_attempt(ip)
        audit_logger.warning(
            "LOGIN_FAILED | ip=%s | user=%.30s | reason=%s",
            ip, kullanici_adi, "no_user" if not k else "bad_password",
        )
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"hata": "Kullanıcı adı veya şifre hatalı"},
        )

    audit_logger.info(
        "LOGIN_OK | ip=%s | user=%s | rol=%s", ip, k["kullanici_adi"], k["rol"]
    )
    request.session["kullanici_adi"] = k["kullanici_adi"]
    if k.get("sifre_degistir"):
        return RedirectResponse("/sifre-degistir", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Admin: kullanıcı listesi ──────────────────────────────────────────────────
@app.get("/api/admin/kullanicilar")
def kullanicilar_listesi(request: Request):
    k = oturum_kullanicisi(request)
    if not k or k["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, kullanici_adi, ad_soyad, pozisyon, email, rol, aktif FROM kullanicilar ORDER BY id"
        )).fetchall()
    return JSONResponse([dict(r._mapping) for r in rows])


@app.post("/api/admin/kullanicilar/ekle")
def kullanici_ekle(request: Request, kullanici_adi: str = Form(...), ad_soyad: str = Form(...),
                   pozisyon: str = Form(""), email: str = Form(""), rol: str = Form("kullanici"),
                   sifre: str = Form(...)):
    k = oturum_kullanicisi(request)
    if not k or k["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    h = pwd.hash(sifre)
    try:
        with db_engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO kullanicilar (kullanici_adi, ad_soyad, pozisyon, email, rol, sifre_hash, sifre_degistir)
                VALUES (:k, :a, :p, :e, :r, :h, TRUE)
            """), {"k": kullanici_adi, "a": ad_soyad, "p": pozisyon, "e": email, "r": rol, "h": h})
            conn.commit()
        return JSONResponse({"ok": True})
    except Exception as ex:
        return JSONResponse({"hata": str(ex)}, status_code=400)


@app.post("/api/admin/kullanicilar/{uid}/guncelle")
def kullanici_guncelle(uid: int, request: Request, ad_soyad: str = Form(...),
                       pozisyon: str = Form(""), email: str = Form(""), rol: str = Form("kullanici")):
    k = oturum_kullanicisi(request)
    if not k or k["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    with db_engine.connect() as conn:
        conn.execute(text("""
            UPDATE kullanicilar SET ad_soyad=:a, pozisyon=:p, email=:e, rol=:r WHERE id=:id
        """), {"a": ad_soyad, "p": pozisyon, "e": email, "r": rol, "id": uid})
        conn.commit()
    return JSONResponse({"ok": True})


@app.post("/api/admin/kullanicilar/{uid}/sifre")
def sifre_sifirla(uid: int, request: Request, yeni_sifre: str = Form(...)):
    k = oturum_kullanicisi(request)
    if not k or k["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    with db_engine.connect() as conn:
        conn.execute(text("UPDATE kullanicilar SET sifre_hash=:h, sifre_degistir=TRUE WHERE id=:id"),
                     {"h": pwd.hash(yeni_sifre), "id": uid})
        conn.commit()
    return JSONResponse({"ok": True})


@app.post("/api/admin/kullanicilar/{uid}/sil")
def kullanici_sil(uid: int, request: Request):
    k = oturum_kullanicisi(request)
    if not k or k["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    with db_engine.connect() as conn:
        conn.execute(text("UPDATE kullanicilar SET aktif=FALSE WHERE id=:id"), {"id": uid})
        conn.commit()
    return JSONResponse({"ok": True})


@app.get("/health", include_in_schema=False)
def health():
    return JSONResponse({"status": "ok"})


# ── Snapshot API (admin only) ────────────────────────────────────────────────
@app.get("/api/snapshot/trigger")
def snapshot_trigger(request: Request):
    """Manuel snapshot tetikleme — admin yetkisi gerekli."""
    kullanici = oturum_kullanicisi(request)
    if not kullanici or kullanici["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    try:
        from snapshot_service import take_snapshot
        result = take_snapshot(db_engine)
        logger.info("Manuel snapshot tetiklendi | user=%s | %s",
                    kullanici["kullanici_adi"], result)
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        logger.error("Manuel snapshot hatasi: %s", exc)
        return JSONResponse({"ok": False, "hata": "Snapshot alinamadi"}, status_code=500)


@app.get("/api/snapshot/status")
def snapshot_status(request: Request):
    """Bugünkü snapshot özeti — admin yetkisi gerekli."""
    kullanici = oturum_kullanicisi(request)
    if not kullanici or kullanici["rol"] != "admin":
        return JSONResponse({"hata": "Yetkisiz"}, status_code=403)
    try:
        from snapshot_repository import get_snapshot_summary, get_snapshot_count
        summary = get_snapshot_summary(db_engine)
        total   = get_snapshot_count(db_engine)
        # datetime nesnelerini string'e çevir
        for k, v in summary.items():
            if hasattr(v, "isoformat"):
                summary[k] = v.isoformat()
        return JSONResponse({"ok": True, "today": summary, "total_records": total})
    except Exception as exc:
        logger.error("Snapshot status hatasi: %s", exc)
        return JSONResponse({"ok": False, "hata": "Durum alinamadi"}, status_code=500)


# ── Trend API ────────────────────────────────────────────────────────────────

def _serialize_rows(rows: list) -> list:
    """SQLAlchemy satır listesini JSON-safe dict listesine çevirir."""
    result = []
    for r in rows:
        d = dict(r) if isinstance(r, dict) else r
        out = {}
        for k, v in d.items():
            if v is None:
                out[k] = None
            elif hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            else:
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    out[k] = v
        result.append(out)
    return result


@app.get("/api/tur/{jt_kodu}/trend")
def tur_trend(jt_kodu: str, request: Request):
    """
    Bir turun 30 günlük fiyat, satış ve kontenjan trendini döndürür.
    7 günlük ve 30 günlük delta metrikleri de hesaplanır.
    """
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return JSONResponse({"hata": "Yetkisiz"}, status_code=401)
    try:
        from snapshot_repository import (
            get_price_history, get_quota_history, get_sales_velocity,
        )
        price_hist = get_price_history(db_engine, jt_kodu, days=30)
        quota_hist = get_quota_history(db_engine, jt_kodu, days=30)
        velocity   = get_sales_velocity(db_engine, jt_kodu, days=7)

        # Hesaplanan metrikler
        metrics: dict = {}

        # Fiyat metrikleri
        if price_hist:
            newest = price_hist[0]
            oldest = price_hist[-1]
            metrics["current_price"] = float(newest["current_price"]) if newest["current_price"] else None
            if newest.get("price_delta") is not None:
                metrics["price_1d_delta"] = float(newest["price_delta"])
            if (oldest.get("current_price") and newest.get("current_price")):
                delta30 = float(newest["current_price"]) - float(oldest["current_price"])
                base    = float(oldest["current_price"])
                metrics["price_30d_delta"] = round(delta30, 2)
                metrics["price_30d_pct"]   = round(delta30 / base * 100, 1) if base > 0 else None

        # Satış / kontenjan metrikleri
        if quota_hist:
            newest_q = quota_hist[0]
            oldest_q = quota_hist[-1]
            metrics["current_sales"]     = newest_q.get("current_sales")
            metrics["current_quota"]     = newest_q.get("current_quota")
            metrics["current_occupancy"] = float(newest_q["occupancy_rate"]) if newest_q.get("occupancy_rate") else None
            if oldest_q.get("current_sales") is not None and newest_q.get("current_sales") is not None:
                metrics["sales_30d_delta"]   = (newest_q["current_sales"] or 0) - (oldest_q["current_sales"] or 0)
            if oldest_q.get("occupancy_rate") is not None and newest_q.get("occupancy_rate") is not None:
                metrics["occupancy_delta"] = round(
                    float(newest_q["occupancy_rate"]) - float(oldest_q["occupancy_rate"]), 1
                )

        # 7 günlük satış ivmesi
        if velocity:
            sales_7d = sum(
                v["daily_sales_delta"] for v in velocity
                if v.get("daily_sales_delta") is not None
            )
            metrics["sales_7d_delta"] = sales_7d

        has_data = bool(price_hist or quota_hist)

        return JSONResponse({
            "ok":            True,
            "has_data":      has_data,
            "price_history": _serialize_rows(price_hist),
            "quota_history": _serialize_rows(quota_hist),
            "velocity":      _serialize_rows(velocity),
            "metrics":       metrics,
        })
    except Exception as exc:
        logger.error("Trend API hatasi | tour=%s | %s", jt_kodu, exc, exc_info=True)
        return JSONResponse({"ok": False, "has_data": False, "hata": "Veri alınamadı"}, status_code=500)


@app.get("/api/dashboard/trends")
def dashboard_trends(request: Request):
    """
    Dashboard Trendler widget'ı için özet veri:
    toplam snapshot sayısı, fiyat alarm listesi, bugünkü özet.
    """
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return JSONResponse({"hata": "Yetkisiz"}, status_code=401)
    try:
        from snapshot_repository import get_snapshot_summary, get_price_change_alerts, get_snapshot_count
        total   = get_snapshot_count(db_engine)
        summary = get_snapshot_summary(db_engine)
        alerts  = get_price_change_alerts(db_engine, threshold_pct=5.0) if total > 0 else []

        # Serialize summary
        sum_out = {}
        for k, v in summary.items():
            if v is None:
                sum_out[k] = None
            elif hasattr(v, "isoformat"):
                sum_out[k] = v.isoformat()
            else:
                try:
                    sum_out[k] = float(v)
                except (TypeError, ValueError):
                    sum_out[k] = v

        return JSONResponse({
            "ok":            True,
            "has_data":      total > 0,
            "total_records": total,
            "today_summary": sum_out,
            "price_alerts":  _serialize_rows(alerts[:6]),
        })
    except Exception as exc:
        logger.error("Dashboard trends hatasi: %s", exc, exc_info=True)
        return JSONResponse({"ok": False, "has_data": False}, status_code=500)


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/api/sync")
def manuel_sync(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return JSONResponse({"hata": "Yetkisiz"}, status_code=401)
    try:
        sheets_den_postgresql_kopyala()
        return JSONResponse({"ok": True, "mesaj": "Sync tamamlandi"})
    except Exception as e:
        return JSONResponse({"ok": False, "hata": str(e)}, status_code=500)


@app.patch("/api/tur/{jt_kodu}/rehber")
def rehber_guncelle(jt_kodu: str, body: RehberGuncelle):
    with db_engine.connect() as conn:
        conn.execute(
            text("UPDATE turlar SET rehber = :rehber WHERE jt_kodu = :jt"),
            {"rehber": body.rehber.strip(), "jt": jt_kodu}
        )
        conn.commit()
    return JSONResponse({"ok": True, "rehber": body.rehber.strip()})


@app.get("/sifre-degistir")
def sifre_degistir_sayfasi(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request=request, name="sifre_degistir.html", context={"hata": None, "kullanici": kullanici})


@app.post("/sifre-degistir")
def sifre_degistir_yap(request: Request, yeni_sifre: str = Form(...), yeni_sifre_tekrar: str = Form(...)):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)
    if yeni_sifre != yeni_sifre_tekrar:
        return templates.TemplateResponse(request=request, name="sifre_degistir.html",
                                          context={"hata": "Şifreler eşleşmiyor.", "kullanici": kullanici})
    if len(yeni_sifre) < 6:
        return templates.TemplateResponse(request=request, name="sifre_degistir.html",
                                          context={"hata": "Şifre en az 6 karakter olmalı.", "kullanici": kullanici})
    with db_engine.connect() as conn:
        conn.execute(text("UPDATE kullanicilar SET sifre_hash=:h, sifre_degistir=FALSE WHERE kullanici_adi=:k"),
                     {"h": pwd.hash(yeni_sifre), "k": kullanici["kullanici_adi"]})
        conn.commit()
    return RedirectResponse("/", status_code=302)


def tur_verileri_getir():
    select_sql = """
        SELECT jt_kodu, tur_adi, kalkis_tarihi, havayolu, pax, satilan, kalan, guncel_fiyat, rehber, bitis_tarihi
        FROM turlar
        ORDER BY
            CASE
                WHEN kalkis_tarihi ~ E'^\\d{2}-\\d{2}-\\d{4}$' THEN TO_DATE(kalkis_tarihi, 'DD-MM-YYYY')
                WHEN kalkis_tarihi ~ E'^\\d{2}\\.\\d{2}\\.\\d{4}$' THEN TO_DATE(kalkis_tarihi, 'DD.MM.YYYY')
                ELSE NULL
            END ASC NULLS LAST
    """
    with db_engine.connect() as conn:
        return conn.execute(text(select_sql)).fetchall()


@app.get("/")
def anasayfa(request: Request):
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard")
def dashboard(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)

    turlar = tur_verileri_getir()
    toplam     = len(turlar)
    son_yerler = sum(1 for t in turlar if t[6] is not None and 1 <= t[6] <= 5)
    yakin_dolu = sum(1 for t in turlar if t[6] is not None and 6 <= t[6] <= 10)
    bol        = sum(1 for t in turlar if t[6] is not None and t[6] > 10)

    satis_alertleri = satis_aleri_getir()

    # Havayolu bazlı özet
    from collections import defaultdict
    hy_sayi    = defaultdict(int)
    hy_sonyerler = defaultdict(int)
    for t in turlar:
        hy = t[3] or "Diğer"
        hy_sayi[hy] += 1
        if t[6] is not None and 1 <= t[6] <= 5:
            hy_sonyerler[hy] += 1
    havayolu_ozet = sorted(
        [(hy, hy_sayi[hy], hy_sonyerler[hy]) for hy in hy_sayi],
        key=lambda x: x[1], reverse=True
    )[:8]

    # Satış / doluluk özeti (pasta grafik)
    toplam_pax     = sum(int(t[4]) if t[4] is not None else 0 for t in turlar)
    toplam_satilan = sum(int(t[5]) if t[5] is not None else 0 for t in turlar)
    toplam_kalan   = sum(int(t[6]) if t[6] is not None else 0 for t in turlar)
    doluluk_pct    = round(toplam_satilan / toplam_pax * 100, 1) if toplam_pax > 0 else 0
    bos_pct        = round(100 - doluluk_pct, 1)

    bugun = datetime.today().strftime("%d %B %Y")

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "kullanici": kullanici,
            "aktif_sayfa": "dashboard",
            "toplam": toplam,
            "son_yerler": son_yerler,
            "yakin_dolu": yakin_dolu,
            "bol": bol,
            "satis_alertleri": satis_alertleri,
            "satis_alert_sayisi": len(satis_alertleri),
            "havayolu_ozet": havayolu_ozet,
            "kritik_sayi": son_yerler,
            "bugun": bugun,
            "toplam_pax": toplam_pax,
            "toplam_satilan": toplam_satilan,
            "toplam_kalan": toplam_kalan,
            "doluluk_pct": doluluk_pct,
            "bos_pct": bos_pct,
        }
    )


@app.get("/turlar")
def turlar_sayfasi(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)

    turlar = tur_verileri_getir()
    havayollari = sorted(set([t[3] for t in turlar if t[3]]))
    toplam     = len(turlar)
    son_yerler = sum(1 for t in turlar if t[6] is not None and 1 <= t[6] <= 5)
    satis_alertleri = satis_aleri_getir()

    # Now Boarding — önümüzdeki 10 gün içinde kalkışı olan, en az 1 satışı olan turlar
    from datetime import timedelta as _td
    bugun_dt = datetime.today().date()
    baslangic_dt = bugun_dt - _td(days=2)
    limit_dt     = bugun_dt + _td(days=7)

    def _tarih_parse(s):
        if not s:
            return None
        for fmt in ('%d-%m-%Y', '%d.%m.%Y'):
            try:
                return datetime.strptime(str(s), fmt).date()
            except ValueError:
                pass
        return None

    now_boarding = []
    for _t in turlar:
        _d = _tarih_parse(_t[2])
        _satilan = int(_t[5]) if _t[5] is not None else 0
        if _d is not None and baslangic_dt <= _d <= limit_dt and _satilan >= 1:
            now_boarding.append(_t)
    now_boarding.sort(key=lambda x: _tarih_parse(x[2]) or bugun_dt)

    return templates.TemplateResponse(
        request=request,
        name="turlar.html",
        context={
            "kullanici": kullanici,
            "aktif_sayfa": "turlar",
            "turlar": turlar,
            "havayollari": havayollari,
            "toplam": toplam,
            "satis_alertleri": satis_alertleri,
            "kritik_sayi": son_yerler,
            "now_boarding": now_boarding,
        }
    )


@app.get("/kontenjan")
def kontenjan_sayfasi(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/turlar", status_code=302)


@app.get("/vitrin-takibi")
def vitrin_takibi_sayfasi(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)

    with db_engine.connect() as conn:
        # Hangi platformlar var?
        platform_rows = conn.execute(text(
            "SELECT DISTINCT platform FROM jolly_sonuc WHERE platform IS NOT NULL ORDER BY platform"
        )).fetchall()
        platformlar = [r[0] for r in platform_rows] or ["jolly"]

        # Pivot sorgu: her tur+tarih için tüm platformların durumu
        vitrin_sql = """
            SELECT
                t.tur_adi,
                t.kalkis_tarihi,
                MAX(CASE WHEN j.platform='jolly' THEN j.vitrinde          ELSE '' END) AS jolly_vitrinde,
                MAX(CASE WHEN j.platform='jolly' THEN j.eslesen_jolly_tur ELSE '' END) AS jolly_eslesen,
                MAX(CASE WHEN j.platform='jolly' THEN j.jt_kodu_jolly     ELSE '' END) AS jolly_kodu,
                MAX(CASE WHEN j.platform='jolly' THEN j.kontrol_tarihi    ELSE '' END) AS jolly_kontrol,
                MAX(CASE WHEN j.platform='jolly' THEN j.onceki_vitrinde   ELSE '' END) AS jolly_onceki,
                MAX(CASE WHEN j.platform='jolly' THEN j.degisim_tarihi    ELSE '' END) AS jolly_degisim,
                MAX(CASE WHEN j.platform='tatilsepeti' THEN j.vitrinde          ELSE '' END) AS ts_vitrinde,
                MAX(CASE WHEN j.platform='tatilsepeti' THEN j.eslesen_jolly_tur ELSE '' END) AS ts_eslesen,
                MAX(CASE WHEN j.platform='tatilsepeti' THEN j.kontrol_tarihi    ELSE '' END) AS ts_kontrol,
                MAX(CASE WHEN j.platform='tatilsepeti' THEN j.onceki_vitrinde   ELSE '' END) AS ts_onceki,
                MAX(CASE WHEN j.platform='tatilsepeti' THEN j.degisim_tarihi    ELSE '' END) AS ts_degisim
            FROM turlar t
            LEFT JOIN jolly_sonuc j ON
                LOWER(TRIM(t.tur_adi)) = LOWER(TRIM(j.grup_adi))
                AND COALESCE(t.kalkis_tarihi, '') = COALESCE(j.kalkis_tarihi, '')
            GROUP BY t.tur_adi, t.kalkis_tarihi
            ORDER BY
                CASE
                    WHEN t.kalkis_tarihi ~ E'^\\d{2}-\\d{2}-\\d{4}$' THEN TO_DATE(t.kalkis_tarihi, 'DD-MM-YYYY')
                    WHEN t.kalkis_tarihi ~ E'^\\d{2}\\.\\d{2}\\.\\d{4}$' THEN TO_DATE(t.kalkis_tarihi, 'DD.MM.YYYY')
                    ELSE NULL
                END ASC NULLS LAST
        """
        rows = conn.execute(text(vitrin_sql)).fetchall()

    # Template için dict listesi
    vitrin_verileri = [
        {
            "tur_adi":       r[0],
            "kalkis_tarihi": r[1],
            "jolly": {
                "vitrinde": r[2], "eslesen": r[3], "kodu": r[4],
                "kontrol": r[5], "onceki": r[6], "degisim": r[7],
            },
            "tatilsepeti": {
                "vitrinde": r[8], "eslesen": r[9], "kontrol": r[10],
                "onceki": r[11], "degisim": r[12],
            },
        }
        for r in rows
    ]

    toplam = len(vitrin_verileri)

    # Platform bazında istatistikler
    platform_stats = {}
    for p in ["jolly", "tatilsepeti"]:
        kapanan = sum(1 for v in vitrin_verileri
                      if v[p]["onceki"] == "VAR" and v[p]["vitrinde"] == "YOK")
        acilan  = sum(1 for v in vitrin_verileri
                      if v[p]["onceki"] == "YOK" and v[p]["vitrinde"] == "VAR")
        platform_stats[p] = {
            "var":     sum(1 for v in vitrin_verileri if v[p]["vitrinde"] == "VAR"),
            "yok":     sum(1 for v in vitrin_verileri if v[p]["vitrinde"] == "YOK"),
            "kapanan": kapanan,
            "acilan":  acilan,
        }

    son_yerler = sum(1 for t in tur_verileri_getir() if t[6] is not None and 1 <= t[6] <= 5)

    return templates.TemplateResponse(
        request=request,
        name="vitrin_takibi.html",
        context={
            "kullanici": kullanici,
            "aktif_sayfa": "vitrin",
            "vitrin_verileri": vitrin_verileri,
            "platformlar": platformlar,
            "platform_stats": platform_stats,
            "toplam": toplam,
            "kritik_sayi": son_yerler,
        }
    )


@app.get("/api/vitrin-sync")
def vitrin_sync(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return JSONResponse({"hata": "Yetkisiz"}, status_code=401)
    try:
        jolly_sonuc_kopyala()
        return JSONResponse({"ok": True, "mesaj": "Jolly Sonuc sync tamamlandi"})
    except Exception as e:
        return JSONResponse({"ok": False, "hata": str(e)}, status_code=500)


@app.get("/api/vitrin-debug")
def vitrin_debug(request: Request):
    """Tarih format uyuşmazlığını tespit etmek için debug endpoint."""
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return JSONResponse({"hata": "Yetkisiz"}, status_code=401)
    with db_engine.connect() as conn:
        # jolly_sonuc tablosundan örnek
        js_rows = conn.execute(text(
            "SELECT grup_adi, kalkis_tarihi, vitrinde FROM jolly_sonuc LIMIT 10"
        )).fetchall()
        # turlar tablosundan örnek
        t_rows = conn.execute(text(
            "SELECT tur_adi, kalkis_tarihi FROM turlar LIMIT 10"
        )).fetchall()
        # JOIN sonucu kaç eşleşme var
        match_count = conn.execute(text(
            "SELECT COUNT(*) FROM turlar t "
            "JOIN jolly_sonuc j ON LOWER(TRIM(t.tur_adi))=LOWER(TRIM(j.grup_adi)) "
            "AND COALESCE(t.kalkis_tarihi,'')=COALESCE(j.kalkis_tarihi,'')"
        )).scalar()
        # jolly_sonuc toplam kayıt
        js_total = conn.execute(text("SELECT COUNT(*) FROM jolly_sonuc")).scalar()
    return JSONResponse({
        "jolly_sonuc_toplam": js_total,
        "join_eslesme_sayisi": match_count,
        "jolly_sonuc_ornekler": [
            {"grup_adi": r[0], "kalkis_tarihi": r[1], "vitrinde": r[2]} for r in js_rows
        ],
        "turlar_ornekler": [
            {"tur_adi": r[0], "kalkis_tarihi": r[1]} for r in t_rows
        ],
    })