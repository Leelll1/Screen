# -*- coding: utf-8 -*-
"""조기경보 As-of 백테스트 + 인버스 사냥모드 시뮬레이션 (GitHub Actions 실행용)

목적 (2026-08-27 사용자 지시 — '충분한 검증'):
  1) 시장 조기경보 절차서 v1.0의 상태 기계를 1997~2026 전 기간에 시점고정(As-of)으로
     기계 재현 — 급락 사건을 데이터에서 객관적으로 탐지(11사건 카탈로그보다 넓게)하고
     사건별 리드타임과, 전 기간 스캔으로 과잉 경보율(false alarm)을 실측한다.
  2) 인버스 사냥모드 3조건을 계량 정의하고 전 기간 시뮬레이션 — 규율(1배·손절 3분화·
     추격 금지)의 실효성을 검증한다.

방법론 (정직성 원칙):
  · L1(기압계)은 FRED 원자료로 완전 기계 계산 — 각 날짜에 그 시점까지의 데이터만 사용.
  · L2/L3는 '당시 공개돼 있던 정보'의 연표(EVIDENCE)를 날짜 단위로 코드화 — 사건 구간에
    집중 조사된 연표이므로 적중 방향으로 유리한 편향 가능성을 보고서에 명기한다.
  · V-KOSPI(2010~)는 KRX API로 일별 소급 수집 — L2의 기계적 부분 근사(MECH 변형).
  · 3개 변형을 병렬 판정: L1ONLY(기계·전기간) / MECH(L1+V-KOSPI 기계·2010~) /
    FULL(L1+L2+L3 증거 포함). 변형 간 비교가 각 층의 기여를 드러낸다.

실행 환경: GitHub Actions (Secrets: FRED_API_KEY, KRX_AUTH_KEY). 표준 라이브러리만 사용.
출력: data/backtest/ 아래 소형 CSV·MD (세션이 raw.githubusercontent.com으로 정독).
"""
import os, json, csv, math, datetime, urllib.request, urllib.parse, time, sys

OUT = "data/backtest"
FRED_KEY = os.environ.get("FRED_API_KEY", "")
KRX_KEY = os.environ.get("KRX_AUTH_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (research; contact via github repo)"}

# ══════════════════ 1. 데이터 수집 ══════════════════

def http_get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def fred_series(sid):
    url = ("https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={sid}&api_key={FRED_KEY}&file_type=json&limit=100000")
    data = json.loads(http_get(url))
    out = {}
    for o in data.get("observations", []):
        if o["value"] not in (".", ""):
            out[o["date"]] = float(o["value"])
    print(f"FRED {sid}: {len(out)} obs ({min(out)}..{max(out)})")
    return out

def get_kospi_stooq():
    raw = http_get("https://stooq.com/q/d/l/?s=%5Ekospi&i=d").decode()
    rows = list(csv.DictReader(raw.splitlines()))
    out = {}
    for r in rows:
        try:
            out[r["Date"]] = (float(r["Open"]), float(r["High"]),
                              float(r["Low"]), float(r["Close"]))
        except (ValueError, KeyError):
            continue
    if len(out) < 3000:
        raise RuntimeError(f"stooq rows too few: {len(out)}")
    print(f"KOSPI(stooq): {len(out)} days ({min(out)}..{max(out)})")
    return out, "stooq"

def krx_call(path, bas):
    url = f"http://data-dbg.krx.co.kr/svc/apis/{path}?basDd={bas}"
    req = urllib.request.Request(url, headers={"AUTH_KEY": KRX_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return data.get("OutBlock_1") or []

def fnum(v):
    try:
        s = str(v).replace(",", "").strip()
        return None if s in ("", "-") else float(s)
    except (ValueError, TypeError):
        return None

def get_kospi_krx():
    """Stooq 실패 시 폴백 — 2010-01-04부터 일별 루프 (느림)."""
    out = {}
    d = datetime.date(2010, 1, 4); today = datetime.date.today()
    errs = 0
    while d <= today:
        if d.weekday() < 5:
            bas = d.strftime("%Y%m%d")
            try:
                for r in krx_call("idx/kospi_dd_trd", bas):
                    if str(r.get("IDX_NM", "")).strip() == "코스피":
                        c = fnum(r.get("CLSPRC_IDX"))
                        if c:
                            out[d.isoformat()] = (fnum(r.get("OPNPRC_IDX")) or c,
                                                  fnum(r.get("HGPRC_IDX")) or c,
                                                  fnum(r.get("LWPRC_IDX")) or c, c)
                errs = 0
            except Exception:
                errs += 1
                if errs > 60: raise RuntimeError("KRX kospi backfill: too many errors")
            time.sleep(0.12)
        d += datetime.timedelta(days=1)
    print(f"KOSPI(KRX fallback): {len(out)} days")
    return out, "krx2010"

def get_vkospi():
    """V-KOSPI 일별 2010~ — 증분 수집 (기존 파일 있으면 이어받음)."""
    path = f"{OUT}/vkospi_daily.csv"
    out = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                out[r["date"]] = float(r["close"])
    start = datetime.date(2010, 1, 4)
    if out:
        start = datetime.date.fromisoformat(max(out)) + datetime.timedelta(days=1)
    d, today, errs, calls = start, datetime.date.today(), 0, 0
    while d <= today:
        if d.weekday() < 5:
            bas = d.strftime("%Y%m%d")
            try:
                for r in krx_call("idx/drvprod_dd_trd", bas):
                    if "변동성" in str(r.get("IDX_NM", "")):
                        c = fnum(r.get("CLSPRC_IDX"))
                        if c: out[d.isoformat()] = c
                        break
                errs = 0
            except Exception:
                errs += 1
                if errs > 60:
                    print(f"V-KOSPI 수집 중단(연속 오류): {d} 이후 결측"); break
            calls += 1
            if calls % 500 == 0: print(f"  vkospi … {d} ({len(out)} rows)")
            time.sleep(0.12)
        d += datetime.timedelta(days=1)
    os.makedirs(OUT, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "close"])
        for k in sorted(out): w.writerow([k, out[k]])
    print(f"V-KOSPI: {len(out)} days")
    return out

# ══════════════════ 2. L2/L3 증거 연표 (당시 공개 정보만, 대표 보도일 근사) ══════════════════
# (start, end, layer, score, catalyst?, label)
EVIDENCE = [
    ("1997-01-23","1997-07-14","L3",1,0,"한보·삼미·진로 연쇄부도"),
    ("1997-07-15","1997-12-31","L3",2,1,"기아사태+태국발 아시아 전염"),
    ("1999-11-01","2000-04-30","L2",1,0,"코스닥 극단 과열(타이밍성 없음)"),
    ("2000-03-20","2000-04-30","L3",1,0,"Barron's Burning Up·MS 패소"),
    ("2002-12-01","2003-03-31","L3",1,0,"카드 연체 급증·이라크전 캘린더"),
    ("2003-01-15","2003-04-30","L2",1,0,"카드채 시장 경색 관측"),
    ("2004-04-12","2004-05-31","L3",1,0,"중국 과열·긴축 시사 보도"),
    ("2007-08-09","2008-08-31","L3",1,0,"BNP 동결~베어스턴스 크레딧 경색"),
    ("2007-11-01","2008-10-31","L2",1,0,"외국인 현물 대량 순매도 관측"),
    ("2008-09-01","2008-10-31","L3",2,1,"9월 위기설 공론화·리먼"),
    ("2011-04-18","2011-07-13","L3",1,0,"S&P 미국 전망 부정적"),
    ("2011-07-14","2011-09-30","L3",2,1,"CreditWatch+부채한도 D-2주 에스컬레이션"),
    ("2013-05-22","2013-06-04","L3",1,0,"버냉키 테이퍼 발언"),
    ("2013-06-05","2013-07-10","L3",2,1,"FOMC(6.19) D-2주+신흥국 이탈 가속"),
    ("2015-06-19","2015-08-10","L3",1,0,"상하이 -30%·중국 경착륙 보도"),
    ("2015-08-11","2015-09-15","L3",2,1,"위안 기습 절하 후 에스컬레이션"),
    ("2016-01-04","2016-02-29","L3",1,0,"중국 서킷브레이커·유가 붕괴"),
    ("2018-08-15","2018-11-15","L3",1,0,"IB 메모리 피크 경고·관세 확전·파월 발언"),
    ("2019-05-06","2019-07-31","L3",1,0,"미중 관세 재점화"),
    ("2019-08-01","2019-08-31","L3",2,1,"추가관세 발표·위안 7·원 1,200 돌파"),
    ("2020-01-23","2020-02-17","L3",1,0,"우한 봉쇄·WHO 비상사태"),
    ("2020-02-18","2020-04-30","L3",2,1,"대구 집단감염·글로벌 확산"),
    ("2021-08-13","2022-01-31","L2",2,1,"신용융자 25조 사상 최대(§1 극단 예시)"),
    ("2021-11-30","2022-10-31","L3",1,0,"transitory 폐기·점도표 가속"),
    ("2022-02-01","2022-10-31","L2",1,0,"외국인 대량 순매도 지속"),
    ("2024-07-02","2024-08-09","L2",2,1,"CFTC 엔 숏 사상 최대(주간 공개)"),
    ("2024-07-25","2024-08-09","L3",1,0,"BOJ 인상 관측·기술주 1차 조정"),
    ("2025-02-13","2025-03-25","L3",1,0,"상호관세 각서 D-7주 캘린더"),
    ("2025-03-26","2025-04-30","L3",2,1,"자동차 관세·에스컬레이션 D-1주"),
    ("2026-02-15","2026-05-26","L3",1,0,"블로우오프 톱 지적(2월)"),
    ("2026-04-01","2026-05-26","L2",1,0,"신용융자 급증·쏠림 관측"),
    ("2026-05-27","2026-08-15","L2",2,1,"레버리지 ETF 완판·신용 60조·반대매매 급증"),
    ("2026-06-10","2026-08-15","L3",2,1,"GS·JPM 집중 경고"),
]

def evidence_on(date_s):
    l2 = l3 = 0; cat = False
    for s, e, layer, sc, c, _ in EVIDENCE:
        if s <= date_s <= e:
            if layer == "L2": l2 = max(l2, sc)
            else: l3 = max(l3, sc)
            if c: cat = True
    return l2, l3, cat

# ══════════════════ 3. 지표·상태기계 ══════════════════

def pct_rank(window, v):
    if not window: return None
    return sum(1 for x in window if x <= v) / len(window)

def build_panel(dates, kospi, fred, vkospi):
    """KOSPI 거래일 달력 기준 패널. FRED(미국)는 전일까지만 사용(as-of)."""
    fseries = {k: sorted(v.items()) for k, v in fred.items()}
    fidx = {k: 0 for k in fred}
    panel = []
    hist = {k: [] for k in ["vix", "hy", "cur", "fx", "vk"]}
    closes = []
    for d in dates:
        row = {"date": d}
        # FRED as-of: 관측일 < d (미국 데이터는 익일 아침에 확인)
        for key, sid in [("vix","VIXCLS"),("hy","BAMLH0A0HYM2"),
                         ("cur","T10Y2Y"),("fx","DEXKOUS")]:
            arr = fseries[sid]; i = fidx[sid]
            while i < len(arr) and arr[i][0] < d: i += 1
            fidx[sid] = i
            row[key] = arr[i-1][1] if i > 0 else None
        row["vk"] = vkospi.get(d)
        o, h, l, c = kospi[d]
        row.update(o=o, h=h, l=l, c=c)
        closes.append(c)
        # L1 판정 (각 지표: 1년 백분위>80% 또는 4주 급변)
        warns = 0; l1 = 0; l1x = []
        for key in ["vix", "hy", "cur", "fx"]:
            v = row[key]; H = hist[key]
            if v is None:
                if H: v = H[-1]
                else: continue
            w = False
            win = H[-252:]
            stressed = -v if key == "cur" else v
            swin = [-x for x in win] if key == "cur" else win
            if len(win) >= 200 and pct_rank(swin, stressed) > 0.80: w = True
            if len(H) >= 20:
                v20 = H[-20]
                if key == "vix" and v20 > 0 and v/v20 - 1 >= 0.50: w = True
                if key == "hy" and v - v20 >= 0.50: w = True
                if key == "fx" and v20 > 0 and v/v20 - 1 >= 0.03: w = True
            if w: warns += 1; l1x.append(key)
            H.append(v)
        extreme = False
        if row["vix"] is not None and row["vix"] >= 40: extreme = True
        if row["hy"] is not None and len(hist["hy"]) >= 21 and \
           row["hy"] - hist["hy"][-21] >= 2.0: extreme = True
        l1 = 2 if (warns >= 2 or extreme) else (1 if warns >= 1 else 0)
        row["l1"], row["l1_src"] = l1, "+".join(l1x)
        # L2 기계 근사 (V-KOSPI, 2010~)
        vk = row["vk"]; l2m = 0
        if vk is not None:
            H = hist["vk"]
            if vk >= 40: l2m = 2
            elif vk >= 30 or (len(H) >= 20 and H[-20] > 0 and vk/H[-20]-1 >= 0.5): l2m = 1
            H.append(vk)
        row["l2m"] = l2m
        # 증거 연표
        l2e, l3e, cat = evidence_on(d)
        row["l2e"], row["l3e"], row["cat_ev"] = l2e, l3e, cat
        # 10세션 수익률 (붕괴 확인용)
        row["ret10"] = closes[-1]/closes[-11]-1 if len(closes) >= 11 else 0.0
        panel.append(row)
    return panel

def run_machine(panel, variant):
    """variant: L1ONLY / MECH / FULL. 반환: 상태 리스트 + 전이 로그."""
    s = 0; miss = 0; log = []; states = []
    for row in panel:
        l1 = row["l1"]
        if variant == "L1ONLY": l2, l3 = 0, 0
        elif variant == "MECH": l2, l3 = row["l2m"], 0
        else: l2, l3 = max(row["l2m"], row["l2e"]), row["l3e"]
        collapse = row["ret10"] <= -0.08
        storm = (collapse and l2 >= 1) or (l1 == 2 and l2 == 2)
        rain = (l2 == 2 and (l1 >= 1 or l3 >= 1)) or (l3 == 2 and l1 >= 1) \
               or (l1 == 2 and l2 >= 1)
        cloud = (l1 == 2 or l2 == 2 or l3 == 2) or \
                (sum(1 for x in (l1, l2, l3) if x >= 1) >= 2)
        tgt = 3 if storm else 2 if rain else 1 if cloud else 0
        reason = ""
        if tgt > s:
            reason = f"승급 {s}->{tgt} (L1={l1} L2={l2} L3={l3}" + \
                     (" 붕괴확인" if collapse and tgt == 3 else "") + ")"
            s = tgt; miss = 0
        else:
            cond = {3: storm, 2: rain, 1: cloud, 0: True}[s]
            miss = 0 if cond else miss + 1
            if s > 0 and miss >= 10:
                extra_ok = True
                if s == 3:  # 폭풍 해제 추가 조건: 강제청산 정점 통과 근사
                    vk = row["vk"]
                    extra_ok = (vk is None or vk < 35) and not collapse
                if extra_ok:
                    s -= 1; miss = 0
                    reason = f"해제 {s+1}->{s} (2주 미충족)"
        states.append(s)
        if reason: log.append([row["date"], reason])
    return states, log

# ══════════════════ 4. 사건 탐지·성적표 ══════════════════

def detect_events(panel):
    """252일 이동 고점 대비 -12% 도달 = 사건. 회복(-2% 이내) 시 종료."""
    events = []; cur = None
    closes = [r["c"] for r in panel]
    for i, r in enumerate(panel):
        lo = max(0, i-252)
        peak_i = max(range(lo, i+1), key=lambda j: closes[j])
        dd = closes[i]/closes[peak_i]-1
        if cur is None and dd <= -0.12:
            cur = {"start_i": i, "peak_i": peak_i, "trough_i": i, "trough_dd": dd}
        elif cur is not None:
            if dd < cur["trough_dd"]:
                cur["trough_dd"], cur["trough_i"] = dd, i
            if dd >= -0.02 or i == len(panel)-1:
                cur["end_i"] = i; events.append(cur); cur = None
    return events

def scorecard(panel, states, events):
    rows = []
    closes = [r["c"] for r in panel]
    for ev in events:
        p, t = ev["peak_i"], ev["trough_i"]
        # 최악 단일일 = 붕괴일
        crash_i = max(range(max(p,1), t+1),
                      key=lambda j: -(closes[j]/closes[j-1]-1)) if t > p else t
        first_rain = next((j for j in range(max(0, p-120), t+1) if states[j] >= 2), None)
        first_storm = next((j for j in range(max(0, p-120), t+1) if states[j] >= 3), None)
        rows.append({
            "peak": panel[p]["date"], "trough": panel[t]["date"],
            "drawdown_pct": round(ev["trough_dd"]*100, 1),
            "crash_day": panel[crash_i]["date"],
            "crash_day_ret_pct": round((closes[crash_i]/closes[crash_i-1]-1)*100, 2),
            "first_rain": panel[first_rain]["date"] if first_rain is not None else "",
            "rain_lead_vs_crash_sess": (crash_i - first_rain) if first_rain is not None else "",
            "first_storm": panel[first_storm]["date"] if first_storm is not None else "",
            "avoided_if_derisk_at_rain_pct":
                round((closes[t]/closes[first_rain]-1)*100, 1) if first_rain is not None else "",
        })
    return rows

def false_alarms(panel, states, events):
    """상태>=2(비) 에피소드 중 이후 60세션 내 -8% 하락이 없던 것 = 과잉 경보."""
    closes = [r["c"] for r in panel]
    ev_ranges = [(e["peak_i"], e["end_i"]) for e in events]
    episodes = []; i = 0; n = len(panel)
    while i < n:
        if states[i] >= 2:
            j = i
            while j+1 < n and states[j+1] >= 2: j += 1
            fwd = closes[i:min(n, i+61)]
            worst = min(x/closes[i]-1 for x in fwd)
            in_event = any(a-120 <= i <= b for a, b in ev_ranges)
            episodes.append({"start": panel[i]["date"], "end": panel[j]["date"],
                             "len_sess": j-i+1,
                             "worst_fwd60_pct": round(worst*100, 1),
                             "hit": worst <= -0.08, "near_event": in_event})
            i = j+1
        else: i += 1
    return episodes

# ══════════════════ 5. 인버스 사냥모드 시뮬레이션 ══════════════════

def exhaustion(panel, i):
    """② 상방 동력 소진: 최근 52주 고점 후 8~45세션, 재돌파 실패 + 모멘텀 음전."""
    if i < 262: return False, None
    closes = [r["c"] for r in panel]; highs = [r["h"] for r in panel]
    lo = i-252
    hi_i = max(range(lo, i+1), key=lambda j: closes[j])
    H = closes[hi_i]
    age = i - hi_i
    if not (8 <= age <= 45): return False, None
    dd = closes[i]/H-1
    if not (-0.12 <= dd <= -0.015): return False, None       # 아직 붕괴 전
    retest = any(highs[j] >= 0.98*H and closes[j] < H for j in range(hi_i+1, i+1))
    if not retest: return False, None
    if closes[i] >= closes[i-10]: return False, None          # 10세션 모멘텀 음전
    return True, H

def hunt_sim(panel, states, mode, hold_max=25):
    """mode: A(증거 촉매) / B(기계 촉매) / CHASE(금지된 추격 — 대조군)."""
    closes = [r["c"] for r in panel]
    trades = []; pos = None; cooldown = 0
    for i in range(len(panel)-1):
        row = panel[i]
        if pos:
            exit_reason = None
            if closes[i] > pos["ref_high"]: exit_reason = "가격손절(고점 회복)"
            elif states[i] < 2: exit_reason = "논리손절(상태 해제)"
            elif i - pos["entry_i"] >= hold_max: exit_reason = "시간손절"
            if exit_reason:
                r = -(closes[i]/pos["entry_px"]-1)
                trades.append({**pos, "exit": row["date"], "exit_reason": exit_reason,
                               "inv_ret_pct": round(r*100, 2),
                               "hold_sess": i-pos["entry_i"]})
                pos = None; cooldown = 5
            continue
        if cooldown > 0: cooldown -= 1; continue
        if mode == "CHASE":
            if row["ret10"] <= -0.08 and states[i] >= 2:
                lo = max(0, i-252)
                H = max(closes[lo:i+1])
                pos = {"entry": panel[i+1]["date"], "entry_i": i+1,
                       "entry_px": closes[i+1], "ref_high": H, "mode": mode}
            continue
        if states[i] < 2: continue                             # ① 지반 취약
        ok, H = exhaustion(panel, i)                            # ② 상방 소진
        if not ok: continue
        l2full = max(row["l2m"], row["l2e"])
        cat = row["cat_ev"] if mode == "A" else (l2full == 2 or row["l1"] == 2)
        if not cat: continue                                    # ③ 촉매
        pos = {"entry": panel[i+1]["date"], "entry_i": i+1,
               "entry_px": closes[i+1], "ref_high": H, "mode": mode}
    if pos:  # 미청산 포지션은 마지막 종가 평가
        i = len(panel)-1
        r = -(closes[i]/pos["entry_px"]-1)
        trades.append({**pos, "exit": panel[i]["date"], "exit_reason": "기말 평가",
                       "inv_ret_pct": round(r*100, 2), "hold_sess": i-pos["entry_i"]})
    return trades

# ══════════════════ 6. 메인 ══════════════════

def wcsv(path, rows, cols):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in cols})

def main():
    os.makedirs(OUT, exist_ok=True)
    offline = os.environ.get("BT_OFFLINE") == "1"
    if offline:
        kospi = json.load(open("test_kospi.json")); kospi = {k: tuple(v) for k, v in kospi.items()}
        fred = json.load(open("test_fred.json")); vkospi = json.load(open("test_vk.json"))
        src = "offline"
    else:
        try:
            kospi, src = get_kospi_stooq()
        except Exception as e:
            print(f"stooq 실패({e}) → KRX 폴백")
            kospi, src = get_kospi_krx()
        fred = {sid: fred_series(sid) for sid in
                ["VIXCLS", "BAMLH0A0HYM2", "T10Y2Y", "DEXKOUS"]}
        vkospi = get_vkospi()

    dates = sorted(d for d in kospi if d >= "1996-06-01")
    panel = build_panel(dates, kospi, fred, vkospi)
    events = detect_events(panel)
    print(f"패널 {len(panel)}일, 객관 탐지 사건 {len(events)}건")

    summary = {"kospi_source": src, "days": len(panel),
               "span": [panel[0]["date"], panel[-1]["date"]],
               "events_detected": len(events), "variants": {}}

    for variant in ["L1ONLY", "MECH", "FULL"]:
        states, log = run_machine(panel, variant)
        sc = scorecard(panel, states, events)
        fa = false_alarms(panel, states, events)
        wcsv(f"{OUT}/scorecard_{variant}.csv", sc, list(sc[0].keys()) if sc else ["peak"])
        wcsv(f"{OUT}/rain_episodes_{variant}.csv", fa,
             ["start", "end", "len_sess", "worst_fwd60_pct", "hit", "near_event"])
        with open(f"{OUT}/transitions_{variant}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "reason"]); w.writerows(log)
        n_ep = len(fa); n_hit = sum(1 for e in fa if e["hit"])
        dist = {k: round(sum(1 for s in states if s == k)/len(states)*100, 1)
                for k in range(4)}
        summary["variants"][variant] = {
            "events_with_rain_before": sum(1 for r in sc if r["first_rain"]),
            "events_total": len(sc),
            "rain_episodes": n_ep, "episodes_hit": n_hit,
            "false_alarm_rate": round(1 - n_hit/n_ep, 2) if n_ep else None,
            "state_days_pct": dist, "transitions": len(log)}
        if variant == "FULL":
            states_full = states

    # 사냥모드 (FULL 상태 기준)
    for mode in ["A", "B", "CHASE"]:
        tr = hunt_sim(panel, states_full, mode)
        wcsv(f"{OUT}/hunt_trades_{mode}.csv", tr,
             ["entry", "exit", "entry_px", "ref_high", "inv_ret_pct",
              "hold_sess", "exit_reason", "mode"])
        yrs = len(panel)/252
        rets = [t["inv_ret_pct"] for t in tr]
        summary[f"hunt_{mode}"] = {
            "trades": len(tr), "per_year": round(len(tr)/yrs, 2),
            "win_rate": round(sum(1 for r in rets if r > 0)/len(rets), 2) if rets else None,
            "avg_ret_pct": round(sum(rets)/len(rets), 2) if rets else None,
            "median_ret_pct": round(sorted(rets)[len(rets)//2], 2) if rets else None,
            "worst_pct": min(rets) if rets else None, "best_pct": max(rets) if rets else None,
            "sum_ret_pct": round(sum(rets), 2) if rets else None}

    wcsv(f"{OUT}/events_detected.csv",
         [{"peak": panel[e["peak_i"]]["date"], "trough": panel[e["trough_i"]]["date"],
           "end": panel[e["end_i"]]["date"],
           "drawdown_pct": round(e["trough_dd"]*100, 1)} for e in events],
         ["peak", "trough", "end", "drawdown_pct"])
    # 월말 상태 시계열 (FULL)
    monthly = []
    for i, r in enumerate(panel):
        if i+1 == len(panel) or panel[i+1]["date"][:7] != r["date"][:7]:
            monthly.append({"month": r["date"][:7], "state": states_full[i],
                            "kospi": r["c"], "vix": r["vix"], "vk": r["vk"] or ""})
    wcsv(f"{OUT}/state_monthly_FULL.csv", monthly, ["month", "state", "kospi", "vix", "vk"])

    with open(f"{OUT}/summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
