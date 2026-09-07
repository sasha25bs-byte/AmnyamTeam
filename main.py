"""
Rapira Tournament Bot — на pyTelegramBotAPI (без aiogram/pydantic).
Работает и в Pydroid (локальный тест), и на Railway/любом сервере (продакшн).
Зависимости: pip install pyTelegramBotAPI
"""

import os
import sqlite3
import threading
import time
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import telebot
from telebot import types

# ====================== НАСТРОЙКИ ======================

# Для Pydroid можно вписать токен прямо сюда. Для Railway/сервера — оставь пустым
# и задай переменную окружения BOT_TOKEN (так токен не попадёт в репозиторий на GitHub).
HARDCODED_BOT_TOKEN = ""  # <-- для быстрого локального теста, например "123456:ABC-..."
BOT_TOKEN = os.getenv("BOT_TOKEN") or HARDCODED_BOT_TOKEN

DB_PATH = os.getenv("DB_PATH", "rapira_bot.db")
DT_FORMAT = "%d.%m.%Y %H:%M"
MILESTONES = (20, 15, 10, 5)

# Юзернеймы админов-новостников (без @, регистр не важен), которым бот будет писать в личку,
# когда кто-то запустит /find_players. ВАЖНО: каждый из них должен хотя бы раз написать
# что угодно боту в личку (просто /start) ИЛИ в общий чат, где есть бот — иначе Telegram
# не даст боту написать ему первым.
ADMIN_USERNAMES = {"admin_username_1", "admin_username_2"}  # <-- впиши сюда реальные юзернеймы

# Юзернеймы капитанов (без @), которым доступны команды управления турнирами и рассылка.
# Если оставить пустым множество (set()) — командами сможет пользоваться кто угодно в чате.
CAPTAIN_USERNAMES = set()  # <-- например {"my_username", "second_captain"}

if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден токен бота. Впиши его в HARDCODED_BOT_TOKEN в начале файла "
        "(для Pydroid) или задай переменную окружения BOT_TOKEN (для сервера/Railway)."
    )

telebot.apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
db_lock = threading.Lock()

# Простые in-memory состояния диалогов (создание/редактирование турнира)
NEW_STATE = {}   # chat_id -> {"step": "name"/"datetime"/"roster", "data": {...}}
FIND_STATE = {}  # chat_id -> {"step": "time"/"format", "data": {...}}
EDIT_STATE = {}  # chat_id -> {"tournament_id": int, "field": str}
APPLY_STATE = {}  # chat_id -> {"step": "nick"/"game_id"/"role"/"screenshot"/"timezone", "data": {...}}

READY_WORDS = {"готов", "готова", "да", "гот", "+"}
TAKE_WORDS = {"беру", "занимаю", "возьму", "займу"}

# ====================== БАЗА ДАННЫХ ======================


def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock, db_conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                ping_20 INTEGER NOT NULL DEFAULT 0,
                ping_15 INTEGER NOT NULL DEFAULT 0,
                ping_10 INTEGER NOT NULL DEFAULT 0,
                ping_5 INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS roster (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                slot_index INTEGER NOT NULL,
                username TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS seen_users (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                PRIMARY KEY (chat_id, username)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS admin_ids (
                username TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL
            )"""
        )
        conn.commit()


def create_tournament(chat_id, name, start_time, usernames):
    with db_lock, db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tournaments (chat_id, name, start_time) VALUES (?, ?, ?)",
            (chat_id, name, start_time.isoformat()),
        )
        tid = cur.lastrowid
        for i, u in enumerate(usernames):
            conn.execute(
                "INSERT INTO roster (tournament_id, slot_index, username, status) VALUES (?, ?, ?, 'pending')",
                (tid, i, u.lstrip("@").lower()),
            )
        conn.commit()
        return tid


def get_tournament(tid):
    with db_lock, db_conn() as conn:
        row = conn.execute("SELECT * FROM tournaments WHERE id = ?", (tid,)).fetchone()
        return dict(row) if row else None


def get_roster(tid):
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT * FROM roster WHERE tournament_id = ? ORDER BY slot_index", (tid,)).fetchall()
        return [dict(r) for r in rows]


def get_active_tournaments(chat_id=None):
    with db_lock, db_conn() as conn:
        if chat_id is not None:
            rows = conn.execute(
                "SELECT * FROM tournaments WHERE status='active' AND chat_id=? ORDER BY start_time", (chat_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tournaments WHERE status='active' ORDER BY start_time").fetchall()
        return [dict(r) for r in rows]


def update_tournament_field(tid, field, value):
    with db_lock, db_conn() as conn:
        conn.execute(f"UPDATE tournaments SET {field} = ? WHERE id = ?", (value, tid))
        conn.commit()


def replace_roster(tid, usernames):
    with db_lock, db_conn() as conn:
        conn.execute("DELETE FROM roster WHERE tournament_id = ?", (tid,))
        for i, u in enumerate(usernames):
            conn.execute(
                "INSERT INTO roster (tournament_id, slot_index, username, status) VALUES (?, ?, ?, 'pending')",
                (tid, i, u.lstrip("@").lower()),
            )
        for mark in MILESTONES:
            conn.execute(f"UPDATE tournaments SET ping_{mark} = 0 WHERE id = ?", (tid,))
        conn.commit()


def update_roster_status(rid, status, username=None):
    with db_lock, db_conn() as conn:
        if username is not None:
            conn.execute("UPDATE roster SET status = ?, username = ? WHERE id = ?", (status, username.lower(), rid))
        else:
            conn.execute("UPDATE roster SET status = ? WHERE id = ?", (status, rid))
        conn.commit()


def find_roster_by_username(tid, username):
    username = username.lstrip("@").lower()
    for r in get_roster(tid):
        if r["username"] == username:
            return r
    return None


def cancel_tournament(tid):
    update_tournament_field(tid, "status", "cancelled")


def remember_user(chat_id, username):
    if not username:
        return
    with db_lock, db_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_users (chat_id, username) VALUES (?, ?)", (chat_id, username.lower())
        )
        conn.commit()


def get_known_usernames(chat_id, exclude=()):
    exclude = {u.lstrip("@").lower() for u in exclude}
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT username FROM seen_users WHERE chat_id = ?", (chat_id,)).fetchall()
    return [r["username"] for r in rows if r["username"] not in exclude]


def remember_admin_id(username, user_id):
    with db_lock, db_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO admin_ids (username, user_id) VALUES (?, ?)", (username.lower(), user_id))
        conn.commit()


def get_admin_ids():
    """Возвращает список (username, user_id) для тех админов из ADMIN_USERNAMES, чей ID уже известен."""
    with db_lock, db_conn() as conn:
        rows = conn.execute("SELECT username, user_id FROM admin_ids").fetchall()
    known = {r["username"]: r["user_id"] for r in rows}
    found, missing = [], []
    for admin in ADMIN_USERNAMES:
        key = admin.lstrip("@").lower()
        if key in known:
            found.append((key, known[key]))
        else:
            missing.append(key)
    return found, missing


# ====================== ПЛАНИРОВЩИК ======================


def ready_keyboard(tid, rid):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Готов", callback_data=f"ready:{tid}:{rid}"),
        types.InlineKeyboardButton("❌ Не смогу", callback_data=f"cant:{tid}:{rid}"),
    )
    return kb


def take_keyboard(tid, rid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🙋 Занять место", callback_data=f"take:{tid}:{rid}"))
    return kb


def announce_vacancy(t, roster_entry):
    """Помечает слот вакантным и сразу тегает всех известных боту участников чата, кого нет в составе."""
    roster = get_roster(t["id"])
    roster_usernames = [r["username"] for r in roster]
    candidates = get_known_usernames(t["chat_id"], exclude=roster_usernames)
    if candidates:
        mentions = ", ".join(f"@{u}" for u in candidates)
        text = (
            f"⚠️ @{roster_entry['username']} не сможет прийти на «{t['name']}»!\n"
            f"{mentions} — кто готов выйти на замену? 👇"
        )
    else:
        text = (
            f"⚠️ @{roster_entry['username']} не сможет прийти на «{t['name']}»!\n"
            f"Нужна замена, жми кнопку, если готов 👇"
        )
    bot.send_message(t["chat_id"], text, reply_markup=take_keyboard(t["id"], roster_entry["id"]))


def check_tournament(tid):
    t = get_tournament(tid)
    if not t or t["status"] != "active":
        return

    start_time = datetime.fromisoformat(t["start_time"])
    minutes_left = (start_time - datetime.now()).total_seconds() / 60

    if minutes_left <= 0:
        update_tournament_field(tid, "status", "finished")
        return

    roster = get_roster(tid)
    pending = [r for r in roster if r["status"] == "pending"]

    for mark in MILESTONES:
        if minutes_left <= mark and not t[f"ping_{mark}"]:
            update_tournament_field(tid, f"ping_{mark}", 1)
            if pending:
                mentions = ", ".join(f"@{r['username']}" for r in pending)
                bot.send_message(
                    t["chat_id"],
                    f"⏰ До турнира «{t['name']}» осталось {mark} минут!\n{mentions} — подтвердите готовность 👇",
                )
                for r in pending:
                    bot.send_message(
                        t["chat_id"], f"@{r['username']}, ты готов?", reply_markup=ready_keyboard(tid, r["id"])
                    )
            if mark == 5 and pending:
                for r in pending:
                    update_roster_status(r["id"], "vacant")

    roster = get_roster(tid)
    vacant = [r for r in roster if r["status"] == "vacant"]
    if vacant and minutes_left <= 5:
        for r in vacant:
            bot.send_message(
                t["chat_id"],
                f"🚨 «{t['name']}» через {max(int(minutes_left), 0)} мин, а @{r['username']} "
                f"так и не подтвердил(а) готовность!\nКто готов занять место — жми кнопку 👇",
                reply_markup=take_keyboard(tid, r["id"]),
            )


def background_loop():
    """Фоновый поток: раз в минуту проверяет все активные турниры и шлёт напоминания."""
    while True:
        try:
            for t in get_active_tournaments():
                check_tournament(t["id"])
        except Exception as e:
            print(f"Ошибка в фоновом цикле: {e}")
        time.sleep(60)


def schedule_tournament(tid):
    # Сразу проверяем турнир, чтобы не ждать до минуты, если старт совсем скоро.
    # Фоновый поток дальше сам продолжит проверять его раз в минуту.
    check_tournament(tid)


def remove_job(tid):
    # Активные турниры и так фильтруются в самом цикле по статусу в базе — отдельно снимать нечего.
    pass


# ====================== УТИЛИТЫ ======================


def parse_usernames(text):
    raw = text.replace(",", " ").replace("\n", " ").split()
    return [u.lstrip("@") for u in raw if u.strip()]


def tournaments_keyboard(tournaments, prefix):
    kb = types.InlineKeyboardMarkup()
    for t in tournaments:
        kb.add(types.InlineKeyboardButton(f"#{t['id']} {t['name']}", callback_data=f"{prefix}:{t['id']}"))
    return kb


def is_captain(message):
    """Если CAPTAIN_USERNAMES пуст — доступ разрешён всем."""
    if not CAPTAIN_USERNAMES:
        return True
    username = (message.from_user.username or "").lower()
    return username in {c.lstrip("@").lower() for c in CAPTAIN_USERNAMES}


def is_captain_username(username):
    if not CAPTAIN_USERNAMES:
        return True
    return (username or "").lower() in {c.lstrip("@").lower() for c in CAPTAIN_USERNAMES}


def require_captain(message):
    if is_captain(message):
        return True
    bot.reply_to(message, "🚫 Эта команда доступна только капитанам команды.")
    return False


# ====================== КОМАНДЫ ======================


@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    # Если это админ написал боту в личку — сразу запоминаем его ID, чтобы могли слать ему рассылку
    if message.chat.type == "private" and message.from_user and message.from_user.username:
        if message.from_user.username.lower() in {a.lstrip("@").lower() for a in ADMIN_USERNAMES}:
            remember_admin_id(message.from_user.username, message.from_user.id)

    # Диплинк из мини-приложения: https://t.me/<bot>?start=apply
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].strip() == "apply" and message.chat.type == "private":
        start_application(message)
        return

    bot.reply_to(
        message,
        "👋 Я помогаю собирать команду на турниры Rapira Online.\n\n"
        "<b>/new</b> — создать турнир\n"
        "<b>/list</b> — список активных турниров\n"
        "<b>/edit</b> — изменить турнир\n"
        "<b>/cancel_tournament</b> — отменить турнир\n"
        "<b>/find_players</b> — попросить админов найти прак/спарринг (напишу им в личку)\n"
        "<b>/cancel</b> — прервать текущий ввод\n\n"
        "Тегаю состав за 20/15/10/5 минут до старта и сам ищу замену, если кто-то не подтвердился.",
    )


def start_application(message):
    APPLY_STATE[message.chat.id] = {"step": "nick", "data": {}}
    bot.send_message(
        message.chat.id,
        "📋 <b>Заявка в клан Amnyam Team</b>\n\n"
        "Мне нужно от тебя: ник, ID аккаунта, желаемая роль, скриншот статы из игры и часовой пояс.\n"
        "Идём по порядку — сначала ник.\n\n"
        "Как твой ник в игре?",
    )


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    NEW_STATE.pop(message.chat.id, None)
    EDIT_STATE.pop(message.chat.id, None)
    FIND_STATE.pop(message.chat.id, None)
    APPLY_STATE.pop(message.chat.id, None)
    bot.reply_to(message, "Ок, отменил ввод.")


@bot.message_handler(commands=["new"])
def cmd_new(message):
    if not require_captain(message):
        return
    NEW_STATE[message.chat.id] = {"step": "name", "data": {}}
    bot.reply_to(message, "Как называется турнир?")


@bot.message_handler(commands=["list"])
def cmd_list(message):
    tournaments = get_active_tournaments(message.chat.id)
    if not tournaments:
        bot.reply_to(message, "Активных турниров нет. Создай через /new")
        return
    lines = []
    for t in tournaments:
        dt = datetime.fromisoformat(t["start_time"])
        roster = get_roster(t["id"])
        ready = sum(1 for r in roster if r["status"] == "confirmed")
        lines.append(f"#{t['id']} «{t['name']}» — {dt.strftime(DT_FORMAT)} МСК ({ready}/{len(roster)} готовы)")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["edit"])
def cmd_edit(message):
    if not require_captain(message):
        return
    tournaments = get_active_tournaments(message.chat.id)
    if not tournaments:
        bot.reply_to(message, "Активных турниров нет.")
        return
    bot.reply_to(message, "Какой турнир редактируем?", reply_markup=tournaments_keyboard(tournaments, "edit_pick"))


@bot.message_handler(commands=["cancel_tournament"])
def cmd_cancel_tournament(message):
    if not require_captain(message):
        return
    tournaments = get_active_tournaments(message.chat.id)
    if not tournaments:
        bot.reply_to(message, "Активных турниров нет.")
        return
    bot.reply_to(message, "Какой турнир отменить?", reply_markup=tournaments_keyboard(tournaments, "cancel_pick"))


@bot.message_handler(commands=["find_players"])
def cmd_find_players(message):
    if not require_captain(message):
        return
    if not ADMIN_USERNAMES:
        bot.reply_to(message, "Список админов не настроен (ADMIN_USERNAMES пустой в коде).")
        return
    FIND_STATE[message.chat.id] = {"step": "team", "data": {}}
    bot.reply_to(message, "Как называется команда?")


def subject_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Прак", callback_data="find_subject:Прак"))
    kb.add(types.InlineKeyboardButton("Поиск игроков в команду", callback_data="find_subject:Поиск игроков в команду"))
    return kb


# ====================== ДИАЛОГ СОЗДАНИЯ ======================


@bot.middleware_handler(update_types=["message"])
def track_seen_user(bot_instance, message):
    # Молча запоминаем всех, кто пишет в чат — нужно, чтобы знать, кого тегать на замену.
    # Middleware выполняется до обычных хендлеров и не мешает им обрабатывать то же сообщение.
    if message.content_type == "text" and message.from_user and message.from_user.username:
        username = message.from_user.username
        remember_user(message.chat.id, username)
        if username.lower() in {a.lstrip("@").lower() for a in ADMIN_USERNAMES}:
            remember_admin_id(username, message.from_user.id)


@bot.message_handler(func=lambda m: m.chat.id in NEW_STATE and not m.text.startswith("/"), content_types=["text"])
def handle_new_flow(message):
    state = NEW_STATE[message.chat.id]
    step = state["step"]

    if step == "name":
        state["data"]["name"] = message.text.strip()
        state["step"] = "datetime"
        bot.reply_to(message, "Когда старт? Формат: ДД.ММ.ГГГГ ЧЧ:ММ (время по МСК)\nНапример: 07.09.2026 19:00")

    elif step == "datetime":
        try:
            dt = datetime.strptime(message.text.strip(), DT_FORMAT)
        except ValueError:
            bot.reply_to(message, "Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (по МСК), например 07.09.2026 19:00")
            return
        if dt <= datetime.now():
            bot.reply_to(message, "Это время уже прошло, укажи время в будущем.")
            return
        state["data"]["start_time"] = dt
        state["step"] = "roster"
        bot.reply_to(message, "Состав? Пришли @юзернеймы через пробел или с новой строки.")

    elif step == "roster":
        usernames = parse_usernames(message.text)
        if not usernames:
            bot.reply_to(message, "Не нашёл ни одного юзернейма, попробуй ещё раз.")
            return
        data = state["data"]
        tid = create_tournament(message.chat.id, data["name"], data["start_time"], usernames)
        NEW_STATE.pop(message.chat.id, None)
        schedule_tournament(tid)
        mentions = ", ".join(f"@{u}" for u in usernames)
        bot.reply_to(
            message,
            f"✅ Турнир «{data['name']}» создан (ID {tid})\n"
            f"Старт: {data['start_time'].strftime(DT_FORMAT)} МСК\n"
            f"Состав: {mentions}\n\n"
            f"Буду напоминать за 20/15/10/5 минут до старта и сам разберусь с заменами.",
        )


# ====================== ДИАЛОГ РЕДАКТИРОВАНИЯ ======================


@bot.message_handler(func=lambda m: m.chat.id in EDIT_STATE and not m.text.startswith("/"), content_types=["text"])
def handle_edit_flow(message):
    state = EDIT_STATE.pop(message.chat.id)
    tid = state["tournament_id"]
    field = state["field"]

    if field == "roster":
        usernames = parse_usernames(message.text)
        if not usernames:
            bot.reply_to(message, "Не нашёл юзернеймы, попробуй ещё раз.")
            EDIT_STATE[message.chat.id] = state
            return
        replace_roster(tid, usernames)
        bot.reply_to(message, "Состав обновлён, счётчики готовности сброшены.")
    elif field == "start_time":
        try:
            dt = datetime.strptime(message.text.strip(), DT_FORMAT)
        except ValueError:
            bot.reply_to(message, "Не понял дату, попробуй в формате ДД.ММ.ГГГГ ЧЧ:ММ (по МСК)")
            EDIT_STATE[message.chat.id] = state
            return
        update_tournament_field(tid, "start_time", dt.isoformat())
        for mark in MILESTONES:
            update_tournament_field(tid, f"ping_{mark}", 0)
        bot.reply_to(message, "Время обновлено.")
    else:  # name
        update_tournament_field(tid, "name", message.text.strip())
        bot.reply_to(message, "Название обновлено.")

    schedule_tournament(tid)


# ====================== ДИАЛОГ "ИЩЕМ ИГРОКОВ" ======================


def send_find_broadcast(chat_id, data):
    text = (
        f"🔎 Ищем: {data['subject']}\n"
        f"Команда: {data['team']}\n"
        f"🕒 Удобное время: {data['time']} (МСК)\n"
        f"📩 Писать: {data['contact']}"
    )

    found, missing = get_admin_ids()
    sent, failed = [], []
    for username, user_id in found:
        try:
            bot.send_message(user_id, text)
            sent.append(username)
        except Exception:
            failed.append(username)

    report_lines = []
    if sent:
        report_lines.append("✅ Написал в личку: " + ", ".join(f"@{u}" for u in sent))
    if failed:
        report_lines.append("⚠️ Не смог написать (заблокировали бота?): " + ", ".join(f"@{u}" for u in failed))
    if missing:
        report_lines.append(
            "❗ Ещё не знаю ID: " + ", ".join(f"@{u}" for u in missing)
            + " — пусть напишут боту в личку /start хотя бы раз, тогда смогу писать им."
        )
    if not report_lines:
        report_lines.append("Список админов пуст, некому писать.")

    bot.send_message(chat_id, "\n".join(report_lines))


@bot.message_handler(func=lambda m: m.chat.id in FIND_STATE and not m.text.startswith("/"), content_types=["text"])
def handle_find_flow(message):
    state = FIND_STATE[message.chat.id]

    if state["step"] == "team":
        state["data"]["team"] = message.text.strip()
        state["step"] = "subject"
        bot.reply_to(message, "Что ищем?", reply_markup=subject_keyboard())
        return

    if state["step"] == "time":
        state["data"]["time"] = message.text.strip()
        state["step"] = "contact"
        bot.reply_to(message, "Кому писать / куда обращаться по этому вопросу?")
        return

    if state["step"] == "contact":
        state["data"]["contact"] = message.text.strip()
        FIND_STATE.pop(message.chat.id, None)
        send_find_broadcast(message.chat.id, state["data"])


# ====================== ДИАЛОГ "ЗАЯВКА В КЛАН" ======================


@bot.message_handler(func=lambda m: m.chat.id in APPLY_STATE and not m.text.startswith("/"), content_types=["text"])
def handle_apply_flow_text(message):
    state = APPLY_STATE[message.chat.id]
    step = state["step"]

    if step == "nick":
        state["data"]["nick"] = message.text.strip()
        state["step"] = "game_id"
        bot.send_message(message.chat.id, "Какой ID аккаунта?")
        return

    if step == "game_id":
        state["data"]["game_id"] = message.text.strip()
        state["step"] = "role"
        bot.send_message(message.chat.id, "На какую роль претендуешь?")
        return

    if step == "role":
        state["data"]["role"] = message.text.strip()
        state["step"] = "screenshot"
        bot.send_message(message.chat.id, "Пришли скриншот статы из игры (фото).")
        return

    if step == "screenshot":
        bot.send_message(message.chat.id, "Мне нужен именно скриншот — пришли фото статы 🙂")
        return

    if step == "timezone":
        state["data"]["timezone"] = message.text.strip()
        APPLY_STATE.pop(message.chat.id, None)
        send_application(message, state["data"])


@bot.message_handler(func=lambda m: m.chat.id in APPLY_STATE, content_types=["photo"])
def handle_apply_flow_photo(message):
    state = APPLY_STATE[message.chat.id]
    if state["step"] != "screenshot":
        return
    state["data"]["screenshot_file_id"] = message.photo[-1].file_id
    state["step"] = "timezone"
    bot.send_message(message.chat.id, "Принял скриншот. Какой у тебя часовой пояс? (например: МСК, МСК+2)")


def send_application(message, data):
    caption = (
        f"📥 <b>Новая заявка в клан</b>\n"
        f"Ник: {data['nick']}\n"
        f"ID: {data['game_id']}\n"
        f"Роль: {data['role']}\n"
        f"Часовой пояс: {data['timezone']}\n"
        f"От: @{message.from_user.username or '—'} (id {message.from_user.id})"
    )

    found, missing = get_admin_ids()
    sent = []
    for username, admin_id in found:
        try:
            if data.get("screenshot_file_id"):
                bot.send_photo(admin_id, data["screenshot_file_id"], caption=caption)
            else:
                bot.send_message(admin_id, caption)
            sent.append(username)
        except Exception:
            pass

    if sent:
        bot.send_message(message.chat.id, "✅ Заявка отправлена! Ждите ответа от админов клана.")
    else:
        bot.send_message(
            message.chat.id,
            "⚠️ Не получилось никому отправить заявку — админы ещё не писали боту. "
            "Попробуй написать в общий чат команды напрямую.",
        )


# ====================== ПОДТВЕРЖДЕНИЯ ТЕКСТОМ ======================


@bot.message_handler(
    func=lambda m: m.chat.id not in NEW_STATE
    and m.chat.id not in EDIT_STATE
    and m.text
    and m.text.strip().lower() in READY_WORDS,
    content_types=["text"],
)
def text_ready(message):
    username = (message.from_user.username or "").lower()
    if not username:
        return
    for t in get_active_tournaments(message.chat.id):
        entry = find_roster_by_username(t["id"], username)
        if entry and entry["status"] == "pending":
            update_roster_status(entry["id"], "confirmed")
            bot.reply_to(message, f"✅ Принято, @{username} готов к «{t['name']}»!")
            return


@bot.message_handler(
    func=lambda m: m.chat.id not in NEW_STATE
    and m.chat.id not in EDIT_STATE
    and m.text
    and m.text.strip().lower() in TAKE_WORDS,
    content_types=["text"],
)
def text_take(message):
    if not message.from_user.username:
        bot.reply_to(message, "Сначала установи @username в настройках Telegram.")
        return
    for t in get_active_tournaments(message.chat.id):
        roster = get_roster(t["id"])
        vacant = next((r for r in roster if r["status"] == "vacant"), None)
        if vacant:
            old = vacant["username"]
            update_roster_status(vacant["id"], "confirmed", username=message.from_user.username)
            bot.reply_to(message, f"🔁 @{message.from_user.username} занял место вместо @{old} в «{t['name']}»!")
            return
    bot.reply_to(message, "Свободных мест сейчас нет.")


# ====================== CALLBACK-КНОПКИ ======================


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    data = call.data

    if data.startswith("ready:"):
        _, tid_s, rid_s = data.split(":")
        tid, rid = int(tid_s), int(rid_s)
        roster = get_roster(tid)
        entry = next((r for r in roster if r["id"] == rid), None)
        if not entry:
            bot.answer_callback_query(call.id, "Слот не найден", show_alert=True)
            return
        username = (call.from_user.username or "").lower()
        if username != entry["username"]:
            bot.answer_callback_query(call.id, "Это не твой слот 🙂", show_alert=True)
            return
        if entry["status"] == "confirmed":
            bot.answer_callback_query(call.id, "Уже отмечен как готов ✅")
            return
        if entry["status"] == "vacant":
            bot.answer_callback_query(call.id, "Место уже ушло на замену, напиши в чат", show_alert=True)
            return
        update_roster_status(rid, "confirmed")
        bot.edit_message_text(f"✅ @{entry['username']} готов!", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Готовность подтверждена!")

    elif data.startswith("cant:"):
        _, tid_s, rid_s = data.split(":")
        tid, rid = int(tid_s), int(rid_s)
        t = get_tournament(tid)
        roster = get_roster(tid)
        entry = next((r for r in roster if r["id"] == rid), None)
        if not t or not entry:
            bot.answer_callback_query(call.id, "Слот не найден", show_alert=True)
            return
        username = (call.from_user.username or "").lower()
        if username != entry["username"]:
            bot.answer_callback_query(call.id, "Это не твой слот 🙂", show_alert=True)
            return
        if entry["status"] == "vacant":
            bot.answer_callback_query(call.id, "Замена уже ищется")
            return
        if entry["status"] == "confirmed" and entry["username"] != username:
            bot.answer_callback_query(call.id, "Слот уже занят другим игроком", show_alert=True)
            return
        update_roster_status(rid, "vacant")
        bot.edit_message_text(f"❌ @{entry['username']} не сможет прийти", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Понял, ищу замену в чате")
        announce_vacancy(t, entry)

    elif data.startswith("take:"):
        _, tid_s, rid_s = data.split(":")
        tid, rid = int(tid_s), int(rid_s)
        roster = get_roster(tid)
        entry = next((r for r in roster if r["id"] == rid), None)
        if not entry or entry["status"] != "vacant":
            bot.answer_callback_query(call.id, "Это место уже заняли", show_alert=True)
            return
        if not call.from_user.username:
            bot.answer_callback_query(call.id, "Сначала установи @username в настройках Telegram", show_alert=True)
            return
        old = entry["username"]
        update_roster_status(rid, "confirmed", username=call.from_user.username)
        bot.edit_message_text(
            f"🔁 @{call.from_user.username} занял место вместо @{old}", call.message.chat.id, call.message.message_id
        )
        bot.answer_callback_query(call.id, "Место занято, ты в составе!")

    elif data.startswith("edit_pick:"):
        if not is_captain_username(call.from_user.username):
            bot.answer_callback_query(call.id, "🚫 Доступно только капитанам", show_alert=True)
            return
        tid = int(data.split(":")[1])
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Название", callback_data="edit_field:name"))
        kb.add(types.InlineKeyboardButton("Время", callback_data="edit_field:start_time"))
        kb.add(types.InlineKeyboardButton("Состав", callback_data="edit_field:roster"))
        EDIT_STATE[call.message.chat.id] = {"tournament_id": tid, "field": None}
        bot.edit_message_text("Что меняем?", call.message.chat.id, call.message.message_id, reply_markup=kb)
        bot.answer_callback_query(call.id)

    elif data.startswith("edit_field:"):
        if not is_captain_username(call.from_user.username):
            bot.answer_callback_query(call.id, "🚫 Доступно только капитанам", show_alert=True)
            return
        field = data.split(":")[1]
        state = EDIT_STATE.get(call.message.chat.id)
        if not state:
            bot.answer_callback_query(call.id, "Сессия сброшена, начни заново через /edit", show_alert=True)
            return
        state["field"] = field
        prompts = {
            "name": "Введи новое название:",
            "start_time": "Введи новое время в формате ДД.ММ.ГГГГ ЧЧ:ММ (по МСК):",
            "roster": "Пришли новый состав (@юзернеймы через пробел). Счётчики готовности сбросятся.",
        }
        bot.send_message(call.message.chat.id, prompts[field])
        bot.answer_callback_query(call.id)

    elif data.startswith("cancel_pick:"):
        if not is_captain_username(call.from_user.username):
            bot.answer_callback_query(call.id, "🚫 Доступно только капитанам", show_alert=True)
            return
        tid = int(data.split(":")[1])
        cancel_tournament(tid)
        remove_job(tid)
        bot.edit_message_text("Турнир отменён.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif data.startswith("find_subject:"):
        if not is_captain_username(call.from_user.username):
            bot.answer_callback_query(call.id, "🚫 Доступно только капитанам", show_alert=True)
            return
        subject = data.split(":", 1)[1]
        state = FIND_STATE.get(call.message.chat.id)
        if not state or state["step"] != "subject":
            bot.answer_callback_query(call.id, "Сессия сброшена, начни заново через /find_players", show_alert=True)
            return
        state["data"]["subject"] = subject
        state["step"] = "time"
        bot.edit_message_text(f"Что ищем: {subject}", call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "Какое время удобно? (по МСК)")
        bot.answer_callback_query(call.id)


# ====================== ЗАПУСК ======================

WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


def run_webapp_server():
    """Раздаёт статический webapp/index.html, чтобы Railway видел, что порт слушается."""
    port = int(os.getenv("PORT", "8080"))
    handler = partial(SimpleHTTPRequestHandler, directory=WEBAPP_DIR)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"Веб-сервер мини-приложения запущен на порту {port}, раздаю {WEBAPP_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    init_db()
    threading.Thread(target=background_loop, daemon=True).start()
    threading.Thread(target=run_webapp_server, daemon=True).start()
    print("Бот запущен, начинаю polling...")
    bot.infinity_polling(skip_pending=True)
