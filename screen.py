# -*- coding: utf-8 -*-
"""
미국 상향식 스크린 — SEC XBRL frames 전수 스크리닝 (아키텍처 v2 §5-1 규격)

적용 규칙의 정확한 범위 (2026-08-28 감사 정정 — 과대 주장 금지):
  · 적용: 규정 §2 규칙①(매출 태그 폴백 합집합)·②(빈 응답 감지)·④(Q4 frame 금지)
  · 미적용: 규칙⑤(form 화이트리스트)·§3 filed 기반 PIT 필터 — frames 응답에는
    form·filed 필드가 없어 구조적으로 적용 불가. PIT는 대신 results/history/의
    실행 시점 커밋 스냅샷으로 확보된다 (라이브 운용에 유효 — 단 소급 재실행 시
    재무 재작성(restatement) 누출은 막지 못함. 규정 §5 한계 참조).

[2026-08-28 v1.1 — 가드 2건 신설 (Monitor v1.7·전 프레임워크 재검토 상정 안건 ③)]
  (a) duration 검증 — frames 항목의 (end-start)를 85~95일로 강제. 비역년 결산사
      등 비표준 회계기간이 프레임에 섞여 들어오는 사례를 걸러낸다.
  (b) 프레임 밀도 가드 — 직전 실행이 동일 목표 분기였는데 엔티티 수가 50% 미만으로
      급감하면 snapshot.json에 경고를 남긴다(하드 중단은 아님 — 기존 FETCH_FAILURES와
      동일한 fail-open 감시 패턴. 이유: 실행을 완전히 중단하면 results/latest가
      갱신되지 않아 다운스트림 Monitor 절차가 그 사실 자체를 모를 위험이 있다).
      분기 전환 주(예: 5·8·11월 초)는 구조적으로 희소해지므로 비교 대상에서 제외한다.

GitHub Actions에서 주간 실행된다. 표준 라이브러리만 사용.
출력: results/latest/*.csv + results/history/<날짜>/ (PIT 스냅샷)
"""
import json, os, csv, datetime, urllib.request, time, shutil

UA = {"User-Agent": "LeePersonalResearch daybreakz@daum.net"}
BASE = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{y}Q{q}.json"
FETCH_FAILURES = []      # 3회 실패한 태그·분기 — snapshot.json에 기록 (fail-open 감시)
DURATION_FILTERED = []   # (b) duration 필터로 제외된 항목 수 — 태그·분기별 기록
DENSITY_MIN_RATIO = 0.5  # (a) 프레임 밀도 가드 참조 파라미터 — 조정 근거는 분기 사후 검증만

# 규칙 ① 매출 태그 폴백 체인 (합집합, 앞선 태그 우선) — 규정 §2와 일치 (2026-08-28 정정:
# SalesRevenueNet(2018년 이후 사실상 폐기된 레거시)을 규정 체인의 IncludingAssessedTax로 교체)
REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax"]
OP_TAG = "OperatingIncomeLoss"
NI_TAG = "NetIncomeLoss"

def pick_quarter(today):
    """실행일 기준 최신 관측 가능 분기. 규칙 ④: Q4 frame 절대 금지.
    한계(2026-08-28 명기): 5·8·11월 초 실행은 직전 분기 10-Q 마감(분기말+40~45일)
    이전이라 커버리지 불완전. 1~4월은 전년 Q3 사용 — Q4 변곡 기업은 최대 7개월
    늦게 관측된다 (연간 CY 보조 스크린은 미구현)."""
    y, m = today.year, today.month
    if m in (1, 2, 3, 4):   return y - 1, 3   # 전년 Q3 (Q4는 금지, 연간은 별도)
    if m in (5, 6, 7):      return y, 1
    if m in (8, 9, 10):     return y, 2
    return y, 3                                # 11~12월

def valid_duration(d):
    """(a) duration 검증 — 분기 항목은 (end-start)가 85~95일이어야 한다
    (규정 §2 규칙4와 동일 사상 — Q4 frame 금지가 못 거르는, 프레임 내부에 섞여
    들어오는 비표준 기간 항목을 걸러낸다). start/end가 없거나 파싱 불가하면
    보수적으로 제외한다."""
    try:
        start = datetime.date.fromisoformat(d["start"])
        end = datetime.date.fromisoformat(d["end"])
        return 85 <= (end - start).days <= 95
    except (KeyError, ValueError, TypeError):
        return False

def fetch(tag, y, q):
    url = BASE.format(tag=tag, y=y, q=q)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            raw = data.get("data", [])
            # 규칙 ② 빈 응답 감지: HTTP 200이어도 data가 비면 '없음'
            # (a) duration 검증: 85~95일 밖의 항목은 조용히 섞이지 않고 제외된다
            valid = [d for d in raw if valid_duration(d)]
            dropped = len(raw) - len(valid)
            pts = {int(d["cik"]): (d["val"], d.get("entityName", "")) for d in valid}
            print(f"  {tag} CY{y}Q{q}: {len(pts)}社" + (f" (duration 제외 {dropped}건)" if dropped else ""))
            if dropped:
                DURATION_FILTERED.append(f"{tag}@CY{y}Q{q}:{dropped}")
            return pts
        except Exception as e:
            print(f"  재시도 {attempt+1}: {tag} CY{y}Q{q}: {e}")
            time.sleep(5)
    print(f"  실패(3회): {tag} CY{y}Q{q} — 빈 결과로 처리 (snapshot에 기록)")
    FETCH_FAILURES.append(f"{tag}@CY{y}Q{q}")
    return {}

def load_prior_snapshot():
    """(b) 프레임 밀도 가드 — 이번 실행이 덮어쓰기 전에, 직전 실행의 snapshot.json을
    미리 읽어 비교 기준으로 삼는다. 없으면(첫 실행) None."""
    try:
        with open("results/latest/snapshot.json") as f:
            return json.load(f)
    except Exception:
        return None

def density_check(prior, quarter_str, n_rev, n_op, n_ni):
    """(b) 직전 실행이 같은 목표 분기였는데 엔티티 수가 DENSITY_MIN_RATIO 미만으로
    급감했으면 경고를 남긴다. 분기가 바뀐 전환 주는 구조적으로 희소해지므로
    (pick_quarter의 한계 설명 참조) 비교에서 제외한다 — 하드 중단이 아니라 기록."""
    if not prior or prior.get("quarter") != quarter_str:
        return dict(checked=False, alert=False,
                     reason="직전 스냅샷 없음 또는 목표 분기 전환 — 비교 제외")
    detail = []
    for label, cur, key in (("revenue", n_rev, "n_rev_merged"),
                             ("op_income", n_op, "n_op"),
                             ("net_income", n_ni, "n_ni")):
        prev = prior.get(key)
        if prev and cur < DENSITY_MIN_RATIO * prev:
            detail.append(f"{label}: {cur} < {DENSITY_MIN_RATIO:.0%} of prior {prev}")
    return dict(checked=True, alert=bool(detail), prior_run_date=prior.get("run_date"), detail=detail)

def merged_revenue(y, q):
    """규칙 ①·③: 태그 합집합 — CIK별로 체인 앞선 태그 우선."""
    merged, per_tag = {}, {}
    for tag in REV_TAGS:
        pts = fetch(tag, y, q)
        per_tag[tag] = len(pts)
        for cik, v in pts.items():
            merged.setdefault(cik, v)
        time.sleep(1)
    return merged, per_tag

def main():
    today = datetime.date.today()
    y, q = pick_quarter(today)
    py, pq = y - 1, q          # 전년 동분기 (YoY)
    print(f"기준 분기: CY{y}Q{q} / 전년 동분기: CY{py}Q{pq}")

    prior_snap = load_prior_snapshot()   # (b) 덮어쓰기 전에 먼저 읽어 둔다

    rev_c, cov_c = merged_revenue(y, q)
    rev_p, cov_p = merged_revenue(py, pq)
    op_c = fetch(OP_TAG, y, q);  time.sleep(1)
    op_p = fetch(OP_TAG, py, pq); time.sleep(1)
    ni_c = fetch(NI_TAG, y, q);  time.sleep(1)
    ni_p = fetch(NI_TAG, py, pq)

    M = 1_000_000
    rows = []
    for cik in rev_c:
        if cik not in rev_p or cik not in op_c or cik not in op_p:
            continue
        rc, name = rev_c[cik]; rp = rev_p[cik][0]
        oc = op_c[cik][0]; op_ = op_p[cik][0]
        nc = ni_c.get(cik, (None,))[0]; np_ = ni_p.get(cik, (None,))[0]
        if rp <= 0 or rc < 20 * M:      # 유니버스 가드: 분기 매출 $20M 이상
            continue
        yoy = (rc - rp) / rp
        mc, mp = oc / rc, op_ / rp
        dm, imp = mc - mp, (oc - op_) / rc
        tags = []
        # 참조 파라미터 (KD-0-1 동형) — 조정 근거는 분기 사후 검증만
        if op_ > 0 and oc >= 3 * M and yoy >= 0.30 and (oc - op_) / op_ >= 0.40:
            tags.append("S1")
        if op_ < 0 and oc >= 3 * M:
            tags.append("S2")
        if op_ > 0 and oc >= 3 * M and dm >= 0.07 and yoy >= -0.10:
            tags.append("S3")
        rows.append(dict(cik=cik, name=name, tags=tags, rc=rc, rp=rp, oc=oc,
                         op=op_, nc=nc, np=np_, yoy=yoy, dm=dm, imp=imp))

    pool = [r for r in rows if r["tags"]]
    def pct(key):
        vals = sorted(p[key] for p in pool)
        n = max(len(vals) - 1, 1)
        return {id(p): vals.index(p[key]) / n for p in pool}
    py_, pi_, pd_ = pct("yoy"), pct("imp"), pct("dm")
    for p in pool:
        p["score"] = (0.40 * py_[id(p)] + 0.35 * pi_[id(p)] + 0.25 * pd_[id(p)]
                      + (0.05 if len(p["tags"]) >= 2 else 0))
    pool.sort(key=lambda p: -p["score"])

    os.makedirs("results/latest", exist_ok=True)
    with open("results/latest/screen_top.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "cik", "name", "tags", "score",
                    "rev_cur_M", "rev_yoy_pct", "op_cur_M", "op_prev_M", "margin_delta_pp"])
        for i, p in enumerate(pool[:50], 1):
            w.writerow([i, p["cik"], p["name"], "+".join(p["tags"]), round(p["score"], 3),
                        round(p["rc"] / M, 1), round(p["yoy"] * 100, 1),
                        round(p["oc"] / M, 1), round(p["op"] / M, 1), round(p["dm"] * 100, 1)])
    with open("results/latest/pool_all.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cik", "name", "tags", "score", "rev_cur", "rev_prev",
                    "op_cur", "op_prev", "ni_cur", "ni_prev"])
        for p in pool:
            w.writerow([p["cik"], p["name"], "+".join(p["tags"]), round(p["score"], 3),
                        p["rc"], p["rp"], p["oc"], p["op"], p["nc"], p["np"]])
    density = density_check(prior_snap, f"CY{y}Q{q}", len(rev_c), len(op_c), len(ni_c))
    if density["alert"]:
        print(f"⚠ (b) 밀도 가드 경고 — 직전 실행({density.get('prior_run_date')}) 대비 급감: {density['detail']}")

    meta = dict(run_date=str(today), quarter=f"CY{y}Q{q}", prior=f"CY{py}Q{pq}",
                fetch_failures=FETCH_FAILURES,   # 비어 있지 않으면 이번 회차 결과 불신
                duration_filtered=DURATION_FILTERED,  # (a) 제외 건수 — 태그·분기별
                density_guard=density,                 # (b) 밀도 가드 결과
                coverage_rev_cur=cov_c, coverage_rev_prior=cov_p,
                n_rev_merged=len(rev_c), n_op=len(op_c), n_ni=len(ni_c),
                n_universe=len(rows), n_pool=len(pool),
                params=dict(min_rev="20M", min_op="3M", s1="yoy>=30% & opg>=40%",
                            s2="op turn, op>=3M", s3="dm>=7pp"))
    with open("results/latest/snapshot.json", "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    hist = f"results/history/{today}"
    if os.path.isdir(hist):
        shutil.rmtree(hist)
    shutil.copytree("results/latest", hist)
    alert_note = " ⚠ 밀도 경고 있음" if density["alert"] else ""
    print(f"완료: 유니버스 {len(rows)} / 그물 {len(pool)} / 상위 50 저장. PIT: {hist}{alert_note}")

if __name__ == "__main__":
    main()
