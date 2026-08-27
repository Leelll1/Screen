# -*- coding: utf-8 -*-
"""KRX Open API 일일 수집 v2 — 조기경보(L2 지반) + 국내발굴 지원용 확장판.

v1 → v2 변경 (2026-08-27):
  · 수집 서비스 7개 → 17개(일일) + 3개(주간 명부) 확장 — 포털 전 서비스 신청 완료 반영
  · 파생상품지수(V-KOSPI), ETF/ETN(레버리지·인버스 거래대금), 채권지수·국채시장 추가
  · 조기경보 핵심 지표를 스크립트가 직접 계산해 시계열 CSV(ews_daily.csv)에 누적
    → 세션은 이 작은 CSV 하나만 읽으면 됨 (원본 JSON은 latest/에 보존, git 이력이 PIT 역할)
  · 금요일 실행(및 수동 실행) 시 종목기본정보 3종을 수확 — 국내발굴 명부의 원천
    (2026-08 '명부 구멍' 사고 재발 방지: KRX 공식 명부가 정본)

헤더 인증(AUTH_KEY)이 필요해 GitHub Actions에서 실행한다 (세션 직접 호출 불가).
키는 저장소 Secret(KRX_AUTH_KEY)에서 읽는다 — 코드에 키를 쓰지 않는다.
미신청/만료 서비스는 401/403 — status.csv에 기록되어 자가 진단된다.
"""
import os, json, csv, datetime, urllib.request

KEY = os.environ["KRX_AUTH_KEY"]
BASE = "https://data-dbg.krx.co.kr/svc/apis/"  # 공식 명세서 기준 https (2026-08-27)

# ── 일일 수집 서비스 (매 실행) ─────────────────────────────────────────────
DAILY = [
    # 주식
    ("sto/stk_bydd_trd",   "유가증권 일별매매"),
    ("sto/ksq_bydd_trd",   "코스닥 일별매매"),
    ("sto/knx_bydd_trd",   "코넥스 일별매매"),
    # 지수
    ("idx/krx_dd_trd",     "KRX 지수"),
    ("idx/kospi_dd_trd",   "KOSPI 지수"),
    ("idx/kosdaq_dd_trd",  "KOSDAQ 지수"),
    ("idx/bon_dd_trd",     "채권지수"),
    ("idx/drvprod_dd_trd", "파생상품지수(V-KOSPI 포함)"),
    # 증권상품
    ("etp/etf_bydd_trd",   "ETF 일별매매"),
    ("etp/etn_bydd_trd",   "ETN 일별매매"),
    # 채권
    ("bon/kts_bydd_trd",   "국채전문유통시장"),
    ("bon/bnd_bydd_trd",   "일반채권시장"),
    # 파생상품
    ("drv/fut_bydd_trd",   "선물(주식선물外)"),
    ("drv/opt_bydd_trd",   "옵션(주식옵션外)"),
    # 일반상품
    ("gen/gold_bydd_trd",  "금시장"),
    ("gen/oil_bydd_trd",   "석유시장"),
    ("gen/ets_bydd_trd",   "배출권시장"),
]

# ── 주간 수집 (금요일 실행 또는 수동 실행 시) — 국내발굴 명부의 정본 ──────
WEEKLY = [
    ("sto/stk_isu_base_info", "유가증권 종목기본정보"),
    ("sto/ksq_isu_base_info", "코스닥 종목기본정보"),
    ("sto/knx_isu_base_info", "코넥스 종목기본정보"),
]

def biz_day():
    # KST 기준 '어제'(직전 완료 거래일) — 러너는 UTC이므로 +9h 보정 후 하루 전.
    # 22:20 UTC(=익일 07:20 KST) 정기 실행 시 당일 KST 종가를 정확히 잡는다.
    kst = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(hours=9)).date()
    d = kst - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")  # 한국 공휴일이면 0행 응답 — status.csv로 확인

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

def fnum(v):
    """KRX 숫자 문자열('1,234.56', '-', '') → float 또는 None"""
    try:
        s = str(v).replace(",", "").strip()
        if s in ("", "-"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None

def fetch(path):
    url = f"{BASE}{path}?basDd={BAS}"
    req = urllib.request.Request(url, headers={"AUTH_KEY": KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    block = data.get("OutBlock_1")
    if not isinstance(block, list):
        block = next((v for v in data.values() if isinstance(v, list)), [])
    return data, block

def compute_ews(rows):
    """수집된 원본에서 조기경보 일일 지표를 계산해 dict로 반환. 실패 항목은 빈칸."""
    g = lambda k: rows.get(k) or []
    m = {}
    # 지수 종가 — KOSPI/KOSDAQ/KOSPI200
    def idx_close(block, name):
        for r in block:
            if str(r.get("IDX_NM", "")).strip() == name:
                return (fnum(r.get("CLSPRC_IDX")), fnum(r.get("FLUC_RT")),
                        fnum(r.get("ACC_TRDVAL")))
        return None, None, None
    m["kospi_close"], m["kospi_chg_rt"], tv = idx_close(g("idx/kospi_dd_trd"), "코스피")
    if tv is not None: m["kospi_trdval"] = int(tv)   # 거래대금(원) — 사냥 조건② 관측용
    m["kospi200_close"], _, _ = idx_close(g("idx/kospi_dd_trd"), "코스피 200")
    m["kosdaq_close"], m["kosdaq_chg_rt"], tv = idx_close(g("idx/kosdaq_dd_trd"), "코스닥")
    if tv is not None: m["kosdaq_trdval"] = int(tv)
    # V-KOSPI — 파생상품지수 중 '변동성' 포함 지수
    for r in g("idx/drvprod_dd_trd"):
        if "변동성" in str(r.get("IDX_NM", "")):
            m["vkospi_close"] = fnum(r.get("CLSPRC_IDX"))
            m["vkospi_chg_rt"] = fnum(r.get("FLUC_RT"))
            break
    # 시장 폭(breadth) — 유가+코스닥 상승/하락 종목 수
    adv = dec = 0
    for blk in (g("sto/stk_bydd_trd"), g("sto/ksq_bydd_trd")):
        for r in blk:
            v = fnum(r.get("FLUC_RT"))
            if v is None:
                continue
            if v > 0: adv += 1
            elif v < 0: dec += 1
    if adv or dec:
        m["adv_cnt"], m["dec_cnt"] = adv, dec
    # 레버리지·인버스 ETF 거래대금 (원) — 과열/공포 강도
    lev = inv = inv2x = 0
    for r in g("etp/etf_bydd_trd"):
        nm = str(r.get("ISU_NM", ""))
        val = fnum(r.get("ACC_TRDVAL")) or 0
        if "레버리지" in nm: lev += val
        if "인버스" in nm:
            inv += val
            if "2X" in nm.upper(): inv2x += val
    if lev or inv:
        m["lev_etf_val"], m["inv_etf_val"], m["inv2x_etf_val"] = int(lev), int(inv), int(inv2x)
    # 풋/콜 거래량 비율 — 옵션 일별매매에서 권리유형 합산
    put = call = 0
    for r in g("drv/opt_bydd_trd"):
        tp = str(r.get("RGHT_TP_NM", "")).upper()
        vol = fnum(r.get("ACC_TRDVOL")) or 0
        if "P" in tp or "풋" in tp: put += vol
        elif "C" in tp or "콜" in tp: call += vol
    if put or call:
        m["put_vol"], m["call_vol"] = int(put), int(call)
        m["pcr"] = round(put / call, 4) if call else None
    return m

EWS_COLS = ["bas_dd", "kospi_close", "kospi_chg_rt", "kospi200_close",
            "kosdaq_close", "kosdaq_chg_rt", "kospi_trdval", "kosdaq_trdval",
            "vkospi_close", "vkospi_chg_rt",
            "adv_cnt", "dec_cnt", "lev_etf_val", "inv_etf_val", "inv2x_etf_val",
            "put_vol", "call_vol", "pcr", "run_utc"]

def append_ews(metrics):
    path = "results/krx/ews_daily.csv"
    hist = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                hist[row["bas_dd"]] = row
    rec = {c: "" for c in EWS_COLS}
    rec["bas_dd"], rec["run_utc"] = BAS, now_utc()
    for k, v in metrics.items():
        if k in rec and v is not None:
            rec[k] = v
    hist[BAS] = {c: str(rec.get(c, "")) for c in EWS_COLS}
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EWS_COLS)
        w.writeheader()
        for d in sorted(hist):
            w.writerow({c: hist[d].get(c, "") for c in EWS_COLS})

def main():
    os.makedirs("results/krx/latest", exist_ok=True)
    os.makedirs("results/krx/roster", exist_ok=True)
    status, raw = [], {}

    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    friday = datetime.date.today().weekday() == 4
    targets = [(p, l, "results/krx/latest") for p, l in DAILY]
    if manual or friday:
        targets += [(p, l, "results/krx/roster") for p, l in WEEKLY]

    for path, label, outdir in targets:
        try:
            data, block = fetch(path)
            n = len(block)
            name = path.replace("/", "_")
            with open(f"{outdir}/{name}.json", "w") as f:
                json.dump(data, f, ensure_ascii=False)
            raw[path] = block
            status.append([path, label, "OK", n])
            print(f"OK   {path} ({label}): {n} rows")
        except urllib.error.HTTPError as e:
            status.append([path, label, f"HTTP {e.code}", 0])
            print(f"FAIL {path} ({label}): HTTP {e.code}"
                  + (" — 포털에서 이 서비스를 신청(또는 기간 갱신)해야 합니다"
                     if e.code in (401, 403) else ""))
        except Exception as e:
            status.append([path, label, f"ERR {type(e).__name__}", 0])
            print(f"ERR  {path}: {e}")

    try:
        append_ews(compute_ews(raw))
        print("EWS  ews_daily.csv 갱신 완료")
    except Exception as e:
        print(f"ERR  ews_daily 계산 실패: {e}")

    with open("results/krx/status.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "label", "status", "rows", "basDd", "run_utc"])
        for row in status:
            w.writerow(row + [BAS, now_utc()])
    ok = sum(1 for s in status if s[2] == "OK")
    print(f"\n요약: {ok}/{len(targets)} 서비스 성공 (basDd={BAS})")

if __name__ == "__main__":
    BAS = biz_day()
    main()
