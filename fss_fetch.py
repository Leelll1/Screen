#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fss_fetch.py — 공공데이터포털(금융위원회 계열) 조기경보 지표 수집기  v1.4 (2026-09-24)

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
results/fss/etf_skip.csv   (v1.4) ETF 백필에서 0행·불완전·포털 오류가 반복된 과거 날짜 — 30일 동안 다시 묻지 않는다
results/fss/run_alarm.txt  (v1.4) 이 실행이 실패·부분 실패였을 때만 생긴다. 정상 실행이 지운다
results/fss/accept_revision.txt (v1.4 · 사람이 만든다) 과거 값 정정을 받아들일 지표 이름 — 적용되면 스크립트가 지운다

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
7) **v1.3 — 포털 도달 불가는 '지표 실패'가 아니라 '경로 실패'다.**
   2026-08-30 실측: GitHub 러너에서 apis.data.go.kr 전 호출이 타임아웃 났다.
   같은 시각 같은 키로 다른 경로(세션)에서는 정상 응답이었으므로 포털도 키도
   정상이었고, 막힌 것은 러너의 경로다. 그런데 v1.2는 8개 오퍼레이션을 각각
   3회 × 40초씩 기다리며 **17분을 태우고** 똑같은 오류 8줄만 남겼다.
   → 연속 타임아웃이 4회에 이르면 경로 자체가 죽은 것으로 보고 **남은 호출을
     건너뛴다.** 실패가 3분 안에 드러나고, 막힌 서버를 계속 두드리지 않는다.
   → 호출 사이에 짧은 간격(PACE)을 둔다. 직전 대량 호출 직후에 차단이 시작된
     정황이 있어, 재발 유발을 줄이기 위한 예방이다 — 원인 확정은 아니다.
8) **v1.4 — 독립 검토 결함 12건(F-1~F-12) 교정.** 원칙은 셋이다.
   ⓐ 이력은 줄지 않는다 — 좋은 값을 0·부분합·빈 행으로 덮지 않고, 행을 지우는 것은
      «값이 하나도 없는 행»뿐이다(F-3·F-4·F-5·F-8·F-10·F-12). 매 실행 과거 420일을 다시 받으므로
      포털이 과거 값을 한꺼번에 0 으로·절반 넘게 바꿔 보내면 덮지 않고 알람을 낸다(guard_revisions).
   ⓑ 이력은 스스로 깊어지고 스스로 메워진다 — 신용·증시자금·CMA 는 매 실행 최신
      TARGET_DATES 개 날짜를 요청하므로 창 안의 구멍이 다음 실행에 메워진다(F-1·F-7).
      ETF 는 날짜마다 따로 불러야 하므로 실행당 ETF_BACKFILL_MAX 개씩 빈 날짜를 채운다(F-2).
   ⓒ 실패는 실패로 보인다 — 호출 실패·부분 응답·핵심 계열 0건이면 run_alarm.txt 를 쓰고
      종료코드 1 로 끝난다(F-6·F-11). 쓰기는 전부 임시 파일 → os.replace(F-9).
      워크플로가 fss 단계를 continue-on-error 로 돌리므로 종료코드 1 이어도 커밋은 된다.
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
UA = {"User-Agent": "ews-fss-collector/1.4"}

# v1.4 — 이력 깊이와 백필
TARGET_DATES = 420       # 신용·증시자금·CMA 를 매 실행 이만큼(날짜 수) 요청한다. 백분위 창 245영업일 + 여유
KOFIA_PAGE = 1000        # 한 페이지 행 수. 서버가 더 적게 주면 fetch_series 가 실제 크기로 다시 계산한다
ETF_BACKFILL_MAX = 20    # 실행당 ETF 백필 날짜 상한(날짜당 1~2콜). 하루 3슬롯이면 약 60일/일
ETF_RECENT_GUARD = 3     # 최근 이 영업일 수 안의 0행은 «미공개»일 수 있으므로 건너뛰기 목록에 올리지 않는다
ETF_SKIP_AFTER = 2       # 과거 날짜가 0행·불완전·포털 오류 코드를 이 횟수 받으면 다시 묻지 않는다(경로 장애는 세지 않는다)
ALARM_PATH = f"{OUT}/run_alarm.txt"
SKIP_PATH = f"{OUT}/etf_skip.csv"
OK_CODES = {"00", "EMPTY", "SKIP", "-", "FALLBACK"}   # 이 밖의 코드가 status 에 있으면 알람이다
ETF_SKIP_DAYS = 30       # 건너뛰기는 이 날수가 지나면 한 번 더 묻는다(일시 오류가 영구 결측이 되지 않게)
REVISE_MAX = 3           # 한 지표에서 과거 값이 절반 넘게 바뀐 날짜가 이보다 많으면 덮지 않는다(0 으로 바뀌는 것은 1건이라도 막는다)
NONZERO = {"credit_loan_total", "credit_loan_kospi", "credit_loan_kosdaq", "deposit_collateral_loan",
           "investor_deposit", "unpaid_broker", "cma_balance_total", "etf_total_trprc"}   # 0 이 나올 수 없는 지표 — 0 은 판독 불가로 본다
ACCEPT_PATH = f"{OUT}/accept_revision.txt"   # 사람이 지표 이름을 적어 두면 그 실행에서 개정을 받아들이고 지운다
ROSTER_MIN_RATIO = 0.9   # 새 명부가 기존 명부 행 수의 이 비율보다 적으면 덮지 않는다
BACKFILL_BUDGET_S = 480  # 실행 시작부터 이 초가 지나면 ETF 백필을 멈춘다(장애 날 실행 시간 상한)

PACE = 0.7           # 호출 간 최소 간격(초) — 버스트 억제
TIMEOUT_STREAK = 4   # 연속 타임아웃 이 횟수면 경로 사망으로 보고 조기 중단
_streak = 0          # 연속 타임아웃 카운터
NET_DOWN = False     # 조기 중단 플래그
ROSTER_FAILED = False  # v1.4 — 명부 조회가 0행이 아닌 이유로 실패했다
T0 = 0.0               # v1.4 — 실행 시작 시각(백필 시간 예산용)


def log(op, code, rows, note=""):
    STATUS.append([op, str(code), str(rows), note])
    print(f"[{op}] code={code} rows={rows} {note}", flush=True)


def call(base, op, qs, tries=3):
    """키를 문자열로 이어 붙여 호출하고 JSON dict를 돌려준다. 실패 시 None.

    v1.3: 경로가 죽었다고 판정되면(NET_DOWN) 즉시 반환한다. 40초 × 3회를
    오퍼레이션마다 반복하는 것은 정보를 더 주지 않고 시간만 태운다.
    """
    global _streak, NET_DOWN
    if NET_DOWN:
        return None, "SKIP 포털 도달 불가(조기 중단)"
    url = f"{base}/{op}?serviceKey={KEY}&resultType=json&{qs}"
    last = ""
    for i in range(tries):
        time.sleep(PACE)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read().decode("utf-8", "replace")
            _streak = 0                      # 본문이 왔다 — 경로는 살아 있다
            if body.lstrip().startswith("<"):
                # XML 오류 응답 — 코드만 뽑아 기록
                import re
                m = re.search(r"<resultCode>(\d+)</resultCode>", body)
                n = re.search(r"<resultMsg>([^<]*)</resultMsg>", body)
                last = f"XML {m.group(1) if m else '?'} {n.group(1) if n else ''}"
                return None, last
            d = json.loads(body)
            _streak = 0                      # 응답이 왔다 — 경로는 살아 있다
            hdr = d.get("response", {}).get("header", {})
            rc = str(hdr.get("resultCode", "?"))
            if rc not in ("00", "0"):
                return None, f"{rc} {hdr.get('resultMsg','')}"
            return d.get("response", {}).get("body", {}), "ok"
        except Exception as e:
            last = f"EXC {type(e).__name__}: {e}"
            if "timed out" in str(e).lower() or isinstance(e, (TimeoutError,)):
                _streak += 1
                if _streak >= TIMEOUT_STREAK:
                    NET_DOWN = True
                    log("NETWORK", "DOWN", 0,
                        f"연속 타임아웃 {_streak}회 — 포털 도달 불가로 판정, 남은 호출 생략. "
                        f"같은 키가 다른 경로에서 동작하는지 확인할 것")
                    return None, last
            else:
                _streak = 0
            time.sleep(2 + 3 * i)
    return None, last


def path_failure(code):
    """경로·일시 장애인가 — 타임아웃·연결 오류·조기 중단·HTTP 502/503/504. 그 밖의 HTTP 오류(4xx·500)와
    포털 결과 코드 오류는 «요청이나 그 날짜의 문제»로 본다(v1.4)."""
    code = str(code)
    return (code.startswith("SKIP") or code == "DOWN"
            or (code.startswith("EXC") and "HTTP Error" not in code)
            or any(f"HTTP Error {c}" in code for c in ("502", "503", "504")))


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
    """최신 want개 **날짜**의 원자료 행을 확보한다. (v1.1 재작성 · v1.4 보강)

    반환: (rows, boundary, ok)
      rows     — 최신 want개 날짜에 속하는 모든 원자료 행 (dedup하지 않는다.
                 하루에 여러 행이 오는 계열은 호출부가 합산해야 하기 때문이다)
      boundary — 페이지를 다 읽지 못한 채 멈췄을 때 '경계에서 잘렸을 수 있는'
                 가장 오래된 날짜. 합산 계열은 이 날짜를 버려야 과소집계를 피한다.
      ok       — (v1.4) 요청한 만큼을 온전히 받았으면 True. 중간 페이지가 실패했거나
                 totalCount 를 읽지 못했으면 False — 받은 행은 쓰되 status 에 PARTIAL 로 남긴다.

    v1.0에서 무엇이 틀렸나 — 방향과 무관하게 1페이지와 마지막 페이지를 둘 다 받았다.
    최신순 계열에서는 마지막 페이지가 '가장 오래된 자료'이고, 하루에 여러 행이 오는
    CMA에서는 고유 날짜 수가 want에 못 미쳐 그 오래된 날짜가 결과에 그대로 남았다.
    2026-08-30 첫 수집에서 2021-10-26~28 유령 행으로 실제 발현됐다.

    v1.4에서 고친 것
      F-11 — 중간 페이지를 잃고도 code=00 으로 적던 것을 PARTIAL 로 적는다.
      F-10 — totalCount 를 못 읽으면 조용히 1페이지로 끝내지 않고 PARTIAL(최신순) 또는
             실패(오래된순 — 최신 쪽 페이지를 알 수 없다)로 처리한다.
      F-12 — 오래된순 계열에서 이미 받은 1페이지를 다시 부르던 것을 재사용으로 바꿨다.
      서버가 page_size 보다 적게 주면(페이지 크기 상한) 실제 크기로 마지막 페이지를 다시 계산한다.
    """
    qs = lambda p: f"numOfRows={page_size}&pageNo={p}&{extra}".rstrip("&")
    b1, m1 = call(base, op, qs(1))
    if b1 is None:
        log(op, m1, 0, "page1 실패")
        return [], None, False
    raw1 = items_of(b1)
    first = [r for r in raw1 if str(r.get("basDt", "")).strip()]
    if not first:
        log(op, "EMPTY", 0, "1페이지 0행")
        return [], None, True
    try:
        total = int(b1.get("totalCount"))
    except Exception:
        total = None

    # 정렬 방향 판정 — 1페이지 안에서 첫 항목이 마지막 항목보다 최신이면 최신순이다.
    desc = str(first[0]["basDt"]) >= str(first[-1]["basDt"])

    if total is None:
        # 마지막 페이지를 알 수 없다. 최신순이면 1페이지가 곧 최신이므로 그것만 쓰고 부분으로 적는다.
        if not desc:
            log(op, "PARTIAL", 0, "totalCount 파싱 실패 · 오래된순이라 최신 페이지를 알 수 없음 — 이번 회차 미수집")
            return [], None, False
        rows = list(first)
        dates = sorted({str(r["basDt"]) for r in rows}, reverse=True)
        keep = set(dates[:want])
        rows = [r for r in rows if str(r["basDt"]) in keep]
        rows.sort(key=lambda r: str(r["basDt"]), reverse=True)
        boundary = min(keep) if keep else None
        log(op, "PARTIAL", len(keep), f"totalCount 파싱 실패 — 1페이지만 사용 · 경계버림={boundary}")
        return rows, boundary, False

    eff = page_size
    if len(raw1) < page_size and total > len(raw1):
        eff = len(raw1)                       # 서버의 페이지 크기 상한
        # 이후 페이지는 실제 크기(eff)로 요청한다 — 서버가 페이지 위치를 요청 크기로 셈하든 실제 크기로
        # 셈하든 어긋나지 않게 하기 위해서다(1페이지는 어느 쪽이든 처음 eff 행이다).
        qs = lambda p: f"numOfRows={eff}&pageNo={p}&{extra}".rstrip("&")
        # 페이지가 작아진 만큼 페이지 상한을 늘린다 — 종전 상한 그대로면 받는 날짜가 그만큼 줄어든다.
        # 다만 한 계열이 60콜을 넘지 않게 묶는다(경로가 느릴 때 실행 시간이 늘어지지 않도록).
        max_pages = min(60, -(-max_pages * page_size // eff))
    last_page = max(1, (total + eff - 1) // eff)

    used, lost = 1, None
    if desc:
        rows, seq = list(first), list(range(2, last_page + 1))
        exhausted = last_page <= 1
    else:
        # 오래된순 — 최신 쪽(마지막 페이지)부터 거슬러 받는다. 1페이지(가장 과거)는 이미
        # 받았으므로 다시 부르지 않고, 2페이지까지 받고도 모자랄 때만 재사용한다(F-12).
        rows, seq = [], list(range(last_page, 1, -1))
        exhausted = last_page <= 1
        if exhausted:
            rows = list(first)
    capped = False
    for pg in seq:
        if len({str(r["basDt"]) for r in rows}) >= want:
            break
        if used >= max_pages:
            capped = True
            break
        b, m = call(base, op, qs(pg))
        used += 1
        if b is None:
            lost = f"page{pg} 실패({m})"
            break
        got = [r for r in items_of(b) if str(r.get("basDt", "")).strip()]
        rows += got
        if not got:
            lost = f"page{pg} 0행(totalCount={total})"
            break
        if desc and pg == last_page:
            exhausted = True
        if not desc and pg == 2 and len({str(r["basDt"]) for r in rows}) < want:
            rows += first
            exhausted = True

    dates = sorted({str(r["basDt"]) for r in rows}, reverse=True)
    keep = set(dates[:want])
    rows = [r for r in rows if str(r["basDt"]) in keep]
    rows.sort(key=lambda r: str(r["basDt"]), reverse=True)   # 호출부가 최신부터 읽는다
    if capped and lost is None and not exhausted and len(keep) < want:
        lost = f"페이지 상한 {max_pages} 도달 — {len(keep)}/{want}일만 받음"
    boundary = None if (exhausted and lost is None) else (min(keep) if keep else None)
    note = (f"total={total} 최신={dates[0] if dates else '-'} "
            f"정렬={'최신순' if desc else '오래된순'} 페이지={used}"
            + (f" 페이지크기={eff}(요청 {page_size})" if eff != page_size else "")
            + (f" 경계버림={boundary}" if boundary else ""))
    if lost:
        log(op, "PARTIAL", len(keep), note + f" · {lost}")
        return rows, boundary, False
    log(op, "00", len(keep), note)
    return rows, boundary, True


def fetch_kofia(op, want, max_pages):
    """KOFIA 통계 계열을 1,000행 페이지로 받는다. 1페이지부터 실패하면 100행 페이지로 한 번 더 시도한다.

    1,000행 페이지는 v1.4 에서 처음 쓰는 크기다(ETF·명부는 이미 1,000행으로 받고 있다). 포털이 그
    크기를 오류로 거절하는 경우에도 수집이 멈추지 않게 종전 크기로 물러선다. 물러서서 온전히 받으면
    첫 실패를 FALLBACK 으로 고쳐 적는다 — 알람은 아니지만 status.csv 에 매 실행 남아 사람이 볼 수 있다.
    경로 장애(path_failure)에는 물러서지 않는다 — 같은 장애에 호출만 두 배가 되기 때문이다.
    """
    i0 = len(STATUS)
    rows, boundary, ok = fetch_series(B_KOFIA, op, want, page_size=KOFIA_PAGE, max_pages=max_pages)
    first_code = STATUS[i0][1] if len(STATUS) > i0 else ""
    # 경로·일시 장애(타임아웃·연결 오류·502/503/504)에는 물러서지 않는다. 그 밖의 HTTP 오류와
    # 포털 결과 코드 오류는 «1,000행 요청을 거절했을 수 있는» 경우로 보고 100행으로 한 번 더 묻는다.
    network = path_failure(first_code)
    if not rows and not ok and not NET_DOWN and not network:
        log(op, "-", 0, "1,000행 페이지 거절 — 100행 페이지로 다시 시도")
        rows, boundary, ok = fetch_series(B_KOFIA, op, want, page_size=100,
                                          max_pages=min(60, max_pages * 10))
        if rows and ok:
            # 물러서서 온전히 받았다 — 데이터는 멀쩡하므로 매 실행 알람을 울리지 않는다.
            # 거절 사실은 status.csv 에 FALLBACK 으로 계속 남는다.
            STATUS[i0][1] = "FALLBACK"
            STATUS[i0][3] = f"1,000행 페이지 거절({first_code}) — 100행으로 받음"
    return rows, boundary, ok


def drop_zeros(op, out):
    """0 이 나올 수 없는 지표(NONZERO)의 0 을 판독 불가(None)로 바꾸고, 있었으면 알람 코드로 남긴다.
    포털이 값 대신 0 을 보내는 사고가 좋은 값을 덮거나 새 날짜에 0 을 쌓지 않게 한다."""
    hit = []
    for d, vals in out.items():
        for k, v in vals.items():
            if k in NONZERO and v == 0:
                vals[k] = None
                hit.append((d, k))
    if hit:
        d0, k0 = max(hit)
        log(op + ":영", "ZERO", len(hit), f"0 이 나올 수 없는 칸의 0 {len(hit)}건을 쓰지 않았다 (예: {d0} {k0})")
    return out


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
def check_fields(op, out):
    """최신 날짜에 비어 있는 칸이 있으면 알람 코드로 남긴다 — 응답 필드 하나의 이름만 바뀌어도
    그 칸이 조용히 멈추지 않게 한다(0 이 나올 수 없는 지표의 0 도 여기서 빈 칸으로 센다)."""
    if not out:
        return
    d = max(out)
    empty = [k for k, v in out[d].items() if v is None]
    if empty:
        log(op + ":칸", "FIELD", len(empty), f"최신 {d} 에 비어 있는 칸: {','.join(empty)}")


def collect_credit(want=TARGET_DATES):
    rows, _, _ = fetch_kofia("getGrantingOfCreditBalanceInfo", want, max_pages=6)
    out = {}
    for d, r in one_per_date(rows).items():
        out[d] = {
            "credit_loan_total":  num(r.get("crdTrFingWhl")),
            "credit_loan_kospi":  num(r.get("crdTrFingScrs")),
            "credit_loan_kosdaq": num(r.get("crdTrFingKosdaq")),
            "credit_short_total": num(r.get("crdTrLndrWhl")),
            "deposit_collateral_loan": num(r.get("dpsgScrtMogFing")),
        }
    out = drop_zeros("getGrantingOfCreditBalanceInfo", out)
    check_fields("getGrantingOfCreditBalanceInfo", out)
    return out


# ─────────────────────────────── 2) 증시자금추이 (반대매매) — 세션 호출 불가 구간
def collect_market_cash(want=TARGET_DATES):
    rows, _, _ = fetch_kofia("getSecuritiesMarketTotalCapitalInfo", want, max_pages=6)
    out = {}
    for d, r in one_per_date(rows).items():
        out[d] = {
            "investor_deposit":   num(r.get("invrDpsgAmt")),
            "unpaid_broker":      num(r.get("brkTrdUcolMny")),
            "forced_liq_amt":     num(r.get("brkTrdUcolMnyVsOppsTrdAmt")),
            "forced_liq_ratio":   num(r.get("ucolMnyVsOppsTrdRlImpt")),
        }
    out = drop_zeros("getSecuritiesMarketTotalCapitalInfo", out)
    check_fields("getSecuritiesMarketTotalCapitalInfo", out)
    return out


# ─────────────────────────────────────────────────────────── 3) 일자별 CMA 현황
AGG_LABELS = {"합계", "총계", "소계", "전체", "계"}


def collect_cma(want=TARGET_DATES):
    """CMA는 하루에 여러 행(운용대상 × 투자자 구분)이 온다 — 날짜별로 합산해야 한다.

    v1.0: basDt로 dedup한 뒤 합산해 실제로는 **한 구분의 잔액**(MMF형 개인)을
          'cma_balance_total'로 적었다. 컬럼 이름은 총계인데 값은 총계가 아니었다.
    v1.2: dedup을 걷어내고 전부 더했더니 이번엔 **정확히 2배**가 됐다.
          응답에 `mngInvTgt="합계"` 행이 명세 행과 **함께** 오기 때문이다.
          실측 20260826 — 명세 10행 합 = 합계 2행 합 = 105조 2,177억,
          둘을 다 더하면 210조 4,355억.
          → 합계 행이 있으면 그것만 쓰고(원자료가 직접 준 값이므로 더 정확하다),
            없으면 명세 행만 더한다.
    v1.4: F-1 — 요청 날짜 수를 호출부가 넘긴다(종전 30 고정) · 한 페이지 1,000행 · 페이지 상한 12.
            하루 약 12행이라 420일 ≈ 6콜이다. 종전 설계(100행 × 6페이지)는 약 50일이 천장이었다.
          F-4 — 잔액을 읽지 못한 행(`"-"`·빈칸·키 없음)이 하나라도 있는 날짜는 «쓰지 않는다».
            종전에는 `or 0.0` 이 그 행을 0 으로 바꿔 좋은 값 위에 작은 합(최악은 0)을 썼다.
    """
    rows, boundary, _ = fetch_kofia("getCMAStatus", want, max_pages=12)
    by_date, seen, dup = {}, set(), 0
    for r in rows:
        d = str(r["basDt"])
        if d == boundary:          # 페이지 경계에서 잘렸을 수 있는 날짜 — 합계가 과소가 된다
            continue
        # v1.4 — 완전히 같은 행은 한 번만 센다. 수집 도중 새 날짜가 공개되면 페이지가 밀려
        # 같은 행이 두 페이지에 걸쳐 오고, 그대로 더하면 그날 합계가 2배가 된다.
        sig = tuple(sorted((k, str(v)) for k, v in r.items()))
        if sig in seen:
            dup += 1
            continue
        seen.add(sig)
        by_date.setdefault(d, []).append(r)
    if dup:
        log("getCMAStatus:중복", "-", dup, "완전히 같은 행을 한 번만 셌다(페이지 밀림)")

    picked = {}
    for d, rs in by_date.items():
        agg   = [r for r in rs if str(r.get("mngInvTgt", "")).strip() in AGG_LABELS]
        parts = [r for r in rs if str(r.get("mngInvTgt", "")).strip() not in AGG_LABELS]
        picked[d] = (bool(agg), agg if agg else parts)
    # 날짜마다 행 수가 같아야 한다(보통 합계행 2개). 최근 20개 날짜에서 가장 흔한 행 수와 다른 날짜는
    # 페이지 경계에서 잘렸거나(적음) 겹쳐 왔을(많음) 수 있으므로 쓰지 않는다. 기준을 «최근»으로 두는
    # 것은 포털의 구분 체계가 바뀌었을 때 오래된 날짜가 새 날짜를 막지 않게 하기 위해서다.
    counts = {}
    for d in sorted(picked, reverse=True)[:20]:
        is_agg, use = picked[d]
        counts[(is_agg, len(use))] = counts.get((is_agg, len(use)), 0) + 1
    norm = {}
    for (is_agg, n), c in counts.items():
        if c > norm.get(is_agg, (0, 0))[1]:
            norm[is_agg] = (n, c)
    out, used_agg, unread = {}, 0, []
    for d, (is_agg, use) in picked.items():
        vals = [num(r.get("actBal")) for r in use]
        if (not vals or any(v is None for v in vals) or is_agg not in norm
                or len(use) != norm[is_agg][0] or sum(vals) == 0):
            unread.append(d)               # F-4 — 못 읽었거나 행 수가 다르거나 합이 0 이면 그날은 쓰지 않는다
            continue
        if is_agg:
            used_agg += 1
        out[d] = {"cma_balance_total": sum(vals)}
    if by_date:
        log("getCMAStatus:합계행", "-", used_agg, f"날짜 {len(by_date)}개 중 합계행 사용 {used_agg}개")
    if unread:
        # 최근 3개 날짜 안에서 못 읽었으면 CMA 가 멈춘 것이므로 알람이다. 그보다 오래된 날짜는 기록만.
        recent3 = set(sorted(by_date, reverse=True)[:3])
        hot = sorted(set(unread) & recent3, reverse=True)
        log("getCMAStatus:미판독", "UNREAD" if hot else "SKIP", len(unread),
            f"잔액을 읽지 못했거나 행이 모자라 쓰지 않은 날짜 {len(unread)}개 (최근 {max(unread)})"
            + (f" · 최근 날짜 {','.join(hot)} 포함 — CMA 가 멈춘다" if hot else ""))
    return out


# ────────────────────────────────────────── 4) ETF 시세 → 레버리지·인버스 거래대금
def collect_etf(basdt, trading=False):
    """그날 ETF 전 종목 시세를 받아 레버리지·인버스 거래대금을 합산한다.

    반환: (agg, state) — state 는 "OK" · "EMPTY"(그날 0행 — 미공개 또는 휴장) ·
          "FAIL"(경로 장애 — 타임아웃·연결 오류·HTTP 502/503/504·조기 중단) · "APIERR"(포털 결과 코드 오류나
          HTTP 4xx·500 — 그 날짜의 문제일 수 있다) · "BAD"(받았으나 온전하지 않다 — totalCount·행 수·거래대금 판독).
    v1.4: F-5 — 중간 페이지가 실패하면 받은 만큼의 부분합을 «그날의 값»으로 쓰지 않는다(FAIL·APIERR).
          F-10 — totalCount 를 읽지 못하면 조용히 1페이지로 끝내지 않고 BAD 로 처리한다.
          받은 행 수가 totalCount 에 못 미친 채 끝나도(페이지 상한 등) BAD 다.
          거래대금(trPrc)을 읽지 못한 행이 5% 를 넘으면 BAD, 전부 0 이면 영업일(trading)은 BAD ·
          그 밖은 EMPTY(휴장일)다 — 어느 쪽이든 0 합계가 «정상 값»으로 영구 기록되지 않게 한다.
    """
    agg = {"etf_total_trprc": 0.0, "etf_lev_trprc": 0.0,
           "etf_inv_trprc": 0.0, "etf_inv2x_trprc": 0.0,
           "etf_lev_lstg_cnt": 0.0}
    page, got, total, unread = 1, 0, None, 0
    while page <= 15:
        b, m = call(B_SECPRD, "getETFPriceInfo", f"numOfRows=1000&pageNo={page}&basDt={basdt}")
        if b is None:
            log("getETFPriceInfo", m, got, f"basDt={basdt} page={page} 실패 — 이 날짜는 쓰지 않는다")
            return None, ("FAIL" if path_failure(m) else "APIERR")
        rows = items_of(b)
        if not rows:
            break
        try:
            total = int(b.get("totalCount"))
        except Exception:
            log("getETFPriceInfo", "PARTIAL", got,
                f"basDt={basdt} page={page} totalCount 파싱 실패 — 이 날짜는 쓰지 않는다")
            return None, "BAD"
        for r in rows:
            nm = str(r.get("itmsNm", ""))
            tp = num(r.get("trPrc"))
            if tp is None:
                unread += 1
                tp = 0.0
            agg["etf_total_trprc"] += tp
            if "레버리지" in nm:
                agg["etf_lev_trprc"] += tp
                agg["etf_lev_lstg_cnt"] += (num(r.get("stLstgCnt")) or 0.0)
            if "인버스" in nm:
                agg["etf_inv_trprc"] += tp
                if "2X" in nm.upper() or "2배" in nm:
                    agg["etf_inv2x_trprc"] += tp
        got += len(rows)
        if got >= total:
            break
        page += 1
    if got == 0:
        log("getETFPriceInfo", "EMPTY", 0, f"basDt={basdt} 0행 — 미공개(D+1 규칙) 또는 휴장")
        return None, "EMPTY"
    if total is None or got < total:
        log("getETFPriceInfo", "PARTIAL", got,
            f"basDt={basdt} {got}/{total}행에서 멈춤 — 이 날짜는 쓰지 않는다")
        return None, "BAD"
    if unread > got * 0.05:
        log("getETFPriceInfo", "UNREAD", got,
            f"basDt={basdt} 거래대금을 읽지 못한 행 {unread}/{got} — 이 날짜는 쓰지 않는다")
        return None, "BAD"
    if agg["etf_total_trprc"] == 0:
        # 행은 왔으나 거래대금이 전부 0. 영업일(trading — 신용·증시자금이 있는 날, 12/31 제외)이면
        # 판독 사고(BAD · 알람)이고, 그 밖이면 휴장일에 포털이 빈 시세를 준 것으로 본다. 어느 쪽이든 0 은 쓰지 않는다.
        if trading and not basdt.endswith("1231"):
            log("getETFPriceInfo", "UNREAD", got, f"basDt={basdt} 영업일인데 {got}행 전부 거래대금 0 — 쓰지 않는다")
            return None, "BAD"
        log("getETFPriceInfo", "EMPTY", got, f"basDt={basdt} {got}행 전부 거래대금 0 — 휴장일로 보고 쓰지 않는다")
        return None, "EMPTY"
    log("getETFPriceInfo", "00", got, f"basDt={basdt} 레버리지 {agg['etf_lev_trprc']:.0f}")
    return agg, "OK"


# ────────────────────────────────────────────────── 5) KRX 상장종목 매핑표 (주 1회)
def collect_roster(basdt):
    """v1.4 F-10 — totalCount 를 못 읽거나 받은 행이 그에 못 미치면 None 을 돌려준다.
    종전에는 그 경우 조용히 멈춘 1,000행으로 정상 2,800행 명부를 덮었다.
    실패(0행이 아닌 것)면 ROSTER_FAILED 를 세워 호출부가 더 오래된 날짜로 헛돌지 않게 한다."""
    global ROSTER_FAILED
    rows, page, got, total = [], 1, 0, None
    while page <= 10:
        b, m = call(B_KRXLST, "getItemInfo", f"numOfRows=1000&pageNo={page}&basDt={basdt}")
        if b is None:
            log("getItemInfo", m, got, f"basDt={basdt} page={page}")
            ROSTER_FAILED = True
            return None
        it = items_of(b)
        if not it:
            break
        try:
            total = int(b.get("totalCount"))
        except Exception:
            log("getItemInfo", "PARTIAL", got, f"basDt={basdt} page={page} totalCount 파싱 실패 — 명부를 덮지 않는다")
            ROSTER_FAILED = True
            return None
        rows += it
        got += len(it)
        if got >= total:
            break
        page += 1
    if not rows:
        log("getItemInfo", "EMPTY", 0, f"basDt={basdt} 0행")
        return None
    if total is None or got < total:
        log("getItemInfo", "PARTIAL", got, f"basDt={basdt} {got}/{total}행에서 멈춤 — 명부를 덮지 않는다")
        ROSTER_FAILED = True
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


def write_atomic(path, write_fn):
    """v1.4 F-9 — 임시 파일에 다 쓰고 os.replace 로 바꾼다. 중간에 죽어도 잘린 파일이 남지 않는다."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            write_fn(f)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):          # 쓰다 죽은 임시 파일이 커밋되지 않게 지운다
            os.remove(tmp)
        raise


def read_rows(path):
    """v1.4 F-8 — BOM 이 붙어 있어도 읽는다(utf-8-sig). 머리에 basDt 가 없거나 같은 날짜가
    두 번 나오면 예외를 낸다 — 종전에는 BOM 하나로 모든 행이 hist[""] 한 칸으로 뭉개진 뒤
    그 2행짜리 파일이 그대로 저장·커밋됐다. 이제는 쓰기 전에 멈추고 알람이 남는다."""
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        if "basDt" not in (rd.fieldnames or []):
            raise ValueError(f"{path}: 머리 줄에 basDt 가 없다 — {rd.fieldnames[:3] if rd.fieldnames else rd.fieldnames}")
        rows = list(rd)
    keys = [str(r.get("basDt", "")) for r in rows if str(r.get("basDt", "")).strip()]
    dup = len(keys) - len(set(keys))      # basDt 가 빈 행은 세지 않는다 — prune_orphans 가 지운다
    if dup:
        raise ValueError(f"{path}: 같은 basDt 가 {dup}번 겹친다 — 파일을 덮어쓰지 않는다")
    return rows


def history_depth(path, col):
    """CSV에 그 컬럼이 실제로 채워진 행이 몇 개인가."""
    return sum(1 for row in read_rows(path) if str(row.get(col, "")).strip())


METRICS = FIELDS[1:]   # basDt 를 뺀 지표 칸 전부


def prune_orphans(path):
    """값이 하나도 없는 행과 basDt 가 빈 행만 지운다. (v1.4 F-3·F-12 재작성)

    종전(v1.0~v1.3): 「신용·증시자금·ETF 중 하나라도 값이 있는 가장 오래된 날짜」보다 앞선 행을
    «값이 있든 없든» 매 실행 지웠다. v1.0 의 CMA 유령 행을 치우려던 것인데, CMA·ETF 백필이
    신용보다 깊이 들어가는 순간 그날 백필한 행을 같은 실행이 지우게 된다(F-3).
    유령 행은 v1.1 이후 생기지 않으므로 날짜 기준 삭제를 없앴다. 지우는 것은 두 가지뿐이다 —
    basDt 가 빈 행(영구 불변으로 맨 앞에 남던 것 · F-12) · 지표 칸이 전부 빈 행.
    """
    rows = read_rows(path)
    keep = [r for r in rows
            if str(r.get("basDt", "")).strip()
            and any(str(r.get(k, "")).strip() for k in METRICS)]
    dropped = len(rows) - len(keep)
    if dropped:
        def wr(f):
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in sorted(keep, key=lambda x: str(x.get("basDt", ""))):
                w.writerow(r)
        write_atomic(path, wr)
    return dropped


def upsert(path, merged):
    """기존 CSV를 읽어 basDt 키로 갱신·추가한다. 값이 있는 필드만 덮어쓴다.

    v1.4: 읽기는 read_rows(BOM·중복 방어 · F-8), 쓰기는 write_atomic(F-9).
          값이 하나도 없는 날짜로 «새» 행을 만들지 않는다(F-12).
    """
    hist = {str(row.get("basDt", "")): row for row in read_rows(path)}
    for d, vals in merged.items():
        vals = {k: v for k, v in (vals or {}).items() if v is not None}
        if not d or (d not in hist and not vals):
            continue
        row = hist.get(d, {k: "" for k in FIELDS})
        row["basDt"] = d
        for k, v in vals.items():
            row[k] = ("%.4f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
        for k in FIELDS:
            row.setdefault(k, "")
        hist[d] = row

    def wr(f):
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in sorted(hist):
            w.writerow(hist[d])
    write_atomic(path, wr)
    return len(hist)


def load_skip():
    """ETF 백필 건너뛰기 목록 — {basDt: [attempts, last_try(YYYYMMDD)]}.
    과거 날짜가 0행이거나 실패한 횟수를 센다. ETF_SKIP_DAYS 가 지난 항목은 한 번 더 묻도록 횟수를 낮춘다."""
    out = {}
    if os.path.exists(SKIP_PATH):
        with open(SKIP_PATH, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                try:
                    out[str(r.get("basDt", "")).strip()] = [int(r.get("attempts", 0) or 0),
                                                            str(r.get("last_try", "") or "").strip()]
                except Exception:
                    continue
    out.pop("", None)
    today = datetime.now(KST).date()
    for d, (n, last) in out.items():
        try:
            age = (today - datetime.strptime(last, "%Y%m%d").date()).days
        except Exception:
            age = ETF_SKIP_DAYS
        if n >= ETF_SKIP_AFTER and age >= ETF_SKIP_DAYS:
            out[d][0] = ETF_SKIP_AFTER - 1       # 만료 — 한 번 더 묻는다
    return out


def save_skip(skip):
    def wr(f):
        w = csv.writer(f)
        w.writerow(["basDt", "attempts", "last_try"])
        for d in sorted(skip):
            w.writerow([d, skip[d][0], skip[d][1]])
    write_atomic(SKIP_PATH, wr)


def kofia_calendar(existing, merged):
    """신용 또는 증시자금 값이 있는 날짜 — 포털이 자료를 낸 영업일로 본다."""
    cal = set()
    for src in (existing, merged):
        for d, r in src.items():
            if (str(r.get("credit_loan_total", "") or "").strip()
                    or str(r.get("investor_deposit", "") or "").strip()):
                cal.add(d)
    return cal


def etf_targets(existing, merged, skip, fetched):
    """ETF 백필 대상 날짜 — 최신부터, 실행당 ETF_BACKFILL_MAX 개. (v1.4 F-2)

    기준 달력은 «신용 또는 증시자금 값이 있는 날짜»(= 포털이 자료를 낸 영업일)다.
    그 최신 TARGET_DATES 개 중 ETF 값이 없고, 건너뛰기 목록에 없고, 이번 실행에 이미 부르지
    않은 날짜를 고른다. 종전에는 최신 1일만 불렀다 — 이력이 주 5일 이상 늘 수 없었다.
    """
    have_etf, cal = set(), set()
    for src in (existing, merged):
        for d, r in src.items():
            v = num(r.get("etf_total_trprc", ""))
            if v:                              # 비었거나 0 이면 «값 없음» — 다시 묻는다
                have_etf.add(d)
            if (str(r.get("credit_loan_total", "") or "").strip()
                    or str(r.get("investor_deposit", "") or "").strip()):
                cal.add(d)
    window = sorted(cal, reverse=True)[:TARGET_DATES]
    todo = [d for d in window
            if d not in have_etf and skip.get(d, [0, ""])[0] < ETF_SKIP_AFTER and d not in fetched]
    return todo[:ETF_BACKFILL_MAX], len(todo)


def guard_revisions(existing, merged):
    """매 실행 과거 420일을 다시 받으므로, 포털 사고 한 번이 과거 값을 한꺼번에 덮을 수 있다.
    ① 기존 좋은 값이 0 으로 바뀌는 것은 1건이라도 덮지 않는다(REVISED 알람).
    ② 한 지표에서 «절반 넘게 바뀐» 과거 날짜가 REVISE_MAX 보다 많으면 단위·명세가 바뀐 것으로 보고
       이번 실행에서는 그 지표를 «새 날짜까지» 전혀 쓰지 않는다 — 원과 백만원이 한 계열에 섞이지 않게.
       알람은 사람이 원인을 확인할 때까지 매 실행 남는다.
    그보다 작은 개정은 받아들이고 바뀐 칸 수만 적는다.
    ③ 사람이 정정을 확인했으면 results/fss/accept_revision.txt 에 지표 이름을 한 줄씩 적는다 —
       다음 실행이 그 지표의 개정을 전부 받아들이고 파일을 지운다(1회 승인)."""
    accept = set()
    if os.path.exists(ACCEPT_PATH):
        with open(ACCEPT_PATH, encoding="utf-8-sig") as f:
            accept = {x.strip() for x in f if x.strip() and not x.strip().startswith("#")}
    unknown = sorted(accept - set(METRICS))
    if unknown:
        log("개정승인", "ACCEPT?", len(unknown), f"accept_revision.txt 의 모르는 지표 이름: {','.join(unknown)} — 칸 이름을 확인할 것")
    changed, blocked, applied = 0, [], set()
    for k in METRICS:
        zero_over, big = [], []
        for d, vals in merged.items():
            v = vals.get(k)
            old = str((existing.get(d) or {}).get(k, "") or "").strip()
            if v is None or not old:
                continue
            try:
                o = float(old)
            except Exception:
                continue
            new_s = ("%.4f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
            if new_s == old:
                continue
            changed += 1
            if v == 0 and o != 0:
                zero_over.append((d, old, new_s))
            elif o != 0 and abs(v - o) / abs(o) > 0.5:
                big.append((d, old, new_s))
        if k in accept:
            if zero_over or big:
                applied.add(k)
                log("개정승인", "-", len(zero_over) + len(big), f"{k} 개정을 accept_revision.txt 에 따라 받아들였다"
                    + (" · 단위 변경이면 창 밖(420일보다 오래된) 행은 사람이 환산해야 한다" if big else ""))
            continue
        for d, _, _ in zero_over:
            merged[d].pop(k, None)
        if zero_over:
            d0, o0, n0 = max(zero_over)
            log("개정감지", "REVISED", len(zero_over), f"{k} 좋은 값이 0 으로 바뀌는 {len(zero_over)}칸을 덮지 않았다 (예: {d0} {o0}→{n0})"
                f" · 정정이 맞으면 results/fss/accept_revision.txt 에 {k} 를 적어 커밋")
        if len(big) > REVISE_MAX:
            for d in merged:
                merged[d].pop(k, None)
            d0, o0, n0 = max(big)
            blocked.append(k)
            log("개정감지", "REVISED", len(big),
                f"{k} 과거 값 {len(big)}일이 절반 넘게 바뀌어 이번 실행에서 이 지표를 쓰지 않았다(새 날짜 포함) "
                f"— 단위·명세 변경을 확인할 것 (예: {d0} {o0}→{n0}) · 정정이 맞으면 "
                f"results/fss/accept_revision.txt 에 {k} 를 적어 커밋하면 다음 실행이 받아들인다")
    if changed:
        log("개정", "-", changed, "기존 값과 다른 칸 수(작은 개정은 받아들인다)"
            + (f" · 막은 지표 {','.join(blocked)}" if blocked else ""))
    return accept, applied


def run():
    global T0
    T0 = time.time()
    if not KEY:
        log("KEY", "NOKEY", 0, "DATA_GO_KR_KEY 미설정 — 호출하지 않고 중단")
        raise SystemExit("DATA_GO_KR_KEY 미설정 — 중단")
    os.makedirs(OUT, exist_ok=True)
    for fn in os.listdir(OUT):                 # 직전 실행이 남긴 임시 파일(쓰다 죽은 흔적)
        if fn.endswith(".tmp"):
            os.remove(os.path.join(OUT, fn))
    merged = {}

    def merge(d):
        for k, v in (d or {}).items():
            merged.setdefault(k, {}).update(v)

    # 백분위 판정에는 1년(약 245영업일) 이력이 필요하다.
    # v1.4 F-7: 종전에는 「이력이 300행을 넘으면 30일만」 요청해 한 번 넘은 뒤로는 창 안의 구멍이
    # 영영 메워지지 않았다(한 방향 래치). 이제 매 실행 TARGET_DATES 를 요청한다 —
    # 한 페이지 1,000행이라 신용·증시자금은 각 1콜, CMA 는 약 6콜이다.
    ews = f"{OUT}/ews_fss.csv"
    if os.path.exists(ews) and os.path.getsize(ews) == 0:
        # 0바이트 파일은 빈 이력으로 보고 다시 쌓는다. 창 밖(420일보다 오래된) 이력은 깃 기록에서 사람이 복구한다.
        log("ews_fss.csv", "EMPTYFILE", 0, "0바이트 이력 — 빈 이력으로 다시 쌓는다 · 이전 이력은 깃 기록에서 복구할 것")
        os.remove(ews)
    existing = {str(r.get("basDt", "")): r for r in read_rows(ews)}
    print(f"기존 이력: 신용 {history_depth(ews, 'credit_loan_total')}행 · "
          f"CMA {history_depth(ews, 'cma_balance_total')}행 · "
          f"ETF {history_depth(ews, 'etf_total_trprc')}행 → 이번 회차 {TARGET_DATES}일 요청", flush=True)

    core = {}
    for name, fn in (("신용공여잔고", collect_credit), ("증시자금", collect_market_cash), ("CMA", collect_cma)):
        got = fn(TARGET_DATES) or {}
        # 값이 하나라도 채워진 날짜만 센다 — 응답 필드 이름이 바뀌어 전부 None 이면 0건이다.
        core[name] = sum(1 for v in got.values() if any(x is not None for x in v.values()))
        merge(got)

    # ETF — ① 최신 1일(종전과 같다: 최근 영업일부터 거슬러 첫 성공까지. 하루 3슬롯이 같은 날짜를
    #        다시 받는 것은 아침 슬롯의 이른 값을 저녁 값으로 고치기 위해서다)
    #       ② 빈 날짜 백필(F-2) — 과거 날짜가 0행이거나 실패하면 횟수를 세어 ETF_SKIP_AFTER 번째에
    #          건너뛴다(ETF_SKIP_DAYS 뒤 한 번 더 묻는다). 연속 실패 2회면 이번 실행의 ETF 를 멈춘다.
    cal = kofia_calendar(existing, merged)
    fetched, fails = set(), 0
    for bd in biz_days_back(5):
        agg, state = collect_etf(bd, trading=(bd in cal))
        fetched.add(bd)
        if agg:
            merged.setdefault(bd, {}).update(agg)
            fails = 0
            break
        fails = fails + 1 if state in ("FAIL", "APIERR") else 0
        if fails >= 2:                       # 최신일부터 연속 실패 2회 — 포털 전체 장애(키 오류 포함)로 보고
            break                            # ETF 를 더 부르지 않는다(백필도 하지 않는다)
    skip = load_skip()
    recent = set(biz_days_back(ETF_RECENT_GUARD))
    todo, remain = etf_targets(existing, merged, skip, fetched)
    filled, stopped = 0, ""
    if fails >= 2:
        todo, stopped = [], "최신일 조회가 연속 실패"
    today = datetime.now(KST).strftime("%Y%m%d")
    apierr, apierr_dates = 0, []
    for bd in todo:
        if NET_DOWN:
            stopped = "경로 사망"
            break
        if time.time() - T0 > BACKFILL_BUDGET_S:
            stopped = f"시간 예산 {BACKFILL_BUDGET_S}초"
            break
        agg, state = collect_etf(bd, trading=True)
        if agg:
            merged.setdefault(bd, {}).update(agg)
            skip.pop(bd, None)
            filled += 1
            fails = 0
            continue
        if bd not in recent and state in ("EMPTY", "BAD", "APIERR"):
            # 0행 · 온전하지 않음 · 포털 오류 코드인 과거 날짜는 횟수를 센다(그 날짜 하나가 매 실행 알람을
            # 만들거나 백필을 막지 않게). 경로 장애(FAIL)는 세지 않는다 — 일시 장애가 멀쩡한 날짜를 빼지 않게.
            skip[bd] = [skip.get(bd, [0, ""])[0] + 1, today]
        fails = fails + 1 if state == "FAIL" else 0
        if state == "APIERR":
            apierr += 1
            apierr_dates.append(bd)
        else:
            apierr, apierr_dates = 0, []
        if fails >= 2:                       # 경로 장애 연속 2회면 이번 실행의 백필을 멈춘다
            stopped = "경로 장애 연속 2회"
            break
        if apierr >= 3:                      # 포털 오류 코드 연속 3회 — 날짜 문제가 아니라 전체 장애일 수 있다
            for d0 in apierr_dates:          # 그렇다면 방금 올린 건너뛰기 횟수는 날짜 탓이 아니다 — 되돌린다
                if d0 in skip and d0 not in recent:
                    skip[d0][0] -= 1
                    if skip[d0][0] <= 0:
                        skip.pop(d0)
            stopped = "포털 오류 코드 연속 3회"
            break
    if todo or os.path.exists(SKIP_PATH):
        save_skip(skip)
    log("ETF백필", "-", filled,
        f"대상 {len(todo)}일 중 {filled}일 채움 · 남은 빈 날짜 {max(0, remain - filled)}일 · "
        f"건너뛰기 {sum(1 for v in skip.values() if v[0] >= ETF_SKIP_AFTER)}일"
        + (f" · 중단({stopped})" if stopped else ""))
    etf_have = [d for src in (existing, merged) for d, r in src.items() if num(r.get("etf_total_trprc", ""))]
    if cal and etf_have:
        lag = sum(1 for d in cal if d > max(etf_have) and not d.endswith("1231"))
        if lag > 3:
            log("ETF신선도", "STALE", lag, f"ETF 최신 {max(etf_have)} 가 신용·증시자금 최신 {max(cal)} 보다 영업일 {lag}일 뒤처졌다")

    accept, applied = guard_revisions(existing, merged)
    n = upsert(ews, merged)
    if applied:
        # 1회 승인 — 이번 실행에서 실제로 받아들인 지표만 파일에서 지운다(연결이 끊긴 실행이나
        # 이름을 잘못 적은 경우 승인이 소모되지 않게). 남은 이름이 없으면 파일을 지운다.
        rest = sorted(accept - applied)
        if rest:
            write_atomic(ACCEPT_PATH, lambda f: f.write("\n".join(rest) + "\n"))
        else:
            os.remove(ACCEPT_PATH)
        print(f"{ACCEPT_PATH} — 적용 {','.join(sorted(applied))} · 남김 {','.join(rest) or '없음'}", flush=True)
    d = prune_orphans(ews)
    if d:
        n -= d
        print(f"빈 행 {d}개 제거 (지표 칸이 전부 비었거나 basDt 가 빈 행)", flush=True)
    print(f"ews_fss.csv 누적 {n}행", flush=True)

    # 매핑표는 월요일(KST)에만 갱신 — 상장 명부는 매일 바뀌지 않는다.
    if datetime.now(KST).weekday() == 0 or not os.path.exists(f"{OUT}/roster_map.csv"):
        for bd in biz_days_back(5):
            rows = collect_roster(bd)
            if ROSTER_FAILED or NET_DOWN:    # 실패면 더 오래된 날짜로 헛돌지 않는다(0행만 다음 날짜로)
                break
            if rows:
                old_n = 0
                if os.path.exists(f"{OUT}/roster_map.csv"):
                    with open(f"{OUT}/roster_map.csv", newline="", encoding="utf-8-sig") as f:
                        old_n = max(0, sum(1 for _ in f) - 1)
                if old_n and len(rows) < old_n * ROSTER_MIN_RATIO:
                    log("getItemInfo", "SHRINK", len(rows),
                        f"basDt={bd} 새 명부 {len(rows)}행 < 기존 {old_n}행의 {ROSTER_MIN_RATIO:.0%} — 덮지 않는다")
                    break

                def wr(f):
                    w = csv.writer(f)
                    w.writerow(["basDt", "srtnCd", "isinCd", "crno", "mrktCtg", "itmsNm", "corpNm"])
                    for r in rows:
                        w.writerow([r.get("basDt", ""), r.get("srtnCd", ""), r.get("isinCd", ""),
                                    r.get("crno", ""), r.get("mrktCtg", ""),
                                    r.get("itmsNm", ""), r.get("corpNm", "")])
                write_atomic(f"{OUT}/roster_map.csv", wr)
                print(f"roster_map.csv {len(rows)}종목 (basDt={bd})", flush=True)
                break

    # ── 알람 판정 (v1.4 F-6) ─────────────────────────────────────────────
    # 종전에는 전 호출이 인증 오류여도 종료코드 0 · 알람 없음이었다. 워크플로의 「수집 상태 확인」은
    # krx 알람만 보므로 fss 실패는 초록불로 지나갔다.
    reasons = [f"{name} 0건 — 핵심 계열을 한 날짜도 받지 못했다" for name, c in core.items() if c == 0]
    reasons += [f"{op} code={code} rows={rows} {note}" for op, code, rows, note in STATUS
                if code not in OK_CODES]
    return reasons


def write_status(now):
    def wr(f):
        w = csv.writer(f)
        w.writerow(["op", "code", "rows", "note", "run_at_kst"])
        for s in STATUS:
            w.writerow(s + [now])
    write_atomic(f"{OUT}/status.csv", wr)
    print("status.csv 기록 완료", flush=True)


def write_alarm(now, lines):
    def wr(f):
        f.write(f"{now} KST  fss_fetch.py 수집 알람 — {len(lines)}건\n\n")
        for x in lines:
            f.write(f"  {x}\n")
        f.write("\n인증 오류(30·SERVICE_KEY)면 포털 키·신청 기간을, 타임아웃이면 러너 경로를, "
                "PARTIAL 이면 응답 명세 변경을 본다.\n")
    write_atomic(ALARM_PATH, wr)
    print(f"ALARM {ALARM_PATH} 기록 — 종료코드 1", flush=True)


def main():
    """v1.4 F-6 — 어떤 예외로 죽더라도 status·알람을 남기고, 알람이면 종료코드 1 로 끝낸다.
    워크플로는 이 단계를 continue-on-error 로 돌리므로 종료코드 1 이어도 커밋 단계는 돈다."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    try:
        reasons = run()
    except BaseException as e:
        import traceback
        reasons = [f"예외로 중단: {type(e).__name__}: {e}"] + traceback.format_exc().splitlines()[-6:]
    try:
        os.makedirs(OUT, exist_ok=True)
        write_status(now)
    except Exception as e:
        reasons.append(f"status.csv 기록 실패: {type(e).__name__}: {e}")
    if reasons:
        write_alarm(now, reasons)
        sys.exit(1)
    if os.path.exists(ALARM_PATH):
        os.remove(ALARM_PATH)
        print(f"ALARM 해제 — {ALARM_PATH} 삭제", flush=True)


if __name__ == "__main__":
    main()
