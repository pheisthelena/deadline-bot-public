"""Телеграм-бот: дедлайны из Google Classroom и с wiki-страницы Calculus 2.

Установка:
    pip install aiogram apscheduler google-api-python-client google-auth-oauthlib beautifulsoup4

Настройки (BOT_TOKEN, CHAT_ID, SMARTLMS_ICS) лежат в файле config.env рядом с ботом,
по одной на строку в виде ИМЯ=значение. Команды export больше не нужны.

Запуск:
    python deadline_bot.py

Рядом с файлом должны лежать config.env, credentials.json и token.json.
"""

import asyncio
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests
from aiogram import Bot, Dispatcher
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import BotCommand, ChatMemberUpdated, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------- Настройки ----------

def load_config(path="config.env"):
    """Читает настройки из файла config.env (строки вида ИМЯ=значение)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            # если переменная уже задана снаружи (export или панель хостинга), она главнее
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_config()

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Папка для файлов, которые бот создаёт сам (на хостинге это /app/data)
DATA_DIR = os.environ.get("DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else ".")
os.makedirs(DATA_DIR, exist_ok=True)
# "Тихие" чаты: бот в них только читает сообщения и НИКОГДА ничего не пишет.
# В config.env: SILENT_CHATS=-1001234567890:ADS,-1009876543210:Python  (id чата:название предмета)
SILENT_CHATS = {}
for _part in os.environ.get("SILENT_CHATS", "").split(","):
    if ":" in _part:
        _cid, _label = _part.split(":", 1)
        SILENT_CHATS[int(_cid.strip())] = _label.strip()
# Чаты, которым бот отвечает и пишет о новых дедлайнах (личный чат и/или чат группы)
_chat_list = [int(x) for x in os.environ.get("CHAT_ID", "").split(",") if x.strip()]
CHAT_IDS = set(_chat_list) - set(SILENT_CHATS)  # тихий чат главнее: туда не пишем никогда
# Владелец бота: ему в личку приходят служебные сообщения (id новых чатов и т.п.)
OWNER_ID = int(os.environ.get("OWNER_ID") or next((c for c in _chat_list if c > 0), 0))
TZ = ZoneInfo("Europe/Moscow")
CHECK_MINUTES = 120  # как часто проверять, не появились ли новые дедлайны
# True: команду /deadlines может вызвать кто угодно (например, одногруппник в личке с ботом).
# False: бот отвечает только в чатах из CHAT_ID.
# Добавлять и удалять дедлайны в любом случае можно только в чатах из CHAT_ID.
PUBLIC_DEADLINES = True
# Сколько дней держать в списке задание из чата, если срок сдачи в сообщении не найден
NO_DUE_DAYS = 14

# Wiki-страница лектора по Calculus 2
WIKI_URL = "https://wiki.cs.hse.ru/Calculus_2_DSBA_2026/2027"
WIKI_GROUP = "253"            # столбец таблицы Homework
WIKI_COURSE_NAME = "Calculus 2"

# SmartLMS: личная ссылка на экспорт календаря (в ней секретный ключ, в код не вставляем)
SMARTLMS_ICS = os.environ.get("SMARTLMS_ICS", "")
# Какие курсы SmartLMS показывать: начало кода курса (поле CATEGORIES) -> название
SMARTLMS_COURSES = {
    "1125760": "Economics",
}

# Как узнать курсы в Classroom: кусочки названия (регистр не важен)
CALCULUS_KEYS = ("calculus", "калькулюс", "матан", "математический анализ")
DM_KEYS = ("discrete", "дискрет", "dm2", "dm 2", "дм2", "дм 2")
EXCLUDE_KEYS = ("english", "263")    # курсы, которые не показываем (есть не у всей группы)
DM_MAX_DAYS = 8               # по ДМ2 показываем домашки со сроком через 7 дней и ближе

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
]

# сайты ВШЭ отвечают 403 на запросы без "браузерных" заголовков
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}

# SmartLMS дописывает к названию "- надо сдать" / "закрывается"; "открывается" - не дедлайн
LMS_SUFFIX_RE = re.compile(r"\s*(?:-\s*надо сдать|закрывается|is due|closes)\s*$", re.IGNORECASE)
LMS_OPENS_RE = re.compile(r"(?:открывается|opens)\s*$", re.IGNORECASE)

HW_NUMBER_RE = re.compile(
    r"(?:hw|home\s*work|home\s*assignment|дз|домашн\w*(?:\s+задани\w*)?)[\W_]*(\d+)",
    re.IGNORECASE,
)


def hw_number(title):
    """Номер домашки из названия: 'HW_5', 'ДЗ 5', 'Homework 5' -> 5; иначе None."""
    m = HW_NUMBER_RE.search(title)
    return int(m.group(1)) if m else None


def has_key(name, keys):
    name = name.lower()
    return any(k in name for k in keys)


# ---------- Google Classroom ----------

def get_service():
    """Возвращает клиент Classroom API; при первом запуске открывает вход в Google."""
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if creds and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())  # обычное продление входа, без браузера
        except Exception:
            creds = None  # вход истёк или отозван, нужен новый
    if not creds or not creds.valid:
        if not sys.stdin.isatty():
            # на сервере браузера нет: войти заново можно только на своём компьютере
            raise RuntimeError("вход в Google истёк, нужен новый token.json")
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
    try:
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    except OSError:
        pass  # на сервере файл может быть только для чтения, это не страшно
    return build("classroom", "v1", credentials=creds)


def list_all(method, key, **params):
    """Собирает все страницы ответа API в один список."""
    items, token = [], None
    while True:
        # num_retries: при временной ошибке Google (5xx) запрос повторяется с паузой
        resp = method(pageToken=token, **params).execute(num_retries=4)
        items += resp.get(key, [])
        token = resp.get("nextPageToken")
        if not token:
            return items


def fetch_classroom(now):
    """Задания из Classroom с будущим сроком (сданные тоже остаются в списке).

    Дополнительно возвращает номера и даты показанных заданий курса Calculus,
    чтобы не дублировать их домашками с wiki.
    """
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
                # API отдаёт срок в UTC, переводим в московское время
                due = datetime(d["year"], d["month"], d["day"],
                               t.get("hours", 0), t.get("minutes", 0),
                               tzinfo=timezone.utc).astimezone(TZ)
            if due is None or due < now:
                continue  # без срока или срок уже прошёл
            if is_dm and (due.date() - now.date()).days >= DM_MAX_DAYS:
                continue  # по ДМ2 далёкие домашки пока не показываем
            if is_calc:
                # запоминаем только то, что реально попало в сводку:
                # домашка с wiki скрывается, только если она уже есть в списке
                number = hw_number(w["title"])
                if number is not None:
                    calc_numbers.add(number)
                else:
                    calc_dates.add(due.date())
            items.append({"due": due, "course": name, "title": w["title"],
                          "url": w.get("alternateLink"), "has_time": True,
                          "number": hw_number(w["title"]) if is_calc else None})
    return items, calc_numbers, calc_dates


# ---------- Wiki по Calculus 2 ----------

def fetch_wiki():
    """Домашки с wiki: во всех таблицах ищем столбец WIKI_GROUP с ссылками HW_N и датами."""
    resp = requests.get(WIKI_URL, timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    found = {}  # (номер, дата) -> домашка; ключ защищает от повторов
    tables = soup.find_all("table")
    for table in tables:
        col = None  # номер столбца нашей группы в этой таблице
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if col is None:
                # ищем строку-заголовок: ячейка, которая начинается с номера группы
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
                continue  # домашка ещё не выложена
            day, month, year = map(int, date.groups())
            if year < 100:
                year += 2000
            title = link.get_text(strip=True)
            # на wiki только дата; срок сдачи всегда 23:59
            due = datetime(year, month, day, 23, 59, tzinfo=TZ)
            found[(hw_number(title), due)] = {
                "due": due, "course": WIKI_COURSE_NAME, "title": title,
                "url": link.get("href"), "has_time": True, "number": hw_number(title),
            }

    if not found:
        # на странице всегда есть хотя бы старые домашки; пусто = страница пришла не та
        with open("wiki_debug.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        raise RuntimeError(f"домашки не найдены: таблиц {len(tables)}, "
                           f"символов {len(resp.text)}; страница сохранена в wiki_debug.html")
    return list(found.values())


# ---------- SmartLMS ----------

def fetch_smartlms():
    """Все события из календаря SmartLMS: срок, код курса, название."""
    resp = requests.get(SMARTLMS_ICS, timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    text = re.sub(r"\r?\n[ \t]", "", resp.text)  # склеиваем перенесённые строки
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
                # время с буквой Z на конце записано в UTC
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


# ---------- Дедлайны, добавленные вручную ----------

MANUAL_FILE = os.path.join(DATA_DIR, "manual_deadlines.json")
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?:\s+(\d{1,2}):(\d{2}))?$")


def load_manual(now):
    """Ручные дедлайны с будущим сроком, по порядку."""
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
    """'12.10', '12.10 18:00', '12.10.2026 18:00' -> дата; без времени ставим 23:59."""
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
            due = due.replace(year=year + 1)  # дата без года уже прошла -> следующий год
    except ValueError:
        return None
    return due


# ---------- Задания из "тихих" чатов (ссылки на Яндекс Контест) ----------

CONTESTS_FILE = os.path.join(DATA_DIR, "contests_seen.json")
CONTEST_LINK_RE = re.compile(r"contest\.yandex\.(?:ru|com)/contest/(\d+)", re.IGNORECASE)
# Слова, по которым контест считается домашним заданием (в названии контеста или в сообщении)
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
    """Срок сдачи из текста сообщения: (дата, указано ли время) или (None, False).

    Понимает '20.10', '20.10 в 18:00', 'через неделю', 'через 3 дня', 'завтра'.
    Если в тексте несколько дат, берётся та, что стоит раньше.
    """
    found = []  # (позиция в тексте, дата, есть ли время)
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
    """Время из атрибута страницы: число секунд/миллисекунд или строка вида 2026-10-14T19:40:00Z."""
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
    """Название и время окончания контеста со страницы входа (она видна без логина).

    Возвращает (название или None, время окончания или None).
    """
    url = f"https://contest.yandex.ru/contest/{contest_id}/enter/"
    resp = requests.get(url, timeout=20, headers=BROWSER_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", attrs={"property": "og:title"})
    title = (meta.get("content") if meta else None) or (soup.title.get_text() if soup.title else "")
    title = re.sub(r"\s*[—–-]\s*(Yandex\.?\s*Contest|Яндекс\.?\s*Контест)\s*$", "",
                   title.strip(), flags=re.IGNORECASE)
    if not title or "captcha" in title.lower() or "captcha" in str(getattr(resp, "url", "")).lower():
        return None, None  # Яндекс показал проверку "я не робот" вместо страницы

    # На странице есть поля "начало" и "конец"; время лежит в тегах <time>
    times = []
    for tag in soup.find_all("time"):
        for attr in ("timestamp", "datetime", "data-timestamp", "data-time"):
            dt = to_datetime(tag.get(attr))
            if dt:
                times.append(dt)
                break
    if not times:
        # запасной вариант: ищем время окончания прямо в коде страницы
        for m in re.finditer(r'(?:end|finish)\w*"?\s*[:=]\s*"?(\d{13}|\d{4}-\d\d-\d\dT[\d:.]+Z?)',
                             resp.text, re.IGNORECASE):
            dt = to_datetime(m.group(1))
            if dt:
                times.append(dt)
    end = max(times) if times else None  # из "начала" и "конца" конец позже
    if end is None:
        # чтобы можно было разобраться, печатаем в логи кусок страницы возле слова "конец"
        pos = max(resp.text.find("конец"), resp.text.find(">end"))
        print(f"[контест {contest_id}] время окончания не найдено; фрагмент страницы: "
              f"{resp.text[max(pos - 300, 0):pos + 500]!r}", flush=True)
    return title, end


def collect_contests(chat_id, text, links, now):
    """Разбирает сообщение из тихого чата.

    Возвращает (сколько заданий добавлено, список ссылок, про которые не удалось понять, дз это или нет).
    """
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
            continue  # этот контест уже разбирали
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
                unknown.append(url)  # название узнать не удалось, а в тексте подсказок нет
            continue
        # срок: сначала время окончания со страницы контеста, потом дата из сообщения
        due, has_time = (end, True) if end and end >= now else find_date_in_text(text, now)
        entries = load_manual(now)
        entries.append({
            "course": SILENT_CHATS[chat_id], "title": title or f"Контест {cid}", "url": url,
            "contest_id": cid,
            # если срока нигде нет, держим задание в списке NO_DUE_DAYS дней
            "due": (due or now + timedelta(days=NO_DUE_DAYS)).isoformat(),
            "no_due": due is None, "has_time": has_time,
        })
        save_manual(entries)
        added += 1
    with open(CONTESTS_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)
    return added, unknown


def refresh_contests(now):
    """Перечитывает время окончания собранных контестов: вдруг преподаватель продлил дедлайн."""
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


# ---------- Дубликаты ----------

# Разные названия одного предмета. Нужны, чтобы понять, что "Теорвер" из /add
# и курс "Probability Theory..." из Classroom — это одно и то же.
# Короткие слова (до 3 букв) должны совпасть с целым словом в названии, длинные — с его частью.
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
    """Номер предмета по списку SUBJECT_ALIASES; если предмета там нет, само название."""
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
    """Одно и то же задание: тот же предмет и тот же номер дз (или, если номера нет, тот же день сдачи)."""
    if not same_subject(a["course"], b["course"]):
        return False
    na, nb = hw_number(a["title"]), hw_number(b["title"])
    if na is not None and nb is not None:
        return na == nb
    if a.get("no_due") or b.get("no_due"):
        return False
    return a["due"].date() == b["due"].date()


# ---------- Сводка ----------

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
        # Classroom недоступен: показываем остальные источники и предупреждение
        traceback.print_exc()
        items, calc_numbers, calc_dates = [], set(), set()
        warnings.append(f"⚠️ Не удалось прочитать Google Classroom ({type(e).__name__}: {str(e)[:150]})")

    try:
        for item in fetch_wiki():
            if item["due"] < now:
                continue
            # домашка уже есть в Classroom -> не дублируем
            if item["number"] in calc_numbers or item["due"].date() in calc_dates:
                continue
            items.append(item)
    except Exception as e:
        traceback.print_exc()  # полный текст ошибки появится в Терминале
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
            # текст ошибки не показываем: в нём может быть ссылка с секретным ключом
            warnings.append(f"⚠️ Не удалось прочитать календарь SmartLMS ({type(e).__name__})")

    for e in load_manual(now):
        manual = {"due": datetime.fromisoformat(e["due"]), "course": e["course"],
                  "title": e["title"], "url": e.get("url"), "has_time": e.get("has_time", True),
                  "no_due": e.get("no_due", False)}
        twin = next((it for it in items if is_duplicate(it, manual)), None)
        if twin is not None:
            # это задание уже пришло из Classroom/wiki/SmartLMS: ручную запись не показываем,
            # а автоматическую считаем "тем же самым" дедлайном (чтобы не объявлять заново)
            twin.setdefault("also_keys", []).append(item_key(manual))
            continue
        items.append(manual)

    # задания без срока показываем в конце списка
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


def format_digest(items, warning=None):
    today = datetime.now(TZ).date()
    lines = [f"<b>Учёба · сроки на {today:%d.%m.%Y}</b>", ""]
    if not items:
        lines.append("Активных дедлайнов нет 🎉")
    for it in items:
        lines.append(format_item(it, today) + "\n")
    if warning:
        lines.append(escape(warning))
    return "\n".join(lines)


# ---------- Новые дедлайны ----------

SEEN_FILE = os.path.join(DATA_DIR, "seen_deadlines.json")


def item_key(it):
    """Ключ, по которому бот помнит, что об этом дедлайне уже сообщал."""
    if it.get("number") is not None:
        # домашка по Calculus: wiki и Classroom считаются одной и той же
        return f"calc:{it['number']}"
    if it.get("no_due"):
        return f"{it['course']}|{it['title']}|без срока"
    return f"{it['course']}|{it['title']}|{it['due'].isoformat()}"


def find_new(items, now):
    """Возвращает дедлайны, которых не было при прошлой проверке, и запоминает текущие."""
    first_run = not os.path.exists(SEEN_FILE)
    seen = {}
    if not first_run:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    new = [it for it in items
           if item_key(it) not in seen and not any(k in seen for k in it.get("also_keys", []))]
    for it in items:
        seen[item_key(it)] = it["due"].isoformat()
    # забываем то, что давно прошло, чтобы файл не рос
    limit = now - timedelta(days=2)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= limit}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)
    # при самом первом запуске ничего не рассылаем: всё и так "новое"
    return [] if first_run else new


# ---------- Telegram ----------

class SafeBot(Bot):
    """Бот с предохранителем: в группы, которых нет в CHAT_ID, не уходит НИ ОДНО сообщение.

    Проверка стоит на самом нижнем уровне, через неё проходит любая отправка,
    поэтому ошибка в остальном коде не может привести к сообщению в чужой чат.
    """

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
    # Запросы к Google и wiki синхронные, поэтому запускаем их в отдельном потоке
    items, warning = await asyncio.to_thread(fetch_deadlines)
    return format_digest(items, warning)


def greeting(chat_id):
    """Приветствие со списком команд, доступных в этом чате."""
    lines = ["Привет, я бот, который сообщает о предстоящих дедлайнах. "
             "Вот список моих команд:", ""]
    if PUBLIC_DEADLINES or chat_id in CHAT_IDS:
        lines.append("/deadlines — показать предстоящие дедлайны")
    if chat_id in CHAT_IDS:
        lines.append("/add Предмет | Задание | 12.10 23:59 — добавить дедлайн вручную")
        lines.append("/del — посмотреть и удалить добавленные вручную дедлайны")
    lines.append("/help — показать это сообщение ещё раз")
    if chat_id in CHAT_IDS:
        lines.append("")
        lines.append("Когда появляется новый дедлайн, я сам пишу об этом сюда "
                     "и показываю обновлённый список.")
    return "\n".join(lines)


def can_write(chat):
    """Можно ли писать в этот чат: в личку да, в группы только из CHAT_ID.

    В тихих и незнакомых группах бот не пишет никогда, даже в ответ на команды.
    """
    return chat.type == "private" or chat.id in CHAT_IDS


async def tell_owner(text):
    """Служебное сообщение владельцу бота в личку."""
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
        await message.answer(greeting(message.chat.id))


@dp.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_added_to_chat(event: ChatMemberUpdated):
    """Бота добавили в групповой чат."""
    if event.chat.type not in ("group", "supergroup"):
        return
    if event.chat.id in CHAT_IDS:
        await bot.send_message(event.chat.id, greeting(event.chat.id))
    elif event.chat.id not in SILENT_CHATS:
        # незнакомый чат: в сам чат не пишем, id сообщаем владельцу в личку
        await tell_owner("Меня добавили в новый чат.\n" + new_chat_note(event.chat))


@dp.message(Command("id"))
async def cmd_id(message: Message):
    # нужен владельцу бота, чтобы добавить чат в CHAT_ID или SILENT_CHATS
    if can_write(message.chat):
        await message.answer(f"Chat id этого чата: {message.chat.id}")
    elif message.chat.id not in SILENT_CHATS:
        await tell_owner(new_chat_note(message.chat))


@dp.message(Command("deadlines"))
async def cmd_deadlines(message: Message):
    if not can_write(message.chat):
        return  # в тихих и незнакомых группах не пишем
    if not PUBLIC_DEADLINES and message.chat.id not in CHAT_IDS:
        return  # чужим чатам бот не отвечает
    await message.answer(await build_digest(), **SEND_OPTIONS)


ADD_HELP = ("Формат: /add Предмет | Задание | 12.10 23:59\n"
            "Время можно не писать, тогда будет 23:59.")


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    if message.chat.id not in CHAT_IDS:
        return
    now = datetime.now(TZ)
    parts = [p.strip() for p in (command.args or "").split("|")]
    due = parse_due(parts[2], now) if len(parts) == 3 else None
    if due is None or not parts[0] or not parts[1]:
        await message.answer(ADD_HELP)
        return
    # проверяем, нет ли уже такого дедлайна в списке
    candidate = {"course": parts[0], "title": parts[1], "due": due}
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
    entries.append({"course": parts[0], "title": parts[1], "due": due.isoformat()})
    save_manual(entries)
    try:
        await check_new_deadlines()  # сразу рассылаем новость и обновлённый список
    except Exception:
        traceback.print_exc()
        await message.answer(f"Добавлено: {parts[0]} — {parts[1]}: {due:%d.%m.%Y %H:%M}")


def describe_entry(e):
    if e.get("no_due"):
        return f"{e['course']} — {e['title']}: срок не указан"
    fmt = "%d.%m.%Y %H:%M" if e.get("has_time", True) else "%d.%m.%Y"
    return f"{e['course']} — {e['title']}: {datetime.fromisoformat(e['due']).strftime(fmt)}"


@dp.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject):
    if message.chat.id not in CHAT_IDS:
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
    await message.answer("\n".join(lines))


@dp.message(Command("due"))
async def cmd_due(message: Message, command: CommandObject):
    """/due 2 15.10 23:59: задаёт срок дедлайну номер 2 из списка /del."""
    if message.chat.id not in CHAT_IDS:
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
        await check_new_deadlines()  # рассылаем обновлённый список
    except Exception:
        traceback.print_exc()
        await message.answer(f"Срок изменён: {describe_entry(e)}")


def debug_info():
    """Служебная информация: какие курсы бот видит и что нашёл на wiki."""
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
    """Незнакомая группа: один раз сообщаем владельцу её id в личку. В саму группу не пишем."""
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


@dp.message()
async def on_any_message(message: Message):
    """Любое другое сообщение. В тихих чатах ищем ссылки на контесты; сами ничего не пишем."""
    if message.chat.id not in SILENT_CHATS:
        await report_unknown_chat(message.chat)
        return
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    links = [e.url for e in entities if e.type == "text_link" and e.url]  # ссылки, спрятанные в текст
    added, unknown = await asyncio.to_thread(
        collect_contests, message.chat.id, text, links, datetime.now(TZ))
    for url in unknown:
        await tell_owner(f"В чате «{SILENT_CHATS[message.chat.id]}» появилась ссылка на контест, "
                         f"но я не смог узнать его название: {url}\n"
                         "Если это домашка, добавь её через /add.")
    if added:
        await scheduled_check()  # рассылаем новость в чаты из CHAT_ID


GREETED_FILE = os.path.join(DATA_DIR, "greeted_chats.json")


async def greet_new_chats():
    """Здоровается в групповых чатах, которые только что добавили в CHAT_ID."""
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


async def check_new_deadlines():
    """Проверяет источники; если появились новые дедлайны, пишет во все чаты из CHAT_ID."""
    async with check_lock:
        items, warning = await asyncio.to_thread(fetch_deadlines, True)
        now = datetime.now(TZ)
        new = find_new(items, now)
        print(f"[{now:%d.%m %H:%M}] проверка: дедлайнов в списке {len(items)}, новых {len(new)}",
              flush=True)
        if not new:
            return
        head = "🆕 <b>Новый дедлайн</b>" if len(new) == 1 else "🆕 <b>Новые дедлайны</b>"
        lines = [head] + [format_item(it, now.date()) for it in new]
        text = "\n".join(lines) + "\n\n" + format_digest(items, warning)
        for chat_id in CHAT_IDS:
            await bot.send_message(chat_id, text, **SEND_OPTIONS)


async def scheduled_check():
    try:
        await check_new_deadlines()
    except Exception:
        traceback.print_exc()  # например, пропал интернет; попробуем в следующий раз


async def main():
    try:
        get_service()  # вход в Google при первом запуске
    except Exception:
        traceback.print_exc()
    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(scheduled_check, "interval", minutes=CHECK_MINUTES,
                      misfire_grace_time=300, coalesce=True)
    scheduler.start()
    # подсказки команд в меню Telegram (кнопка "/" в чате)
    await bot.set_my_commands([
        BotCommand(command="deadlines", description="Предстоящие дедлайны"),
        BotCommand(command="add", description="Добавить дедлайн вручную"),
        BotCommand(command="del", description="Список добавленных дедлайнов и удаление"),
        BotCommand(command="due", description="Изменить срок добавленного дедлайна"),
        BotCommand(command="help", description="Список команд"),
    ])
    print(f"Версия бота: тихие чаты + предохранитель. Пишу только в: {sorted(CHAT_IDS)}; "
          f"молча читаю: {sorted(SILENT_CHATS)}", flush=True)
    await greet_new_chats()
    await scheduled_check()  # первая проверка сразу при запуске
    await bot.delete_webhook()  # на случай, если хостинг включил webhook: мы работаем через polling
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
