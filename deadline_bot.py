import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import BotCommand, BufferedInputFile, ChatMemberUpdated, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


def load_config(path="config.env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_config()

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATA_DIR = os.environ.get("DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
SILENT_CHATS = {}
for _part in os.environ.get("SILENT_CHATS", "").split(","):
    if ":" in _part:
        _cid, _label = _part.split(":", 1)
        SILENT_CHATS[int(_cid.strip())] = _label.strip()
_chat_list = [int(x) for x in os.environ.get("CHAT_ID", "").split(",") if x.strip()]
CHAT_IDS = set(_chat_list) - set(SILENT_CHATS)
OWNER_ID = int(os.environ.get("OWNER_ID") or next((c for c in _chat_list if c > 0), 0))
TZ = ZoneInfo("Europe/Moscow")
REPO_URL = "https://github.com/pheisthelena/deadline-bot-public"
CHECK_MINUTES = 120
PUBLIC_DEADLINES = True
NO_DUE_DAYS = 14
ADMINS_ONLY = True
BACKUP_HOUR = 22

WIKI_URL = "https://wiki.cs.hse.ru/Calculus_2_DSBA_2026/2027"
WIKI_GROUP = "253"
WIKI_COURSE_NAME = "Calculus 2"

SMARTLMS_ICS = os.environ.get("SMARTLMS_ICS", "")
SMARTLMS_COURSES = {
    "1125760": "Economics",
}

CALCULUS_KEYS = ("calculus", "калькулюс", "матан", "математический анализ")
DM_KEYS = ("discrete", "дискрет", "dm2", "dm 2", "дм2", "дм 2")
EXCLUDE_KEYS = ("english", "263")
DM_MAX_DAYS = 8

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}

LMS_SUFFIX_RE = re.compile(r"\s*(?:-\s*надо сдать|закрывается|is due|closes)\s*$", re.IGNORECASE)
LMS_OPENS_RE = re.compile(r"(?:открывается|opens)\s*$", re.IGNORECASE)

HW_NUMBER_RE = re.compile(
    r"(?:hw|home\s*work|home\s*assignment|дз|домашн\w*(?:\s+задани\w*)?)[\W_]*(\d+)",
    re.IGNORECASE,
)


def hw_number(title):
    m = HW_NUMBER_RE.search(title)
    return int(m.group(1)) if m else None


def has_key(name, keys):
    name = name.lower()
    return any(k in name for k in keys)


TOKEN_FILE = "token.json"
_creds = None
_creds_lock = threading.Lock()


def save_token(creds):
    tmp = TOKEN_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(creds.to_json())
        os.replace(tmp, TOKEN_FILE)
    except OSError:
        pass


def get_service():
    global _creds
    with _creds_lock:
        creds = _creds
        broken_file = False
        if creds is None and os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except ValueError:
                broken_file = True
        if creds and not creds.valid and creds.refresh_token:
            try:
                creds.refresh(Request())
                save_token(creds)
            except RefreshError:
                creds = None
        if not creds or not creds.valid:
            if not sys.stdin.isatty():
                if broken_file:
                    raise RuntimeError("файл token.json повреждён: пересобери бота в Bothost, "
                                       "а если не поможет, загрузи новый token.json")
                raise RuntimeError("вход в Google истёк, нужен новый token.json")
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
            save_token(creds)
        _creds = creds
    return build("classroom", "v1", credentials=creds, cache_discovery=False)


def list_all(method, key, **params):
    items, token = [], None
    while True:
        resp = method(pageToken=token, **params).execute(num_retries=4)
        items += resp.get(key, [])
        token = resp.get("nextPageToken")
        if not token:
            return items


def fetch_classroom(now):
    svc = get_service()
    items, calc_numbers, calc_dates = [], set(), set()
    courses = list_all(svc.courses().list, "courses",
                       studentId="me", courseStates=["ACTIVE"])
    for course in courses:
        name = course["name"]
        if has_key(name, EXCLUDE_KEYS):
            continue
        is_calc = has_key(name, CALCULUS_KEYS)
        is_dm = has_key(name, DM_KEYS)

        works = list_all(svc.courses().courseWork().list, "courseWork",
                         courseId=course["id"])
        for w in works:
            due = None
            if "dueDate" in w:
                d, t = w["dueDate"], w.get("dueTime", {})
                due = datetime(d["year"], d["month"], d["day"],
                               t.get("hours", 0), t.get("minutes", 0),
                               tzinfo=timezone.utc).astimezone(TZ)
            if due is None or due < now:
                continue
            if is_dm and (due.date() - now.date()).days >= DM_MAX_DAYS:
                continue
            if is_calc:
                number = hw_number(w["title"])
                if number is not None:
                    calc_numbers.add(number)
                else:
                    calc_dates.add(due.date())
            items.append({"due": due, "course": name, "title": w["title"],
                          "url": w.get("alternateLink"), "has_time": True,
                          "number": hw_number(w["title"]) if is_calc else None})
    return items, calc_numbers, calc_dates


def fetch_wiki():
    resp = requests.get(WIKI_URL, timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    found = {}
    tables = soup.find_all("table")
    for table in tables:
        col = None
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if col is None:
                col = next((i for i, c in enumerate(cells)
                            if c.get_text(strip=True).startswith(WIKI_GROUP)), None)
                continue
            if col >= len(cells):
                continue
            cell = cells[col]
            link = next((a for a in cell.find_all("a")
                         if hw_number(a.get_text()) is not None), None)
            date = re.search(r"\((\d{1,2})\.(\d{1,2})\.(\d{2,4})\)", cell.get_text())
            if not link or not date:
                continue
            day, month, year = map(int, date.groups())
            if year < 100:
                year += 2000
            title = link.get_text(strip=True)
            due = datetime(year, month, day, 23, 59, tzinfo=TZ)
            found[(hw_number(title), due)] = {
                "due": due, "course": WIKI_COURSE_NAME, "title": title,
                "url": link.get("href"), "has_time": True, "number": hw_number(title),
            }

    if not found:
        with open("wiki_debug.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        raise RuntimeError(f"домашки не найдены: таблиц {len(tables)}, "
                           f"символов {len(resp.text)}; страница сохранена в wiki_debug.html")
    return list(found.values())


def fetch_smartlms():
    resp = requests.get(SMARTLMS_ICS, timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    text = re.sub(r"\r?\n[ \t]", "", resp.text)
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        props = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                props[key.split(";")[0]] = value.strip()
        start = props.get("DTSTART", "")
        try:
            if "T" in start:
                due = datetime.strptime(start[:15], "%Y%m%dT%H%M%S")
                due = (due.replace(tzinfo=timezone.utc).astimezone(TZ)
                       if start.endswith("Z") else due.replace(tzinfo=TZ))
            else:
                due = datetime.strptime(start[:8], "%Y%m%d").replace(hour=23, minute=59, tzinfo=TZ)
        except ValueError:
            continue
        title = props.get("SUMMARY", "")
        title = title.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        events.append({"due": due, "category": props.get("CATEGORIES", ""), "title": title})
    return events


def smartlms_course(category):
    return next((name for code, name in SMARTLMS_COURSES.items()
                 if category.startswith(code)), None)


MANUAL_FILE = os.path.join(DATA_DIR, "manual_deadlines.json")
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?:\s+(\d{1,2}):(\d{2}))?$")


def load_manual(now):
    if not os.path.exists(MANUAL_FILE):
        return []
    with open(MANUAL_FILE, encoding="utf-8") as f:
        entries = json.load(f)
    entries = [e for e in entries if datetime.fromisoformat(e["due"]) >= now]
    return sorted(entries, key=lambda e: datetime.fromisoformat(e["due"]))


def save_manual(entries):
    with open(MANUAL_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def parse_due(text, now):
    m = DATE_RE.match(text.strip())
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    year = int(year) if year else now.year
    if year < 100:
        year += 2000
    try:
        due = datetime(year, int(month), int(day),
                       int(hour) if hour else 23, int(minute) if minute else 59, tzinfo=TZ)
        if not m.group(3) and due < now:
            due = due.replace(year=year + 1)
    except ValueError:
        return None
    return due


CONTESTS_FILE = os.path.join(DATA_DIR, "contests_seen.json")
CONTEST_LINK_RE = re.compile(r"contest\.yandex\.(?:ru|com)/contest/(\d+)", re.IGNORECASE)
HOMEWORK_RE = re.compile(
    r"(?<![a-zа-яё])(homework|home\s*work|hw|classwork|class\s*work|дз|домашн\w*|"
    r"классворк\w*|классн\w+\s+контест\w*)(?![a-zа-яё])",
    re.IGNORECASE,
)
TEXT_DATE_RE = re.compile(
    r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d.])(?:\D{0,12}?(\d{1,2}):(\d{2}))?"
)


RELATIVE_RE = re.compile(
    r"(послезавтра|завтра|через\s+(?:(\d+)\s+)?(недел\w*|дн\w*|день))", re.IGNORECASE)


def find_date_in_text(text, now):
    found = []
    for m in TEXT_DATE_RE.finditer(text):
        day, month, year, hour, minute = m.groups()
        year = int(year) if year else now.year
        if year < 100:
            year += 2000
        try:
            due = datetime(year, int(month), int(day),
                           int(hour) if hour else 23, int(minute) if minute else 59, tzinfo=TZ)
        except ValueError:
            continue
        if now <= due <= now + timedelta(days=120):
            found.append((m.start(), due, hour is not None))
    for m in RELATIVE_RE.finditer(text):
        word, count, unit = m.group(1).lower(), m.group(2), (m.group(3) or "").lower()
        if word == "завтра":
            days = 1
        elif word == "послезавтра":
            days = 2
        else:
            days = int(count or 1) * (7 if unit.startswith("недел") else 1)
        day = (now + timedelta(days=days)).replace(hour=23, minute=59, second=0, microsecond=0)
        found.append((m.start(), day, False))
    if not found:
        return None, False
    _, due, has_time = min(found, key=lambda x: x[0])
    return due, has_time


def to_datetime(value):
    value = (value or "").strip()
    try:
        if re.fullmatch(r"\d{13}", value):
            return datetime.fromtimestamp(int(value) / 1000, TZ)
        if re.fullmatch(r"\d{10}", value):
            return datetime.fromtimestamp(int(value), TZ)
        if re.match(r"\d{4}-\d\d-\d\dT", value):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(TZ)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def fetch_contest_info(contest_id):
    url = f"https://contest.yandex.ru/contest/{contest_id}/enter/"
    resp = requests.get(url, timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", attrs={"property": "og:title"})
    title = (meta.get("content") if meta else None) or (soup.title.get_text() if soup.title else "")
    title = re.sub(r"\s*[—–-]\s*(Yandex\.?\s*Contest|Яндекс\.?\s*Контест)\s*$", "",
                   title.strip(), flags=re.IGNORECASE)
    if not title or "captcha" in title.lower() or "captcha" in str(getattr(resp, "url", "")).lower():
        return None, None

    times = []
    for tag in soup.find_all("time"):
        for attr in ("timestamp", "datetime", "data-timestamp", "data-time"):
            dt = to_datetime(tag.get(attr))
            if dt:
                times.append(dt)
                break
    if not times:
        for m in re.finditer(r'(?:end|finish)\w*"?\s*[:=]\s*"?(\d{13}|\d{4}-\d\d-\d\dT[\d:.]+Z?)',
                             resp.text, re.IGNORECASE):
            dt = to_datetime(m.group(1))
            if dt:
                times.append(dt)
    end = max(times) if times else None
    if end is None:
        pos = max(resp.text.find("конец"), resp.text.find(">end"))
        print(f"[контест {contest_id}] время окончания не найдено; фрагмент страницы: "
              f"{resp.text[max(pos - 300, 0):pos + 500]!r}", flush=True)
    return title, end


def collect_contests(chat_id, text, links, now):
    ids = []
    for m in CONTEST_LINK_RE.finditer(" ".join([text] + links)):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    if not ids:
        return 0, []
    seen = []
    if os.path.exists(CONTESTS_FILE):
        with open(CONTESTS_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    added, unknown = 0, []
    for cid in ids:
        if cid in seen:
            continue
        url = f"https://contest.yandex.ru/contest/{cid}/enter/"
        try:
            title, end = fetch_contest_info(cid)
        except Exception:
            traceback.print_exc()
            title, end = None, None
        is_homework = bool(HOMEWORK_RE.search(title or "") or HOMEWORK_RE.search(text))
        print(f"[чат {chat_id}] контест {cid}: название {title!r}, конец {end}, "
              f"домашка: {is_homework}", flush=True)
        seen.append(cid)
        if not is_homework:
            if title is None:
                unknown.append(url)
            continue
        due, has_time = (end, True) if end and end >= now else find_date_in_text(text, now)
        entries = load_manual(now)
        entries.append({
            "course": SILENT_CHATS[chat_id], "title": title or f"Контест {cid}", "url": url,
            "contest_id": cid,
            "due": (due or now + timedelta(days=NO_DUE_DAYS)).isoformat(),
            "no_due": due is None, "has_time": has_time,
        })
        save_manual(entries)
        added += 1
    with open(CONTESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    return added, unknown


def refresh_contests(now):
    entries = load_manual(now)
    changed = False
    for e in entries:
        if not e.get("contest_id"):
            continue
        try:
            _, end = fetch_contest_info(e["contest_id"])
        except Exception:
            traceback.print_exc()
            continue
        if end and end >= now and end.isoformat() != e["due"]:
            print(f"[контест {e['contest_id']}] срок изменился: {e['due']} -> {end.isoformat()}",
                  flush=True)
            e["due"], e["no_due"], e["has_time"] = end.isoformat(), False, True
            changed = True
    if changed:
        save_manual(entries)


SUBJECT_ALIASES = [
    ("теорвер", "тв", "твимс", "вероятн", "статист", "probab", "statist", "pts"),
    ("дискр", "дм", "dm", "discrete"),
    ("калькулюс", "матан", "кальк", "матанализ", "calc"),
    ("алгос", "алгоритм", "аисд", "ads", "algorithm"),
    ("эконом", "микро", "макро", "econ", "micro", "macro"),
    ("bmgc", "менеджмент", "бизнес", "business", "management"),
    ("англ", "english"),
    ("истори", "history"),
]


def subject_id(name):
    name = name.lower().replace("ё", "е")
    words = re.findall(r"[a-zа-я]+", name)
    for i, group in enumerate(SUBJECT_ALIASES):
        for key in group:
            if (key in words) if len(key) <= 3 else (key in name):
                return i
    return " ".join(words)


def same_subject(a, b):
    ia, ib = subject_id(a), subject_id(b)
    if isinstance(ia, int) or isinstance(ib, int):
        return ia == ib
    return bool(ia) and bool(ib) and (ia in ib or ib in ia)


def is_duplicate(a, b):
    if not same_subject(a["course"], b["course"]):
        return False
    na, nb = hw_number(a["title"]), hw_number(b["title"])
    if na is not None and nb is not None:
        return na == nb
    if a.get("no_due") or b.get("no_due"):
        return False
    return a["due"].date() == b["due"].date()


def fetch_deadlines(refresh=False):
    now = datetime.now(TZ)
    if refresh:
        try:
            refresh_contests(now)
        except Exception:
            traceback.print_exc()
    warnings = []
    try:
        items, calc_numbers, calc_dates = fetch_classroom(now)
    except Exception as e:
        traceback.print_exc()
        items, calc_numbers, calc_dates = [], set(), set()
        warnings.append(f"⚠️ Не удалось прочитать Google Classroom ({type(e).__name__}: {str(e)[:150]})")

    try:
        for item in fetch_wiki():
            if item["due"] < now:
                continue
            if item["number"] in calc_numbers or item["due"].date() in calc_dates:
                continue
            items.append(item)
    except Exception as e:
        traceback.print_exc()
        warnings.append(f"⚠️ Не удалось прочитать wiki по Calculus 2 ({type(e).__name__}: {str(e)[:200]})")

    if SMARTLMS_ICS:
        try:
            for ev in fetch_smartlms():
                course = smartlms_course(ev["category"])
                if course is None or ev["due"] < now or LMS_OPENS_RE.search(ev["title"]):
                    continue
                items.append({"due": ev["due"], "course": course,
                              "title": LMS_SUFFIX_RE.sub("", ev["title"]),
                              "url": None, "has_time": True})
        except Exception as e:
            traceback.print_exc()
            warnings.append(f"⚠️ Не удалось прочитать календарь SmartLMS ({type(e).__name__})")

    for e in load_manual(now):
        manual = {"due": datetime.fromisoformat(e["due"]), "course": e["course"],
                  "title": e["title"], "url": e.get("url"), "has_time": e.get("has_time", True),
                  "no_due": e.get("no_due", False)}
        twin = next((it for it in items if is_duplicate(it, manual)), None)
        if twin is not None:
            twin.setdefault("also_keys", []).append(stable_key(manual))
            continue
        items.append(manual)

    return sorted(items, key=lambda x: (x.get("no_due", False), x["due"])), "\n".join(warnings)


def format_item(it, today):
    if it.get("no_due"):
        title = escape(it["title"])
        if it["url"]:
            title = f'<a href="{escape(it["url"], quote=True)}">{title}</a>'
        return f"{escape(it['course'])} — {title}: срок не указан"
    due = it["due"]
    days = (due.date() - today).days
    when = "сегодня" if days == 0 else f"через {days} дн."
    date = f"{due:%d.%m.%Y %H:%M}" if it["has_time"] else f"{due:%d.%m.%Y}, время не указано"
    title = escape(it["title"])
    if it["url"]:
        title = f'<a href="{escape(it["url"], quote=True)}">{title}</a>'
    return f"{escape(it['course'])} — {title}: {date} ({when})"


def format_short(it, today, with_date):
    title = escape(it["title"])
    if it["url"]:
        title = f'<a href="{escape(it["url"], quote=True)}">{title}</a>'
    head = f"{escape(it['course'])} — {title}"
    if it.get("no_due"):
        return head
    due = it["due"]
    if not with_date:
        return f"{head}: до {due:%H:%M}" if it["has_time"] else head
    date = f"{due:%d.%m %H:%M}" if it["has_time"] else f"{due:%d.%m}"
    return f"{head}: {date} (через {(due.date() - today).days} дн.)"


DIGEST_SECTIONS = [
    ("Сегодня", lambda d: d <= 0, False),
    ("Завтра", lambda d: d == 1, False),
    ("Ближайшие 7 дней", lambda d: 2 <= d <= 7, True),
    ("Позже", lambda d: d > 7, True),
]


def format_digest(items, warning=None):
    today = datetime.now(TZ).date()
    lines = [f"<b>Учёба · сроки на {today:%d.%m.%Y}</b>"]
    if not items:
        lines += ["", "Активных дедлайнов нет 🎉"]
    for title, fits, with_date in DIGEST_SECTIONS:
        part = [it for it in items
                if not it.get("no_due") and fits((it["due"].date() - today).days)]
        if part:
            lines += ["", f"<b>{title}</b>"] + ["• " + format_short(it, today, with_date) for it in part]
    no_due = [it for it in items if it.get("no_due")]
    if no_due:
        lines += ["", "<b>Срок не указан</b>"] + ["• " + format_short(it, today, True) for it in no_due]
    if warning:
        lines += ["", escape(warning)]
    return "\n".join(lines)


SEEN_FILE = os.path.join(DATA_DIR, "seen_deadlines.json")


def stable_key(it):
    if it.get("number") is not None:
        return f"calc:{it['number']}"
    return f"{it['course']}|{it['title']}"


def item_key(it):
    due = "без срока" if it.get("no_due") else it["due"].isoformat()
    return f"{stable_key(it)}|{due}"


def due_value(it):
    return None if it.get("no_due") else it["due"].isoformat()


def load_seen(now):
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    seen = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            seen[key] = value
            continue
        if key.startswith("calc:"):
            seen[key] = {"due": value, "keep": value}
        else:
            base, _, due = key.rpartition("|")
            record = {"due": None if due == "без срока" else value, "keep": value}
            seen[key] = record
            seen.setdefault(base, record)
    return seen


def save_seen(seen, now):
    limit = now - timedelta(days=2)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v["keep"]) >= limit}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def keys_for(items):
    counts = {}
    for it in items:
        counts[stable_key(it)] = counts.get(stable_key(it), 0) + 1
    return [stable_key(it) if counts[stable_key(it)] == 1 else item_key(it) for it in items]


def find_changes(items, now):
    seen = load_seen(now)
    first_run = seen is None
    seen = seen or {}
    new, changed = [], []
    for it, key in zip(items, keys_for(items)):
        record = seen.get(key)
        twin_record = next((seen[k] for k in it.get("also_keys", []) if k in seen), None)
        if record is None and twin_record is not None:
            old = twin_record["due"]
            if old and not it.get("no_due") and \
                    datetime.fromisoformat(old).date() != it["due"].date():
                changed.append((it, old))
        elif record is None:
            new.append(it)
        elif record["due"] != due_value(it):
            changed.append((it, record["due"]))
        keep = it["due"].isoformat()
        seen[key] = {"due": due_value(it), "keep": keep}
    save_seen(seen, now)
    return ([], []) if first_run else (new, changed)


class SafeBot(Bot):

    async def __call__(self, method, *args, **kwargs):
        chat_id = getattr(method, "chat_id", None)
        if isinstance(chat_id, int) and chat_id < 0 and chat_id not in CHAT_IDS:
            print(f"[предохранитель] заблокирована отправка в чат {chat_id}", flush=True)
            return None
        return await super().__call__(method, *args, **kwargs)


bot = SafeBot(BOT_TOKEN)
dp = Dispatcher()
SEND_OPTIONS = {"parse_mode": "HTML", "disable_web_page_preview": True}


async def build_digest():
    items, warning = await asyncio.to_thread(fetch_deadlines)
    return format_digest(items, warning)


def greeting(chat_id):
    lines = ["Привет, я бот, который сообщает о предстоящих дедлайнах. "
             "Вот список моих команд:", ""]
    if PUBLIC_DEADLINES or chat_id in CHAT_IDS:
        lines.append("/deadlines — показать предстоящие дедлайны")
    if chat_id in CHAT_IDS:
        admins_note = " (только админы чата)" if ADMINS_ONLY and chat_id < 0 else ""
        lines.append("")
        lines.append(f"Изменение списка{admins_note}:")
        lines.append("/add Предмет | Задание | 12.10 23:59 | ссылка — добавить дедлайн вручную "
                     "(ссылку можно не писать)")
        lines.append("/muteadd — то же самое, но без объявления в чатах")
        lines.append("/del — посмотреть и удалить добавленные вручную дедлайны")
        lines.append("/due 1 15.10 23:59 — изменить срок добавленного вручную дедлайна")
        lines.append("/link 1 https://... — добавить ссылку к добавленному вручную дедлайну")
        lines.append("")
    lines.append("/about — как я устроен и где мой код")
    lines.append("/help — показать это сообщение ещё раз")
    if chat_id in CHAT_IDS:
        lines.append("")
        lines.append("Когда появляется новый дедлайн или меняется срок сдачи, "
                     "я сам пишу об этом сюда и показываю обновлённый список.")
    if REPO_URL:
        lines.append("")
        lines.append(f"Описание и исходный код: {REPO_URL}")
    return "\n".join(lines)


def about_text():
    return ("Я собираю дедлайны по домашкам из Google Classroom, SmartLMS, wiki и чатов "
            "преподавателей в один список и сообщаю о новых заданиях.\n\n"
            f"Как я устроен, список возможностей и исходный код: {REPO_URL}\n"
            "Там же есть инструкция, как сделать такого бота для своей группы.")


def can_write(chat):
    return chat.type == "private" or chat.id in CHAT_IDS


async def tell_owner(text):
    if OWNER_ID:
        try:
            await bot.send_message(OWNER_ID, text)
        except Exception:
            traceback.print_exc()


def new_chat_note(chat):
    return (f"Чат «{chat.title}», id: {chat.id}\n"
            "Сейчас я в нём молчу и ничего не делаю.\n"
            f"• Чтобы я писал туда дедлайны: добавь {chat.id} в CHAT_ID.\n"
            f"• Чтобы я молча собирал оттуда задания: добавь в config.env строку "
            f"SILENT_CHATS={chat.id}:Название предмета")


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    if can_write(message.chat):
        await message.answer(greeting(message.chat.id), disable_web_page_preview=True)


@dp.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_added_to_chat(event: ChatMemberUpdated):
    if event.chat.type not in ("group", "supergroup"):
        return
    if event.chat.id in CHAT_IDS:
        await bot.send_message(event.chat.id, greeting(event.chat.id))
    elif event.chat.id not in SILENT_CHATS:
        await tell_owner("Меня добавили в новый чат.\n" + new_chat_note(event.chat))


@dp.message(Command("about"))
async def cmd_about(message: Message):
    if can_write(message.chat):
        await message.answer(about_text(), disable_web_page_preview=True)


@dp.message(Command("id"))
async def cmd_id(message: Message):
    if can_write(message.chat):
        await message.answer(f"Chat id этого чата: {message.chat.id}")
    elif message.chat.id not in SILENT_CHATS:
        await tell_owner(new_chat_note(message.chat))


@dp.message(Command("deadlines"))
async def cmd_deadlines(message: Message):
    if not can_write(message.chat):
        return
    if not PUBLIC_DEADLINES and message.chat.id not in CHAT_IDS:
        return
    await message.answer(await build_digest(), **SEND_OPTIONS)


ADD_HELP = ("Формат: /add Предмет | Задание | 12.10 23:59 | ссылка\n"
            "Время можно не писать, тогда будет 23:59. Ссылку тоже можно не писать.\n"
            "Ссылку можно и спрятать прямо в название задания через форматирование Telegram.\n"
            "/muteadd с тем же форматом добавляет дедлайн без объявления в чатах.")
URL_RE = re.compile(r"https?://[^\s|]+", re.IGNORECASE)


def split_add_args(message: Message, args: str):
    args = re.sub(r"^\s*(?:/\w+(?:@\w+)?\s+)+", "", args)
    url = None
    found = URL_RE.search(args)
    if found:
        url = found.group(0).rstrip(".,;)")
        args = URL_RE.sub("", args)
    if url is None:
        for entity in getattr(message, "entities", None) or []:
            if entity.type == "text_link" and entity.url:
                url = entity.url
                break
    parts = [p.strip() for p in args.split("|")]
    while len(parts) > 3 and not parts[-1]:
        parts.pop()
    if len(parts) != 3:
        return None
    course, title, date_text = parts
    return course, title, date_text, url


def mark_seen(item):
    now = datetime.now(TZ)
    seen = load_seen(now) or {}
    seen[stable_key(item)] = {"due": due_value(item), "keep": item["due"].isoformat()}
    save_seen(seen, now)


_admin_cache = {}


async def can_edit(message: Message):
    if message.chat.id not in CHAT_IDS:
        return False
    if not ADMINS_ONLY:
        return True
    user_id = message.from_user.id if message.from_user else None
    if message.chat.type == "private":
        return user_id == OWNER_ID
    if user_id == OWNER_ID:
        return True
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True
    cached = _admin_cache.get(message.chat.id)
    if cached is None or (datetime.now(TZ) - cached[0]).total_seconds() > 600:
        try:
            admins = await bot.get_chat_administrators(message.chat.id)
            cached = (datetime.now(TZ), {m.user.id for m in admins})
            _admin_cache[message.chat.id] = cached
        except Exception:
            traceback.print_exc()
            cached = (datetime.now(TZ), set())
    if user_id in cached[1]:
        return True
    await message.answer("Добавлять и менять дедлайны могут только админы чата. "
                         "Если заметил новую домашку, напиши старосте или заму.")
    return False


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    await add_deadline(message, command, announce=True)


@dp.message(Command("muteadd"))
async def cmd_muteadd(message: Message, command: CommandObject):
    await add_deadline(message, command, announce=False)


async def add_deadline(message: Message, command: CommandObject, announce: bool):
    if not await can_edit(message):
        return
    now = datetime.now(TZ)
    parsed = split_add_args(message, command.args or "")
    due = parse_due(parsed[2], now) if parsed else None
    if due is None or not parsed[0] or not parsed[1]:
        await message.answer(ADD_HELP)
        return
    course, title, _, url = parsed
    parts = [course, title]
    candidate = {"course": course, "title": title, "due": due}
    try:
        items, _ = await asyncio.to_thread(fetch_deadlines)
        twin = next((it for it in items if is_duplicate(it, candidate)), None)
    except Exception:
        traceback.print_exc()
        twin = None
    if twin is not None:
        await message.answer("Такой дедлайн уже есть в списке, второй раз не добавляю:\n"
                             + format_item(twin, now.date()), **SEND_OPTIONS)
        return
    entries = load_manual(now)
    entry = {"course": course, "title": title, "due": due.isoformat()}
    if url:
        entry["url"] = url
    entries.append(entry)
    link_note = " (со ссылкой)" if url else ""
    if not announce:
        async with check_lock:
            save_manual(entries)
            mark_seen(candidate)
        await message.answer(f"Добавлено без объявления: {course} — {title}: "
                             f"{due:%d.%m.%Y %H:%M}{link_note}")
        return
    save_manual(entries)
    try:
        await check_new_deadlines()
    except Exception:
        traceback.print_exc()
        await message.answer(f"Добавлено: {course} — {title}: {due:%d.%m.%Y %H:%M}{link_note}")


@dp.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject):
    if not await can_edit(message):
        return
    now = datetime.now(TZ)
    entries = load_manual(now)
    number, _, rest = (command.args or "").strip().partition(" ")
    found = URL_RE.search(rest)
    if not number.isdigit() or not 1 <= int(number) <= len(entries) or not found:
        await message.answer("Формат: /link номер ссылка\nНомера смотри в списке /del.")
        return
    e = entries[int(number) - 1]
    e["url"] = found.group(0).rstrip(".,;)")
    save_manual(entries)
    await message.answer(f"Ссылка добавлена: {e['course']} — {e['title']}")


def describe_entry(e):
    link = " (есть ссылка)" if e.get("url") else ""
    if e.get("no_due"):
        return f"{e['course']} — {e['title']}: срок не указан{link}"
    fmt = "%d.%m.%Y %H:%M" if e.get("has_time", True) else "%d.%m.%Y"
    return f"{e['course']} — {e['title']}: {datetime.fromisoformat(e['due']).strftime(fmt)}{link}"


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    if not await can_edit(message):
        return
    entries = load_manual(datetime.now(TZ))
    arg = (command.args or "").strip()
    if arg.isdigit() and 1 <= int(arg) <= len(entries):
        e = entries.pop(int(arg) - 1)
        save_manual(entries)
        await message.answer(f"Удалено: {e['course']} — {e['title']}")
        return
    if not entries:
        await message.answer("Дедлайнов, добавленных вручную или из чатов, нет.")
        return
    lines = ["Дедлайны, добавленные вручную и из чатов:"]
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. {describe_entry(e)}")
    lines.append("")
    lines.append("Удалить: /del 1")
    lines.append("Изменить срок: /due 1 12.10 23:59")
    lines.append("Добавить ссылку: /link 1 https://...")
    await message.answer("\n".join(lines))


@dp.message(Command("due"))
async def cmd_due(message: Message, command: CommandObject):
    if not await can_edit(message):
        return
    now = datetime.now(TZ)
    entries = load_manual(now)
    number, _, date_text = (command.args or "").strip().partition(" ")
    due = parse_due(date_text, now) if date_text else None
    if not number.isdigit() or not 1 <= int(number) <= len(entries) or due is None:
        await message.answer("Формат: /due номер 12.10 23:59\nНомера смотри в списке /del.")
        return
    e = entries[int(number) - 1]
    e["due"], e["no_due"], e["has_time"] = due.isoformat(), False, True
    save_manual(entries)
    try:
        await check_new_deadlines()
    except Exception:
        traceback.print_exc()
        await message.answer(f"Срок изменён: {describe_entry(e)}")


def debug_info():
    svc = get_service()
    lines = ["Курсы в Classroom:"]
    for course in list_all(svc.courses().list, "courses",
                           studentId="me", courseStates=["ACTIVE"]):
        name = course["name"]
        tags = [t for t, keys in (("скрыт", EXCLUDE_KEYS), ("calculus", CALCULUS_KEYS),
                                  ("дм2", DM_KEYS)) if has_key(name, keys)]
        works = list_all(svc.courses().courseWork().list, "courseWork",
                         courseId=course["id"])
        lines.append(f"• {name} [{', '.join(tags) or 'обычный'}], заданий: {len(works)}")
        if "calculus" in tags:
            for w in works:
                d = w.get("dueDate")
                due = f"{d['day']:02}.{d['month']:02}.{d['year']}" if d else "без срока"
                lines.append(f"    – {w['title']} ({due})")
    lines.append("")
    lines.append("Wiki Calculus 2:")
    try:
        for it in sorted(fetch_wiki(), key=lambda x: x["due"]):
            lines.append(f"• {it['title']}: {it['due']:%d.%m.%Y}")
    except Exception as e:
        lines.append(f"ошибка: {type(e).__name__}: {str(e)[:200]}")
    lines.append("")
    lines.append("SmartLMS:")
    if not SMARTLMS_ICS:
        lines.append("ссылка SMARTLMS_ICS не задана")
    else:
        try:
            for ev in sorted(fetch_smartlms(), key=lambda x: x["due"]):
                shown = smartlms_course(ev["category"]) or "скрыт"
                lines.append(f"• {ev['title']}: {ev['due']:%d.%m.%Y %H:%M}, "
                             f"код {ev['category']} [{shown}]")
        except Exception as e:
            lines.append(f"ошибка: {type(e).__name__}")
    return "\n".join(lines)[:4000]


@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id not in CHAT_IDS:
        return
    await message.answer(await asyncio.to_thread(debug_info))


REPORTED_FILE = os.path.join(DATA_DIR, "reported_chats.json")


async def report_unknown_chat(chat):
    if chat.type == "private" or chat.id in CHAT_IDS:
        return
    reported = []
    if os.path.exists(REPORTED_FILE):
        with open(REPORTED_FILE, encoding="utf-8") as f:
            reported = json.load(f)
    if chat.id in reported:
        return
    reported.append(chat.id)
    with open(REPORTED_FILE, "w", encoding="utf-8") as f:
        json.dump(reported, f)
    await tell_owner("Я нахожусь в чате, которого нет в настройках.\n" + new_chat_note(chat))


BACKUP_FORMAT = "deadline-bot-backup"
BACKUP_HASH_FILE = os.path.join(DATA_DIR, "last_backup_hash.txt")


def read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_backup():
    data = {
        "format": BACKUP_FORMAT,
        "created": datetime.now(TZ).isoformat(timespec="minutes"),
        "manual_deadlines": read_json(MANUAL_FILE, []),
        "contests_seen": read_json(CONTESTS_FILE, []),
    }
    content = {k: data[k] for k in ("manual_deadlines", "contests_seen")}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), digest


async def send_backup(force=False):
    if not OWNER_ID:
        return
    payload, digest = make_backup()
    last = open(BACKUP_HASH_FILE).read().strip() if os.path.exists(BACKUP_HASH_FILE) else ""
    if not force and digest == last:
        return
    name = f"deadlines_backup_{datetime.now(TZ):%Y-%m-%d}.json"
    count = len(json.loads(payload)["manual_deadlines"])
    await bot.send_document(
        OWNER_ID, BufferedInputFile(payload, filename=name),
        caption=(f"Резервная копия: дедлайнов, добавленных вручную и из чатов, — {count}.\n"
                 "Если данные на сервере пропадут, просто перешли этот файл мне."))
    with open(BACKUP_HASH_FILE, "w") as f:
        f.write(digest)


async def scheduled_backup():
    try:
        await send_backup()
    except Exception:
        traceback.print_exc()


@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.chat.type == "private" and message.from_user and message.from_user.id == OWNER_ID:
        await send_backup(force=True)


def restore_backup(data):
    if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
        raise ValueError("это не файл резервной копии бота")
    now = datetime.now(TZ)
    entries = load_manual(now)
    known = {(e["course"], e["title"]) for e in entries}
    added = []
    for e in data.get("manual_deadlines", []):
        if (e.get("course"), e.get("title")) in known:
            continue
        if datetime.fromisoformat(e["due"]) < now:
            continue
        entries.append(e)
        known.add((e["course"], e["title"]))
        added.append(e)
    save_manual(entries)
    seen = set(read_json(CONTESTS_FILE, [])) | set(data.get("contests_seen", []))
    with open(CONTESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f)
    for e in added:
        mark_seen({"course": e["course"], "title": e["title"],
                   "due": datetime.fromisoformat(e["due"]), "no_due": e.get("no_due", False)})
    return added


@dp.message(F.document & (F.chat.type == "private"))
async def on_document(message: Message):
    if not message.from_user or message.from_user.id != OWNER_ID:
        return
    if not (message.document.file_name or "").endswith(".json"):
        await message.answer("Чтобы восстановить дедлайны, пришли файл резервной копии (.json).")
        return
    try:
        buffer = await bot.download(message.document)
        data = json.loads(buffer.read().decode("utf-8"))
        async with check_lock:
            added = await asyncio.to_thread(restore_backup, data)
    except Exception as e:
        traceback.print_exc()
        await message.answer(f"Не получилось восстановить: {e}")
        return
    if added:
        lines = [f"Восстановлено дедлайнов: {len(added)}"] + [f"• {describe_entry(e)}" for e in added]
    else:
        lines = ["Нечего восстанавливать: все дедлайны из файла уже есть в списке или их срок прошёл."]
    await message.answer("\n".join(lines))


@dp.message()
async def on_any_message(message: Message):
    if message.chat.id not in SILENT_CHATS:
        await report_unknown_chat(message.chat)
        return
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    links = [e.url for e in entities if e.type == "text_link" and e.url]
    added, unknown = await asyncio.to_thread(
        collect_contests, message.chat.id, text, links, datetime.now(TZ))
    for url in unknown:
        await tell_owner(f"В чате «{SILENT_CHATS[message.chat.id]}» появилась ссылка на контест, "
                         f"но я не смог узнать его название: {url}\n"
                         "Если это домашка, добавь её через /add.")
    if added:
        await scheduled_check()


GREETED_FILE = os.path.join(DATA_DIR, "greeted_chats.json")


async def greet_new_chats():
    greeted = []
    if os.path.exists(GREETED_FILE):
        with open(GREETED_FILE, encoding="utf-8") as f:
            greeted = json.load(f)
    for chat_id in CHAT_IDS:
        if chat_id < 0 and chat_id not in greeted:
            try:
                await bot.send_message(chat_id, greeting(chat_id))
                greeted.append(chat_id)
            except Exception:
                traceback.print_exc()
    with open(GREETED_FILE, "w", encoding="utf-8") as f:
        json.dump(greeted, f)


check_lock = asyncio.Lock()


def describe_shift(old, new):
    delta = new - old
    word = "продлён" if delta.total_seconds() > 0 else "сокращён"
    minutes = round(abs(delta.total_seconds()) / 60)
    if minutes < 60:
        return f"{word} на {minutes} мин"
    days, hours = divmod(round(minutes / 60), 24)
    parts = ([f"{days} дн."] if days else []) + ([f"{hours} ч"] if hours else [])
    return f"{word} на {' '.join(parts)}"


def format_change(it, old):
    title = escape(it["title"])
    if it["url"]:
        title = f'<a href="{escape(it["url"], quote=True)}">{title}</a>'
    head = f"{escape(it['course'])} — {title}"
    if it.get("no_due"):
        return f"{head}: срок теперь не указан"
    fmt = "%d.%m.%Y %H:%M" if it["has_time"] else "%d.%m.%Y"
    new_text = it["due"].strftime(fmt)
    if old is None:
        return f"{head}: срок назначен — {new_text}"
    old_dt = datetime.fromisoformat(old)
    return (f"{head}: было {old_dt.strftime(fmt)}, стало <b>{new_text}</b> "
            f"({describe_shift(old_dt, it['due'])})")


async def check_new_deadlines():
    async with check_lock:
        items, warning = await asyncio.to_thread(fetch_deadlines, True)
        now = datetime.now(TZ)
        new, changed = find_changes(items, now)
        print(f"[{now:%d.%m %H:%M}] проверка: дедлайнов в списке {len(items)}, "
              f"новых {len(new)}, изменённых {len(changed)}", flush=True)
        if not new and not changed:
            return
        parts = []
        if new:
            head = "🆕 <b>Новый дедлайн</b>" if len(new) == 1 else "🆕 <b>Новые дедлайны</b>"
            parts.append("\n".join([head] + [format_item(it, now.date()) for it in new]))
        if changed:
            head = ("🔄 <b>Изменение срока сдачи</b>" if len(changed) == 1
                    else "🔄 <b>Изменения сроков сдачи</b>")
            parts.append("\n".join([head] + [format_change(it, old) for it, old in changed]))
        text = "\n\n".join(parts) + "\n\n" + format_digest(items, warning)
        for chat_id in CHAT_IDS:
            await bot.send_message(chat_id, text, **SEND_OPTIONS)


async def scheduled_check():
    try:
        await check_new_deadlines()
    except Exception:
        traceback.print_exc()


async def main():
    try:
        get_service()
    except Exception:
        traceback.print_exc()
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(scheduled_check, "interval", minutes=CHECK_MINUTES,
                      misfire_grace_time=300, coalesce=True)
    scheduler.add_job(scheduled_backup, "cron", hour=BACKUP_HOUR,
                      misfire_grace_time=3600, coalesce=True)
    scheduler.start()
    await bot.set_my_commands([
        BotCommand(command="deadlines", description="Предстоящие дедлайны"),
        BotCommand(command="add", description="Добавить дедлайн вручную"),
        BotCommand(command="muteadd", description="Добавить дедлайн без объявления"),
        BotCommand(command="del", description="Список добавленных дедлайнов и удаление"),
        BotCommand(command="due", description="Изменить срок добавленного дедлайна"),
        BotCommand(command="link", description="Добавить ссылку к добавленному дедлайну"),
        BotCommand(command="about", description="О боте и исходный код"),
        BotCommand(command="help", description="Список команд"),
    ])
    print(f"Версия бота: тихие чаты + предохранитель + бэкапы. Пишу только в: {sorted(CHAT_IDS)}; "
          f"молча читаю: {sorted(SILENT_CHATS)}", flush=True)
    await greet_new_chats()
    await scheduled_check()
    await bot.delete_webhook()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
