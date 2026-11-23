#!/usr/bin/env python3
"""바쁘다 바빠 플리사회 🤍 사이트 만들기 (표준 라이브러리만 사용, 파이썬 3.9+)

하는 일
  1) plave.global 공식 스케줄을 월별로 읽어서 data/official/YYYY-MM.json 에 보관 (읽기 실패하면 보관본 사용)
     - 이번 달 + 다음 2개월을 매번 읽고, 지난달은 맨 처음 한 번만 읽어요
  2) plave_extra.txt (직접 적은 플레이브 일정) 과 ticketing.txt (예매·예약 = 플리 일정) 를 읽기
  3) 직접 적은 플레이브 일정이 공식 일정과 같은 것이면 직접 적은 쪽을 빼기 (같은 시각 30분 이내 + 제목 비슷)
  4) template.html 에 데이터를 넣어 docs/index.html 만들기

사용법
  python3 build_site.py              # 사이트에서 읽어서 만들기
  python3 build_site.py --offline    # 읽지 않고 보관본으로만 만들기 (테스트용)
"""
import argparse
import calendar
import difflib
import glob
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API_URL = "https://plave.global/api/v1/schedule"   # 개발자 도구 Request URL 로 확인된 주소
KST = timezone(timedelta(hours=9))
SEOUL = ZoneInfo("Asia/Seoul")
CACHE_DIR = "data/official"
LAST_OK = "data/last_ok.txt"
MONTHS_AHEAD = 2                                     # 이번 달 + 다음 2개월을 매번 읽음
PREV_DONE = "data/prev_month_done.txt"               # 지난달은 맨 처음 한 번만 읽고 이 표시를 남김
DUP_MINUTES = 30
WD = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
RE_MONTHLY = re.compile(r"^매월\s+(\d)(?:번째|째)\s*([월화수목금토일])(?:요일)?\s+(\d{1,2}:\d{2})$")


# ------------------------------------------------------------ 공식 일정 읽기/보관
def ym_add(y, m, delta):
    t = y * 12 + (m - 1) + delta
    return t // 12, t % 12 + 1


def fetch_month(y, m):
    url = f"{API_URL}?type=events&year={y}&month={m}&lang=ko"
    req = urllib.request.Request(url, headers={"User-Agent": "plli-calendar-site/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("예상과 다른 응답 형식")
    return data


def refresh_cache(offline):
    """공식 일정을 읽어 보관본을 갱신. 성공한 달 수를 돌려줌."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    if offline:
        return 0
    now = datetime.now(KST)
    ok = 0
    offsets = list(range(0, MONTHS_AHEAD + 1))
    if not os.path.exists(PREV_DONE):      # 맨 처음 한 번만 지난달도 읽기
        offsets.insert(0, -1)
    for d in offsets:
        y, m = ym_add(now.year, now.month, d)
        try:
            data = fetch_month(y, m)
        except Exception as ex:
            print(f"{y}-{m:02d} 읽기 실패: {ex}", file=sys.stderr)
            continue
        with open(f"{CACHE_DIR}/{y}-{m:02d}.json", "w", encoding="utf-8") as f:
            json.dump(sorted(data, key=lambda e: (e["utcStart"], e["id"])), f, ensure_ascii=False, indent=1)
        if d == -1:
            with open(PREV_DONE, "w", encoding="utf-8") as f:
                f.write(f"{y}-{m:02d} 읽음")
        ok += 1
    if ok:
        with open(LAST_OK, "w", encoding="utf-8") as f:
            f.write(now.strftime("%Y-%m-%d %H:%M"))
    return ok


def load_official():
    events, seen, loaded = [], set(), []
    for path in sorted(glob.glob(f"{CACHE_DIR}/*.json")):
        loaded.append(os.path.basename(path)[:-5])
        with open(path, encoding="utf-8") as f:
            for e in json.load(f):
                if e["id"] in seen:          # 달 경계에서 겹치는 일정 제거
                    continue
                seen.add(e["id"])
                out = {"id": e["id"], "title": e["title"], "utcStart": e["utcStart"],
                       "isAllDay": bool(e.get("isAllDay")),
                       "links": [{"url": l["url"]} for l in e.get("links", []) if l.get("url")]}
                events.append(out)
    return events, loaded


# ------------------------------------------------------------ 직접 적은 일정
def parse_manual(path):
    """한 줄 형식:  날짜 [시간] | 제목 | 메모(선택) | 링크(선택) | 시간대(선택, 기본 Asia/Seoul)
    매달 반복(한국 시간만):  매월 3번째 목 16:00 | 제목 | 메모 | 링크"""
    one, monthly = [], []
    if not os.path.exists(path):
        return one, monthly
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            c = [x.strip() for x in line.split("|")]
            c += [""] * (5 - len(c))
            when, title, note, url, tzname = c[:5]
            try:
                if not title:
                    raise ValueError("제목이 비어 있음")
                if url and not url.startswith("https://"):
                    raise ValueError("링크는 https:// 로 시작해야 함")
                tz = ZoneInfo(tzname or "Asia/Seoul")
                links = [{"url": url}] if url else []
                rm = RE_MONTHLY.match(when)
                if rm:
                    nth, wd, hhmm = int(rm.group(1)), WD[rm.group(2)], rm.group(3)
                    if not 1 <= nth <= 4:
                        raise ValueError("반복은 1~4번째만 지원")
                    if (tzname or "Asia/Seoul") != "Asia/Seoul":
                        raise ValueError("반복 일정은 한국 시간만 지원")
                    hh, mm = map(int, hhmm.split(":"))
                    monthly.append({"id": "m-" + hashlib.sha1(line.encode()).hexdigest()[:8],
                                    "title": title, "nth": nth, "dow": (wd + 1) % 7,   # 자바스크립트 기준(일=0)
                                    "hour": hh, "minute": mm, "note": note, "links": links})
                    continue
                if len(when) > 10:
                    st = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                    all_day = False
                    iso = st.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                else:
                    st = datetime.strptime(when, "%Y-%m-%d")
                    all_day = True
                    iso = st.strftime("%Y-%m-%dT00:00:00.000Z")
                one.append({"id": "x-" + hashlib.sha1(f"{when}|{title}".encode()).hexdigest()[:10],
                            "title": title, "utcStart": iso, "isAllDay": all_day, "note": note, "links": links})
            except Exception as ex:
                print(f"{path}:{n}: 건너뜀 ({ex})", file=sys.stderr)
    return one, monthly


# ------------------------------------------------------------ 중복 거르기
GENERIC = {"live", "day", "plave", "플레이브", "the", "of", "in"}


def tokens(s):
    return {w for w in re.findall(r"[0-9A-Za-z가-힣]+", s.lower())
            if len(w) >= 2 and not w.isdigit() and w not in GENERIC}


def is_dup(manual, official):
    if manual["isAllDay"] or official["isAllDay"]:
        return False
    a = datetime.strptime(manual["utcStart"], "%Y-%m-%dT%H:%M:%S.000Z")
    b = datetime.strptime(official["utcStart"], "%Y-%m-%dT%H:%M:%S.%fZ")
    if abs((a - b).total_seconds()) > DUP_MINUTES * 60:
        return False
    if tokens(manual["title"]) & tokens(official["title"]):
        return True
    return difflib.SequenceMatcher(None, manual["title"].lower(), official["title"].lower()).ratio() >= 0.5


def drop_duplicates(manual, official):
    kept = []
    for m in manual:
        hit = next((o for o in official if is_dup(m, o)), None)
        if hit:
            print(f"중복 제외(직접 적은 쪽): {m['title']}  ~  공식: {hit['title']}")
        else:
            kept.append(m)
    return kept


# ------------------------------------------------------------ 사이트 만들기
def dump(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="사이트에서 읽지 않고 보관본으로만 만들기")
    ap.add_argument("--out", default="docs/index.html")
    a = ap.parse_args()

    ok = refresh_cache(a.offline)
    official, loaded = load_official()
    plave_extra, extra_monthly = parse_manual("plave_extra.txt")
    fan_once, fan_monthly = parse_manual("ticketing.txt")
    if extra_monthly:
        print("plave_extra.txt 의 반복 일정은 아직 지원하지 않아 건너뜀", file=sys.stderr)
    plave_extra = drop_duplicates(plave_extra, official)

    now = datetime.now(KST)
    y0, m0 = now.year, now.month
    y1, m1 = ym_add(y0, m0, 30)
    updated = open(LAST_OK, encoding="utf-8").read().strip() if os.path.exists(LAST_OK) else "아직 없음"

    html = open("template.html", encoding="utf-8").read()
    for key, val in {
        "__DATA__": dump(official), "__PLAVE_EXTRA__": dump(plave_extra), "__FAN__": dump(fan_once),
        "__FAN_MONTHLY__": dump(fan_monthly), "__LOADED__": dump(loaded),
        "__REPEAT_FROM__": f"{y0}-{m0:02d}", "__REPEAT_UNTIL__": f"{y1}-{m1:02d}", "__UPDATED__": updated,
    }.items():
        assert key in html, f"템플릿에 {key} 가 없음"
        html = html.replace(key, val)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"공식 {len(official)}건, 플레이브(직접) {len(plave_extra)}건, 플리 {len(fan_once)}건 + 반복 {len(fan_monthly)}개 -> {a.out}")
    if not a.offline and ok == 0:
        sys.exit("공식 사이트에서 한 달도 읽지 못했습니다. (보관본으로 만들었어요)")


if __name__ == "__main__":
    main()
