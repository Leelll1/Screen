# -*- coding: utf-8 -*-
"""KRX Open API 일일 수집 — 조기경보 L2(지반) 지표용.
헤더 인증(AUTH_KEY)이 필요해 GitHub Actions에서 실행한다 (세션 직접 호출 불가).
키는 저장소 Secret(KRX_AUTH_KEY)에서 읽는다 — 코드에 키를 쓰지 않는다.
서비스별 개별 신청제이므로, 미신청 서비스는 401/403 — 상태를 기록해 자가 진단한다.
"""
import os, json, csv, datetime, urllib.request

KEY = os.environ["KRX_AUTH_KEY"]
BASE = "http://data-dbg.krx.co.kr/svc/apis/"

# 후보 서비스 (KRX Open API 포털에서 신청한 것만 성공한다)
SERVICES = [
    ("sto/stk_bydd_trd",  "유가증권 일별 매매정보"),
    ("sto/ksq_bydd_trd",  "코스닥 일별 매매정보"),
    ("idx/krx_dd_trd",    "KRX 지수 일별시세"),
    ("idx/kospi_dd_trd",  "KOSPI 지수 일별시세"),
    ("idx/kosdaq_dd_trd", "KOSDAQ 지수 일별시세"),
    ("drv/fut_bydd_trd",  "선물 일별 매매정보"),
    ("drv/opt_bydd_trd",  "옵션 일별 매매정보"),
]

def biz_day():
    d = datetime.date.today() - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")

def main():
    bas = biz_day()
    os.makedirs("results/krx", exist_ok=True)
    status = []
    for path, label in SERVICES:
        url = f"{BASE}{path}?basDd={bas}"
        req = urllib.request.Request(url, headers={"AUTH_KEY": KEY})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.load(r)
            block = data.get("OutBlock_1") or next(iter(data.values()), [])
            n = len(block) if isinstance(block, list) else 0
            name = path.replace("/", "_")
            with open(f"results/krx/{name}.json", "w") as f:
                json.dump(data, f, ensure_ascii=False)
            status.append([path, label, "OK", n])
            print(f"OK   {path} ({label}): {n} rows")
        except urllib.error.HTTPError as e:
            status.append([path, label, f"HTTP {e.code}", 0])
            print(f"FAIL {path} ({label}): HTTP {e.code}"
                  + (" — 포털에서 이 서비스를 신청해야 합니다" if e.code in (401, 403) else ""))
        except Exception as e:
            status.append([path, label, f"ERR {type(e).__name__}", 0])
            print(f"ERR  {path}: {e}")
    with open("results/krx/status.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "label", "status", "rows", "basDd", "run_utc"])
        for row in status:
            w.writerow(row + [bas, datetime.datetime.utcnow().isoformat(timespec="seconds")])
    ok = sum(1 for s in status if s[2] == "OK")
    print(f"\n요약: {ok}/{len(SERVICES)} 서비스 성공 (basDd={bas})")

if __name__ == "__main__":
    main()
