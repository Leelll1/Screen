# -*- coding: utf-8 -*-
"""threshold_lab — 조기경보 v2.0 참조 계수의 격자 탐색 (절차서 §9 재검토 도구)

목적: 백테스트 캐시 데이터를 재사용해 세 가지 참조 파라미터를 실측 보정한다.
  ① L2 변동성 임계(참고용 — v2.0에서 V-KOSPI는 예측층에서 제외됐으나, 확인·해제
     성분의 절대/상대/하이브리드 임계 비교 근거로 산출)
  ② 폭풍 확인 σ 계수 (v2.0 §3의 -2.2σ×√10 — 후보 1.6~3.0 비교)
  ③ 사냥모드 역행 가격손절 폭 (v2.0 §4의 -8% — 후보 비교)
실행: GitHub Actions (backtest.py와 같은 저장소·같은 Secrets). 캐시가 있으면 수 분.
출력: data/backtest/lab_l2_grid.csv · lab_storm_grid.csv · lab_stop_grid.csv
"""
import os, csv, json, math
import backtest as bt

OUT = bt.OUT

def load_data():
    if os.environ.get("BT_OFFLINE") == "1":
        kospi = {k: tuple(v) for k, v in json.load(open("test_kospi.json")).items()}
        fred = json.load(open("test_fred.json"))
        vk = {k: float(v) for k, v in json.load(open("test_vk.json")).items()}
        return kospi, fred, vk
    try:
        kospi = {}
        try:
            s = bt.get_stooq("%5Ekospi", "KOSPI", 500); kospi.update(s)
        except Exception as e: print(f"stooq 불가({e})")
        y = bt.get_yahoo("^KS11", "KOSPI")
        if y: kospi.update(y)
        krx_kp, vk = bt.get_krx_history()   # 캐시 완비 시 즉시 반환
        kospi.update(krx_kp)
        fred = {sid: bt.fred_series(sid) for sid in
                ["VIXCLS", "BAMLH0A0HYM2", "T10Y2Y", "DEXKOUS"]}
        return kospi, fred, vk
    except Exception as e:
        raise SystemExit(f"데이터 확보 실패: {e}")

def wcsv(path, rows):
    if not rows: return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows: w.writerow(r)

def clone(panel):
    return [dict(r) for r in panel]

def summarize(panel, states, events, min_state=2):
    n = len(panel)
    days = sum(1 for s in states if s >= min_state)
    caught = 0
    for ev in events:
        p, t = ev["peak_i"], ev["trough_i"]
        if any(states[j] >= min_state for j in range(max(0, p-120), t+1)):
            caught += 1
    # 에피소드·과잉경보 (min_state 기준)
    closes = [r["c"] for r in panel]
    eps = []; i = 0
    while i < n:
        if states[i] >= min_state:
            j = i
            while j+1 < n and states[j+1] >= min_state: j += 1
            fwd = closes[i:min(n, i+61)]
            worst = min(x/closes[i]-1 for x in fwd)
            eps.append((j-i+1, worst <= -0.08))
            i = j+1
        else: i += 1
    hits = sum(1 for _, h in eps if h)
    return {"caught": f"{caught}/{len(events)}", "episodes": len(eps), "hits": hits,
            "fa_rate": round(1-hits/len(eps), 2) if eps else "",
            "days_pct": round(days/n*100, 1),
            "longest_ep": max((l for l, _ in eps), default=0)}

def main():
    os.makedirs(OUT, exist_ok=True)
    kospi, fred, vk = load_data()
    dates = sorted(d for d in kospi if d >= "1996-06-01")
    panel = bt.build_panel(dates, kospi, fred, vk, "KR")
    events = bt.detect_events(panel)
    print(f"패널 {len(panel)}일, 사건 {len(events)}건")

    # 파생 시계열: vk 1년 백분위, σ20 (20일 수익률 표준편차)
    closes = [r["c"] for r in panel]
    vks = [r["vk"] for r in panel]
    vk_pct = [None]*len(panel); sig20 = [0.01]*len(panel)
    hist = []
    rets = []
    for i, r in enumerate(panel):
        if i > 0: rets.append(closes[i]/closes[i-1]-1)
        if len(rets) >= 20:
            w = rets[-20:]; mu = sum(w)/20
            sig20[i] = max(math.sqrt(sum((x-mu)**2 for x in w)/20), 0.002)
        v = vks[i]
        if v is not None:
            win = hist[-252:]
            if len(win) >= 200:
                vk_pct[i] = sum(1 for x in win if x <= v)/len(win)
            hist.append(v)

    # ── ① L2 변동성 임계 격자 (MECH 변형으로 격리 평가) ──
    rows = []
    def run_l2(label, fn):
        p2 = clone(panel)
        for i, r in enumerate(p2):
            r["l2m"] = fn(i)
        states, _ = bt.run_machine(p2, "MECH")
        rows.append({"rule": label, **summarize(p2, states, events)})
    for t in (35, 40, 45, 50):
        run_l2(f"ABS {t} (경계 {t-10})",
               lambda i, t=t: 0 if vks[i] is None else (2 if vks[i] >= t else 1 if vks[i] >= t-10 else 0))
    for p in (0.85, 0.90, 0.95):
        run_l2(f"REL p{int(p*100)} (경계 p{int(p*100)-10})",
               lambda i, p=p: 0 if vk_pct[i] is None else (2 if vk_pct[i] >= p else 1 if vk_pct[i] >= p-0.10 else 0))
    for t, p in ((40, 0.85), (40, 0.90), (45, 0.85)):
        run_l2(f"HYB abs{t}&p{int(p*100)}",
               lambda i, t=t, p=p: 0 if (vks[i] is None or vk_pct[i] is None) else
               (2 if (vks[i] >= t and vk_pct[i] >= p) else 1 if (vks[i] >= t-10 or vk_pct[i] >= p) else 0))
    wcsv(f"{OUT}/lab_l2_grid.csv", rows)
    print("① L2 격자 완료:", len(rows), "규칙")

    # ── ② 폭풍 확인 σ 계수 격자 (FULL 변형, ret10 스케일 주입) ──
    rows = []
    def run_storm(label, k):
        p2 = clone(panel)
        if k is not None:
            for i, r in enumerate(p2):
                denom = k * sig20[i] * math.sqrt(10)
                r["ret10"] = r["ret10"] * (0.08/denom)
        states, _ = bt.run_machine(p2, "FULL")
        s = summarize(p2, states, events, min_state=3)
        rows.append({"rule": label, "storm_caught": s["caught"],
                     "storm_days_pct": s["days_pct"], "storm_episodes": s["episodes"],
                     "longest_storm": s["longest_ep"]})
    run_storm("고정 -8% (v1.0)", None)
    for k in (1.6, 2.0, 2.2, 2.6, 3.0):
        run_storm(f"-{k}σ×√10 (v2.0 후보)", k)
    wcsv(f"{OUT}/lab_storm_grid.csv", rows)
    print("② 폭풍 σ 격자 완료")

    # ── ③ 사냥 역행 손절 격자 (B형, FULL 상태) ──
    states_full, _ = bt.run_machine(panel, "FULL")
    rows = []
    def hunt_with_stop(stop):
        trades = []; pos = None; cd = 0
        for i in range(len(panel)-1):
            r = panel[i]
            if pos:
                reason = None
                adverse = closes[i]/pos["px"]-1
                if stop == "sigma":
                    lim = 1.5*sig20[pos["ei"]]*math.sqrt(5)
                    if adverse >= lim: reason = "역행손절(σ)"
                elif stop is not None and adverse >= stop:
                    reason = f"역행손절({int(stop*100)}%)"
                if not reason:
                    if closes[i] > pos["hi"]: reason = "가격손절(고점회복)"
                    elif states_full[i] < 2: reason = "논리손절"
                    elif i-pos["ei"] >= 25: reason = "시간손절"
                if reason:
                    trades.append(-(closes[i]/pos["px"]-1))
                    pos = None; cd = 5
                continue
            if cd: cd -= 1; continue
            if states_full[i] < 2: continue
            ok, H = bt.exhaustion(panel, i)
            if not ok: continue
            l2f = max(r["l2m"], r["l2e"])
            if not (l2f == 2 or r["l1"] == 2): continue
            pos = {"px": closes[i+1], "ei": i+1, "hi": H}
        if pos: trades.append(-(closes[-1]/pos["px"]-1))
        n = len(trades)
        return {"trades": n,
                "win_rate": round(sum(1 for t in trades if t > 0)/n, 2) if n else "",
                "avg_pct": round(sum(trades)/n*100, 2) if n else "",
                "worst_pct": round(min(trades)*100, 2) if n else "",
                "sum_pct": round(sum(trades)*100, 2) if n else ""}
    for label, stop in [("현행(역행손절 없음)", None), ("-6%", 0.06), ("-8%", 0.08),
                        ("-10%", 0.10), ("1.5σ√5", "sigma")]:
        rows.append({"stop": label, **hunt_with_stop(stop)})
    wcsv(f"{OUT}/lab_stop_grid.csv", rows)
    print("③ 손절 격자 완료")
    print(json.dumps({"files": ["lab_l2_grid.csv", "lab_storm_grid.csv",
                                "lab_stop_grid.csv"]}, ensure_ascii=False))

if __name__ == "__main__":
    main()
