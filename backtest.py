# -*- coding: utf-8 -*-
"""조기경보 As-of 백테스트 v2 — 한국·미국 2시장 분리판 + 커플링 분석 + 인버스 사냥모드

v1 → v2 (2026-08-27, 사용자 설계 질문 반영):
  · 미국 편(S&P500) 추가 — 같은 L1(공용 기압계) + 미국 증거 연표로 별도 상태기계
  · 커플링 분석 — 각 사건이 '한국 단독형'인지 '글로벌 동조형'인지 데이터로 분류
    (미국 견조 + 한국 단독 폭락 사례의 빈도·구조를 실측)
  · 전파 규칙 검증 — "미국 상태가 한국 상태의 하한을 끌어올린다"(비대칭 전파)를
    변형(FULL_X)으로 돌려 리드타임 개선 vs 과잉경보 증가를 측정
  · 사냥모드도 시장별 실행 (KR: KOSPI 인버스 / US: S&P 인버스 근사)

방법론 원칙(v1과 동일): L1은 FRED 완전 기계·시점고정, L2/L3는 당시 공개 정보의
증거 연표(시장 태그 KR/US/BOTH), V-KOSPI는 KRX 소급 수집(KR 기계 L2 근사).
실행: GitHub Actions (Secrets: FRED_API_KEY, KRX_AUTH_KEY). 표준 라이브러리만.
출력: data/backtest/ 소형 CSV·JSON.
"""
import os, json, csv, datetime, urllib.request, urllib.parse, time

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

def get_yahoo(symbol, label):
    """야후 차트 API — 명시적 기간(period1=0)으로 전 기간 요청, 호스트 2중 시도.
    행수가 적어도 실패로 던지지 않고 확보분을 반환한다 (병합 소스의 하나일 뿐)."""
    now = int(time.time())
    variants = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?period1=0&period2={now}&interval=1d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?period1=0&period2={now}&interval=1d",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=max&interval=1d",
    ]
    best = {}
    for url in variants:
        try:
            data = json.loads(http_get(url))
            res = data["chart"]["result"][0]
            ts = res["timestamp"]; q = res["indicators"]["quote"][0]
            off = res.get("meta", {}).get("gmtoffset", 0)
            out = {}
            for i, t in enumerate(ts):
                c = q["close"][i]
                if c is None: continue
                d = datetime.datetime.fromtimestamp(
                    t + off, datetime.timezone.utc).date().isoformat()
                out[d] = (q["open"][i] or c, q["high"][i] or c, q["low"][i] or c, c)
            if len(out) > len(best): best = out
            if len(best) >= 3000: break
        except Exception as e:
            print(f"  yahoo {symbol} 변형 실패: {e}")
    if best:
        print(f"{label}(yahoo): {len(best)} days ({min(best)}..{max(best)})")
    return best

def get_stooq(symbol, label, min_rows=3000):
    raw = http_get(f"https://stooq.com/q/d/l/?s={symbol}&i=d").decode()
    rows = list(csv.DictReader(raw.splitlines()))
    out = {}
    for r in rows:
        try:
            out[r["Date"]] = (float(r["Open"]), float(r["High"]),
                              float(r["Low"]), float(r["Close"]))
        except (ValueError, KeyError):
            continue
    if len(out) < min_rows:
        raise RuntimeError(f"stooq {symbol} rows too few: {len(out)}")
    print(f"{label}(stooq): {len(out)} days ({min(out)}..{max(out)})")
    return out

def krx_call(path, bas):
    # 공식 명세서 기준 https (2026-08-27 확인 — http는 과거 일자에서 403 의심)
    url = f"https://data-dbg.krx.co.kr/svc/apis/{path}?basDd={bas}"
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

def load_cache(path, parse):
    out = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                out[r["date"]] = parse(r)
    return out

def get_krx_history():
    """KRX 소급 수확 — 코스피(OHLC)와 V-KOSPI를 한 번의 역방향 걷기로 동시 수집.
    오늘부터 2010-01-04까지, 거부 벽(연속 401/403)을 만나면 확보분으로 정상 종료.
    두 캐시 파일로 증분 재실행 지원."""
    kp_path = f"{OUT}/krx_kospi_daily.csv"; vk_path = f"{OUT}/vkospi_daily.csv"
    kospi = load_cache(kp_path, lambda r: (float(r["o"]), float(r["h"]),
                                           float(r["l"]), float(r["c"])))
    vk = load_cache(vk_path, lambda r: float(r["close"]))
    d = datetime.date.today(); floor = datetime.date(2010, 1, 4)
    deny = errs = calls = 0
    while d >= floor:
        ds = d.isoformat()
        if d.weekday() < 5 and (ds not in kospi or ds not in vk):
            ok_any = False
            for path, kind in [("idx/kospi_dd_trd", "kp"), ("idx/drvprod_dd_trd", "vk")]:
                if (kind == "kp" and ds in kospi) or (kind == "vk" and ds in vk):
                    continue
                try:
                    for r in krx_call(path, d.strftime("%Y%m%d")):
                        nm = str(r.get("IDX_NM", "")).strip()
                        if kind == "kp" and nm == "코스피":
                            c = fnum(r.get("CLSPRC_IDX"))
                            if c: kospi[ds] = (fnum(r.get("OPNPRC_IDX")) or c,
                                               fnum(r.get("HGPRC_IDX")) or c,
                                               fnum(r.get("LWPRC_IDX")) or c, c)
                            if kind == "kp": break
                        if kind == "vk" and "변동성" in nm:
                            c = fnum(r.get("CLSPRC_IDX"))
                            if c: vk[ds] = c
                            break
                    ok_any = True; errs = 0
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403): deny += 1
                    else: errs += 1
                except Exception:
                    errs += 1
                calls += 1
                time.sleep(0.05)
            if ok_any: deny = 0
            if deny >= 6:
                print(f"KRX 소급: {d} 이전 거부(HTTP 401/403 연속) — 확보분으로 진행 "
                      f"(코스피 {len(kospi)}일, V-KOSPI {len(vk)}일)"); break
            if errs >= 40:
                print(f"KRX 소급 중단(오류 누적): {d}"); break
            if calls % 1000 == 0 and calls:
                print(f"  krx … {d} (kospi {len(kospi)} / vk {len(vk)})")
        d -= datetime.timedelta(days=1)
    os.makedirs(OUT, exist_ok=True)
    with open(kp_path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "o", "h", "l", "c"])
        for k in sorted(kospi): w.writerow([k, *kospi[k]])
    with open(vk_path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "close"])
        for k in sorted(vk): w.writerow([k, vk[k]])
    print(f"KRX 소급 완료: 코스피 {len(kospi)}일 / V-KOSPI {len(vk)}일")
    return kospi, vk

# ══════════ 2. 증거 연표 (당시 공개 정보만 · 시장 태그 KR/US/BOTH) ══════════
# (start, end, layer, score, catalyst?, market, label)
EVIDENCE = [
    ("1997-01-23","1997-07-14","L3",1,0,"KR","한보·삼미·진로 연쇄부도"),
    ("1997-07-15","1997-12-31","L3",2,1,"KR","기아사태+태국발 아시아 전염"),
    ("1999-11-01","2000-04-30","L2",1,0,"BOTH","닷컴·코스닥 극단 과열, 마진데트 급증"),
    ("2000-03-20","2000-04-30","L3",1,0,"BOTH","Barron's Burning Up·MS 패소"),
    ("2002-12-01","2003-03-31","L3",1,0,"KR","카드 연체 급증·이라크전 캘린더"),
    ("2003-01-15","2003-04-30","L2",1,0,"KR","카드채 시장 경색 관측"),
    ("2004-04-12","2004-05-31","L3",1,0,"BOTH","중국 과열·긴축 시사 보도"),
    ("2007-08-09","2008-08-31","L3",1,0,"BOTH","BNP 동결~베어스턴스 크레딧 경색"),
    ("2007-11-01","2008-10-31","L2",1,0,"KR","외국인 현물 대량 순매도 관측"),
    ("2007-07-01","2008-10-31","L2",1,0,"US","마진데트 사상 최대·서브프라임 손실 공개"),
    ("2008-09-01","2008-10-31","L3",2,1,"BOTH","9월 위기설 공론화·리먼"),
    ("2011-04-18","2011-07-13","L3",1,0,"BOTH","S&P 미국 전망 부정적"),
    ("2011-07-14","2011-09-30","L3",2,1,"BOTH","CreditWatch+부채한도 D-2주 에스컬레이션"),
    ("2013-05-22","2013-06-04","L3",1,0,"BOTH","버냉키 테이퍼 발언"),
    ("2013-06-05","2013-07-10","L3",2,1,"BOTH","FOMC(6.19) D-2주+신흥국 이탈 가속"),
    ("2015-06-19","2015-08-10","L3",1,0,"BOTH","상하이 -30%·중국 경착륙 보도"),
    ("2015-08-11","2015-09-15","L3",2,1,"BOTH","위안 기습 절하 후 에스컬레이션"),
    ("2016-01-04","2016-02-29","L3",1,0,"BOTH","중국 서킷브레이커·유가 붕괴"),
    ("2018-08-15","2018-11-15","L3",1,0,"KR","IB 메모리 피크 경고(반도체 편중 직격)"),
    ("2018-09-24","2018-12-31","L3",1,0,"BOTH","미중 관세 확전·파월 중립금리 발언"),
    ("2019-05-06","2019-07-31","L3",1,0,"KR","미중 관세 재점화+한일 수출규제"),
    ("2019-08-01","2019-08-31","L3",2,1,"KR","추가관세 발표·위안 7·원 1,200 돌파"),
    ("2020-01-23","2020-02-17","L3",1,0,"BOTH","우한 봉쇄·WHO 비상사태"),
    ("2020-02-18","2020-04-30","L3",2,1,"BOTH","대구 집단감염·글로벌 확산"),
    ("2021-08-13","2022-01-31","L2",2,1,"KR","신용융자 25조 사상 최대(§1 극단 예시)"),
    ("2021-10-15","2022-06-30","L2",2,1,"US","FINRA 마진데트 사상 최대(9,360억 달러)"),
    ("2021-11-30","2022-10-31","L3",1,0,"BOTH","transitory 폐기·점도표 가속"),
    ("2022-02-01","2022-10-31","L2",1,0,"KR","외국인 대량 순매도 지속"),
    ("2024-07-02","2024-08-09","L2",2,1,"BOTH","CFTC 엔 숏 사상 최대(주간 공개)"),
    ("2024-07-25","2024-08-09","L3",1,0,"BOTH","BOJ 인상 관측·기술주 1차 조정"),
    ("2025-02-13","2025-03-25","L3",1,0,"BOTH","상호관세 각서 D-7주 캘린더"),
    ("2025-03-26","2025-04-30","L3",2,1,"BOTH","자동차 관세·에스컬레이션 D-1주"),
    ("2026-02-15","2026-05-26","L3",1,0,"KR","블로우오프 톱 지적(2월)"),
    ("2026-04-01","2026-05-26","L2",1,0,"KR","신용융자 급증·쏠림 관측"),
    ("2026-05-27","2026-08-15","L2",2,1,"KR","레버리지 ETF 완판·신용 60조·반대매매 급증"),
    ("2026-06-10","2026-08-15","L3",2,1,"KR","GS·JPM 집중 경고"),
]

def evidence_on(date_s, market):
    l2 = l3 = 0; cat = False
    for s, e, layer, sc, c, m, _ in EVIDENCE:
        if s <= date_s <= e and (m == market or m == "BOTH"):
            if layer == "L2": l2 = max(l2, sc)
            else: l3 = max(l3, sc)
            if c: cat = True
    return l2, l3, cat

# ══════════════════ 3. 지표·상태기계 ══════════════════

def pct_rank(window, v):
    if not window: return None
    return sum(1 for x in window if x <= v) / len(window)

def build_panel(dates, prices, fred, vkospi, market):
    """market: KR(미국 데이터는 전일까지 as-of) / US(당일 종가 기준 동시성)."""
    fseries = {k: sorted(v.items()) for k, v in fred.items()}
    fidx = {k: 0 for k in fred}
    panel = []; hist = {k: [] for k in ["vix", "hy", "cur", "fx", "vk"]}
    closes = []
    for d in dates:
        row = {"date": d}
        for key, sid in [("vix","VIXCLS"),("hy","BAMLH0A0HYM2"),
                         ("cur","T10Y2Y"),("fx","DEXKOUS")]:
            arr = fseries[sid]; i = fidx[sid]
            # as-of: KR은 미국 데이터를 전일까지만(<d), US는 당일 종가까지(<=d)
            while i < len(arr) and (arr[i][0] < d if market == "KR"
                                    else arr[i][0] <= d): i += 1
            fidx[sid] = i
            row[key] = arr[i-1][1] if i > 0 else None
        row["vk"] = vkospi.get(d) if market == "KR" else None
        o, h, l, c = prices[d]
        row.update(o=o, h=h, l=l, c=c)
        closes.append(c)
        warns = 0; l1x = []
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
        row["l1"] = 2 if (warns >= 2 or extreme) else (1 if warns >= 1 else 0)
        row["l1_src"] = "+".join(l1x)
        vk = row["vk"]; l2m = 0
        if vk is not None:
            H = hist["vk"]
            if vk >= 40: l2m = 2
            elif vk >= 30 or (len(H) >= 20 and H[-20] > 0 and vk/H[-20]-1 >= 0.5): l2m = 1
            H.append(vk)
        row["l2m"] = l2m
        l2e, l3e, cat = evidence_on(d, market)
        row["l2e"], row["l3e"], row["cat_ev"] = l2e, l3e, cat
        row["ret10"] = closes[-1]/closes[-11]-1 if len(closes) >= 11 else 0.0
        panel.append(row)
    return panel

def run_machine(panel, variant, floor_by_date=None):
    """variant: L1ONLY/MECH/FULL. floor_by_date: {date: 하한상태} (전파 규칙 검증용)."""
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
        floor = floor_by_date.get(row["date"], 0) if floor_by_date else 0
        tgt = max(tgt, min(floor, 2))   # 전파 하한은 최대 '비'까지 (폭풍은 자체 확인 필요)
        reason = ""
        if tgt > s:
            reason = f"승급 {s}->{tgt} (L1={l1} L2={l2} L3={l3}" + \
                     (" 전파하한" if floor > s and tgt == min(floor,2) and not (storm or rain or cloud and tgt==1) else "") + ")"
            s = tgt; miss = 0
        else:
            cond = {3: storm, 2: rain, 1: cloud, 0: True}[s]
            if s > 0 and s <= min(floor, 2): cond = True   # 하한 유지 중엔 해제 보류
            miss = 0 if cond else miss + 1
            if s > 0 and miss >= 10:
                extra_ok = True
                if s == 3:
                    vk = row["vk"]
                    extra_ok = (vk is None or vk < 35) and not collapse
                if extra_ok:
                    s -= 1; miss = 0
                    reason = f"해제 {s+1}->{s} (2주 미충족)"
        states.append(s)
        if reason: log.append([row["date"], reason])
    return states, log

# ══════════════════ 4. 사건 탐지·성적표·과잉경보 ══════════════════

def detect_events(panel):
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
    rows = []; closes = [r["c"] for r in panel]
    for ev in events:
        p, t = ev["peak_i"], ev["trough_i"]
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

# ══════════════════ 5. 커플링 분석 (한국 단독형 vs 글로벌 동조형) ══════════════════

def coupling(panel_a, events_a, panel_b, label_a, label_b):
    """A시장 사건마다 같은 기간 B시장 최대 낙폭을 재서 동조성 분류."""
    b_dates = {r["date"]: i for i, r in enumerate(panel_b)}
    b_closes = [r["c"] for r in panel_b]
    rows = []
    for ev in events_a:
        d0 = panel_a[max(0, ev["peak_i"]-20)]["date"]
        d1 = panel_a[min(len(panel_a)-1, ev["trough_i"]+20)]["date"]
        idx = [i for d, i in b_dates.items() if d0 <= d <= d1]
        if not idx: continue
        lo, hi = min(idx), max(idx)
        pk = max(b_closes[max(0, lo-5):hi+1])
        tr = min(b_closes[lo:hi+1])
        b_dd = tr/pk - 1
        a_dd = ev["trough_dd"]
        ratio = b_dd/a_dd if a_dd < 0 else 0   # B 낙폭 / A 낙폭 (동조 강도)
        cls = (f"{label_a} 단독" if (b_dd > -0.10 or ratio < 0.35) else
               "글로벌 동조" if ratio >= 0.6 else "부분 동조")
        rows.append({"market": label_a, "peak": panel_a[ev["peak_i"]]["date"],
                     "trough": panel_a[ev["trough_i"]]["date"],
                     f"{label_a}_dd_pct": round(a_dd*100, 1),
                     f"{label_b}_dd_pct": round(b_dd*100, 1),
                     "ratio": round(ratio, 2), "class": cls})
    return rows

# ══════════════════ 6. 인버스 사냥모드 ══════════════════

def exhaustion(panel, i):
    if i < 262: return False, None
    closes = [r["c"] for r in panel]; highs = [r["h"] for r in panel]
    lo = i-252
    hi_i = max(range(lo, i+1), key=lambda j: closes[j])
    H = closes[hi_i]; age = i - hi_i
    if not (8 <= age <= 45): return False, None
    dd = closes[i]/H-1
    if not (-0.12 <= dd <= -0.015): return False, None
    if not any(highs[j] >= 0.98*H and closes[j] < H for j in range(hi_i+1, i+1)):
        return False, None
    if closes[i] >= closes[i-10]: return False, None
    return True, H

def hunt_sim(panel, states, mode, hold_max=25):
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
                H = max(closes[max(0, i-252):i+1])
                pos = {"entry": panel[i+1]["date"], "entry_i": i+1,
                       "entry_px": closes[i+1], "ref_high": H, "mode": mode}
            continue
        if states[i] < 2: continue
        ok, H = exhaustion(panel, i)
        if not ok: continue
        l2full = max(row["l2m"], row["l2e"])
        cat = row["cat_ev"] if mode == "A" else (l2full == 2 or row["l1"] == 2)
        if not cat: continue
        pos = {"entry": panel[i+1]["date"], "entry_i": i+1,
               "entry_px": closes[i+1], "ref_high": H, "mode": mode}
    if pos:
        i = len(panel)-1
        r = -(closes[i]/pos["entry_px"]-1)
        trades.append({**pos, "exit": panel[i]["date"], "exit_reason": "기말 평가",
                       "inv_ret_pct": round(r*100, 2), "hold_sess": i-pos["entry_i"]})
    return trades

def hunt_stats(trades, n_days):
    yrs = n_days/252
    rets = [t["inv_ret_pct"] for t in trades]
    return {"trades": len(trades), "per_year": round(len(trades)/yrs, 2),
            "win_rate": round(sum(1 for r in rets if r > 0)/len(rets), 2) if rets else None,
            "avg_ret_pct": round(sum(rets)/len(rets), 2) if rets else None,
            "median_ret_pct": round(sorted(rets)[len(rets)//2], 2) if rets else None,
            "worst_pct": min(rets) if rets else None,
            "best_pct": max(rets) if rets else None,
            "sum_ret_pct": round(sum(rets), 2) if rets else None,
            "stop_mix": {k: sum(1 for t in trades if t["exit_reason"].startswith(k))
                         for k in ["가격", "논리", "시간", "기말"]}}

# ══════════════════ 7. 메인 ══════════════════

def wcsv(path, rows, cols):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in cols})

def analyze(tag, panel, variants, summary, floor=None):
    events = detect_events(panel)
    out_states = {}
    for variant in variants:
        fl = floor if variant.endswith("_X") else None
        base = variant.replace("_X", "")
        states, log = run_machine(panel, base, fl)
        sc = scorecard(panel, states, events)
        fa = false_alarms(panel, states, events)
        wcsv(f"{OUT}/scorecard_{tag}_{variant}.csv", sc,
             list(sc[0].keys()) if sc else ["peak"])
        wcsv(f"{OUT}/rain_episodes_{tag}_{variant}.csv", fa,
             ["start", "end", "len_sess", "worst_fwd60_pct", "hit", "near_event"])
        with open(f"{OUT}/transitions_{tag}_{variant}.csv", "w", newline="") as f:
            w = csv.writer(f); w.writerow(["date", "reason"]); w.writerows(log)
        n_ep = len(fa); n_hit = sum(1 for e in fa if e["hit"])
        summary.setdefault(tag, {})[variant] = {
            "events_with_rain_before": sum(1 for r in sc if r["first_rain"]),
            "events_total": len(sc),
            "rain_episodes": n_ep, "episodes_hit": n_hit,
            "false_alarm_rate": round(1 - n_hit/n_ep, 2) if n_ep else None,
            "state_days_pct": {k: round(sum(1 for s in states if s == k)/len(states)*100, 1)
                               for k in range(4)},
            "transitions": len(log)}
        out_states[variant] = states
    wcsv(f"{OUT}/events_detected_{tag}.csv",
         [{"peak": panel[e["peak_i"]]["date"], "trough": panel[e["trough_i"]]["date"],
           "end": panel[e["end_i"]]["date"],
           "drawdown_pct": round(e["trough_dd"]*100, 1)} for e in events],
         ["peak", "trough", "end", "drawdown_pct"])
    return events, out_states

def main():
    os.makedirs(OUT, exist_ok=True)
    offline = os.environ.get("BT_OFFLINE") == "1"
    if offline:
        kospi = {k: tuple(v) for k, v in json.load(open("test_kospi.json")).items()}
        spx = {k: tuple(v) for k, v in json.load(open("test_spx.json")).items()} \
              if os.path.exists("test_spx.json") else None
        fred = json.load(open("test_fred.json")); vkospi = json.load(open("test_vk.json"))
        src = "offline"
    else:
        # ── KOSPI: 3소스 병합 (KRX 공식 > 야후 > Stooq) ──
        kospi = {}; parts = []
        try:
            s = get_stooq("%5Ekospi", "KOSPI", 500); kospi.update(s)
            parts.append(f"stooq {len(s)}")
        except Exception as e: print(f"stooq KOSPI 불가({e})")
        y = get_yahoo("^KS11", "KOSPI")
        if y: kospi.update(y); parts.append(f"yahoo {len(y)}")
        krx_kp, vkospi = get_krx_history()
        if krx_kp: kospi.update(krx_kp); parts.append(f"krx {len(krx_kp)}")
        src = "+".join(parts) or "none"
        if len(kospi) < 1500:
            raise RuntimeError(f"KOSPI 데이터 부족({len(kospi)}일, 소스: {src}) — "
                               "전 소스 점검 필요")
        print(f"KOSPI 병합: {len(kospi)}일 ({min(kospi)}..{max(kospi)}) [{src}]")
        # ── S&P500: 야후 > Stooq > FRED NASDAQCOM(대용 지수) ──
        spx = get_yahoo("^GSPC", "S&P500") or None
        if not spx or len(spx) < 1500:
            try:
                spx = get_stooq("%5Espx", "S&P500", 1500)
            except Exception as e:
                print(f"stooq SPX 불가({e})")
                try:
                    nas = fred_series("NASDAQCOM")
                    spx = {d: (v, v, v, v) for d, v in nas.items()}
                    print(f"미국 편: NASDAQCOM 대용 지수 사용 ({len(spx)}일)")
                except Exception as e2:
                    print(f"미국 편 생략({e2})"); spx = None
        fred = {sid: fred_series(sid) for sid in
                ["VIXCLS", "BAMLH0A0HYM2", "T10Y2Y", "DEXKOUS"]}

    summary = {"kospi_source": src, "us_leg": spx is not None}

    kr_dates = sorted(d for d in kospi if d >= "1996-06-01")
    kr_panel = build_panel(kr_dates, kospi, fred, vkospi, "KR")
    summary["kr_span"] = [kr_panel[0]["date"], kr_panel[-1]["date"], len(kr_panel)]

    us_panel = None
    if spx:
        us_dates = sorted(d for d in spx if d >= "1996-06-01")
        us_panel = build_panel(us_dates, spx, fred, {}, "US")
        summary["us_span"] = [us_panel[0]["date"], us_panel[-1]["date"], len(us_panel)]
        us_events, us_states = analyze("US", us_panel, ["L1ONLY", "FULL"], summary)
        # 미국 상태를 한국 날짜에 as-of 매핑 (미국 전일 상태 → 한국 아침)
        us_full = us_states["FULL"]
        floor = {}
        ui = 0; us_d = [r["date"] for r in us_panel]
        for r in kr_panel:
            while ui < len(us_d) and us_d[ui] < r["date"]: ui += 1
            st = us_full[ui-1] if ui > 0 else 0
            floor[r["date"]] = st - 1 if st >= 2 else 0   # 미국 비→한국 하한 구름, 폭풍→비
    else:
        floor = None

    kr_variants = ["L1ONLY", "MECH", "FULL"] + (["FULL_X"] if floor else [])
    kr_events, kr_states = analyze("KR", kr_panel, kr_variants, summary, floor)

    if us_panel:
        cp = coupling(kr_panel, kr_events, us_panel, "KR", "US") + \
             coupling(us_panel, us_events, kr_panel, "US", "KR")
        cols = sorted({k for r in cp for k in r}, key=lambda x: x != "market")
        wcsv(f"{OUT}/coupling.csv", cp, cols)
        summary["coupling"] = {
            "KR사건_중_KR단독": sum(1 for r in cp if r["market"] == "KR" and r["class"].endswith("단독")),
            "KR사건_총": sum(1 for r in cp if r["market"] == "KR"),
            "US사건_중_US단독": sum(1 for r in cp if r["market"] == "US" and r["class"].endswith("단독")),
            "US사건_총": sum(1 for r in cp if r["market"] == "US")}

    # 사냥모드 — 시장별
    for tag, panel, states_map in [("KR", kr_panel, kr_states)] + \
            ([("US", us_panel, us_states)] if us_panel else []):
        st = states_map["FULL"]
        modes = ["A", "B", "CHASE"] if tag == "KR" else ["B", "CHASE"]
        for mode in modes:
            tr = hunt_sim(panel, st, mode)
            wcsv(f"{OUT}/hunt_trades_{tag}_{mode}.csv", tr,
                 ["entry", "exit", "entry_px", "ref_high", "inv_ret_pct",
                  "hold_sess", "exit_reason", "mode"])
            summary[f"hunt_{tag}_{mode}"] = hunt_stats(tr, len(panel))

    # 월말 상태 시계열
    for tag, panel, states_map in [("KR", kr_panel, kr_states)] + \
            ([("US", us_panel, us_states)] if us_panel else []):
        st = states_map["FULL"]
        monthly = []
        for i, r in enumerate(panel):
            if i+1 == len(panel) or panel[i+1]["date"][:7] != r["date"][:7]:
                monthly.append({"month": r["date"][:7], "state": st[i],
                                "close": r["c"], "vix": r["vix"], "vk": r["vk"] or ""})
        wcsv(f"{OUT}/state_monthly_{tag}.csv", monthly,
             ["month", "state", "close", "vix", "vk"])

    with open(f"{OUT}/summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
