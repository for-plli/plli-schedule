"""사이트에 쓰는 일정 데이터로 캘린더 구독 파일(.ics)을 만듭니다. (표준 라이브러리만 사용)

  docs/schedule.ics  플리 일정(티켓팅·예약, 매달 반복 포함) - 당일 아침 9시 안내 + 시작 10분 전 알림용
"""
from datetime import datetime, timedelta, timezone

STAMP = "20260101T000000Z"        # 고정값: 내용이 같으면 파일도 똑같이 나오게 함
DEFAULT_MINUTES = 60              # 끝나는 시각이 없을 때 기본 길이
ALARM_FAN = 10                    # 플리 일정(시간 있는 것) 알림: 10분 전
MORNING_HOUR = 9                  # 일정이 있는 날 아침 알림 시각(한국 시간)
BYDAY = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"]   # 자바스크립트 요일(일=0) 순서


def esc(text):
    return (str(text).replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line):
    """RFC5545: 75바이트 단위로 줄바꿈 (한글 중간에서 끊기지 않게)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts, cur, size = [], "", 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        limit = 75 if not parts else 74
        if size + b > limit:
            parts.append(cur)
            cur, size = ch, b
        else:
            cur += ch
            size += b
    parts.append(cur)
    return "\r\n ".join(parts)


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def alarm(minutes, text):
    return ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(text)}", f"TRIGGER:-PT{minutes}M", "END:VALARM"]


def morning_alarm(minutes_after_midnight, text):
    """일정이 있는 날 아침 9시 알림. 반복 일정에도 쓸 수 있게 일정 시작 시각 기준의 상대값으로 적습니다."""
    gap = minutes_after_midnight - MORNING_HOUR * 60      # 일정 시각이 아침 알림보다 얼마나 뒤인지(분)
    if gap >= 20:                                         # 10분 전 알림과 겹칠 만큼 가까우면 아침 알림은 생략
        return ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc(text)}", f"TRIGGER:-PT{gap}M", "END:VALARM"]
    return []


def event(e, alarm_min, category=""):
    start = utc(e["utcStart"])
    lines = ["BEGIN:VEVENT", f"UID:{e['id']}@plli-schedule", f"DTSTAMP:{STAMP}"]
    if e.get("isAllDay"):
        d = start.date()
        lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
    else:
        end = utc(e["utcEnd"]) if e.get("utcEnd") else start + timedelta(minutes=DEFAULT_MINUTES)
        if end <= start:
            end = start + timedelta(minutes=DEFAULT_MINUTES)
        lines += [f"DTSTART:{stamp(start)}", f"DTEND:{stamp(end)}"]
    lines.append(f"SUMMARY:{esc(e['title'])}")
    urls = [l["url"] for l in e.get("links", []) if l.get("url")]
    desc = "\n".join(([e["note"]] if e.get("note") else []) + urls)
    if desc:
        lines.append(f"DESCRIPTION:{esc(desc)}")
    if urls:
        lines.append(f"URL:{urls[0]}")
    if category:
        lines.append(f"CATEGORIES:{esc(category)}")
    if e.get("isAllDay"):   # 종일 일정: 당일 아침 9시에 안내
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{esc('오늘 ' + e['title'])}",
                  f"TRIGGER:PT{MORNING_HOUR}H", "END:VALARM"]
    else:
        k = start + timedelta(hours=9)
        lines += morning_alarm(k.hour * 60 + k.minute, f"오늘 {k:%H:%M} {e['title']}")
        if alarm_min:
            lines += alarm(alarm_min, e["title"])
    lines.append("END:VEVENT")
    return lines


def monthly_event(r, first_day, until_day):
    """매달 N번째 요일 반복 일정: RRULE 한 줄로 (한국 시간 기준)."""
    y, m = first_day.year, first_day.month
    first_wd = datetime(y, m, 1).weekday()            # 월=0
    js_first = (first_wd + 1) % 7                     # 일=0
    day = 1 + (r["dow"] - js_first + 7) % 7 + 7 * (r["nth"] - 1)
    start = datetime(y, m, day, r["hour"], r["minute"])
    end = start + timedelta(minutes=DEFAULT_MINUTES)
    lines = ["BEGIN:VEVENT", f"UID:{r['id']}@plli-schedule", f"DTSTAMP:{STAMP}",
             f"DTSTART;TZID=Asia/Seoul:{start:%Y%m%dT%H%M%S}", f"DTEND;TZID=Asia/Seoul:{end:%Y%m%dT%H%M%S}",
             f"RRULE:FREQ=MONTHLY;BYDAY={r['nth']}{BYDAY[r['dow']]};UNTIL={until_day:%Y%m%d}T145959Z",
             f"SUMMARY:{esc(r['title'])}"]
    urls = [l["url"] for l in r.get("links", []) if l.get("url")]
    desc = "\n".join(([r["note"]] if r.get("note") else []) + urls)
    if desc:
        lines.append(f"DESCRIPTION:{esc(desc)}")
    if urls:
        lines.append(f"URL:{urls[0]}")
    lines.append("CATEGORIES:플리")
    lines += morning_alarm(r["hour"] * 60 + r["minute"], f"오늘 {r['hour']:02d}:{r['minute']:02d} {r['title']}")
    lines += alarm(ALARM_FAN, r["title"])
    lines.append("END:VEVENT")
    return lines


def calendar_text(name, desc, event_lines):
    head = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//plli-schedule//KO", "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH", f"X-WR-CALNAME:{esc(name)}", f"X-WR-CALDESC:{esc(desc)}",
            "X-WR-TIMEZONE:Asia/Seoul", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
            "BEGIN:VTIMEZONE", "TZID:Asia/Seoul", "BEGIN:STANDARD", "DTSTART:19700101T000000",
            "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900", "TZNAME:KST", "END:STANDARD", "END:VTIMEZONE"]
    lines = head + event_lines + ["END:VCALENDAR"]
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def write_all(outdir, official, plave_extra, fan_once, fan_monthly, first_day, until_day):
    """플리 일정(티켓팅·예약)만 담은 알림용 캘린더(schedule.ics). 플레이브 일정은 사이트에서 봅니다."""
    rows = [(e["utcStart"], e["id"], event(e, ALARM_FAN, "플리")) for e in fan_once]
    lines = []
    for _, _, ev in sorted(rows, key=lambda r: (r[0], r[1])):
        lines += ev
    for r in fan_monthly:
        lines += monthly_event(r, first_day, until_day)
    text = calendar_text("플리 일정", "플리 티켓팅·예약 일정 알림 (비공식 팬 제작 캘린더)", lines)
    with open(f"{outdir}/schedule.ics", "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return text.count("BEGIN:VEVENT")
