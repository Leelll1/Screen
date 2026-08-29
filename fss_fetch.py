#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fss_fetch.py — 공공데이터포털(금융위원회 계열) 조기경보 지표 수집기  v1.2 (2026-08-30)

왜 이 스크립트가 따로 있나
--------------------------
· 증시자금추이(반대매매)는 세션의 WebFetch에서 경로 단위로 403이 난다. 실측 확인됨.
  → 인터넷 제약이 없는 GitHub Actions에서만 수집 가능하다. 그것이 이 파일의 존재 이유다.
· krx_fetch.py를 건드리지 않는다. 수집 계층을 분리해 한쪽 실패가 다른 쪽을 죽이지 않게 한다.

산출
----
results/fss/ews_fss.csv    일별 1행 누적 (basDt 키로 upsert)
results/fss/roster_map.csv 단축코드·ISIN·법인등록번호·회사명 4중 매핑 (주 1회 갱신)
results/fss/status.csv     오퍼레이션별 수집 상태 자가진단

주의 (실측으로 확립된 함정)
---------------------------
1) 인증키는 이미 URL 인코딩된 문자열이다. 재인코딩하면 code 30이 난다.
   → params= 딕셔너리를 쓰지 않고 URL에 문자열로 이어 붙인다.
2) 정렬 방향이 오퍼레이션마다 다르다. 첫 페이지만 보고 "최신 없음"을 판정하면 오판이다.
   → **v1.1**: 1페이지로 방향을 판정한 뒤 필요한 쪽으로만 페이지를 넘긴다.
     v1.0은 방향과 무관하게 마지막 페이지를 함께 받았는데, 최신순 계열에서
     '가장 오래된 페이지'가 딸려 들어와 유령 행을 만들었다(첫 수집에서 실측).
5) 하루에 여러 행이 오는 계열(CMA: 운용대상×투자자 구분)은 날짜별로 합산해야 한다.
   v1.0은 basDt로 dedup한 뒤 합산해 **한 구분의 잔액을 전체로 적었다**(실측 교정).
   **v1.2**: 그런데 응답에 `mngInvTgt="합계"` 행이 함께 온다. 전부 더하면 정확히
   2배가 된다(실측: 210.4조 vs 실제 105.2조). 집계 행과 명세 행을 구분해야 한다.
   — 「하루에 여러 행이 온다」를 알아챈 것만으로는 부족하고, **그 여러 행이 서로
     배타적인지**까지 확인해야 한다는 사례로 남긴다.
6) 백분위 판정에는 이력이 필요하다. CSV가 얕으면 첫 회차에 과거를 함께 끌어온다.
3) 필수 파라미터가 오퍼레이션마다 다르다. 여분은 code 10, 누락은 code 11.
4) 실패는 status.csv에 남기고 계속 진행한다. 한 지표 실패가 회차를 죽이지 않는다.
"""

import os, sys, csv, json, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

KEY = os.environ.get("DATA_GO_KR_KEY", "").strip()
OUT = "results/fss"
KST = timezone(timedelta(hours=9))

B_KOFIA = "https://apis.data.go.kr/1160100/service/GetKofiaStatisticsInfoService"
B_SECPRD = "https://apis.data.go.kr/1160100/service/GetSecuritiesProductInfoService"
B_KRXLST = "https://apis.data.go.kr/1160100/service/GetKrxListedInfoService"

STATUS = []          # [op, http/code, rows, note]
UA = {"User-Agent": "ews-fss-collector/1.2"}


def log(op, code, rows, note=""):
    STATUS.append([op, str(code), str(rows), note])
    print(f"[{op}] code={code} rows={rows} {note}", flush=True)


def call(base, op, qs, tries=3):
    """키를 문자열로 이어 붙여 호출하고 JSON dict를 돌려준다. 실패 시 None."""
    url = f"{base}/{op}?serviceKey={KEY}&resultType=json&{qs}"
    last = ""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8", "replace")
            if body.lstrip().startswith("<"):
                # XML 오류 응답 — 코드만 뽑아 기록
                import re
                m = re.search(r"<resultCode>(\d+)</resultCode>", body)
                n = re.search(r"<resultMsg>([^<]*)</resultMsg>", body)
                last = f"XML {m.group(1) if m else '?'} {n.group(1) if n else ''}"
                return None, last
            d = json.loads(body)
            hdr = d.get("response", {}).get("header", {})
            rc = str(hdr.get("resultCode", "?"))
            if rc not in ("00", "0"):
                return None, f"{rc} {hdr.get('resultMsg','')}"
            return d.get("response", {}).get("body", {}), "ok"
        except Exception as e:
            last = f"EXC {type(e).__name__}: {e}"
            time.sleep(2 + 3 * i)
    return None, last


def items_of(body):
    """items 구조가 dict/list 양쪽으로 오는 것을 흡수한다."""
    if not body:
        return []
    it = body.get("items")
    if it in (None, "", []):
        return []
    if isinstance(it, dict):
        it = it.get("item", [])
    if isinstance(it, dict):
        return [it]
    return it or []


def fetch_series(base, op, want=30, extra="", page_size=100, max_pages=8):
    """최신 want개 **날짜**의 원자료 행을 확보한다. (v1.1 재작성)

    반환: (rows, boundary)
      rows     — 최신 want개 날짜에 속하는 모든 원자료 행 (dedup하지 않는다.
                 하루에 여러 행이 오는 계열은 호출부가 합산해야 하기 때문이다)
      boundary — 페이지를 다 읽지 못한 채 멈췄을 때 '경계에서 잘렸을 수 있는'
                 가장 오래된 날짜. 합산 계열은 이 날짜를 버려야 과소집계를 피한다.

    v1.0에서 무엇이 틀렸나 — 방향과 무관하게 1페이지와 마지막 페이지를 둘 다 받았다.
    최신순 계열에서는 마지막 페이지가 '가장 오래된 자료'이고, 하루에 여러 행이 오는
    CMA에서는 고유 날짜 수가 want에 못 미쳐 그 오래된 날짜가 결과에 그대로 남았다.
    2026-08-30 첫 수집에서 2021-10-26~28 유령 행으로 실제 발현됐다.
    """
    qs = lambda p: f"numOfRows={page_size}&pageNo={p}&{extra}".rstrip("&")
    b1, m1 = call(base, op, qs(1))
    if b1 is None:
        log(op, m1, 0, "page1 실패")
        return [], None
    first = [r for r in items_of(b1) if str(r.get("basDt", "")).strip()]
    if not first:
        log(op, "00", 0, "1페이지 0행")
        return [], None
    try:
        total = int(b1.get("totalCount", 0))
    except Exception:
        total = 0
    last_page = max(1, (total + page_size - 1) // page_size)

    # 정렬 방향 판정 — 1페이지 안에서 첫 항목이 마지막 항목보다 최신이면 최신순이다.
    desc = str(first[0]["basDt"]) >= str(first[-1]["basDt"])

    if desc:
        rows, seq = list(first), list(range(2, last_page + 1))
    else:
        rows, seq = [], list(range(last_page, 0, -1))   # 1페이지(가장 과거)는 버린다

    used, exhausted = 1, (last_page <= 1)
    for pg in seq:
        if len({str(r["basDt"]) for r in rows}) >= want:
            break
        if used >= max_pages:
            break
        b, m = call(base, op, qs(pg))
        used += 1
        if b is None:
            break
        got = [r for r in items_of(b) if str(r.get("basDt", "")).strip()]
        rows += got
        if pg == (last_page if desc else 1):
            exhausted = True
        if not got:
            break

    dates = sorted({str(r["basDt"]) for r in rows}, reverse=True)
    keep = set(dates[:want])
    rows = [r for r in rows if str(r["basDt"]) in keep]
    rows.sort(key=lambda r: str(r["basDt"]), reverse=True)   # 호출부가 최신부터 읽는다
    boundary = None if exhausted else (min(keep) if keep else None)
    log(op, "00", len(keep),
        f"total={total} 최신={dates[0] if dates else '-'} "
        f"정렬={'최신순' if desc else '오래된순'} 페이지={used}"
        + (f" 경계버림={boundary}" if boundary else ""))
    return rows, boundary


def one_per_date(rows):
    """날짜당 1행만 오는 계열용 — 첫 등장만 취한다."""
    out = {}
    for r in rows:
        out.setdefault(str(r["basDt"]), r)
    return out


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────── 1) 신용공여잔고추이
def collect_credit(want=30):
    rows, _ = fetch_series(B_KOFIA, "getGrantingOfCreditBalanceInfo", want)
    out = {}
    for d, r in one_per_date(rows).items():
        out[d] = {
            "credit_loan_total":  num(r.get("crdTrFingWhl")),
            "credit_loan_kospi":  num(r.get("crdTrFingScrs")),
            "credit_loan_kosdaq": num(r.get("crdTrFingKosdaq")),
            "credit_short_total": num(r.get("crdTrLndrWhl")),
            "deposit_collateral_loan": num(r.get("dpsgScrtMogFing")),
        }
    return out


# ─────────────────────────────── 2) 증시자금추이 (반대매매) — 세션 호출 불가 구간
def collect_market_cash(want=30):
    rows, _ = fetch_series(B_KOFIA, "getSecuritiesMarketTotalCapitalInfo", want)
    out = {}
    for d, r in one_per_date(rows).items():
        out[d] = {
            "investor_deposit":   num(r.get("invrDpsgAmt")),
            "unpaid_broker":      num(r.get("brkTrdUcolMny")),
            "forced_liq_amt":     num(r.get("brkTrdUcolMnyVsOppsTrdAmt")),
            "forced_liq_ratio":   num(r.get("ucolMnyVsOppsTrdRlImpt")),
        }
    return out


# ─────────────────────────────────────────────────────────── 3) 일자별 CMA 현황
AGG_LABELS = {"합계", "총계", "소계", "전체", "계"}


def collect_cma(want=30):
    """CMA는 하루에 여러 행(운용대상 × 투자자 구분)이 온다 — 날짜별로 합산해야 한다.

    v1.0: basDt로 dedup한 뒤 합산해 실제로는 **한 구분의 잔액**(MMF형 개인)을
          'cma_balance_total'로 적었다. 컬럼 이름은 총계인데 값은 총계가 아니었다.
    v1.2: dedup을 걷어내고 전부 더했더니 이번엔 **정확히 2배**가 됐다.
          응답에 `mngInvTgt="합계"` 행이 명세 행과 **함께** 오기 때문이다.
          실측 20260826 — 명세 10행 합 = 합계 2행 합 = 105조 2,177억,
          둘을 다 더하면 210조 4,355억.
          → 합계 행이 있으면 그것만 쓰고(원자료가 직접 준 값이므로 더 정확하다),
            없으면 명세 행만 더한다.
    """
    rows, boundary = fetch_series(B_KOFIA, "getCMAStatus", want, page_size=100, max_pages=6)
    by_date = {}
    for r in rows:
        d = str(r["basDt"])
        if d == boundary:          # 페이지 경계에서 잘렸을 수 있는 날짜 — 합계가 과소가 된다
            continue
        by_date.setdefault(d, []).append(r)

    out, used_agg = {}, 0
    for d, rs in by_date.items():
        agg   = [r for r in rs if str(r.get("mngInvTgt", "")).strip() in AGG_LABELS]
        parts = [r for r in rs if str(r.get("mngInvTgt", "")).strip() not in AGG_LABELS]
        use = agg if agg else parts
        if agg:
            used_agg += 1
        out[d] = {"cma_balance_total": sum((num(r.get("actBal")) or 0.0) for r in use)}
    if by_date:
        log("getCMAStatus:합계행", "-", used_agg, f"날짜 {len(by_date)}개 중 합계행 사용 {used_agg}개")
    return out


# ────────────────────────────────────────── 4) ETF 시세 → 레버리지·인버스 거래대금
def collect_etf(basdt):
    agg = {"etf_total_trprc": 0.0, "etf_lev_trprc": 0.0,
           "etf_inv_trprc": 0.0, "etf_inv2x_trprc": 0.0,
           "etf_lev_lstg_cnt": 0.0}
    page, got = 1, 0
    while page <= 15:
        b, m = call(B_SECPRD, "getETFPriceInfo", f"numOfRows=1000&pageNo={page}&basDt={basdt}")
        if b is None:
            log("getETFPriceInfo", m, got, f"basDt={basdt} page={page}")
            return None if got == 0 else agg
        rows = items_of(b)
        if not rows:
            break
        for r in rows:
            nm = str(r.get("itmsNm", ""))
            tp = num(r.get("trPrc")) or 0.0
            agg["etf_total_trprc"] += tp
            if "레버리지" in nm:
                agg["etf_lev_trprc"] += tp
                agg["etf_lev_lstg_cnt"] += (num(r.get("stLstgCnt")) or 0.0)
            if "인버스" in nm:
                agg["etf_inv_trprc"] += tp
                if "2X" in nm.upper() or "2배" in nm:
                    agg["etf_inv2x_trprc"] += tp
        got += len(rows)
        try:
            if got >= int(b.get("totalCount", 0)):
                break
        except Exception:
            break
        page += 1
    if got == 0:
        log("getETFPriceInfo", "00", 0, f"basDt={basdt} 0행 — 미공개(D+1 규칙)")
        return None
    log("getETFPriceInfo", "00", got, f"basDt={basdt} 레버리지 {agg['etf_lev_trprc']:.0f}")
    return agg


# ────────────────────────────────────────────────── 5) KRX 상장종목 매핑표 (주 1회)
def collect_roster(basdt):
    rows, page, got = [], 1, 0
    while page <= 10:
        b, m = call(B_KRXLST, "getItemInfo", f"numOfRows=1000&pageNo={page}&basDt={basdt}")
        if b is None:
            log("getItemInfo", m, got, f"basDt={basdt} page={page}")
            return None
        it = items_of(b)
        if not it:
            break
        rows += it
        got += len(it)
        try:
            if got >= int(b.get("totalCount", 0)):
                break
        except Exception:
            break
        page += 1
    if not rows:
        log("getItemInfo", "00", 0, f"basDt={basdt} 0행")
        return None
    log("getItemInfo", "00", got, f"basDt={basdt}")
    return rows


# ─────────────────────────────────────────────────────────────────────── 유틸
def biz_days_back(n=7):
    """KST 기준 오늘부터 거슬러 올라가며 평일 목록 (공휴일은 API 0행으로 자연 처리)."""
    d, out = datetime.now(KST).date(), []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
    return out


FIELDS = ["basDt",
          "credit_loan_total", "credit_loan_kospi", "credit_loan_kosdaq",
          "credit_short_total", "deposit_collateral_loan",
          "investor_deposit", "unpaid_broker", "forced_liq_amt", "forced_liq_ratio",
          "cma_balance_total",
          "etf_total_trprc", "etf_lev_trprc", "etf_inv_trprc", "etf_inv2x_trprc",
          "etf_lev_lstg_cnt"]


def history_depth(path, col):
    """CSV에 그 컬럼이 실제로 채워진 행이 몇 개인가."""
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if str(row.get(col, "")).strip():
                n += 1
    return n


def prune_orphans(path):
    """신용·증시자금·ETF 어느 것도 없는, 홀로 떠 있는 과거 행을 제거한다.

    v1.0의 '마지막 페이지 무조건 수신' 때문에 CMA 전용 유령 행(2021년)이 섞였다.
    기준: 실제 지표가 하나라도 있는 가장 오래된 날짜보다 앞선 행은 버린다.
    """
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    anchor = [r.get("basDt", "") for r in rows
              if str(r.get("credit_loan_total", "")).strip()
              or str(r.get("investor_deposit", "")).strip()
              or str(r.get("etf_total_trprc", "")).strip()]
    if not anchor:
        return 0
    floor = min(anchor)
    keep = [r for r in rows if str(r.get("basDt", "")) >= floor]
    dropped = len(rows) - len(keep)
    if dropped:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in sorted(keep, key=lambda x: str(x.get("basDt", ""))):
                w.writerow(r)
    return dropped


def upsert(path, merged):
    """기존 CSV를 읽어 basDt 키로 갱신·추가한다. 값이 있는 필드만 덮어쓴다."""
    hist = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                hist[row.get("basDt", "")] = row
    for d, vals in merged.items():
        row = hist.get(d, {k: "" for k in FIELDS})
        row["basDt"] = d
        for k, v in vals.items():
            if v is not None:
                row[k] = ("%.4f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
        for k in FIELDS:
            row.setdefault(k, "")
        hist[d] = row
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in sorted(hist):
            w.writerow(hist[d])
    return len(hist)


def main():
    if not KEY:
        print("DATA_GO_KR_KEY 미설정 — 중단", file=sys.stderr)
        sys.exit(1)
    os.makedirs(OUT, exist_ok=True)
    merged = {}

    def merge(d):
        for k, v in (d or {}).items():
            merged.setdefault(k, {}).update(v)

    # 백분위 판정에는 1년(약 245영업일) 이력이 필요하다. CSV가 얕으면 과거를 함께
    # 끌어온다 — API에는 4.7년치가 있는데 CSV에 25일치만 쌓으면 회차는 1년 내내
    # '잠정' 판정에 묶인다. 승격의 취지와 정반대가 된다.
    deep = history_depth(f"{OUT}/ews_fss.csv", "credit_loan_total")
    want = 30 if deep >= 300 else 420
    print(f"신용·증시자금 이력: 기존 {deep}행 → 이번 회차 {want}일 요청", flush=True)

    merge(collect_credit(want))
    merge(collect_market_cash(want))
    merge(collect_cma(30))

    # ETF는 날짜 지정이 필요하다. 최근 영업일부터 거슬러 최대 5일 시도(D+1 공개 규칙).
    for bd in biz_days_back(5):
        agg = collect_etf(bd)
        if agg:
            merged.setdefault(bd, {}).update(agg)
            break

    n = upsert(f"{OUT}/ews_fss.csv", merged)
    d = prune_orphans(f"{OUT}/ews_fss.csv")
    if d:
        n -= d
        print(f"유령 행 {d}개 제거 (v1.0 잔재)", flush=True)
    print(f"ews_fss.csv 누적 {n}행", flush=True)

    # 매핑표는 월요일(KST)에만 갱신 — 상장 명부는 매일 바뀌지 않는다.
    if datetime.now(KST).weekday() == 0 or not os.path.exists(f"{OUT}/roster_map.csv"):
        for bd in biz_days_back(5):
            rows = collect_roster(bd)
            if rows:
                with open(f"{OUT}/roster_map.csv", "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["basDt", "srtnCd", "isinCd", "crno", "mrktCtg", "itmsNm", "corpNm"])
                    for r in rows:
                        w.writerow([r.get("basDt", ""), r.get("srtnCd", ""), r.get("isinCd", ""),
                                    r.get("crno", ""), r.get("mrktCtg", ""),
                                    r.get("itmsNm", ""), r.get("corpNm", "")])
                print(f"roster_map.csv {len(rows)}종목 (basDt={bd})", flush=True)
                break

    with open(f"{OUT}/status.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["op", "code", "rows", "note", "run_at_kst"])
        now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        for s in STATUS:
            w.writerow(s + [now])
    print("status.csv 기록 완료", flush=True)


if __name__ == "__main__":
    main()
