#!/usr/bin/env python3
"""시외버스 티머니(txbus.t-money.co.kr) 잔여좌석 감시기.

매진된 배차에 좌석이 다시 생기면 Gmail로 알림을 보낸다.
표준 라이브러리만 사용한다.

사용법:
    python3 watch.py find 춘천              # 출발지 터미널 코드 검색
    python3 watch.py arrivals 0511601 춘천  # 해당 출발지에서 갈 수 있는 도착지 검색
    python3 watch.py check                  # 1회 조회 (cron/launchd가 호출)
    python3 watch.py check --dry-run        # 메일 안 보내고 현재 상태만 출력
    python3 watch.py test-mail              # 메일 설정 점검
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate

BASE = "https://txbus.t-money.co.kr"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")


# ---------------------------------------------------------------- HTTP

def ssl_context():
    """python.org 설치본은 CA 번들이 비어 있는 경우가 있어 certifi로 보강한다."""
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats()["x509_ca"]:
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
    return ctx


def make_opener():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=ssl_context()))
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept-Language", "ko-KR,ko;q=0.9"),
        ("Referer", BASE + "/"),
    ]
    # 메인 페이지를 먼저 열어 세션 쿠키를 받는다.
    opener.open(BASE + "/", timeout=20).read()
    return opener


def post(opener, path, params, timeout=30):
    data = urllib.parse.urlencode(params, encoding="utf-8").encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    with opener.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------- 터미널 조회

def terminal_list(opener, area_cd="", keyword="", pre_trml_cd="", gbn="01"):
    body = post(opener, "/otck/readTrmlList.do", {
        "cty_Bus_Area_Cd": area_cd,
        "trml_Nm": keyword,
        "pre_Trml_Cd": pre_trml_cd,
        "rtnGbn": gbn,
    })
    return json.loads(body)


def cmd_find(args):
    opener = make_opener()
    seen = {}
    # 지역코드를 순회하며 전체 터미널을 모은다.
    for area in [""] + ["%02d" % i for i in range(1, 18)]:
        try:
            for t in terminal_list(opener, area_cd=area, gbn="01"):
                seen[t["trml_Cd"]] = t
        except Exception:
            continue
    hits = [t for t in seen.values() if args.keyword in t["trml_Nm"]]
    if not hits:
        print("일치하는 터미널이 없습니다:", args.keyword)
        return 1
    for t in sorted(hits, key=lambda x: x["trml_Nm"]):
        print("%s  %s  (%s)" % (t["trml_Cd"], t["trml_Nm"], t.get("cty_Bus_Area_Nm") or ""))
    return 0


def cmd_arrivals(args):
    opener = make_opener()
    rows = terminal_list(opener, pre_trml_cd=args.depr_cd, gbn="02")
    hits = [t for t in rows if not args.keyword or args.keyword in t["trml_Nm"]]
    if not hits:
        print("일치하는 도착지가 없습니다. (전체 %d곳)" % len(rows))
        return 1
    for t in sorted(hits, key=lambda x: x["trml_Nm"]):
        print("%s  %s" % (t["trml_Cd"], t["trml_Nm"]))
    return 0


# ------------------------------------------------------------ 배차 파싱

# 예매 버튼 onclick 인자 순서:
# rot_Id, rot_Sqno, alcn_Dt, ?, depr_Cd, arvl_Cd, ?, ?, depr_Time,
# bus_Cls_Cd, ?, 운수사, 등급, ?, ?, ?, 잔여석, 총좌석, ...
RESERVE_RE = re.compile(r"readSasFeeInf\((.*?)\);", re.S)
ROW_RE = re.compile(r"<tr(?![^>]*class=\"detail\")[^>]*>(.*?)</tr>", re.S)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
SEAT_RE = re.compile(r"(\d+)\s*석[^<]*?/\s*총\s*(\d+)\s*석", re.S)


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_schedules(page):
    """배차 목록 페이지에서 편별 정보를 뽑는다. 첫 번째(PC용) 표만 읽는다."""
    # 같은 데이터가 PC/모바일 두 표에 중복 출력되므로 rot_id+시각으로 중복 제거한다.
    out = {}
    for row_html in ROW_RE.findall(page):
        tds = TD_RE.findall(row_html)
        if len(tds) < 3:
            continue
        depart = strip_tags(tds[0])
        if not re.fullmatch(r"\d{2}:\d{2}", depart):
            continue

        m = RESERVE_RE.search(row_html)
        rot_id, seq, company, grade = "", "", "", ""
        remaining, total = None, None
        if m:
            argv = [a.strip().strip("'\"") for a in m.group(1).split(",")]
            if len(argv) >= 18:
                rot_id, seq = argv[0], argv[1]
                company, grade = argv[11], argv[12]
                try:
                    remaining, total = int(argv[16]), int(argv[17])
                except ValueError:
                    pass

        last = strip_tags(tds[-1])
        if remaining is None:
            sm = SEAT_RE.search(strip_tags(row_html))
            if sm:
                remaining, total = int(sm.group(1)), int(sm.group(2))

        if not company:
            company = strip_tags(tds[1])
        status = last if remaining is None else ""

        key = "%s|%s|%s" % (depart, rot_id, seq)
        if key in out:
            continue
        out[key] = {
            "depart": depart,
            "company": re.sub(r"\d+:\d+ 소요 예상", "", company).strip(),
            "grade": grade,
            "remaining": remaining,      # None이면 좌석수 미확인(매진/불가 등)
            "total": total,
            "status": status,            # 좌석수가 없을 때의 화면 문구
            "rot_id": rot_id,
        }
    return sorted(out.values(), key=lambda x: (x["depart"], x["rot_id"]))


def fetch_route(opener, depr_cd, arvl_cd, date, records=100):
    page = post(opener, "/otck/readAlcnList.do", {
        "depr_Trml_Cd": depr_cd,
        "arvl_Trml_Cd": arvl_cd,
        "depr_Dt": date,             # YYYY-MM-DD
        "bef_Aft_Dvs": "D",
        "req_Rec_Num": str(records),
        "depr_Time": "000000",
        "ig": "1", "im": "0", "ic": "0", "iv": "0",
    })
    return parse_schedules(page)


# -------------------------------------------------------------- 알림

def send_gmail(cfg, subject, body):
    g = cfg["gmail"]
    msg = EmailMessage()
    msg["From"] = g["user"]
    msg["To"] = ", ".join(g["to"]) if isinstance(g["to"], list) else g["to"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl_context(), timeout=30) as s:
        s.login(g["user"], g["app_password"].replace(" ", ""))
        s.send_message(msg)


def notify_macos(title, text):
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e",
             'display notification %s with title %s sound name "Glass"'
             % (json.dumps(text), json.dumps(title))],
            timeout=10, check=False)
    except Exception:
        pass


# --------------------------------------------------------------- 체크

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def gmail_from_env():
    """GitHub Actions 등에서는 비밀번호를 파일 대신 환경변수로 넘긴다."""
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("GMAIL_TO")
    if not (user and pw and to):
        return None
    to_list = [t.strip() for t in to.split(",") if t.strip()]
    return {"user": user, "app_password": pw, "to": to_list}


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)


def cmd_check(args):
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json 이 없습니다. config.example.json 을 복사해서 채워주세요.", file=sys.stderr)
        return 2
    if "gmail" not in cfg:
        env_gmail = gmail_from_env()
        if env_gmail is None:
            print("gmail 설정이 없습니다 (config.json 의 gmail 또는 "
                  "GMAIL_USER/GMAIL_APP_PASSWORD/GMAIL_TO 환경변수).", file=sys.stderr)
            return 2
        cfg["gmail"] = env_gmail

    # 첫 실행에는 현재 상태만 기록한다. 안 그러면 이미 여유 있는 편까지 전부 알림이 간다.
    first_run = not os.path.exists(STATE_PATH)
    state = load_json(STATE_PATH, {})
    today = datetime.now().strftime("%Y-%m-%d")
    found = []          # 이번에 새로 좌석이 생긴 편
    new_state = {}

    opener = None
    for attempt in range(3):
        try:
            opener = make_opener()
            break
        except Exception as e:
            log("세션 생성 실패 (%d/3): %s" % (attempt + 1, e))
            if attempt < 2:
                time.sleep(10)
    if opener is None:
        log("세션 생성에 계속 실패하여 이번 실행은 건너뜁니다.")
        return 0

    for route in cfg["routes"]:
        min_seats = int(route.get("min_seats", 1))
        t_from = route.get("time_from", "00:00")
        t_to = route.get("time_to", "23:59")

        for date in route["dates"]:
            if date < today:            # 지난 날짜는 건너뛴다
                continue
            try:
                buses = fetch_route(opener, route["depr_cd"], route["arvl_cd"], date)
            except Exception as e:
                time.sleep(5)
                try:
                    buses = fetch_route(opener, route["depr_cd"], route["arvl_cd"], date)
                except Exception:
                    log("조회 실패 %s %s: %s" % (route.get("name", ""), date, e))
                    # 조회에 실패한 날짜의 이전 상태는 그대로 유지한다.
                    for k, v in state.items():
                        if k.startswith("%s|%s|" % (route.get("name", ""), date)):
                            new_state[k] = v
                    continue

            for b in buses:
                if not (t_from <= b["depart"] <= t_to):
                    continue
                key = "%s|%s|%s|%s" % (route.get("name", ""), date, b["depart"], b["rot_id"])
                avail = b["remaining"] is not None and b["remaining"] >= min_seats
                new_state[key] = 1 if avail else 0
                # 직전 조회에서 좌석이 없었는데 지금 생긴 경우에만 알린다.
                if avail and state.get(key, 0) == 0:
                    found.append((route, date, b))

            log("%s %s: %d편 조회, 여유 %d편"
                % (route.get("name", ""), date, len(buses),
                   sum(1 for b in buses
                       if b["remaining"] and b["remaining"] >= min_seats
                       and t_from <= b["depart"] <= t_to)))

    if first_run:
        log("첫 실행: 현재 상태를 기준으로 저장했습니다. (알림 없음, 여유 %d편)" % len(found))
        found = []

    if found:
        lines = []
        for route, date, b in found:
            lines.append("%s  %s %s  %s %s  잔여 %d/%d석"
                         % (date, b["depart"], route.get("name", ""),
                            b["company"], b["grade"], b["remaining"], b["total"]))
        body = ("매진이던 시외버스에 좌석이 생겼습니다.\n\n"
                + "\n".join(lines)
                + "\n\n예매: https://txbus.t-money.co.kr/\n"
                + "확인시각: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        subject = "[시외버스] 좌석 발생 %d건 - %s" % (len(found), lines[0])

        log("좌석 발생 %d건" % len(found))
        for l in lines:
            log("  " + l)

        if args.dry_run:
            log("(dry-run: 메일 미발송)")
        else:
            try:
                send_gmail(cfg, subject[:120], body)
                log("Gmail 발송 완료 -> %s" % cfg["gmail"]["to"])
            except Exception as e:
                log("Gmail 발송 실패: %s" % e)
                # 메일에 실패했으면 다음 실행에서 다시 알리도록 상태를 되돌린다.
                for route, date, b in found:
                    key = "%s|%s|%s|%s" % (route.get("name", ""), date, b["depart"], b["rot_id"])
                    new_state[key] = 0
            if cfg.get("notify_macos"):
                notify_macos("시외버스 좌석 발생", lines[0])

    if not args.dry_run:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False)
    return 0


def cmd_test_mail(args):
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json 이 없습니다.", file=sys.stderr)
        return 2
    if "gmail" not in cfg:
        env_gmail = gmail_from_env()
        if env_gmail is None:
            print("gmail 설정이 없습니다.", file=sys.stderr)
            return 2
        cfg["gmail"] = env_gmail
    send_gmail(cfg, "[시외버스] 알림 설정 테스트",
               "이 메일이 보이면 Gmail 발송 설정이 정상입니다.\n"
               + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("발송 완료 ->", cfg["gmail"]["to"])
    return 0


def cmd_show(args):
    """설정한 노선의 현재 배차 상태를 그대로 출력한다."""
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        print("config.json 이 없습니다.", file=sys.stderr)
        return 2
    opener = make_opener()
    for route in cfg["routes"]:
        for date in route["dates"]:
            print("\n=== %s  %s ===" % (route.get("name", ""), date))
            for b in fetch_route(opener, route["depr_cd"], route["arvl_cd"], date):
                seat = ("%d/%d석" % (b["remaining"], b["total"])
                        if b["remaining"] is not None else (b["status"] or "예매불가"))
                print("  %s  %-12s %-6s %s" % (b["depart"], b["company"], b["grade"], seat))
    return 0


def main():
    p = argparse.ArgumentParser(description="시외버스 잔여좌석 감시")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="터미널 코드 검색")
    f.add_argument("keyword")
    f.set_defaults(func=cmd_find)

    a = sub.add_parser("arrivals", help="출발지에서 갈 수 있는 도착지 검색")
    a.add_argument("depr_cd")
    a.add_argument("keyword", nargs="?", default="")
    a.set_defaults(func=cmd_arrivals)

    c = sub.add_parser("check", help="1회 조회 후 필요시 메일 발송")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("show", help="현재 배차 상태 출력")
    s.set_defaults(func=cmd_show)

    t = sub.add_parser("test-mail", help="메일 설정 테스트")
    t.set_defaults(func=cmd_test_mail)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
