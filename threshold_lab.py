# -*- coding: utf-8 -*-
"""threshold_lab — 조기경보 v2.0 참조 계수의 격자 탐색 (절차서 §9 재검토 도구)

목적: 백테스트 캐시 데이터를 재사용해 세 가지 참조 파라미터를 실측 보정한다.
  ① L2 변동성 임계(참고용 — v2.0에서 V-KOSPI는 예측층에서 제외됐으나, 확인·해제
     성분의 절대/상대/하이브리드 임계 비교 근거로 산출)
  ② 폭풍 확인 σ 계수 (v2.0 §3의 -2.2σ×√10 — 후보 1.6~3.0 비교)
  ③ 사냥모드 역행 가격손절 폭 (v2.0 §4의 -8% — 후보 비교)
실행: GitHub Actions (backtest.py와 같은 저장소·같은 Secrets). 캐시가 있으면 수 분.
출력: data/backtest/lab_l2_grid.csv · lab_storm_grid.csv · lab_stop_grid.csv

2026-09-11 — 우연 기준선 3열 신설 (미결 I-16).
  종전 격자에는 caught 열만 있어 「8/23 이 좋은 값인가」를 판정할 수 없었다.
  경보를 오래 켜 두는 규칙은 포착 수가 저절로 오르기 때문이다. 그래서 각 행에
  같은 가동률·같은 연속 길이를 유지한 채 «시간 정렬만» 깬 귀무분포를 함께 싣는다.
  구현은 backtest.null_baseline 을 그대로 부른다 — 두 산출물의 기준선 정의가
  갈라지지 않게 하기 위해서다(정의를 두 곳에 적지 않는다).
  ⚠️ 해석 경계. ① 격자는 MECH 변형(기계 입력 전용)이므로 낮은 p 를 «정렬»의
     증거로 읽을 수 있다. ② 폭풍 격자는 FULL 변형이며 l2e/l3e 가 손으로 매긴
     EVIDENCE 표에서 오므로 그 p 는 예측력의 근거가 아니다(미결 I-13).
     ③ V-KOSPI 는 2010 이전이 없어 MECH 는 패널의 약 45%에서 발화 자체가
     불가능하다 — 회전이 그 구간을 사건들에 섞으므로 값은 실제보다 나쁘게 나온다.
"""
import os, csv, json, math
import backtest as bt

OUT = bt.OUT

# 우연 기준선을 낼 수 없을 때 쓰는 빈 칸. 열 자체는 항상 존재해야 CSV 머리가
# 행마다 달라지지 않는다(DictWriter 는 첫 행의 키로 머리를 만든다).
NULL_EMPTY = {"caught_null_mean": "", "caught_null_p95": "",
              "p_value": "", "null_draws": "", "null_degenerate": ""}


def null_cols(states, events, level):
    """이 규칙의 우연 기준선 — backtest.null_baseline 의 산출을 열로 편다.

    getattr 로 부르는 이유: backtest 가 아직 이 함수를 갖지 않은 판일 때도
    격자 실행 자체는 죽지 않게 한다(분할 머지 대비 — summarize 의 lead 와 같은 처리).
    """
    nb = getattr(bt, "null_baseline", None)
    if nb is None:
        return dict(NULL_EMPTY)
    try:
        r = nb(states, events, level=level)
    except Exception as e:
        print(f"  우연 기준선 계산 실패({e}) — 빈 칸으로 둔다")
        return dict(NULL_EMPTY)
    if not r:
        return dict(NULL_EMPTY)
    return {"caught_null_mean": r["mean"],
            "caught_null_p95": r["p95"],
            "p_value": r["p_value_one_sided"],
            "null_draws": r["draws"],
            "null_degenerate": int(r["degenerate"])}


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
    # 2026-09-05 교정 — backtest.scorecard 와 «같은» 결함을 공유하고 있었다.
    # 탐색 창이 사후 확정 저점 t 까지 열려 있어 폭락 뒤 경보도 caught 로 셌고,
    # 그래서 격자 비교의 caught 열 «전체»가 같은 편향을 안고 있었다.
    # 이제 고점 이전만 caught 로 세고, v1 정의는 caught_v1 로 남겨 나란히 본다.
    # getattr — backtest 가 아직 구판일 때도 죽지 않게 한다(분할 머지 대비).
    lead = getattr(bt, "LEAD_WINDOW", 120)
    caught = 0; caught_v1 = 0
    for ev in events:
        p, t = ev["peak_i"], ev["trough_i"]
        lo = max(0, p - lead)
        if any(states[j] >= min_state for j in range(lo, p+1)):
            caught += 1
        if any(states[j] >= min_state for j in range(lo, t+1)):
            caught_v1 += 1
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
    # 우연 기준선 — caught 와 «같은» 채점 함수(backtest.caught_count)를 쓰므로
    # 두 열을 직접 빼서 간격으로 읽을 수 있다.
    return {"caught": f"{caught}/{len(events)}",
            "caught_v1": f"{caught_v1}/{len(events)}",
            **null_cols(states, events, min_state),
            "lead_window": lead,
            "panel_sessions": n, "panel_span": f"{panel[0]['date']}..{panel[-1]['date']}",
            "episodes": len(eps), "hits": hits,
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
    print(f"우연 기준선 회전 수 {getattr(bt, 'NULL_SHIFTS', '?')} "
          f"· 사전 창 {getattr(bt, 'LEAD_WINDOW', '?')} 세션")

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
    # MECH 는 기계 입력만 쓴다(l2m = V-KOSPI). 손으로 매긴 EVIDENCE 표를 받지
    # 않으므로 이 격자의 p 값은 «정렬»의 증거로 읽을 수 있다(미결 I-13 의 누출 없음).
    rows = []
    def run_l2(label, fn):
        p2 = clone(panel)
        for i, r in enumerate(p2):
            r["l2m"] = fn(i)
        states, _ = bt.run_machine(p2, "MECH")
        row = {"rule": label, **summarize(p2, states, events)}
        rows.append(row)
        print(f"  {label}: 실제 {row['caught']} · 우연평균 {row['caught_null_mean']} "
              f"· p {row['p_value']} · 경보 {row['days_pct']}%")
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
        # ⚠️ caught_v1 을 반드시 함께 싣는다. 교정 후 storm_caught 가 전 후보에서
        #    0 으로 눌리면 이 격자의 «순위 열»이 상수가 되어 비교가 죽는다 —
        #    실제로 합성 패널에서 6개 후보가 전부 8/13 → 0/13 이 됐다.
        # ⚠️ 이 격자는 FULL 변형이라 l2e/l3e 가 손으로 매긴 EVIDENCE 표에서 온다.
        #    그래서 아래 p 값은 «예측력»의 근거가 아니다 — 후보 사이의 상대 비교에만
        #    쓴다(미결 I-13). 열 이름에 그 사실을 담을 수 없으므로 여기 적어 둔다.
        rows.append({"rule": label, "storm_caught": s["caught"],
                     "storm_caught_v1": s["caught_v1"],
                     "storm_null_mean": s["caught_null_mean"],
                     "storm_null_p95": s["caught_null_p95"],
                     "storm_p_value": s["p_value"],
                     "storm_null_degenerate": s["null_degenerate"],
                     "panel_sessions": s["panel_sessions"],
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
