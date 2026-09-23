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

[2026-09-24 v1.2 — 백지 저장 차단 (저장소 수정 목록 G-1 · 미결 C-41)]
  종전에는 SEC 조회가 전부 실패해도 머리글만 있는 CSV를 results/latest 와
  results/history/<날짜>/ 에 덮어쓰고 종료코드 0으로 끝났다(urlopen 차단으로 재현됨).
  (b) 밀도 가드는 경고만 찍었고, 그마저 CSV를 쓴 «뒤»에 돌았다.
  이제 결과 파일을 쓰기 «전»에 판정한다. 아래 중 하나라도 걸리면 어떤 결과 파일도
  건드리지 않고(직전 정상 결과·스냅샷 보존) ALARM_PATH 에 사유를 남긴 뒤 종료코드 1로 끝난다.
    ① 필수 입력(매출 태그 3종·영업이익 — 이번·전년 분기)의 조회가 재시도까지 모두(4회) 실패했다
    ② 결과가 비었다(유니버스 0 또는 그물 0)
    ③ (b) 밀도 가드 경고(같은 목표 분기인데 매출·영업이익 엔티티 수가 직전의 50% 미만)
  순이익(NI)만 실패하면 종전대로 결과를 쓰고 snapshot.json 의 fetch_failures 에 남긴다
  (순이익은 표시용 열이며 그물 판정에 쓰지 않는다).
  (b) 의 «하드 중단이 아니라 기록» 판단은 여기서 바뀐다 — 그 이유였던 «중단하면 다운스트림이
  그 사실을 모른다»는 위험은 빨간불(종료코드 1)과 알람 파일이 대신 알린다. 직전 정상 결과가
  남으므로 snapshot.json 의 run_date 가 갱신되지 않은 것으로도 알 수 있다.
  결과 파일은 세 개 모두 임시 파일에 먼저 쓴 뒤 연달아 교체하고, history 는 새 사본을 다 만든 뒤
  옛 사본과 바꾼다. 시작할 때 지난 실행이 남긴 임시 파일·임시 폴더를 치우고, 교체 도중 끊겨
  옛 history 사본만 남아 있으면 그것을 되돌려 놓는다.
  밀도 비교 대상에 유니버스·그물 수와 매출 태그별 커버리지(이번·전년 분기)를 더했다 — 한 태그가
  HTTP 200 빈 응답으로 통째로 빠져도 합계는 50% 선을 넘길 수 있기 때문이다(독립 검토 F1).
  조회 재시도는 3회·5초 간격에서 4회·5/20/60초 간격으로 늘렸다 — 필수 입력 실패가 이제 그 주를
  통째로 막으므로 잠깐의 장애로 막히지 않게 한다(독립 검토 F6).
  차단 사유는 `::error::` 줄로도 찍어 Actions 실행 요약에 드러나게 한다(독립 검토 F3).
  같은 분기 안에서 기업 수가 «실제로» 줄어든 경우(예: SEC 의 프레임 산출 방식 변경)에는 매주
  차단된다 — 그때는 사람이 results/latest/snapshot.json 을 지우고 수동 실행하면 비교 기준이
  새로 잡힌다(독립 검토 F7 — 자동 우회 장치는 두지 않았다).
  ⚠️ 현재 워크플로(us-screen.yml)는 이 단계가 실패하면 커밋 단계를 건너뛰므로 알람 파일은
     러너에만 남고 저장소에는 올라가지 않는다(실행 로그·빨간불·`::error::` 요약으로 드러난다).
     알람 파일까지 저장소에 남기려고 워크플로를 바꿀 때는, 실패한 실행에서는
     results/_meta/screen_run_alarm.txt «하나만» 커밋하게 한다 — `git add results/` 를 그대로 두면
     예외로 끊긴 실행이 남긴 반쯤 바뀐 파일까지 올라간다(독립 검토 2차 M2).

GitHub Actions에서 주간 실행된다. 표준 라이브러리만 사용.
출력: results/latest/*.csv + results/history/<날짜>/ (PIT 스냅샷)
"""
import json, os, csv, datetime, urllib.request, time, shutil, sys, traceback

UA = {"User-Agent": "LeePersonalResearch daybreakz@daum.net"}
BASE = "https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{y}Q{q}.json"
FETCH_FAILURES = []      # 재시도까지 모두 실패한 태그·분기 — snapshot.json에 기록. v1.2부터 필수 입력(매출·영업이익)이면 쓰기 전에 차단
DURATION_FILTERED = []   # (a) duration 필터로 제외된 항목 수 — 태그·분기별 기록
DENSITY_MIN_RATIO = 0.5  # (b) 프레임 밀도 가드 참조 파라미터 — 조정 근거는 분기 사후 검증만
ALARM_PATH = "results/_meta/screen_run_alarm.txt"  # v1.2 — 차단·예외 사유. 정상 종료 시 지운다
RETRY_WAITS = (5, 20, 60)  # v1.2 — 조회 재시도 사이 대기(초). 4회 시도
WRITE_STATE = []           # v1.2 — 이번 실행이 교체를 마친 결과 파일(예외 알람 문구용)

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
    for attempt in range(len(RETRY_WAITS) + 1):
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
            if attempt < len(RETRY_WAITS):
                time.sleep(RETRY_WAITS[attempt])
    print(f"  실패({len(RETRY_WAITS) + 1}회): {tag} CY{y}Q{q} — 필수 입력이면 쓰기 전에 차단, 순이익이면 snapshot 에 기록")
    FETCH_FAILURES.append(f"{tag}@CY{y}Q{q}")
    return {}

def load_prior_snapshot():
    """(b) 프레임 밀도 가드 — 이번 실행이 덮어쓰기 전에, 직전 실행의 snapshot.json을
    미리 읽어 비교 기준으로 삼는다. 없으면(첫 실행) None."""
    try:
        with open("results/latest/snapshot.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        # v1.2 — 깨진 스냅샷은 비교 기준이 없는 것과 같다. 조용히 넘기지 않고 경고로 드러낸다
        print(f"::warning::us-screen 직전 snapshot.json 을 읽지 못해 밀도 비교를 건너뛴다: {e}")
        return None

def density_check(prior, quarter_str, counts, cov_c, cov_p):
    """(b) 직전 실행이 같은 목표 분기였는데 엔티티 수가 DENSITY_MIN_RATIO 미만으로
    급감했으면 경고를 남긴다. 분기가 바뀐 전환 주는 구조적으로 희소해지므로
    (pick_quarter의 한계 설명 참조) 비교에서 제외한다.
    v1.2 — 순이익을 뺀 항목의 급감은 blocking_detail 에도 담기고, main() 이 그것으로
    쓰기를 막는다(순이익은 표시용 열이라 기록만 한다)."""
    if not prior or prior.get("quarter") != quarter_str:
        return dict(checked=False, alert=False, blocking_detail=[],
                     reason="직전 스냅샷 없음 또는 목표 분기 전환 — 비교 제외")
    pairs = [("revenue", counts["n_rev_merged"], prior.get("n_rev_merged")),
             ("op_income", counts["n_op"], prior.get("n_op")),
             ("net_income", counts["n_ni"], prior.get("n_ni")),
             ("universe", counts["n_universe"], prior.get("n_universe")),
             ("pool", counts["n_pool"], prior.get("n_pool"))]
    for tag in REV_TAGS:
        pairs.append((f"rev_cur:{tag}", cov_c.get(tag, 0), (prior.get("coverage_rev_cur") or {}).get(tag)))
        pairs.append((f"rev_prior:{tag}", cov_p.get(tag, 0), (prior.get("coverage_rev_prior") or {}).get(tag)))
    detail, blocking = [], []
    for label, cur, prev in pairs:
        if prev and cur < DENSITY_MIN_RATIO * prev:
            msg = f"{label}: {cur} < {DENSITY_MIN_RATIO:.0%} of prior {prev}"
            detail.append(msg)
            if label != "net_income":
                blocking.append(msg)
    return dict(checked=True, alert=bool(detail), prior_run_date=prior.get("run_date"),
                detail=detail, blocking_detail=blocking)

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

def write_alarm(lines, header="us-screen 차단 — 결과 파일을 쓰지 않았다(직전 정상 결과 보존)"):
    """v1.2 — 차단·예외 사유를 알람 파일에 남긴다(krx_fetch.py 의 run_alarm.txt 와 같은 형태)."""
    os.makedirs(os.path.dirname(ALARM_PATH), exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(ALARM_PATH, "w", encoding="utf-8") as f:
        f.write(stamp + "  " + header + "\n")
        for line in lines:
            f.write("  · " + line + "\n")
    print(f"ALARM {ALARM_PATH} 기록 — 이 실행은 실패로 끝난다")

def clear_alarm():
    if os.path.exists(ALARM_PATH):
        os.remove(ALARM_PATH)
        print(f"ALARM 해제 — {ALARM_PATH} 삭제")

def stage_csv(path, header, body):
    """v1.2 — 임시 파일(<path>.tmp)에만 쓴다. 교체는 commit_staged() 가 한꺼번에 한다."""
    with open(path + ".tmp", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(body)

def stage_json(path, obj):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

def commit_staged(paths):
    """세 임시 파일을 다 쓴 «뒤»에 연달아 교체한다 — 새 CSV 와 옛 CSV·스냅샷이 섞일 틈을 줄인다."""
    for path in paths:
        os.replace(path + ".tmp", path)
        WRITE_STATE.append(path)

def swap_history(hist):
    """새 history 사본을 다 만든 뒤 옛 사본과 바꾼다. 옛 사본은 .old 로 비켜 두었다가 마지막에 지운다."""
    tmp_hist, old_hist = hist + ".tmp", hist + ".old"
    if os.path.isdir(tmp_hist):
        shutil.rmtree(tmp_hist)
    shutil.copytree("results/latest", tmp_hist)
    if os.path.isdir(hist):
        if os.path.isdir(old_hist):
            shutil.rmtree(old_hist)
        WRITE_STATE.append(f"{hist} → {old_hist} 로 비켜 둠")
        os.replace(hist, old_hist)
    os.replace(tmp_hist, hist)
    WRITE_STATE.append(hist)
    if os.path.isdir(old_hist):
        try:
            shutil.rmtree(old_hist)
        except Exception as e:   # 교체는 끝났다 — 뒷정리 실패로 정상 실행을 버리지 않는다
            print(f"::warning::us-screen 옛 history 사본 정리 실패(다음 실행이 다시 치운다): {e}")

def cleanup_leftovers():
    """v1.2 — 지난 실행이 교체 도중 끊겨 남긴 것을 치운다. history 의 .old 만 남았으면 되돌린다.
    뒷정리이므로 실패해도 실행을 멈추지 않는다(경고만 남긴다)."""
    try:
        _cleanup_leftovers()
    except Exception as e:
        print(f"::warning::us-screen 지난 실행의 임시 파일 정리 실패: {e}")

def _cleanup_leftovers():
    if os.path.isdir("results/latest"):
        for name in os.listdir("results/latest"):
            if name.endswith(".tmp"):
                os.remove(os.path.join("results/latest", name))
                print(f"  정리: results/latest/{name}")
    if os.path.isdir("results/history"):
        for name in sorted(os.listdir("results/history")):
            full = os.path.join("results/history", name)
            if name.endswith(".tmp") and os.path.isdir(full):
                shutil.rmtree(full)
                print(f"  정리: {full}")
            elif name.endswith(".old") and os.path.isdir(full):
                base = full[:-4]
                if os.path.isdir(base):
                    shutil.rmtree(full)
                    print(f"  정리: {full}")
                else:
                    os.replace(full, base)
                    print(f"  복구: {full} → {base} (교체 도중 끊긴 사본을 되돌림)")

def block_reasons(rows, pool, density):
    """v1.2 — 결과 파일을 쓰기 «전»의 판정. 빈 목록이면 써도 된다."""
    required = set(REV_TAGS) | {OP_TAG}
    req_fail = [x for x in FETCH_FAILURES if x.split("@")[0] in required]
    reasons = []
    if req_fail:
        reasons.append(f"필수 입력 조회 실패 {len(req_fail)}건: {', '.join(req_fail)}")
    if not rows or not pool:
        reasons.append(f"결과가 비었다 — 유니버스 {len(rows)} / 그물 {len(pool)}")
    # 순이익은 표시용 열이라 그 급감만으로는 막지 않는다(경고는 snapshot.json 에 남는다)
    hard = density.get("blocking_detail", [])
    if hard:
        reasons.append(f"밀도 가드 경고 — 직전 실행({density.get('prior_run_date')}) 대비 급감: {hard}")
    return reasons

def main():
    today = datetime.date.today()
    y, q = pick_quarter(today)
    py, pq = y - 1, q          # 전년 동분기 (YoY)
    print(f"기준 분기: CY{y}Q{q} / 전년 동분기: CY{py}Q{pq}")

    cleanup_leftovers()                  # v1.2 — 지난 실행이 남긴 임시 파일·폴더
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

    # v1.2 — 쓰기 «전»에 판정한다. 걸리면 결과 파일을 하나도 건드리지 않는다.
    density = density_check(prior_snap, f"CY{y}Q{q}",
                            dict(n_rev_merged=len(rev_c), n_op=len(op_c), n_ni=len(ni_c),
                                 n_universe=len(rows), n_pool=len(pool)), cov_c, cov_p)
    if density["alert"]:
        print(f"⚠ (b) 밀도 가드 경고 — 직전 실행({density.get('prior_run_date')}) 대비 급감: {density['detail']}")
    reasons = block_reasons(rows, pool, density)
    if reasons:
        for r in reasons:
            print(f"✖ 차단: {r}")
            print(f"::error::us-screen 차단 — {r}")
        write_alarm(reasons + [f"목표 분기 CY{y}Q{q} · 실행일 {today}",
                               f"fetch_failures: {FETCH_FAILURES}"])
        sys.exit(1)
    if FETCH_FAILURES:
        print(f"⚠ 선택 입력(순이익) 조회 실패 — 결과는 쓰되 snapshot.json 에 남긴다: {FETCH_FAILURES}")

    os.makedirs("results/latest", exist_ok=True)
    stage_csv("results/latest/screen_top.csv",
                     ["rank", "cik", "name", "tags", "score",
                      "rev_cur_M", "rev_yoy_pct", "op_cur_M", "op_prev_M", "margin_delta_pp"],
                     [[i, p["cik"], p["name"], "+".join(p["tags"]), round(p["score"], 3),
                       round(p["rc"] / M, 1), round(p["yoy"] * 100, 1),
                       round(p["oc"] / M, 1), round(p["op"] / M, 1), round(p["dm"] * 100, 1)]
                      for i, p in enumerate(pool[:50], 1)])
    stage_csv("results/latest/pool_all.csv",
                     ["cik", "name", "tags", "score", "rev_cur", "rev_prev",
                      "op_cur", "op_prev", "ni_cur", "ni_prev"],
                     [[p["cik"], p["name"], "+".join(p["tags"]), round(p["score"], 3),
                       p["rc"], p["rp"], p["oc"], p["op"], p["nc"], p["np"]]
                      for p in pool])

    meta = dict(run_date=str(today), quarter=f"CY{y}Q{q}", prior=f"CY{py}Q{pq}",
                fetch_failures=FETCH_FAILURES,   # v1.2부터 필수 입력 실패는 쓰기 전에 차단되므로 여기 남는 것은 순이익 실패뿐이다
                duration_filtered=DURATION_FILTERED,  # (a) 제외 건수 — 태그·분기별
                density_guard=density,                 # (b) 밀도 가드 결과
                coverage_rev_cur=cov_c, coverage_rev_prior=cov_p,
                n_rev_merged=len(rev_c), n_op=len(op_c), n_ni=len(ni_c),
                n_universe=len(rows), n_pool=len(pool),
                params=dict(min_rev="20M", min_op="3M", s1="yoy>=30% & opg>=40%",
                            s2="op turn, op>=3M", s3="dm>=7pp"))
    stage_json("results/latest/snapshot.json", meta)
    commit_staged(["results/latest/screen_top.csv", "results/latest/pool_all.csv",
                   "results/latest/snapshot.json"])

    hist = f"results/history/{today}"
    swap_history(hist)
    try:
        clear_alarm()
    except Exception as e:
        print(f"::warning::us-screen 지난 알람 파일 삭제 실패: {e}")
    alert_note = " ⚠ 밀도 경고 있음" if density["alert"] else ""
    print(f"완료: 유니버스 {len(rows)} / 그물 {len(pool)} / 상위 50 저장. PIT: {hist}{alert_note}")

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # v1.2 — 어떤 예외로 죽어도 사유를 남긴다. 교체를 마친 파일이 있었는지도 적는다.
        try:
            done = ", ".join(WRITE_STATE) if WRITE_STATE else "없음 — 직전 정상 결과 보존"
            write_alarm(["스크립트가 예외로 중단됐다", f"교체를 마친 결과: {done}",
                         traceback.format_exc()],
                        header="us-screen 예외 중단")
        except Exception:
            pass
        cleanup_leftovers()   # 비켜 둔 history 사본을 되돌리고 임시 파일을 치운다(최선 노력)
        raise
