#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fss_fetch.py — 공공데이터포털(금융위원회 계열) 조기경보 지표 수집기  v1.0 (2026-08-29)

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
   → 1페이지와 마지막 페이지를 모두 받아 basDt로 정렬한 뒤 최신을 고른다.
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
UA = {"User-Agent": "ews-fss-collector/1.0"}


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


def fetch_series(base, op, n=25, extra=""):
    """정렬 방향을 모르는 시계열에서 최신 n건을 안전하게 확보한다.
    1페이지와 마지막 페이지를 모두 받아 합친 뒤 basDt 내림차순으로 자른다."""
    rows, notes = [], []
    b1, m1 = call(base, op, f"numOfRows={n}&pageNo=1&{extra}".rstrip("&"))
    notes.append(m1)
    if b1 is None:
        log(op, m1, 0, "page1 실패")
        return []
    rows += items_of(b1)
    try:
        total = int(b1.get("totalCount", 0))
    except Exception:
        total = 0
    if total > n:
        last_page = (total + n - 1) // n
        if last_page > 1:
            b2, m2 = call(base, op, f"numOfRows={n}&pageNo={last_page}&{extra}".rstrip("&"))
            notes.append(m2)
            if b2 is not None:
                rows += items_of(b2)
    rows = [r for r in rows if str(r.get("basDt", "")).strip()]
    rows.sort(key=lambda r: str(r["basDt"]), reverse=True)
    seen, uniq = set(), []
    for r in rows:
        k = str(r["basDt"])
        if k not in seen:
            seen.add(k); uniq.append(r)
    log(op, "00", len(uniq), f"total={total} 최신={uniq[0]['basDt'] if uniq else '-'}")
    return uniq[:n]


def num(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────── 1) 신용공여잔고추이
def collect_credit():
    out = {}
    for r in fetch_series(B_KOFIA, "getGrantingOfCreditBalanceInfo", 25):
        d = str(r["basDt"])
        out[d] = {
            "credit_loan_total":  num(r.get("crdTrFingWhl")),
            "credit_loan_kospi":  num(r.get("crdTrFingScrs")),
            "credit_loan_kosdaq": num(r.get("crdTrFingKosdaq")),
            "credit_short_total": num(r.get("crdTrLndrWhl")),
            "deposit_collateral_loan": num(r.get("dpsgScrtMogFing")),
        }
    return out


# ─────────────────────────────── 2) 증시자금추이 (반대매매) — 세션 호출 불가 구간
def collect_market_cash():
    out = {}
    for r in fetch_series(B_KOFIA, "getSecuritiesMarketTotalCapitalInfo", 25):
        d = str(r["basDt"])
        out[d] = {
            "investor_deposit":   num(r.get("invrDpsgAmt")),
            "unpaid_broker":      num(r.get("brkTrdUcolMny")),
            "forced_liq_amt":     num(r.get("brkTrdUcolMnyVsOppsTrdAmt")),
            "forced_liq_ratio":   num(r.get("ucolMnyVsOppsTrdRlImpt")),
        }
    return out


# ─────────────────────────────────────────────────────────── 3) 일자별 CMA 현황
def collect_cma():
    out = {}
    for r in fetch_series(B_KOFIA, "getCMAStatus", 60):
        d = str(r["basDt"])
        bal = num(r.get("actBal")) or 0.0
        cur = out.setdefault(d, {"cma_balance_total": 0.0})
        cur["cma_balance_total"] += bal      # 운용대상·투자자 구분 합산
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

    merge(collect_credit())
    merge(collect_market_cash())
    merge(collect_cma())

    # ETF는 날짜 지정이 필요하다. 최근 영업일부터 거슬러 최대 5일 시도(D+1 공개 규칙).
    for bd in biz_days_back(5):
        agg = collect_etf(bd)
        if agg:
            merged.setdefault(bd, {}).update(agg)
            break

    n = upsert(f"{OUT}/ews_fss.csv", merged)
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
