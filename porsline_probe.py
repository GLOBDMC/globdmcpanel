#!/usr/bin/env python3
"""
Porsline Analytics Endpoint Probe
Hangi endpoint'in aggregate/istatistik verisi döndürdüğünü bulur.

Kullanım:
    python porsline_probe.py
    python porsline_probe.py --survey 12345   # belirli bir survey ID
"""

import json, os, sys, argparse
sys.path.insert(0, os.path.dirname(__file__))

from porsline_service import _get, list_surveys

# Test edilecek endpoint pattern'ları
PATTERNS = [
    "/api/v2/surveys/{sid}/statistics/",
    "/api/v2/surveys/{sid}/analytics/",
    "/api/v2/surveys/{sid}/charts/",
    "/api/v2/surveys/{sid}/summary/",
    "/api/v2/surveys/{sid}/report/",
    "/api/v2/surveys/{sid}/aggregate/",
    "/api/v2/surveys/{sid}/results/",
    "/api/surveys/{sid}/statistics/",
    "/api/surveys/{sid}/analytics/",
    "/api/surveys/{sid}/charts/",
    "/api/surveys/{sid}/summary/",
    "/api/v2/surveys/{sid}/questions/statistics/",
    "/api/v2/surveys/{sid}/questions/analytics/",
]

def probe(sid: str):
    print(f"\n=== Survey ID: {sid} ===")
    for pattern in PATTERNS:
        url = pattern.format(sid=sid)
        result = _get(url)
        status = result.get("error", "OK")
        if status == "OK" or "error" not in result:
            print(f"  ✅ {url}")
            # İlk 500 karakter göster
            snippet = json.dumps(result, ensure_ascii=False)[:500]
            print(f"     {snippet}")
        elif status == 404:
            print(f"  ✗  {url}  → 404")
        else:
            print(f"  ?  {url}  → {status}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--survey", help="Survey ID (yoksa ilk anket alınır)")
    args = parser.parse_args()

    if not os.getenv("PORSLINE_API_KEY"):
        print("PORSLINE_API_KEY tanımlı değil")
        sys.exit(1)

    sid = args.survey
    if not sid:
        print("Anket listesi alınıyor…")
        chunk = list_surveys()
        if not chunk["ok"] or not chunk["surveys"]:
            print("Anket bulunamadı")
            sys.exit(1)
        # Yanıtı olan ilk anketi seç
        for s in chunk["surveys"]:
            count = int(s.get("responses_count") or s.get("respondents_count") or 0)
            if count > 0:
                sid = str(s.get("id") or s.get("uid"))
                title = s.get("title") or s.get("name") or sid
                print(f"Test anketi: {title} (ID={sid}, {count} yanıt)")
                break
        if not sid:
            sid = str(chunk["surveys"][0].get("id") or chunk["surveys"][0].get("uid"))
            print(f"Fallback survey ID: {sid}")

    probe(sid)

if __name__ == "__main__":
    main()
