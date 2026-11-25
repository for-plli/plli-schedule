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
import xml.etree.ElementTree as ET
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
YT_HANDLE_URL = "https://www.youtube.com/@plave_official"   # 공식 유튜브 채널
YT_ID_FILE = "data/youtube_channel_id.txt"                  # 채널 ID (처음 한 번 찾아서 보관)
VIDEO_FILE = "data/videos.json"                             # 지금까지 모은 영상 (RSS는 최근 15개만 줘서 계속 쌓아 둠)
VIDEO_EXTRA = "video_extra.txt"                             # 직접 추가한 영상 링크 (한 줄에 하나)
VIDEO_EXTRA_CACHE = "data/videos_extra.json"                # 링크에서 읽어 온 제목/날짜 보관
VIDEO_EXCLUDE = "video_exclude.txt"                         # 달력에서 뺄 영상 제목 단어
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
                urls = url.split()  # 링크는 공백으로 여러 개 적을 수 있음
                if any(not u.startswith("https://") for u in urls):
                    raise ValueError("링크는 https:// 로 시작해야 함")
                tz = ZoneInfo(tzname or "Asia/Seoul")
                links = [{"url": u} for u in urls]
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


# ------------------------------------------------------------ 공식 유튜브 새 영상
def http_get(url, extra=None):
    h = {"User-Agent": "Mozilla/5.0 (compatible; plli-calendar-site/1.0)", "Accept-Language": "ko,en;q=0.8",
         "Cookie": "CONSENT=YES+1; SOCS=CAI"}
    h.update(extra or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def youtube_channel_id():
    if os.path.exists(YT_ID_FILE):
        cid = open(YT_ID_FILE, encoding="utf-8").read().strip()
        if re.fullmatch(r"UC[0-9A-Za-z_-]{22}", cid):
            return cid
    page = http_get(YT_HANDLE_URL)
    for pat in (r'rel="canonical" href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"',
                r'feeds/videos\.xml\?channel_id=(UC[0-9A-Za-z_-]{22})',
                r'"externalId":"(UC[0-9A-Za-z_-]{22})"'):
        m = re.search(pat, page)
        if m:
            os.makedirs("data", exist_ok=True)
            open(YT_ID_FILE, "w", encoding="utf-8").write(m.group(1) + "\n")
            return m.group(1)
    raise ValueError("채널 ID를 찾지 못함")


def parse_feed(xml_text, short=False):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for en in ET.fromstring(xml_text).findall("a:entry", ns):
        vid = (en.findtext("yt:videoId", "", ns) or "").strip()
        title = (en.findtext("a:title", "", ns) or "").strip()
        pub = (en.findtext("a:published", "", ns) or "").strip()
        if not (vid and title and pub):
            continue
        t = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(timezone.utc)
        out.append({"id": "yt-" + vid, "title": title, "utcStart": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "isAllDay": False, "links": [{"url": "https://www.youtube.com/watch?v=" + vid}],
                    **({"short": True} if short else {})})
    return out


def refresh_videos(offline):
    """새 영상을 읽어 data/videos.json 에 쌓기. 실패해도 사이트 만들기는 계속 (경고만)."""
    saved = {}
    if os.path.exists(VIDEO_FILE):
        try:
            saved = {e["id"]: e for e in json.load(open(VIDEO_FILE, encoding="utf-8"))}
        except Exception:
            saved = {}
    if not offline:
        try:
            cid = youtube_channel_id()
            new = []
            # 일반 영상(UULF) + 쇼츠(UUSH) 목록을 각각 읽어서 합침. 하나가 안 읽혀도 나머지는 사용
            for pre in ("UULF", "UUSH"):
                try:
                    new += parse_feed(http_get("https://www.youtube.com/feeds/videos.xml?playlist_id=" + pre + cid[2:]), short=(pre == "UUSH"))
                except Exception as ex2:
                    print(f"유튜브 {pre} 목록을 읽지 못함: {ex2}", file=sys.stderr)
            if not new:
                new = parse_feed(http_get("https://www.youtube.com/feeds/videos.xml?channel_id=" + cid))
            for e in new:
                saved[e["id"]] = e
            keep = sorted(saved.values(), key=lambda e: e["utcStart"], reverse=True)[:400]
            os.makedirs("data", exist_ok=True)
            json.dump(keep, open(VIDEO_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            saved = {e["id"]: e for e in keep}
            print(f"유튜브: 새로 읽은 영상 {len(new)}개 (누적 {len(saved)}개)")
        except Exception as ex:
            print(f"유튜브 영상은 읽지 못했어요 (보관본 사용): {ex}", file=sys.stderr)
    return list(saved.values())


def yt_id(url):
    m = re.search(r"(?:youtu\.be/|[?&]v=|/shorts/|/live/|/embed/)([0-9A-Za-z_-]{11})", url)
    return m.group(1) if m else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def yt_is_short(vid):
    """youtube.com/shorts/ID 가 그대로 열리면(200) 쇼츠, 다른 곳으로 넘겨지면 일반 영상"""
    op = urllib.request.build_opener(_NoRedirect)
    try:
        r = op.open(urllib.request.Request("https://www.youtube.com/shorts/" + vid,
                    headers={"User-Agent": "Mozilla/5.0", "Cookie": "CONSENT=YES+1; SOCS=CAI"}), timeout=20)
        return r.status == 200
    except Exception:
        return False


def lookup_video(vid, manual_title=""):
    """링크 하나의 제목과 올린 시각을 유튜브에서 읽기"""
    import html as _h
    page = http_get("https://www.youtube.com/watch?v=" + vid)
    title = manual_title
    if not title:
        for pat in (r'<meta property="og:title" content="([^"]+)"', r'<meta name="title" content="([^"]+)"',
                    r'"videoDetails":\{[^}]*?"title":"((?:[^"\\]|\\.)*)"', r"<title>(.*?)(?: - YouTube)?</title>"):
            m = re.search(pat, page, re.S)
            if m and m.group(1).strip() and m.group(1).strip().lower() != "youtube":
                try:
                    title = _h.unescape(json.loads('"' + m.group(1) + '"') if "\\" in m.group(1) else m.group(1)).strip()
                except Exception:
                    title = _h.unescape(m.group(1)).strip()
                break
    if not title:   # 공식 oEmbed 로 제목만이라도
        try:
            o = json.loads(http_get("https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v=" + vid))
            title = (o.get("title") or "").strip()
        except Exception:
            pass
    when = None
    for pat in (r'itemprop="datePublished" content="([^"]+)"', r'"publishDate":"([^"]+)"', r'"uploadDate":"([^"]+)"'):
        m = re.search(pat, page)
        if m:
            try:
                raw = m.group(1)
                d = datetime.fromisoformat(raw.replace("Z", "+00:00")) if "T" in raw else datetime.fromisoformat(raw + "T00:00:00+09:00")
                when = d.astimezone(timezone.utc)
                break
            except Exception:
                pass
    if not title:
        t = re.search(r"<title>(.*?)</title>", page, re.S)
        raise ValueError(f"제목을 찾지 못함 (페이지 {len(page)}자, title={t.group(1)[:60] if t else None!r}, 재생불가={('LOGIN_REQUIRED' in page) or ('UNPLAYABLE' in page)})")
    return title, when, page


def youtube_channel_id_cached():
    try:
        return open(YT_ID_FILE, encoding="utf-8").read().strip()
    except Exception:
        return ""


def video_channel(vid, page, official_id):
    """공식 채널이 아닌 곳에 올라온 영상이면 채널 이름을, 공식 채널이면 빈 문자열을 돌려줌"""
    import html as _h
    cid = None
    for pat in (r'itemprop="(?:channelId|identifier)" content="(UC[\w-]{22})"', r'"channelId":"(UC[\w-]{22})"', r'"externalChannelId":"(UC[\w-]{22})"'):
        m = re.search(pat, page)
        if m:
            cid = m.group(1)
            break
    name = ""
    for pat in (r'"ownerChannelName":"((?:[^"\\]|\\.)*)"', r'<link itemprop="name" content="([^"]+)"', r'"author":"((?:[^"\\]|\\.)*)"'):
        m = re.search(pat, page)
        if m and m.group(1).strip():
            try:
                name = _h.unescape(json.loads('"' + m.group(1) + '"') if "\\" in m.group(1) else m.group(1)).strip()
            except Exception:
                name = _h.unescape(m.group(1)).strip()
            break
    if not name:
        try:
            o = json.loads(http_get("https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v=" + vid))
            name = (o.get("author_name") or "").strip()
        except Exception:
            pass
    if cid and official_id:
        return "" if cid == official_id else (name or "다른 채널")
    if not name:
        return None   # 알 수 없음 (다음에 다시 시도)
    return "" if re.search(r"plave|플레이브", name, re.I) else name


def load_extra_videos(offline):
    """video_extra.txt 의 링크를 영상 목록에 넣기. 한 줄: 링크 [| 제목(생략하면 유튜브에서 읽음)] [| 올린 날짜 2026-11-24 18:00]"""
    if not os.path.exists(VIDEO_EXTRA):
        return []
    cache = {}
    if os.path.exists(VIDEO_EXTRA_CACHE):
        try:
            cache = {e["id"]: e for e in json.load(open(VIDEO_EXTRA_CACHE, encoding="utf-8"))}
        except Exception:
            cache = {}
    out, changed = [], False
    for ln in open(VIDEO_EXTRA, encoding="utf-8"):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [p.strip() for p in ln.split("|")]
        vid = yt_id(parts[0])
        if not vid and re.match(r"https?://", parts[0]) and len(parts) > 2 and parts[1] and parts[2]:
            # 유튜브가 아닌 링크(X 등): 제목과 날짜를 직접 적은 경우 그대로 넣기
            try:
                fmt = "%Y-%m-%d %H:%M" if ":" in parts[2] else "%Y-%m-%d"
                d = datetime.strptime(parts[2], fmt).replace(tzinfo=KST).astimezone(timezone.utc)
            except ValueError:
                print(f"video_extra.txt: 날짜 형식은 2026-11-24 18:00 처럼 적어 주세요: {parts[2]}", file=sys.stderr)
                continue
            out.append({"id": "ext-" + hashlib.md5(parts[0].encode()).hexdigest()[:10], "title": parts[1],
                        "utcStart": d.strftime("%Y-%m-%dT%H:%M:%SZ"), "isAllDay": False,
                        "links": [{"url": parts[0]}], "manual": True})
            continue
        if not vid:
            print(f"video_extra.txt: 링크를 알아볼 수 없어 건너뜀: {ln[:60]}", file=sys.stderr)
            continue
        key = "yt-" + vid
        ent = cache.get(key)
        if ent and "channel" not in ent and not offline:   # 예전에 저장된 영상: 채널만 다시 확인
            try:
                _t, _w, page = lookup_video(vid, ent["title"])
                ch = video_channel(vid, page, youtube_channel_id_cached())
                if ch is not None:
                    ent["channel"] = ch
                    cache[key] = ent
                    changed = True
                    print(f"영상 채널 확인: {ent['title'][:30]} -> {ch or '공식'}")
            except Exception as ex:
                print(f"영상 채널을 읽지 못함 ({vid}): {ex}", file=sys.stderr)
        if not ent and not offline:
            try:
                title, when, page = lookup_video(vid, parts[1] if len(parts) > 1 else "")
                ch = video_channel(vid, page, youtube_channel_id_cached())
                ent = {"id": key, "title": title,
                       "utcStart": (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "isAllDay": False, "links": [{"url": "https://www.youtube.com/watch?v=" + vid}],
                       "manual": True, "dateGuessed": when is None}
                if ch is not None:
                    ent["channel"] = ch
                if yt_is_short(vid):
                    ent["short"] = True
                cache[key] = ent
                changed = True
                print(f"영상 추가: {title[:40]} ({ent['utcStart']})")
            except Exception as ex:
                print(f"영상 정보를 읽지 못함 ({vid}): {ex}", file=sys.stderr)
        if ent and len(parts) > 2 and parts[2]:   # 세 번째 칸에 날짜(시간)를 적으면 그 날짜로 (한국 시간)
            try:
                fmt = "%Y-%m-%d %H:%M" if ":" in parts[2] else "%Y-%m-%d"
                d = datetime.strptime(parts[2], fmt).replace(tzinfo=KST).astimezone(timezone.utc)
                ent = dict(ent, utcStart=d.strftime("%Y-%m-%dT%H:%M:%SZ"), dateGuessed=False)
            except ValueError:
                print(f"video_extra.txt: 날짜 형식은 2026-11-24 18:00 처럼 적어 주세요: {parts[2]}", file=sys.stderr)
        if ent and "/live/" in parts[0]:   # youtube.com/live/ 링크는 라이브 다시보기로 [LIVE] 표시
            ent = dict(ent, live=True)
        if ent:
            out.append(ent)
    if changed:
        os.makedirs("data", exist_ok=True)
        json.dump(list(cache.values()), open(VIDEO_EXTRA_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    return out


RE_TAIL_TAGS = re.compile(r"(?:\s+[#@]\S+)+\s*$")
RE_SUB = re.compile(r"\(\s*SUB\s*\)\s*", re.I)
# 멤버별 상징 컬러 하트 (위버스 라이브 제목의 💙💜🩷❤🖤 순서와 같음: 예준·노아·밤비·은호·하민)
MEMBER_HEART = [
    ("💙", ("yejun", "예준")), ("💜", ("noah", "노아")), ("🩷", ("bamby", "밤비")),
    ("❤️", ("eunho", "은호")), ("🖤", ("hamin", "하민")),
]


def member_hearts(title):
    """제목의 #해시태그 에서 멤버 이름을 찾아 하트 이모지로. 5명 전부면 단체라서 표시하지 않음"""
    tags = [x.lower() for x in re.findall(r"[#＃]([^\s#@]+)", title)]
    out = [h for h, names in MEMBER_HEART if any(any(n in t for n in names) for t in tags)]
    return "" if len(out) >= 5 else "".join(out)


def video_view(videos):
    """달력에 보일 제목 다듬기: 끝에 줄줄이 붙은 #해시태그/@멘션 은 떼고, 해시태그의 멤버는 하트로,
    쇼츠는 앞에 '[Shorts] ' 를 붙임"""
    out = []
    for e in videos:
        base = RE_SUB.sub("", e["title"]).strip() or e["title"]      # (SUB) 표시는 뺌
        t = RE_TAIL_TAGS.sub("", base).strip() or base
        hearts = member_hearts(e["title"])
        if hearts:
            t = hearts + " " + t
        if e.get("short"):
            t = "[Shorts] " + t
        if e.get("live") and not re.match(r"\s*\[\s*LIVE\s*\]", t, re.I):
            t = "[LIVE] " + t
        d = dict(e)
        if d.get("channel"):   # 채널 이름은 짧게 ('서울시 · Seoul' -> '서울시')
            d["channel"] = re.split(r"\s*[·|/]\s*", d["channel"])[0].strip() or d["channel"]
        d["title"] = t
        out.append(d)
    return out


def video_filter(videos):
    """video_exclude.txt: 일반 줄 = 이 단어가 제목에 있으면 라이브 다시보기로 표시, '!' 로 시작 = 이 단어가 있으면 표시하지 않음"""
    bad, keep = [], []
    if os.path.exists(VIDEO_EXCLUDE):
        for ln in open(VIDEO_EXCLUDE, encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            (keep if ln.startswith("!") else bad).append(ln.lstrip("!").strip().lower())
    def has(word, t):   # 영어 단어는 낱말 단위로만 (Deliver 속 'live' 는 제외)
        if re.fullmatch(r"[a-z0-9/ ]+", word):
            return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", t) is not None
        return word in t
    out = []
    for e in videos:
        t = e["title"].lower()
        if not e.get("manual") and any(has(b, t) for b in bad) and not any(has(k, t) for k in keep):
            e = dict(e, live=True)   # 라이브 다시보기: 빼지 않고 [LIVE] 로 표시
        out.append(e)
    return out


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
    videos = refresh_videos(a.offline)
    seen = {e['id'] for e in videos}
    videos += [e for e in load_extra_videos(a.offline) if e['id'] not in seen]
    videos = video_view(video_filter(videos))
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
        "__FAN_MONTHLY__": dump(fan_monthly), "__LOADED__": dump(loaded), "__VIDEOS__": dump(videos),
        "__REPEAT_FROM__": f"{y0}-{m0:02d}", "__REPEAT_UNTIL__": f"{y1}-{m1:02d}", "__UPDATED__": updated,
    }.items():
        assert key in html, f"템플릿에 {key} 가 없음"
        html = html.replace(key, val)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"공식 {len(official)}건, 플레이브(직접) {len(plave_extra)}건, 플리 {len(fan_once)}건 + 반복 {len(fan_monthly)}개, 영상 {len(videos)}건 -> {a.out}")
    if not a.offline and ok == 0:
        sys.exit("공식 사이트에서 한 달도 읽지 못했습니다. (보관본으로 만들었어요)")


if __name__ == "__main__":
    main()
