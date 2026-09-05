# -*- coding: utf-8 -*-
"""KRX Open API 일일 수집 v3.1 — 조기경보(L2 지반) + 국내발굴 지원용 확장판.

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

v2 → v3 변경 (2026-09-05)
-------------------------
1) **백필 신설 (R-3).** v2는 매 실행이 «어제» 하루만 받아 ews_daily.csv 에 1행을 더했다.
   그래서 2026-09-05 시점의 유효 행이 8개(V-KOSPI 6개)뿐이고, 조기경보가 요구하는
   1년 백분위(p90)·중앙값을 **원리적으로 계산할 수 없었다**(미결 C-20·C-21).
   fss_fetch.py 가 이미 쓰던 「이력이 얕으면 과거를 함께 끌어온다」 패턴을 옮겨 왔다.
   ⚠️ KRX 호출 한도가 미확인이므로 **1회 실행에 다 하지 않는다** — 슬롯당 BACKFILL_MAX_DAYS
   만큼만 메우고, 하루 3슬롯이 쌓아 올린다. 245영업일은 약 2~3주면 찬다.
2) **빈 행을 쓰지 않는다 (R-4).** v2는 수집이 0행이어도 그 날짜 행을 만들었고, 이후
   어떤 실행도 그 행을 다시 채우지 않았다. 20260827 이 그렇게 «영구 결측»이 됐다.
   이제 지표가 하나도 없으면 행을 쓰지 않으며, 빈 행은 백필 대상으로 자동 재시도된다.
3) **results/_meta/source_health.csv 신설 (R-5).** 호출 단위 관측 기록(소스·오퍼레이션·
   HTTP·행수·소요·오류)을 **누적**한다. status.csv 는 매 실행 덮어쓰기라 「어느 슬롯이
   무엇을 채웠나」를 사후에 잴 수 없었다(R-9). 이 파일이 그 축을 갖는다.
4) 잔가지 — `urllib.error` 명시 import(R-7) · 주간 명부 판정을 KST 기준으로(R-8) ·
   전역 BAS 제거하고 인자로 전달(R-11).

v3 → v3.1 변경 (2026-09-05 · 실행 로그 실측 반영)
-------------------------------------------------
5) **0행을 성공으로 세지 않는다 (R-18).** 2026-09-05 07:24 KST 실행(krx-daily #27)이
   20개 서비스 전부 0행이었는데 요약은 「20/20 서비스 성공」이었다. HTTP 200 과
   「데이터를 받았다」는 다른 사건이다. 이제 status 는 OK / **EMPTY** / HTTP / ERR 로
   갈리고, 요약은 「데이터 n · 빈응답 n · 실패 n」으로 찍힌다. 한 건도 못 받으면 WARN.
6) **status.csv 의 basDd 가 행마다 다르다 (R-9 보강).** v3 는 백필 행에도 그날의
   `bas` 를 찍어 어느 날짜를 받은 기록인지 알 수 없었다. 이제 각 행이 자기 basDd 를 든다.

배경 실측 — KRX 는 D일 데이터를 **D+1 저녁**에야 공개한다(R-17). ews_daily.csv 에서 값이
채워진 6행의 run_utc 가 전부 13:0x~13:1x UTC(=22:0x KST)이고, 아침 슬롯(07:2x KST)과
심야 임시 실행이 남긴 2행은 전부 공란이다. 즉 아침 슬롯의 0행은 «장애가 아니라 정상»이며,
그래서 더더욱 성공으로 세면 안 된다 — 진짜 장애와 구분이 사라지기 때문이다.
"""
import os, json, csv, datetime, time, urllib.request, urllib.error

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

SERVICE_LABEL = {p: l for p, l in DAILY + WEEKLY}


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


# ── v3: 호출 단위 건강 기록 (R-5) ─────────────────────────────────────────
# status.csv 는 매 실행 «덮어쓰기»라 어느 슬롯이 무엇을 채웠는지 사후에 못 잰다(R-9).
# 이 파일은 누적한다 — 「설정 — 외부 데이터 관리 대장 (Live)」 §3 안정성 집계의 입력.
HEALTH_PATH = "results/_meta/source_health.csv"
HEALTH_COLS = ["run_ts", "source_id", "op", "bas_dd", "http_status", "rows", "elapsed_ms", "error"]
_health_rows = []


def health(op, basdd, status, rows, elapsed, err):
    _health_rows.append({
        "run_ts": now_utc(), "source_id": "S-12", "op": op, "bas_dd": basdd,
        "http_status": str(status), "rows": str(rows),
        "elapsed_ms": str(int(elapsed * 1000)), "error": err[:200],
    })


def flush_health():
    if not _health_rows:
        return
    os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
    new_file = not os.path.exists(HEALTH_PATH)
    with open(HEALTH_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEALTH_COLS)
        if new_file:
            w.writeheader()
        for r in _health_rows:
            w.writerow(r)
    print(f"HEALTH  {HEALTH_PATH} +{len(_health_rows)}행")


# ── v3: 백필 (R-3·R-4) ────────────────────────────────────────────────────
# 왜 필요한가 — v2 는 하루에 1행씩만 쌓아서 245영업일이 차려면 1년이 걸린다.
# 조기경보 절차서가 «정규 판정 성분»으로 지정한 V-KOSPI 백분위·레버리지 ETF 백분위가
# 그동안 계산 불가이고, 그 결과 KR L2 는 확정값이 아니라 «하한»으로만 나온다(C-21).
#
# ⚠️ 한 번에 다 받지 않는다. KRX 호출 한도가 미확인(카탈로그 미확인 항목)이므로
#    슬롯당 상한을 두고 3슬롯 × N일로 나눠 받는다. 실패해도 다음 슬롯이 이어받는다.
BACKFILL_MAX_DAYS = int(os.environ.get("KRX_BACKFILL_MAX_DAYS", "5"))
BACKFILL_TARGET   = int(os.environ.get("KRX_BACKFILL_TARGET", "245"))   # 1년 영업일
# ews 지표 계산에 실제로 쓰이는 서비스만 소급한다 — 17종을 다 받으면 호출이 3배가 된다.
BACKFILL_SERVICES = ["sto/stk_bydd_trd", "sto/ksq_bydd_trd", "idx/kospi_dd_trd",
                     "idx/kosdaq_dd_trd", "idx/drvprod_dd_trd", "etp/etf_bydd_trd",
                     "drv/opt_bydd_trd"]


def load_ews():
    """ews_daily.csv 를 {bas_dd: row} 로 읽는다. 없으면 빈 dict."""
    path = "results/krx/ews_daily.csv"
    hist = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                hist[row["bas_dd"]] = row
    return hist


def is_empty_row(row):
    """지표가 하나도 없는 행인가 — bas_dd·run_utc 말고 전부 공란이면 참."""
    return not any(str(row.get(c, "")).strip() for c in EWS_COLS
                   if c not in ("bas_dd", "run_utc"))


def backfill_targets(hist, upto):
    """메워야 할 거래일을 최신순으로 고른다.

    두 종류를 함께 잡는다 —
      ① 아예 없는 날짜 (수집이 시작되기 전의 과거)
      ② 있는데 비어 있는 날짜 (v2 가 공개 전에 돌아 만든 유령 행 — 20260827 이 그것)
    ②를 함께 잡는 것이 R-4 의 소급 보정이다. v2 는 이 행들을 영원히 다시 보지 않았다.
    """
    have = {d for d, r in hist.items() if not is_empty_row(r)}
    out, d = [], datetime.datetime.strptime(upto, "%Y%m%d").date()
    scanned = 0
    while len(out) < BACKFILL_MAX_DAYS and scanned < BACKFILL_TARGET:
        if d.weekday() < 5:
            scanned += 1
            key = d.strftime("%Y%m%d")
            if key not in have:
                out.append(key)
        d -= datetime.timedelta(days=1)
    return out

def fnum(v):
    """KRX 숫자 문자열('1,234.56', '-', '') → float 또는 None"""
    try:
        s = str(v).replace(",", "").strip()
        if s in ("", "-"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None

def fetch(path, basdd):
    """한 서비스를 한 거래일치 받는다. (v3: 전역 BAS 대신 인자 — R-11)"""
    url = f"{BASE}{path}?basDd={basdd}"
    req = urllib.request.Request(url, headers={"AUTH_KEY": KEY})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    block = data.get("OutBlock_1")
    if not isinstance(block, list):
        block = next((v for v in data.values() if isinstance(v, list)), [])
    health(path, basdd, 200, len(block), time.time() - t0, "")
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
    # 쏠림도 — 유가+코스닥 상위 2종목 시가총액 비중 (조기경보 L2 관측, 2026-08-28 신설)
    caps, total = [], 0.0
    for blk in (g("sto/stk_bydd_trd"), g("sto/ksq_bydd_trd")):
        for r in blk:
            v = fnum(r.get("MKTCAP"))
            if v and v > 0:
                caps.append(v); total += v
    if total and len(caps) >= 2:
        caps.sort(reverse=True)
        m["top2_mcap_pct"] = round((caps[0] + caps[1]) / total * 100, 2)
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
            "top2_mcap_pct", "put_vol", "call_vol", "pcr", "run_utc"]

def append_ews(metrics, basdd, hist=None):
    """한 거래일치 지표를 upsert 한다.

    v3 변경 (R-4) — **지표가 하나도 없으면 행을 쓰지 않는다.**
    v2 는 수집 0행이어도 빈 행을 만들었고, 그 행은 이후 어떤 실행도 다시 채우지 않았다
    (`hist[BAS] = rec` 가 매번 덮어썼으므로 «있다»고 판정되어 백필 대상도 아니었다).
    2026-08-27 이 그렇게 8일 넘게 영구 결측으로 남았다. 이제 그런 날짜는 파일에
    나타나지 않고, 다음 실행의 백필 대상으로 자동 재시도된다.
    """
    path = "results/krx/ews_daily.csv"
    if hist is None:
        hist = load_ews()
    rec = {c: "" for c in EWS_COLS}
    rec["bas_dd"], rec["run_utc"] = basdd, now_utc()
    got = 0
    for k, v in metrics.items():
        if k in rec and v is not None:
            rec[k] = v
            got += 1
    if got == 0:
        print(f"SKIP {basdd}: 지표 0개 — 행을 쓰지 않는다 (미공개이거나 휴장)")
        return hist, False
    hist[basdd] = {c: str(rec.get(c, "")) for c in EWS_COLS}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EWS_COLS)
        w.writeheader()
        for d in sorted(hist):
            w.writerow({c: hist[d].get(c, "") for c in EWS_COLS})
    return hist, True


def collect_day(basdd, services, outdir=None, save_json=False):
    """한 거래일치를 받아 (raw, status행들) 로 돌려준다. 백필과 정기 수집이 함께 쓴다."""
    raw, status = {}, []
    for path in services:
        label = SERVICE_LABEL.get(path, path)
        try:
            data, block = fetch(path, basdd)
            if save_json and outdir:
                os.makedirs(outdir, exist_ok=True)
                with open(f"{outdir}/{path.replace('/', '_')}.json", "w") as f:
                    json.dump(data, f, ensure_ascii=False)
            raw[path] = block
            # v3.1 (R-18): HTTP 200 이지만 0행인 응답을 «성공»으로 세지 않는다.
            # 2026-09-05 krx-daily #27 이 20건 전부 0행이었는데 요약은 「20/20 성공」이었다.
            # KRX 는 D일 데이터를 D+1 저녁에야 공개하므로 0행은 정상적인 «아직 없음»일 수도,
            # 서비스 해지·명세 변경일 수도 있다. 둘을 구분하는 것은 이 스크립트의 일이 아니고,
            # 「받은 게 없다」를 성공 칸에서 빼는 것까지가 이 스크립트의 일이다.
            st = "OK" if block else "EMPTY"
            status.append([path, label, st, len(block), basdd])
            print(f"{'OK   ' if block else 'EMPTY'} {path} ({label}) {basdd}: {len(block)} rows")
        except urllib.error.HTTPError as e:
            status.append([path, label, f"HTTP {e.code}", 0, basdd])
            health(path, basdd, e.code, 0, 0.0, str(e)[:200])
            print(f"FAIL {path} ({label}): HTTP {e.code}"
                  + (" — 포털에서 이 서비스를 신청(또는 기간 갱신)해야 합니다"
                     if e.code in (401, 403) else ""))
        except Exception as e:
            status.append([path, label, f"ERR {type(e).__name__}", 0, basdd])
            health(path, basdd, "ERR", 0, 0.0, f"{type(e).__name__}: {e}")
            print(f"ERR  {path}: {e}")
    return raw, status

def main():
    os.makedirs("results/krx/latest", exist_ok=True)
    os.makedirs("results/krx/roster", exist_ok=True)
    bas = biz_day()

    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    # v3 (R-8): 주간 명부 판정을 **KST 기준**으로 바꿨다.
    # v2 는 `datetime.date.today()`(러너 UTC)를 썼다. 22:20 UTC 금요일 = KST 토요일 07:20 이라
    # 결과적으로 토 10:00 국내발굴 직전에 명부가 갱신되어 «맞게» 동작했으나, 그 정합이
    # 코드 어디에도 적혀 있지 않았다. biz_day() 는 KST 를 쓰는데 이 줄만 UTC 인 것도 어긋났다.
    # 이제 「직전 완료 거래일이 금요일이면」으로 읽는다 — 의도와 문면이 같다.
    friday = datetime.datetime.strptime(bas, "%Y%m%d").weekday() == 4

    targets = [(p, "results/krx/latest") for p, _ in DAILY]
    if manual or friday:
        targets += [(p, "results/krx/roster") for p, _ in WEEKLY]

    # ── 1) 정기 수집 — 직전 거래일 ────────────────────────────────────────
    raw, status = {}, []
    for outdir in ("results/krx/latest", "results/krx/roster"):
        svc = [p for p, o in targets if o == outdir]
        if not svc:
            continue
        r, s = collect_day(bas, svc, outdir=outdir, save_json=True)
        raw.update(r)
        status += s

    hist = load_ews()
    try:
        hist, wrote = append_ews(compute_ews(raw), bas, hist)
        print("EWS  ews_daily.csv 갱신 완료" if wrote else "EWS  이번 거래일은 아직 미공개")
    except Exception as e:
        print(f"ERR  ews_daily 계산 실패: {e}")

    # ── 2) 백필 — 비었거나 없는 과거 거래일 (R-3·R-4) ─────────────────────
    # 슬롯당 BACKFILL_MAX_DAYS 만큼만. 3슬롯 × 5일이면 245영업일이 약 2~3주에 찬다.
    # ⚠️ KRX 호출 한도가 미확인이라 의도적으로 느리게 간다 — 한도를 재고 나면 올린다.
    filled = 0
    for d in backfill_targets(hist, bas):
        if d == bas:
            continue
        r, s = collect_day(d, BACKFILL_SERVICES)
        status += s
        try:
            hist, wrote = append_ews(compute_ews(r), d, hist)
            if wrote:
                filled += 1
        except Exception as e:
            print(f"ERR  백필 {d} 계산 실패: {e}")
    depth = sum(1 for row in hist.values() if str(row.get("vkospi_close", "")).strip())
    print(f"BACKFILL 이번 실행 {filled}일 채움 · V-KOSPI 유효 {depth}행 "
          f"(목표 {BACKFILL_TARGET})")

    # ── 3) 상태 기록 ─────────────────────────────────────────────────────
    with open("results/krx/status.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["service", "label", "status", "rows", "basDd", "run_utc"])
        for row in status:
            w.writerow(row + [now_utc()])
    flush_health()
    ok    = sum(1 for s in status if s[2] == "OK")
    empty = sum(1 for s in status if s[2] == "EMPTY")
    bad   = len(status) - ok - empty
    print(f"\n요약: 호출 {len(status)}건 — 데이터 {ok} · 빈응답 {empty} · 실패 {bad} "
          f"(basDd={bas}) · 백필 {filled}일 · V-KOSPI 이력 {depth}행")
    if ok == 0 and status:
        print("WARN 이번 실행은 «한 건도» 데이터를 받지 못했다 — "
              "KRX 미공개 시각이거나 서비스·키 상태를 확인해야 한다.")


if __name__ == "__main__":
    main()
