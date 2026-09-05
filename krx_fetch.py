# -*- coding: utf-8 -*-
"""KRX Open API 일일 수집 — 조기경보(L2 지반) + 국내발굴 지원.

버전 v3.3 (2026-09-05). 변경 이력의 «전문»은 저장소 PR #1·#2 와 프로젝트 문서
「설정 — 저장소 수정 목록 (Live)」에 있다. 여기에는 이 코드를 고칠 사람이 반드시
알아야 할 «운용 사실»만 적는다 — 이유가 필요한 곳에는 해당 줄에 주석이 붙어 있다.

무엇을 하나
  · 17개 일일 서비스 + 3개 주간 명부 서비스를 받아 results/krx/latest·roster 에 원본 보존
  · 조기경보 지표를 계산해 results/krx/ews_daily.csv 에 거래일 1행씩 누적
  · 이력이 얕으면 과거 거래일을 슬롯당 몇 일씩 소급 수집(백필)해 245거래일까지 채운다
  · 호출 단위 관측을 results/_meta/source_health_YYYYMM.csv 에 누적

반드시 알아야 할 운용 사실 (전부 실측)
  ① **KRX 는 거래일 D 의 데이터를 D+1 «저녁»에 공개한다.** 하루 3슬롯 중 당일치가
     실제로 오는 것은 13:00 UTC(=22:00 KST) 하나뿐이고, 아침·낮 슬롯은 HTTP 200 +
     빈 배열을 받는다. **그 0행은 장애가 아니라 정상이다.** 그래서 0행을 「성공」으로
     세지도 않고 「실패」로 알람하지도 않는다 — 둘 다 진짜 장애를 가린다.
  ② **KRX 는 과거 basDd 를 정상적으로 돌려준다** (2026-09-05 확인, 48콜 42초, 한도
     거부 없음). 백필이 성립하는 근거다. 다만 «얼마나 오래된 것까지» 주는지는 모르며,
     BACKFILL_HORIZON_DAYS 가 그 미지를 막는다.
  ③ 러너는 매 실행 새 체크아웃이다 — **워크플로가 스테이징하지 않은 경로는 사라진다.**
     results/krx · results/fss · results/_meta 세 곳이 스테이징 대상이다.

이 스크립트가 지키는 규율 (깨뜨리지 말 것)
  · 0행이나 «비정상적으로 적은» 응답으로 기존 값·기존 JSON 을 절대 덮지 않는다.
  · 행은 교체가 아니라 병합한다 — 이번에 못 받은 항목이 지난번 값을 지우지 않는다.
  · 「실패」와 「데이터 없음」을 섞지 않는다. 실패는 알람(빨간불), 없음은 기록만.
  · 어떤 예외로 죽어도 알람과 health 기록은 남긴다 — 워크플로가 커밋 «뒤»에 판정한다.

인증: 헤더 AUTH_KEY. 값은 저장소 Secret(KRX_AUTH_KEY)에서만 읽는다 — 코드에 쓰지 않는다.
환경변수: KRX_BACKFILL_MAX_DAYS(5) · KRX_BACKFILL_TARGET(245) ·
          KRX_BACKFILL_HORIZON_DAYS(540) · KRX_SKIP_AFTER(2) · KRX_FRESH_DAYS(3) ·
          KRX_PARTIAL_MAX(8) · KRX_PARTIAL_COOLDOWN_DAYS(7) · KRX_MUTE_SERVICES("")
"""
import os, json, csv, datetime, time, urllib.request, urllib.error

KEY = os.environ["KRX_AUTH_KEY"]
BASE = "https://data-dbg.krx.co.kr/svc/apis/"  # 공식 명세서 기준 https (2026-08-27)

# ── 일일 수집 서비스 (매 실행) ─────────────────────────────────────────────
DAILY = [
    # 주식
    ("sto/stk_bydd_trd",   "유가증권 일별매매"),
    ("sto/ksq_bydd_trd",   "코스닥 일별매매"),
    ("sto/knx_bydd_trd",   "코넥스 일별매매"),
    # 지수
    ("idx/krx_dd_trd",     "KRX 지수"),
    ("idx/kospi_dd_trd",   "KOSPI 지수"),
    ("idx/kosdaq_dd_trd",  "KOSDAQ 지수"),
    ("idx/bon_dd_trd",     "채권지수"),
    ("idx/drvprod_dd_trd", "파생상품지수(V-KOSPI 포함)"),
    # 증권상품
    ("etp/etf_bydd_trd",   "ETF 일별매매"),
    ("etp/etn_bydd_trd",   "ETN 일별매매"),
    # 채권
    ("bon/kts_bydd_trd",   "국채전문유통시장"),
    ("bon/bnd_bydd_trd",   "일반채권시장"),
    # 파생상품
    ("drv/fut_bydd_trd",   "선물(주식선물外)"),
    ("drv/opt_bydd_trd",   "옵션(주식옵션外)"),
    # 일반상품
    ("gen/gold_bydd_trd",  "금시장"),
    ("gen/oil_bydd_trd",   "석유시장"),
    ("gen/ets_bydd_trd",   "배출권시장"),
]

# ── 주간 수집 (금요일 실행 또는 수동 실행 시) — 국내발굴 명부의 정본 ──────
WEEKLY = [
    ("sto/stk_isu_base_info", "유가증권 종목기본정보"),
    ("sto/ksq_isu_base_info", "코스닥 종목기본정보"),
    ("sto/knx_isu_base_info", "코넥스 종목기본정보"),
]

SERVICE_LABEL = {p: l for p, l in DAILY + WEEKLY}


def biz_day():
    # KST 기준 '어제'(직전 완료 거래일) — 러너는 UTC이므로 +9h 보정 후 하루 전.
    # ⚠️ v3.2 교정 — v2 주석은 「22:20 UTC 실행이 당일 KST 종가를 정확히 잡는다」고
    #    적었으나 사실이 아니다. 그 시각(=익일 07:20 KST)에 KRX 는 아직 «공개 전»이라
    #    0행이 온다. 이 함수가 고르는 «날짜»는 맞고, 그 날짜의 데이터가 그 «시각»에
    #    있느냐는 별개다. 실제로 데이터가 오는 슬롯은 13:00 UTC(=22:00 KST) 하나다.
    kst = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(hours=9)).date()
    d = kst - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")  # 한국 공휴일이면 0행 응답 — status.csv로 확인

def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ── v3: 호출 단위 건강 기록 (R-5) ─────────────────────────────────────────
# status.csv 는 매 실행 «덮어쓰기»라 어느 슬롯이 무엇을 채웠는지 사후에 못 잰다(R-9).
# 이 파일은 누적한다 — 「설정 — 외부 데이터 관리 대장 (Live)」 §3 안정성 집계의 입력.
# v3.2 (R-23): 월별로 나눈다. v3.1 은 단일 파일이었고 애초에 커밋되지도 않았다.
HEALTH_PATH = ("results/_meta/source_health_"
               + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m") + ".csv")
HEALTH_COLS = ["run_ts", "source_id", "op", "bas_dd", "http_status", "rows", "elapsed_ms", "error"]
_health_rows = []

# v3.2: 실패를 실패로 보이게 하는 파일 (R-22). 워크플로가 커밋 «뒤»에 이걸 보고 exit 1 한다.
ALARM_PATH = "results/krx/run_alarm.txt"


def write_csv_atomic(path, cols, rows):
    """임시파일에 다 쓰고 os.replace 로 바꾼다 — 중간에 죽어도 잘린 CSV 가 남지 않는다."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def health(op, basdd, status, rows, elapsed, err):
    _health_rows.append({
        "run_ts": now_utc(), "source_id": "S-12", "op": op, "bas_dd": basdd,
        "http_status": str(status), "rows": str(rows),
        "elapsed_ms": str(int(elapsed * 1000)), "error": err[:200],
    })


def flush_health():
    if not _health_rows:
        return
    os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
    # v3.3 (D7): 「없다」가 아니라 「비었다」도 헤더가 필요하다. 0바이트 파일이 남아
    # 있으면 v3.2 는 헤더 없이 데이터만 붙여 파일을 파싱 불가로 만들었다.
    need_header = (not os.path.exists(HEALTH_PATH)) or os.path.getsize(HEALTH_PATH) == 0
    with open(HEALTH_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEALTH_COLS)
        if need_header:
            w.writeheader()
        for r in _health_rows:
            w.writerow(r)
    print(f"HEALTH  {HEALTH_PATH} +{len(_health_rows)}행")
    del _health_rows[:]      # 두 번 불려도 중복 기록되지 않게 (main 의 finally 대비)


# ── v3: 백필 (R-3·R-4) ────────────────────────────────────────────────────
# 왜 필요한가 — v2 는 하루에 1행씩만 쌓아서 245영업일이 차려면 1년이 걸린다.
# 조기경보 절차서가 «정규 판정 성분»으로 지정한 V-KOSPI 백분위·레버리지 ETF 백분위가
# 그동안 계산 불가이고, 그 결과 KR L2 는 확정값이 아니라 «하한»으로만 나온다(C-21).
#
# ⚠️ 한 번에 다 받지 않는다. KRX 호출 한도가 미확인(카탈로그 미확인 항목)이므로
#    슬롯당 상한을 두고 3슬롯 × N일로 나눠 받는다. 실패해도 다음 슬롯이 이어받는다.
BACKFILL_MAX_DAYS = int(os.environ.get("KRX_BACKFILL_MAX_DAYS", "5"))
BACKFILL_TARGET   = int(os.environ.get("KRX_BACKFILL_TARGET", "245"))   # 1년 영업일
# ews 지표 계산에 실제로 쓰이는 서비스만 소급한다 — 17종을 다 받으면 호출이 3배가 된다.
BACKFILL_SERVICES = ["sto/stk_bydd_trd", "sto/ksq_bydd_trd", "idx/kospi_dd_trd",
                     "idx/kosdaq_dd_trd", "idx/drvprod_dd_trd", "etp/etf_bydd_trd",
                     "drv/opt_bydd_trd"]
# v3.3 (D4): 과거 조회 한계가 목표보다 짧을 수 있다 — 창을 무한히 뒤로 밀지 않는다.
BACKFILL_HORIZON_DAYS = int(os.environ.get("KRX_BACKFILL_HORIZON_DAYS", "540"))  # 달력일
STATE_PATH = "results/krx/backfill_state.txt"

# v3.3 (D3): 공개 «도중»에 걸린 응답을 정상으로 받아들이면 지표가 통째로 왜곡된다.
# 2026-09-05 실측 행수 — stk 942~944 · ksq 1,822~1,823 · kospi지수 51 · kosdaq지수 40 ·
# 파생지수 320 · ETF 1,161~1,164 · 옵션 17,676~18,212. 그 절반 수준을 바닥으로 둔다.
MIN_ROWS = {
    "sto/stk_bydd_trd": 400, "sto/ksq_bydd_trd": 800, "sto/knx_bydd_trd": 20,
    "idx/kospi_dd_trd": 20,  "idx/kosdaq_dd_trd": 15, "idx/drvprod_dd_trd": 100,
    "etp/etf_bydd_trd": 400, "etp/etn_bydd_trd": 50,
    "drv/opt_bydd_trd": 3000, "drv/fut_bydd_trd": 100,
    "sto/stk_isu_base_info": 400, "sto/ksq_isu_base_info": 800,
    "sto/knx_isu_base_info": 20,
}
# v3.3 (D8): 이미 아는 고장(예: 구독 만료된 서비스)은 알람에서 뺀다 — 매일 빨간불이면
# 아무도 안 본다. 쉼표로 구분한 서비스 경로. 데이터 수집 자체는 그대로 시도한다.
MUTE = {x.strip() for x in os.environ.get("KRX_MUTE_SERVICES", "").split(",") if x.strip()}

# v3.3 (D2): 「채워진 행」의 기준. 목표가 V-KOSPI 245행인데 v3.2 는 「아무 값이나 있으면
# 완료」로 봐서, 파생지수만 죽은 날들이 V-KOSPI 없이 완료로 굳었다.
# ⚠️ kospi_trdval·top2_mcap_pct 는 2026-08-28 에 신설된 항목이라 20260826 행에 없다 —
#    그래서 그 행은 «부분»으로 잡혀 한 번 더 받아 채워진다.
REQUIRED_COLS = ("kospi_close", "kosdaq_close", "vkospi_close", "adv_cnt",
                 "kospi_trdval", "top2_mcap_pct")


# ── v3.2: 휴장일 대장 (R-20) ─────────────────────────────────────────────
# 왜 필요한가 — 한국 증시 휴장일은 «영원히» 0행이다. v3.1 은 그것을 「아직 못 채운
# 거래일」과 구분하지 못해, 채움 경계 뒤로 휴장일 5개가 쌓이는 순간 매 실행이 같은
# 5개만 다시 시도하며 영구히 정지한다(시뮬레이션: 36회 실행 후 84~86행에서 멈춤).
# 그래서 「두 번 확인했고 충분히 오래된 날짜」를 휴장으로 간주해 후보에서 뺀다.
SKIP_PATH  = "results/krx/ews_skip.csv"
SKIP_COLS  = ["bas_dd", "kind", "attempts", "last_utc", "note"]
SKIP_AFTER = int(os.environ.get("KRX_SKIP_AFTER", "2"))   # 이 횟수 이상 0행이면 제외
# v3.3 (D2 후속): 「행은 있는데 필수항목이 모자란 날짜」는 «영구 제외하지 않는다».
# 한 서비스가 며칠 죽어 있으면 그 기간이 통째로 부분행이 되는데, 그걸 2회 만에
# 포기하면 결국 v3.2 와 같은 자리에 도착한다. 그래서 ① 빈 날짜를 먼저 채우고
# 남는 칸으로만 부분행을 손보고 ② 일정 시간이 지나면 다시 후보로 돌아오게 한다.
PARTIAL_MAX = int(os.environ.get("KRX_PARTIAL_MAX", "8"))
PARTIAL_COOLDOWN_DAYS = int(os.environ.get("KRX_PARTIAL_COOLDOWN_DAYS", "7"))
FRESH_DAYS = int(os.environ.get("KRX_FRESH_DAYS", "3"))   # 최근 N영업일은 등재하지 않는다


def load_ews():
    """ews_daily.csv 를 {bas_dd: row} 로 읽는다. 없으면 빈 dict."""
    path = "results/krx/ews_daily.csv"
    hist = {}
    if os.path.exists(path):
        # v3.3 (D5): utf-8-sig — 엑셀로 한 번 열었다 저장하면 BOM 이 붙고, v3.2 는
        # 그 한 글자에 KeyError 로 죽으면서 status·health·알람을 «전부» 못 남겼다.
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                key = (row.get("bas_dd") or "").strip()
                if key:
                    hist[key] = row
    return hist


def load_skip():
    skip = {}
    if os.path.exists(SKIP_PATH):
        with open(SKIP_PATH, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                key = (row.get("bas_dd") or "").strip()
                if not key:
                    continue
                try:
                    row["attempts"] = int(row.get("attempts") or 0)
                except (TypeError, ValueError):
                    row["attempts"] = 0
                row.setdefault("kind", "empty")
                skip[key] = row
    return skip


def save_skip(skip):
    write_csv_atomic(SKIP_PATH, SKIP_COLS,
                     [{c: str(skip[d].get(c, "")) for c in SKIP_COLS} for d in sorted(skip)])


def recent_biz_days(upto, n):
    """upto 를 포함해 최근 n «평일»(주말만 제외 — 공휴일은 모른다).

    v3.3 (D9) 주의 — 설·추석처럼 평일 휴장이 3일 이상 이어지는 구간에서는 이 창이
    실제 거래일을 한 개도 덮지 못한다. 이 창의 목적은 「아직 공개 전일 수 있는 최근
    날짜를 휴장으로 굳히지 않는 것」이고, 그 보호가 얇아지는 구간이 있다는 뜻이다.
    D1 수정(실패는 적립하지 않음)이 같은 위험의 큰 쪽을 이미 막는다.
    """
    out, d = [], datetime.datetime.strptime(upto, "%Y%m%d").date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= datetime.timedelta(days=1)
    return out


def note_no_progress(skip, basdd, kind, fresh, clean):
    """백필이 그 날짜를 «개선하지 못했을» 때 기록한다.

    v3.3 (D1) — `clean` 이 False 면(그 날짜 호출 중 HTTP·예외 실패가 하나라도 있으면)
    **아무것도 적립하지 않는다.** v3.2 는 「행을 못 썼다」만 봤는데, 키가 만료돼 전량
    401 이 나도 「행을 못 썼다」다. 그 결과 1.3일치 장애가 정상 거래일 9일을 휴장으로
    낙인찍고 영구히 버렸다. 실패는 알람이 다루고, 이 대장은 «정상 응답»만 센다.

    kind: "empty"   0행 — 휴장·무데이터. 245 목표 산정에서 «뺀다».
          "partial" 행은 있는데 필수 항목이 모자람. 목표 산정에는 «넣는다»(행은 있으므로).
    """
    if not clean or basdd in fresh:
        return
    r = skip.get(basdd) or {"bas_dd": basdd, "kind": kind, "attempts": 0,
                            "last_utc": "", "note": ""}
    r["kind"] = kind
    r["attempts"] = int(r.get("attempts") or 0) + 1
    r["last_utc"] = now_utc()
    r["note"] = (("휴장·무데이터 추정" if kind == "empty" else "필수항목 결측 — 소급 불가 추정")
                 if r["attempts"] >= SKIP_AFTER else "재시도 대기")
    skip[basdd] = r


def is_empty_row(row):
    """지표가 하나도 없는 행인가 — bas_dd·run_utc 말고 전부 공란이면 참.

    v3.2: `csv.DictReader` 는 짧은 행의 빠진 필드를 None 으로 채운다. v3.1 은
    `str(None)` = "None" 이 참이라 잘린 행을 「채워진 행」으로 오판했다.
    """
    return not any(str(row.get(c) or "").strip() for c in EWS_COLS
                   if c not in ("bas_dd", "run_utc"))


def is_complete_row(row):
    """소비 항목(REQUIRED_COLS)이 다 찬 행인가 — v3.3 (D2).

    v3.2 는 「아무 값이나 있으면 완료」로 봤다. 그러면 파생지수 서비스가 이틀만 죽어도
    그 기간의 행들이 V-KOSPI 없이 «완료»로 굳고, 백필은 끝났다고 선언한 채 목표보다
    12% 짧은 시계열을 남긴다. 실제로 그 시계열 위에서 1년 p90 을 계산하게 된다.
    """
    return all(str(row.get(c) or "").strip() for c in REQUIRED_COLS)


def backfill_targets(hist, upto, skip=None, exclude=()):
    """메워야 할 거래일을 최신순으로 고른다.

    세 종류를 걸러낸다 —
      ① 이미 값이 있는 날짜
      ② 휴장으로 확정된 날짜 (SKIP_AFTER 회 이상 0행 · 최근 영업일 제외)
      ③ 이번 회차가 정기 수집으로 이미 다룬 날짜(=bas). v3.1 은 이걸 후보로 뽑았다가
         main 에서 버려 5칸 중 1칸을 낭비했다(R-25 — 2026-09-05 실측에서 4일만 채워짐).

    ⚠️ 목표 산정에서 «휴장일은 세지 않는다». v3.1 은 「평일 245개」를 훑었는데,
    그 245개 안에 휴장일이 14~18개 들어 있어 245행은 «원리적으로» 도달 불가였다
    (시뮬레이션: 80회 실행 후 235행에서 정지). 이제 휴장 확정일은 카운트에서 빼고
    그만큼 창을 더 뒤로 넓힌다 — 목표는 「평일 245개」가 아니라 「거래일 245개」다.
    """
    have = {d for d, r in hist.items() if is_complete_row(r)}     # v3.3 (D2)
    skip = skip or {}
    fresh = set(recent_biz_days(upto, FRESH_DAYS))
    end = datetime.datetime.strptime(upto, "%Y%m%d").date()
    horizon = end - datetime.timedelta(days=BACKFILL_HORIZON_DAYS)  # v3.3 (D4)
    primary, secondary, d = [], [], end
    counted = 0
    while counted < BACKFILL_TARGET and d >= horizon:
        if d.weekday() < 5:
            key = d.strftime("%Y%m%d")
            s = skip.get(key) or {}
            att = int(s.get("attempts") or 0)
            kind = s.get("kind") or "empty"
            row = hist.get(key)
            has_row = bool(row) and not is_empty_row(row)
            # 휴장으로 확정된 날짜만 목표 산정에서 뺀다 — 행이 없으니 셀 수 없다.
            dead_empty = (kind == "empty" and att >= SKIP_AFTER and key not in fresh)
            if not dead_empty:
                counted += 1
            if key in have or key in exclude or dead_empty:
                d -= datetime.timedelta(days=1)
                continue
            if has_row:
                # 부분행 — 남는 칸으로만, 그리고 최근에 너무 자주 두드리지 않는다.
                if att < PARTIAL_MAX:
                    secondary.append(key)
            else:
                primary.append(key)
        d -= datetime.timedelta(days=1)
    return (primary + secondary)[:BACKFILL_MAX_DAYS]


def prune_skip(skip, upto):
    """창 밖 항목을 버리고(D10), 오래된 «부분» 항목은 재시도 자격을 돌려준다.

    부분행을 영구히 포기하면 v3.2 와 같은 자리에 도착한다 — 한 서비스가 며칠 죽어
    있던 구간이 통째로 결측인 채 「완료」로 남는다. 그래서 PARTIAL_COOLDOWN_DAYS 가
    지나면 attempts 를 0 으로 되돌려 다시 후보가 되게 한다. 비용은 몇 주에 한 번이다.
    """
    horizon = (datetime.datetime.strptime(upto, "%Y%m%d").date()
               - datetime.timedelta(days=BACKFILL_HORIZON_DAYS)).strftime("%Y%m%d")
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=PARTIAL_COOLDOWN_DAYS)).isoformat()
    out = {}
    for d, r in skip.items():
        if d < horizon:
            continue
        if (r.get("kind") == "partial" and int(r.get("attempts") or 0) >= PARTIAL_MAX
                and str(r.get("last_utc") or "") < cutoff):
            r = dict(r, attempts=0, note="냉각기 경과 — 재시도 자격 회복")
        out[d] = r
    return out

def fnum(v):
    """KRX 숫자 문자열('1,234.56', '-', '') → float 또는 None"""
    try:
        s = str(v).replace(",", "").strip()
        if s in ("", "-"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None

def fetch(path, basdd, tries=2):
    """한 서비스를 한 거래일치 받는다. (v3: 전역 BAS 대신 인자 — R-11)

    v3.3: 일시 오류는 한 번 더 시도한다. 실패 한 번이 알람을 켜고, 그 실패가 백필
    날짜에 걸리면 D1 의 오적립 경로로 이어지기 때문이다. 401·403 은 재시도해도
    달라지지 않으므로 즉시 올린다.
    """
    url = f"{BASE}{path}?basDd={basdd}"
    t0 = time.time()
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"AUTH_KEY": KEY})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404) or attempt == tries:
                raise
            print(f"     재시도 {path} {basdd}: HTTP {e.code} ({attempt}/{tries})")
            time.sleep(2 * attempt)
        except Exception as e:
            if attempt == tries:
                raise
            print(f"     재시도 {path} {basdd}: {type(e).__name__} ({attempt}/{tries})")
            time.sleep(2 * attempt)
    block = data.get("OutBlock_1")
    if not isinstance(block, list):
        block = next((v for v in data.values() if isinstance(v, list)), [])
    health(path, basdd, 200, len(block), time.time() - t0, "")
    return data, block

def compute_ews(rows):
    """수집된 원본에서 조기경보 일일 지표를 계산해 dict로 반환. 실패 항목은 빈칸."""
    g = lambda k: rows.get(k) or []
    m = {}
    # 지수 종가 — KOSPI/KOSDAQ/KOSPI200
    def idx_close(block, name):
        for r in block:
            if str(r.get("IDX_NM", "")).strip() == name:
                return (fnum(r.get("CLSPRC_IDX")), fnum(r.get("FLUC_RT")),
                        fnum(r.get("ACC_TRDVAL")))
        return None, None, None
    m["kospi_close"], m["kospi_chg_rt"], tv = idx_close(g("idx/kospi_dd_trd"), "코스피")
    if tv is not None: m["kospi_trdval"] = int(tv)   # 거래대금(원) — 사냥 조건② 관측용
    m["kospi200_close"], _, _ = idx_close(g("idx/kospi_dd_trd"), "코스피 200")
    m["kosdaq_close"], m["kosdaq_chg_rt"], tv = idx_close(g("idx/kosdaq_dd_trd"), "코스닥")
    if tv is not None: m["kosdaq_trdval"] = int(tv)
    # V-KOSPI — 파생상품지수 중 '변동성' 포함 지수
    for r in g("idx/drvprod_dd_trd"):
        if "변동성" in str(r.get("IDX_NM", "")):
            m["vkospi_close"] = fnum(r.get("CLSPRC_IDX"))
            m["vkospi_chg_rt"] = fnum(r.get("FLUC_RT"))
            break
    # 시장 폭(breadth) — 유가+코스닥 상승/하락 종목 수
    adv = dec = 0
    for blk in (g("sto/stk_bydd_trd"), g("sto/ksq_bydd_trd")):
        for r in blk:
            v = fnum(r.get("FLUC_RT"))
            if v is None:
                continue
            if v > 0: adv += 1
            elif v < 0: dec += 1
    if adv or dec:
        m["adv_cnt"], m["dec_cnt"] = adv, dec
    # 레버리지·인버스 ETF 거래대금 (원) — 과열/공포 강도
    lev = inv = inv2x = 0
    for r in g("etp/etf_bydd_trd"):
        nm = str(r.get("ISU_NM", ""))
        val = fnum(r.get("ACC_TRDVAL")) or 0
        if "레버리지" in nm: lev += val
        if "인버스" in nm:
            inv += val
            if "2X" in nm.upper(): inv2x += val
    if lev or inv:
        m["lev_etf_val"], m["inv_etf_val"], m["inv2x_etf_val"] = int(lev), int(inv), int(inv2x)
    # 쏠림도 — 유가+코스닥 상위 2종목 시가총액 비중 (조기경보 L2 관측, 2026-08-28 신설)
    caps, total = [], 0.0
    for blk in (g("sto/stk_bydd_trd"), g("sto/ksq_bydd_trd")):
        for r in blk:
            v = fnum(r.get("MKTCAP"))
            if v and v > 0:
                caps.append(v); total += v
    if total and len(caps) >= 2:
        caps.sort(reverse=True)
        m["top2_mcap_pct"] = round((caps[0] + caps[1]) / total * 100, 2)
    # 풋/콜 거래량 비율 — 옵션 일별매매에서 권리유형 합산
    put = call = 0
    for r in g("drv/opt_bydd_trd"):
        tp = str(r.get("RGHT_TP_NM", "")).upper()
        vol = fnum(r.get("ACC_TRDVOL")) or 0
        if "P" in tp or "풋" in tp: put += vol
        elif "C" in tp or "콜" in tp: call += vol
    if put or call:
        m["put_vol"], m["call_vol"] = int(put), int(call)
        m["pcr"] = round(put / call, 4) if call else None
    return m

EWS_COLS = ["bas_dd", "kospi_close", "kospi_chg_rt", "kospi200_close",
            "kosdaq_close", "kosdaq_chg_rt", "kospi_trdval", "kosdaq_trdval",
            "vkospi_close", "vkospi_chg_rt",
            "adv_cnt", "dec_cnt", "lev_etf_val", "inv_etf_val", "inv2x_etf_val",
            "top2_mcap_pct", "put_vol", "call_vol", "pcr", "run_utc"]

def append_ews(metrics, basdd, hist=None):
    """한 거래일치 지표를 upsert 한다.

    v3.2 변경 (R-24) — **교체가 아니라 병합이다.** v3.1 은 행을 통째로 갈아끼워서,
    한 서비스만 타임아웃하거나 `IDX_NM` 문자열이 어긋나면 지표 1개짜리 행이 12개짜리
    행을 덮었다. 그 행은 «비어 있지 않으므로» 백필이 다시 보지 않아 영구 동결된다.
    이제 기존 값 위에 이번 회차의 값만 덧씌운다.

    v3 변경 (R-4) — **지표가 하나도 없으면 행을 쓰지 않는다.**
    v2 는 수집 0행이어도 빈 행을 만들었고, 그 행은 이후 어떤 실행도 다시 채우지 않았다
    (`hist[BAS] = rec` 가 매번 덮어썼으므로 «있다»고 판정되어 백필 대상도 아니었다).
    2026-08-27 이 그렇게 8일 넘게 영구 결측으로 남았다. 이제 그런 날짜는 파일에
    나타나지 않고, 다음 실행의 백필 대상으로 자동 재시도된다.
    """
    path = "results/krx/ews_daily.csv"
    if hist is None:
        hist = load_ews()
    base = hist.get(basdd) or {}
    rec = {c: str(base.get(c) or "") for c in EWS_COLS}   # ← 기존 값에서 출발한다
    rec["bas_dd"] = basdd
    got = 0
    for k, v in metrics.items():
        if k in rec and v is not None:
            rec[k] = str(v)
            got += 1
    if got == 0:
        print(f"SKIP {basdd}: 지표 0개 — 행을 쓰지 않는다 (미공개이거나 휴장)")
        return hist, False
    rec["run_utc"] = now_utc()
    hist[basdd] = rec
    write_csv_atomic(path, EWS_COLS,
                     [{c: str(hist[d].get(c) or "") for c in EWS_COLS} for d in sorted(hist)])
    return hist, True


def collect_day(basdd, services, outdir=None, save_json=False):
    """한 거래일치를 받아 (raw, status행들) 로 돌려준다. 백필과 정기 수집이 함께 쓴다."""
    raw, status = {}, []
    for path in services:
        label = SERVICE_LABEL.get(path, path)
        try:
            data, block = fetch(path, basdd)
            # v3.3 (D3): 「비어 있지 않다」와 「쓸 만하다」는 다르다. 공개 도중에 걸리면
            # 900행짜리가 5행으로 온다. v3.2 는 그것을 좋은 행 위에 덮었고, 쏠림도가
            # 0.22% → 20.04% (91배)로 튀었다 — 조기경보를 오작동시킬 수 있는 값이다.
            floor = MIN_ROWS.get(path, 0)
            short = 0 < len(block) < floor
            if short:
                status.append([path, label, "SHORT", len(block), basdd])
                print(f"SHORT {path} ({label}) {basdd}: {len(block)} rows "
                      f"(최소 {floor} 미만 — 공개 도중으로 보고 쓰지 않는다)")
                continue
            # v3.2 (R-26): 받은 것을 «먼저» 확보한다. v3.1 은 JSON 쓰기가 실패하면
            # 이미 받은 행들을 통째로 버리고 ERR 로 기록했다.
            raw[path] = block
            # v3.2 (R-21): **0행이면 기존 스냅샷을 건드리지 않는다.**
            # v3.1 은 여기서 공란 판정 «앞»에 json.dump 를 해서, 하루 3슬롯 중 공개 전인
            # 2슬롯이 매일 두 번 원본을 비웠다. 2026-09-05 실측 시점에 latest/ 17개와
            # roster/ 3개가 전부 18바이트({"OutBlock_1": []})였다.
            if save_json and outdir:
                if block:
                    os.makedirs(outdir, exist_ok=True)
                    jp = f"{outdir}/{path.replace('/', '_')}.json"
                    tmp = jp + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.replace(tmp, jp)
                else:
                    print(f"      (0행 — {outdir} 기존 스냅샷 보존)")
            # v3.1 (R-18): HTTP 200 이지만 0행인 응답을 «성공»으로 세지 않는다.
            # 2026-09-05 krx-daily #27 이 20건 전부 0행이었는데 요약은 「20/20 성공」이었다.
            # KRX 는 D일 데이터를 D+1 저녁에야 공개하므로 0행은 정상적인 «아직 없음»일 수도,
            # 서비스 해지·명세 변경일 수도 있다. 둘을 구분하는 것은 이 스크립트의 일이 아니고,
            # 「받은 게 없다」를 성공 칸에서 빼는 것까지가 이 스크립트의 일이다.
            st = "OK" if block else "EMPTY"
            status.append([path, label, st, len(block), basdd])
            print(f"{'OK   ' if block else 'EMPTY'} {path} ({label}) {basdd}: {len(block)} rows")
        except urllib.error.HTTPError as e:
            status.append([path, label, f"HTTP {e.code}", 0, basdd])
            health(path, basdd, e.code, 0, 0.0, str(e)[:200])
            print(f"FAIL {path} ({label}): HTTP {e.code}"
                  + (" — 포털에서 이 서비스를 신청(또는 기간 갱신)해야 합니다"
                     if e.code in (401, 403) else ""))
        except Exception as e:
            status.append([path, label, f"ERR {type(e).__name__}", 0, basdd])
            health(path, basdd, "ERR", 0, 0.0, f"{type(e).__name__}: {e}")
            print(f"ERR  {path}: {e}")
    return raw, status

def run():
    os.makedirs("results/krx/latest", exist_ok=True)
    os.makedirs("results/krx/roster", exist_ok=True)
    bas = biz_day()

    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    # v3 (R-8): 주간 명부 판정을 **KST 기준**으로 바꿨다. v2 는 `datetime.date.today()`
    # (러너 UTC)를 썼고, biz_day() 는 KST 를 써서 두 줄이 서로 어긋나 있었다.
    # 이제 「직전 완료 거래일이 금요일이면」으로 읽는다 — 의도와 문면이 같다.
    #
    # ⚠️ v3.3 (D6) 정직하게 적는다 — v2 주석은 「토 10:00 국내발굴 직전에 명부가
    #    갱신되어 맞게 동작했다」고 했으나 사실이 아니다. 금요일 명부를 잡는 유일한
    #    토요일 이전 슬롯은 금 22:20 UTC(=토 07:20 KST)인데 그 시각은 공개 전이라 0행이다.
    #    v3.1 까지는 그 0행이 명부를 «비웠고», v3.2 부터는 «쓰지 않는다». 그래서 토요일
    #    회차가 보는 명부는 최대 3일 묵은 것이다 — 비어 있는 것보다는 낫지만 최신은 아니다.
    #    (국내발굴 명부의 정본은 results/fss/roster_map.csv 이고 이 JSON 은 보조다.)
    friday = datetime.datetime.strptime(bas, "%Y%m%d").weekday() == 4

    targets = [(p, "results/krx/latest") for p, _ in DAILY]
    if manual or friday:
        targets += [(p, "results/krx/roster") for p, _ in WEEKLY]

    # ── 1) 정기 수집 — 직전 거래일 ────────────────────────────────────────
    raw, status = {}, []
    for outdir in ("results/krx/latest", "results/krx/roster"):
        svc = [p for p, o in targets if o == outdir]
        if not svc:
            continue
        r, s = collect_day(bas, svc, outdir=outdir, save_json=True)
        raw.update(r)
        status += s

    hist = load_ews()
    try:
        hist, wrote = append_ews(compute_ews(raw), bas, hist)
        print("EWS  ews_daily.csv 갱신 완료" if wrote else "EWS  이번 거래일은 아직 미공개")
    except Exception as e:
        print(f"ERR  ews_daily 계산 실패: {e}")

    # ── 2) 백필 — 비었거나 없는 과거 거래일 (R-3·R-4) ─────────────────────
    # 슬롯당 BACKFILL_MAX_DAYS 만큼만. 3슬롯 × 5일이면 245영업일이 약 2~3주에 찬다.
    # ⚠️ KRX 호출 한도가 미확인이라 의도적으로 느리게 간다 — 한도를 재고 나면 올린다.
    skip = prune_skip(load_skip(), bas)
    fresh = set(recent_biz_days(bas, FRESH_DAYS))
    filled = 0
    # v3.2 (R-25): bas 를 «후보 단계»에서 뺀다 — v3.1 은 뽑았다가 버려 5칸 중 1칸을 낭비했다.
    tgts = backfill_targets(hist, bas, skip=skip, exclude={bas})
    for d in tgts:
        r, s = collect_day(d, BACKFILL_SERVICES)
        status += s
        # v3.3 (D1): 이 날짜의 호출이 «전부 깨끗하게» 끝났는가. 하나라도 HTTP·예외로
        # 실패했으면 적립하지 않는다 — 장애를 휴장으로 낙인찍는 경로를 끊는다.
        clean = all(row[2] in ("OK", "EMPTY", "SHORT") for row in s) and \
                not any(row[2] == "SHORT" for row in s)
        got_rows = any(row[2] == "OK" for row in s)
        try:
            hist, wrote = append_ews(compute_ews(r), d, hist)
            done = wrote and is_complete_row(hist.get(d) or {})
            if done:
                filled += 1
                skip.pop(d, None)        # 완성됐으면 의심을 거둔다
            else:
                # v3.3 (D2): 행은 썼는데 필수 항목이 모자라면 «부분»으로 적립한다 —
                # v3.2 는 이것을 완료로 보고 다시는 오지 않았다.
                note_no_progress(skip, d, "empty" if not got_rows else "partial",
                                 fresh, clean)
        except Exception as e:
            print(f"ERR  백필 {d} 계산 실패: {e}")
    save_skip(skip)
    dead_e = sum(1 for r in skip.values()
                 if int(r.get("attempts") or 0) >= SKIP_AFTER and (r.get("kind") or "empty") == "empty")
    dead_p = sum(1 for r in skip.values()
                 if int(r.get("attempts") or 0) >= SKIP_AFTER and r.get("kind") == "partial")
    depth = sum(1 for row in hist.values() if str(row.get("vkospi_close") or "").strip())
    full  = sum(1 for row in hist.values() if is_complete_row(row))
    print(f"BACKFILL 후보 {len(tgts)}일 · 채움 {filled}일 · 휴장확정 {dead_e}일 · "
          f"결측확정 {dead_p}일 · 완전한 행 {full} · V-KOSPI {depth}행 "
          f"(목표 {BACKFILL_TARGET})")
    # v3.3 (D4): 더 이상 받아올 후보가 없는데 목표에 못 미치면 «수렴이 아니라 한계»다.
    # 조용히 끝나지 않도록 파일로 남긴다 — 알람(빨간불)은 아니다. 매일 빨간불이면 안 본다.
    state = (f"as_of={now_utc()}\nbas_dd={bas}\ntarget={BACKFILL_TARGET}\n"
             f"complete_rows={full}\nvkospi_rows={depth}\n"
             f"pending_targets={len(tgts)}\nfilled_this_run={filled}\n"
             f"dead_holiday={dead_e}\ndead_incomplete={dead_p}\n"
             f"horizon_days={BACKFILL_HORIZON_DAYS}\n")
    if not tgts and full < BACKFILL_TARGET:
        state += ("status=HORIZON_LIMIT\n"
                  "note=더 받아올 후보가 없는데 목표 미달이다. KRX 과거 조회 한계가 "
                  "목표보다 짧거나, 휴장·결측 확정이 과다하다. ews_skip.csv 를 볼 것.\n")
        print("WARN 백필이 목표에 도달하지 못한 채 후보가 소진됐다 — "
              f"완전한 행 {full}/{BACKFILL_TARGET}. backfill_state.txt 참조")
    elif tgts and filled == 0:
        state += "status=NO_PROGRESS\nnote=후보는 있는데 한 날짜도 채우지 못했다.\n"
        print("WARN 백필 후보가 있는데 한 날짜도 채우지 못했다 — 휴장 구간이거나 "
              "KRX 과거 조회가 막혔을 수 있다. ews_skip.csv 의 attempts 를 볼 것.")
    else:
        state += "status=OK\n"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        f.write(state)

    # ── 3) 상태 기록 ─────────────────────────────────────────────────────
    ts = now_utc()
    write_csv_atomic(
        "results/krx/status.csv",
        ["service", "label", "status", "rows", "basDd", "run_utc"],
        [dict(zip(["service", "label", "status", "rows", "basDd"], [str(c) for c in row]),
              run_utc=ts) for row in status])
    flush_health()
    ok    = sum(1 for s in status if s[2] == "OK")
    empty = sum(1 for s in status if s[2] == "EMPTY")
    short = sum(1 for s in status if s[2] == "SHORT")
    bad   = len(status) - ok - empty - short
    # v3.3 (D8): 이미 아는 고장은 알람에서 뺀다. 세는 것은 그대로 센다.
    alarming = [r for r in status if r[2] not in ("OK", "EMPTY") and r[0] not in MUTE]
    print(f"\n요약: 호출 {len(status)}건 — 데이터 {ok} · 빈응답 {empty} · "
          f"부분응답 {short} · 실패 {bad} (basDd={bas}) · 백필 {filled}일 · "
          f"V-KOSPI 이력 {depth}행")

    # ── 4) 알람 (R-22) ───────────────────────────────────────────────────
    # 워크플로가 «커밋 뒤에» 이 파일을 보고 실패 처리한다 — 커밋 이후이므로 데이터는 남는다.
    # ⚠️ 「0행 20건」은 알람이 아니다. 아침 슬롯의 정상 상태이기 때문이다(R-17).
    #    알람은 401·타임아웃 같은 진짜 실패에만 건다.
    if alarming:
        lines = [f"{ts}  basDd={bas}  실패 {bad}건 · 부분응답 {short}건 / 호출 {len(status)}건", ""]
        lines += [f"  {r[0]} ({r[1]}) {r[4]}: {r[2]}" for r in alarming]
        lines.append("")
        lines.append("401/403 이면 포털 서비스 신청·기간을, 그 밖이면 네트워크·명세 변경을 본다.")
        os.makedirs(os.path.dirname(ALARM_PATH), exist_ok=True)
        with open(ALARM_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"ALARM {ALARM_PATH} 기록 — 워크플로가 이 실행을 실패로 표시한다")
    elif os.path.exists(ALARM_PATH):
        os.remove(ALARM_PATH)
        print(f"ALARM 해제 — {ALARM_PATH} 삭제")


def main():
    """v3.3 (D5): 어떤 예외로 죽더라도 health 와 알람은 남긴다.

    v3.2 는 `load_ews()` 가 BOM 하나에 KeyError 로 죽으면 status·health·알람을
    «전부» 못 남겼다. 워크플로는 그 단계를 continue-on-error 로 돌리고 커밋 뒤에
    알람 파일을 보므로, 알람을 못 남기면 사고가 초록불로 지나간다.
    """
    try:
        run()
    except BaseException as e:
        import traceback
        try:
            os.makedirs(os.path.dirname(ALARM_PATH), exist_ok=True)
            with open(ALARM_PATH, "w", encoding="utf-8") as f:
                f.write(f"{now_utc()}  수집 스크립트가 예외로 중단됐다\n\n"
                        f"{traceback.format_exc()}\n")
            print(f"ALARM {ALARM_PATH} 기록 — 예외로 중단: {type(e).__name__}: {e}")
        except Exception:
            pass
        raise
    finally:
        try:
            flush_health()
        except Exception as e:
            print(f"ERR  health 기록 실패: {e}")


if __name__ == "__main__":
    main()
