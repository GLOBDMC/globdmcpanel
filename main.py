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
    print("Tablolar hazir")


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
    print(f"Eklenen: {eklenen}, Guncellenen: {guncellenen}")


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
                print(f"Jolly Sonuc: spreadsheet acilamadi: {e2}")
                return

        ws = None
        for sheet in spreadsheet.worksheets():
            if sheet.title.strip().lower() == "jolly sonuc":
                ws = sheet
                break
        if ws is None:
            print("Jolly Sonuc worksheet bulunamadi (Jolly Matcher henuz calistirilmamis)")
            return

        # get_all_values() kolon adı encoding sorununu bypass eder (pozisyon bazlı)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            print("Jolly Sonuc bos")
            return

        # Beklenen sütun sırası (jolly_matcher _OUTPUT_HEADERS ile eşleşmeli):
        # 0:Grup Adı  1:Gidiş Tarihi  2:Vitrinde  3:Eşleşen Jolly Tur
        # 4:JT Kodu   5:Skor          6:Sebep      7:Kontrol Tarihi
        header = all_values[0]
        print(f"Jolly Sonuc header: {header}")

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
        print(f"Jolly Sonuc: {len(data_rows)} kayit senkronize edildi")
    except Exception as e:
        print(f"Jolly Sonuc sync hatasi: {e}")


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
        print(f"Tablo olusturma hatasi: {e}")
    try:
        sheets_den_postgresql_kopyala()
        print("Baslangic sync tamamlandi")
    except Exception as e:
        print(f"Baslangic sync hatasi: {e}")
    try:
        jolly_sonuc_kopyala()
        print("Jolly Sonuc baslangic sync tamamlandi")
    except Exception as e:
        print(f"Jolly Sonuc baslangic sync hatasi: {e}")
    scheduler = None
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(sheets_den_postgresql_kopyala, 'interval', hours=1)
        scheduler.add_job(jolly_sonuc_kopyala, 'interval', hours=1)
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