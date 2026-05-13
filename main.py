from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
import gspread
from oauth2client.service_account import ServiceAccountCredentials


db_engine = create_engine(
    "postgresql://gokhan:12345@localhost:5432/globdmc_panel"
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    tablo_olustur()
    scheduler = BackgroundScheduler()
    scheduler.add_job(sheets_den_postgresql_kopyala, 'interval', hours=1)
    scheduler.start()
    print("Otomatik senkronizasyon aktif: her 1 saatte bir")
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/")
def anasayfa(request: Request):
    select_sql = """
        SELECT jt_kodu, tur_adi, kalkis_tarihi, havayolu, pax, satilan, kalan, guncel_fiyat
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
            "kullanici": kullanici
        }
    )