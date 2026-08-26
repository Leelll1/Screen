# -*- coding: utf-8 -*-
"""
미국 상향식 스크린 — SEC XBRL frames 전수 스크리닝
(아키텍처 v2 §5-1 규격 / SEC XBRL 재무 소스 규정 5대 규칙 적용)

GitHub Actions에서 주간 실행된다. 표준 라이브러리만 사용.
출력: results/latest/*.csv + results/history/<날짜>/ (PIT 스냅샷)
"""
import json, os, csv, datetime, urllib.request, time, shutil

UA = {"User-Agent": "LeePersonalResearch daybreakz@daum.net"}
BASE = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{y}Q{q}.json"

# 규칙 ① 매출 태그 폴백 체인 (합집합, 앞선 태그 우선)
REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues", "SalesRevenueNet"]
OP_TAG = "OperatingIncomeLoss"
NI_TAG = "NetIncomeLoss"

def pick_quarter(today):
    """실행일 기준 '전수 제출이 끝난' 최신 분기. 규칙 ④: Q4 frame 절대 금지."""
    y, m = today.year, today.month
    if m in (1, 2, 3, 4):   return y - 1, 3   # 전년 Q3 (Q4는 금지, 연간은 별도)
    if m in (5, 6, 7):      return y, 1
    if m in (8, 9, 10):     return y, 2
    return y, 3                                # 11~12월

def fetch(tag, y, q):
    url = BASE.format(tag=tag, y=y, q=q)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            # 규칙 ② 빈 응답 감지: HTTP 200이어도 data가 비면 '없음'
            pts = {int(d["cik"]): (d["val"], d.get("entityName", "")) for d in data.get("data", [])}
            print(f"  {tag} CY{y}Q{q}: {len(pts)}社")
            return pts
        except Exception as e:
            print(f"  재시도 {attempt+1}: {tag} CY{y}Q{q}: {e}")
            time.sleep(5)
    print(f"  실패(3회): {tag} CY{y}Q{q} — 빈 결과로 처리")
    return {}

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
    meta = dict(run_date=str(today), quarter=f"CY{y}Q{q}", prior=f"CY{py}Q{pq}",
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
    print(f"완료: 유니버스 {len(rows)} / 그물 {len(pool)} / 상위 50 저장. PIT: {hist}")

if __name__ == "__main__":
    main()
