import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials


db_engine = create_engine(
    os.getenv("DATABASE_URL")
)


def sheets_baglan():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "service_account.json", scope
    )
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
            kayit_tarihi TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """
    with db_engine.connect() as conn:
        conn.execute(text(sql_turlar))
        conn.execute(text(sql_kullanicilar))
        conn.execute(text("""
            ALTER TABLE turlar ADD COLUMN IF NOT EXISTS rehber VARCHAR(200) DEFAULT ''
        """))
        # Ilk kullaniciyi ekle (eger yoksa)
        conn.execute(text("""
            INSERT INTO kullanicilar (kullanici_adi, ad_soyad, pozisyon, email, rol)
            VALUES ('gokhan', 'Gokhan Kaya', 'Cruise Operation Manager', 'gokhan.kaya@globdmc.com', 'admin')
            ON CONFLICT (kullanici_adi) DO NOTHING
        """))
        conn.commit()
    print("Tablolar hazir")


def sheets_den_postgresql_kopyala():
    client = sheets_baglan()
    sheet = client.open("TUR KONTENJANLARI").sheet1
    veriler = sheet.get_all_values()
    veri_satirlari = veriler[1:]

    eklenen = 0
    guncellenen = 0

    insert_sql = """
        INSERT INTO turlar (jt_kodu, tur_adi, kalkis_tarihi, havayolu, pax, satilan, kalan, guncel_fiyat)
        VALUES (:jt, :tur_adi, :kalkis, :havayolu, :pax, :satilan, :kalan, :fiyat)
        ON CONFLICT (jt_kodu) DO UPDATE SET
            tur_adi = EXCLUDED.tur_adi,
            kalkis_tarihi = EXCLUDED.kalkis_tarihi,
            havayolu = EXCLUDED.havayolu,
            pax = EXCLUDED.pax,
            satilan = EXCLUDED.satilan,
            kalan = EXCLUDED.kalan,
            guncel_fiyat = EXCLUDED.guncel_fiyat,
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
            tur_adi = satir[1] if len(satir) > 1 else ""
            kalkis = satir[3] if len(satir) > 3 else ""
            havayolu = satir[2] if len(satir) > 2 else ""
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
                "jt": jt, "tur_adi": tur_adi, "kalkis": kalkis,
                "havayolu": havayolu, "pax": pax, "satilan": satilan,
                "kalan": kalan, "fiyat": fiyat
            })
            yeni_mi = sonuc.scalar()
            if yeni_mi:
                eklenen += 1
            else:
                guncellenen += 1
        conn.commit()
    print(f"Eklenen: {eklenen}, Guncellenen: {guncellenen}")


def aktif_kullaniciyi_getir():
    """Su an icin sabit kullanici (login eklenince degisecek)."""
    with db_engine.connect() as conn:
        sonuc = conn.execute(text("""
            SELECT kullanici_adi, ad_soyad, pozisyon, email, rol
            FROM kullanicilar WHERE kullanici_adi = 'gokhan' LIMIT 1
        """))
        row = sonuc.fetchone()
        if row:
            ad = row[1]
            bas_harfler = ''.join([p[0].upper() for p in ad.split() if p])[:2]
            return {
                "kullanici_adi": row[0],
                "ad_soyad": row[1],
                "pozisyon": row[2],
                "email": row[3],
                "rol": row[4],
                "bas_harfler": bas_harfler
            }
        return None


def satis_aleri_getir():
    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT jt_kodu, tur_adi, kalkis_tarihi, pax, satilan FROM turlar WHERE pax > 0"
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
            })
        except Exception:
            continue

    return sorted(alerts, key=lambda x: x["days_left"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    tablo_olustur()
    try:
        sheets_den_postgresql_kopyala()
        print("Baslangic sync tamamlandi")
    except Exception as e:
        print(f"Baslangic sync hatasi: {e}")
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(sheets_den_postgresql_kopyala, 'interval', hours=1)
        scheduler.start()
        print("Otomatik senkronizasyon aktif: her 1 saatte bir")
    except Exception as e:
        print(f"Scheduler baslatılamadı: {e}")
    yield
    try:
        scheduler.shutdown()
    except Exception:
        pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class RehberGuncelle(BaseModel):
    rehber: str


@app.get("/api/debug")
def debug():
    sa = os.environ.get("SERVICE_ACCOUNT_JSON")
    db = os.environ.get("DATABASE_URL")
    return JSONResponse({
        "SERVICE_ACCOUNT_JSON": "VAR" if sa else "YOK",
        "SA_uzunluk": len(sa) if sa else 0,
        "DATABASE_URL": "VAR" if db else "YOK",
    })


@app.get("/api/sync")
def manuel_sync():
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


@app.get("/")
def anasayfa(request: Request):
    select_sql = """
        SELECT jt_kodu, tur_adi, kalkis_tarihi, havayolu, pax, satilan, kalan, guncel_fiyat, rehber
        FROM turlar ORDER BY id DESC
    """
    with db_engine.connect() as conn:
        sonuc = conn.execute(text(select_sql))
        turlar = sonuc.fetchall()

    havayollari = sorted(set([t[3] for t in turlar if t[3]]))
    toplam = len(turlar)
    kritik = sum(1 for t in turlar if t[6] is not None and t[6] <= 3)
    orta = sum(1 for t in turlar if t[6] is not None and 3 < t[6] <= 10)
    bol = sum(1 for t in turlar if t[6] is not None and t[6] > 10)

    kullanici = aktif_kullaniciyi_getir()
    satis_alertleri = satis_aleri_getir()

    return templates.TemplateResponse(
        request=request,
        name="anasayfa.html",
        context={
            "turlar": turlar,
            "havayollari": havayollari,
            "toplam": toplam,
            "kritik": kritik,
            "orta": orta,
            "bol": bol,
            "kullanici": kullanici,
            "satis_alertleri": satis_alertleri,
        }
    )