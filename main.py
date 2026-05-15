import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from apscheduler.schedulers.background import BackgroundScheduler
from contextlib import asynccontextmanager
from datetime import datetime
from starlette.middleware.sessions import SessionMiddleware
from passlib.context import CryptContext
import gspread
from oauth2client.service_account import ServiceAccountCredentials

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


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
        # Admin hesabı — ilk kurulumda varsayılan şifre: Glob2025!
        default_hash = pwd.hash("Glob2025!")
        conn.execute(text("""
            INSERT INTO kullanicilar (kullanici_adi, ad_soyad, pozisyon, email, rol, sifre_hash)
            VALUES ('gokhan', 'Gokhan Kaya', 'Cruise Operation Manager', 'gokhan.kaya@globdmc.com', 'admin', :h)
            ON CONFLICT (kullanici_adi) DO UPDATE SET
                sifre_hash = CASE WHEN kullanicilar.sifre_hash IS NULL THEN :h ELSE kullanicilar.sifre_hash END
        """), {"h": default_hash})
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
    try:
        tablo_olustur()
    except Exception as e:
        print(f"Tablo olusturma hatasi: {e}")
    try:
        sheets_den_postgresql_kopyala()
        print("Baslangic sync tamamlandi")
    except Exception as e:
        print(f"Baslangic sync hatasi: {e}")
    scheduler = None
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(sheets_den_postgresql_kopyala, 'interval', hours=1)
        scheduler.start()
        print("Otomatik senkronizasyon aktif: her 1 saatte bir")
    except Exception as e:
        print(f"Scheduler baslatılamadı: {e}")
    yield
    if scheduler:
        try:
            scheduler.shutdown()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "glob-gizli-anahtar-2025"))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


class RehberGuncelle(BaseModel):
    rehber: str


@app.get("/login")
def login_sayfasi(request: Request):
    if request.session.get("kullanici_adi"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={"hata": None})


@app.post("/login")
def login_yap(request: Request, kullanici_adi: str = Form(...), sifre: str = Form(...)):
    k = kullanici_getir(kullanici_adi)
    if not k or not k["sifre_hash"] or not pwd.verify(sifre, k["sifre_hash"]):
        return templates.TemplateResponse(request=request, name="login.html", context={"hata": "Kullanıcı adı veya şifre hatalı"})
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
        }
    )


@app.get("/kontenjan")
def kontenjan_sayfasi(request: Request):
    kullanici = oturum_kullanicisi(request)
    if not kullanici:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/turlar", status_code=302)