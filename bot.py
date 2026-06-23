import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
import datetime as dt_module
from functools import wraps
from zoneinfo import ZoneInfo

from groq import Groq
from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ══════════════════════════════════════════════════════════════════════════════
# Логирование
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Настройки и переменные окружения
# ══════════════════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    logger.critical("TELEGRAM_TOKEN не задан — бот не может запуститься!")
    raise SystemExit(1)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY не задан — распознавание аудио и ИИ-проверка недоступны.")

ADMIN_IDS: list[int] = []
for x in os.environ.get("ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.replace("-", "").isdigit():
        ADMIN_IDS.append(int(x))
if not ADMIN_IDS:
    logger.warning("ADMIN_IDS не заданы — администраторов нет.")

DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
SUMMARY_CHAT_ID = int(os.environ.get("SUMMARY_CHAT_ID", "0")) or (ADMIN_IDS[0] if ADMIN_IDS else 0)

# ── Часовой пояс ──────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("Europe/Chisinau")

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

LATE_THRESHOLD_MIN = 15
MAX_SLOT_DISTANCE_MIN = 180  # Исправление: не привязывать к слоту если прошло > 3 часов
REPORT_COOLDOWN_SEC = 120    # Rate limiting: не чаще раза в 2 минуты
MIN_CLEAN_LEN = 20           # Минимальная длина текста для вызова clean_report

SCHEDULE_A = ["10:00", "12:00", "15:00", "17:00"]
SCHEDULE_B = ["11:00", "13:00", "16:00", "18:00"]
SCHEDULES = {"A": SCHEDULE_A, "B": SCHEDULE_B}

# ══════════════════════════════════════════════════════════════════════════════
# Состояния диалогов
# ══════════════════════════════════════════════════════════════════════════════

(
    ASK_WORKER_ID,
    ASK_LASTNAME,
    ASK_FIRSTNAME,
    ASK_POSITION,
    ASK_GROUP,
    ASK_SCHEDULE,
    ASK_NEEDS_DAILY_FACT,
    ASK_REMOVE_DEPARTMENT,
    ASK_REMOVE_WORKER,
    ASK_DEPARTMENT,
    ASK_REPORT_TIME,
    ASK_LIST_DEPARTMENT,
    ASK_LIST_WORKER,
    ASK_EDIT_FIELD,
    ASK_EDIT_VALUE,
    ASK_EDIT_SCHEDULE,
    ASK_EDIT_DAILY_FACT,
    ASK_EDIT_GROUP_VALUE,
    ASK_ORDER_DEPARTMENT,
    ASK_ORDER_WORKER,
    ASK_ORDER_DIRECTION,
    ASK_EDIT_SORT_ORDER,
) = range(22)

CONVERSATION_TIMEOUT = 300  # 5 минут

# ══════════════════════════════════════════════════════════════════════════════
# Клавиатуры
# ══════════════════════════════════════════════════════════════════════════════

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Сотрудники", "📊 Сводка сейчас"],
        ["➕ Добавить сотрудника", "➖ Удалить сотрудника"],
        ["🏢 Сотрудники отдела", "⏰ Время сводки"],
        ["🆔 ID чата"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
SCHEDULE_KEYBOARD = ReplyKeyboardMarkup([["A", "B"], ["❌ Отмена"]], resize_keyboard=True)
YES_NO_KEYBOARD = ReplyKeyboardMarkup([["Да", "Нет"], ["❌ Отмена"]], resize_keyboard=True)
CANCEL_TEXT = "❌ Отмена"
DIALOG_TEXT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{CANCEL_TEXT}$")


# ══════════════════════════════════════════════════════════════════════════════
# База данных — пул соединений через threading.local
# ══════════════════════════════════════════════════════════════════════════════

_local = threading.local()

def get_db() -> sqlite3.Connection:
    """Возвращает соединение из thread-local пула (один объект на поток)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")   # Лучше для параллельного чтения
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn

def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            telegram_id INTEGER PRIMARY KEY,
            last_name    TEXT NOT NULL,
            first_name   TEXT NOT NULL,
            position     TEXT NOT NULL DEFAULT 'Не указано',
            group_id     INTEGER NOT NULL,
            schedule     TEXT NOT NULL DEFAULT 'A',
            needs_daily_fact INTEGER NOT NULL DEFAULT 1,
            sort_order   INTEGER NOT NULL DEFAULT 0
        )
    """)

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    for col, definition in [
        ("schedule",         "TEXT NOT NULL DEFAULT 'A'"),
        ("needs_daily_fact", "INTEGER NOT NULL DEFAULT 1"),
        ("sort_order",       "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {col} {definition}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id   INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id    INTEGER NOT NULL,
            report_date    TEXT NOT NULL,
            report_type    TEXT NOT NULL,
            slot_time      TEXT,
            received_at    TEXT NOT NULL,
            is_ok          INTEGER NOT NULL,
            is_late        INTEGER NOT NULL DEFAULT 0,
            format_comment TEXT,
            required_action TEXT,
            raw_text       TEXT NOT NULL DEFAULT ''
        )
    """)

    cols_r = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
    if "raw_text" not in cols_r:
        conn.execute("ALTER TABLE reports ADD COLUMN raw_text TEXT NOT NULL DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Хранение незарегистрированных пользователей в БД вместо памяти
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_users (
            telegram_id INTEGER PRIMARY KEY,
            first_name  TEXT,
            last_name   TEXT,
            username    TEXT,
            timestamp   TEXT,
            last_text   TEXT
        )
    """)

    # Rate limiting
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            telegram_id  INTEGER PRIMARY KEY,
            last_report  REAL NOT NULL
        )
    """)

    conn.execute(
        "DELETE FROM workers WHERE last_name LIKE '%Отмена%' "
        "OR first_name LIKE '%Отмена%' OR position LIKE '%Отмена%'"
    )
    conn.commit()
    logger.info("БД инициализирована.")


# ── CRUD работники ────────────────────────────────────────────────────────────

def get_worker(telegram_id: int):
    return get_db().execute(
        "SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()

def get_all_workers():
    return get_db().execute(
        "SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name"
    ).fetchall()

def get_workers_by_position(position: str):
    return get_db().execute(
        "SELECT * FROM workers WHERE lower(position) = lower(?) "
        "ORDER BY sort_order, last_name, first_name",
        (position,),
    ).fetchall()

def upsert_worker(telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order=0):
    conn = get_db()
    conn.execute("""
        INSERT INTO workers (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            last_name=excluded.last_name,
            first_name=excluded.first_name,
            position=excluded.position,
            group_id=excluded.group_id,
            schedule=excluded.schedule,
            needs_daily_fact=excluded.needs_daily_fact,
            sort_order=excluded.sort_order
    """, (telegram_id, last_name, first_name, position, group_id, schedule, int(needs_daily_fact), sort_order))
    conn.commit()

def update_worker_field(telegram_id: int, field: str, value):
    allowed = {"last_name", "first_name", "position", "group_id", "schedule", "needs_daily_fact", "sort_order"}
    if field not in allowed:
        logger.error("Попытка обновить недопустимое поле: %s", field)
        raise ValueError(f"Недопустимое поле: {field}")
    conn = get_db()
    if field == "position":
        conn.execute(
            "UPDATE workers SET position = ?, sort_order = 0 WHERE telegram_id = ?",
            (value, telegram_id)
        )
    else:
        conn.execute(f"UPDATE workers SET {field} = ? WHERE telegram_id = ?", (value, telegram_id))
    conn.commit()

def swap_sort_order(id1: int, id2: int):
    conn = get_db()
    r1 = conn.execute("SELECT sort_order FROM workers WHERE telegram_id = ?", (id1,)).fetchone()
    r2 = conn.execute("SELECT sort_order FROM workers WHERE telegram_id = ?", (id2,)).fetchone()
    if r1 and r2:
        conn.execute("UPDATE workers SET sort_order = ? WHERE telegram_id = ?", (r2["sort_order"], id1))
        conn.execute("UPDATE workers SET sort_order = ? WHERE telegram_id = ?", (r1["sort_order"], id2))
        conn.commit()

def delete_worker(telegram_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM workers WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    return cur.rowcount > 0


# ── CRUD группы ───────────────────────────────────────────────────────────────

def save_group_name(group_id: int, group_name: str):
    conn = get_db()
    conn.execute("""
        INSERT INTO groups (group_id, group_name) VALUES (?, ?)
        ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name
    """, (group_id, group_name))
    conn.commit()

def get_group_name(group_id: int) -> str:
    row = get_db().execute(
        "SELECT group_name FROM groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    return row["group_name"] if row else str(group_id)

async def get_group_name_async(bot, group_id: int) -> str:
    row = get_db().execute(
        "SELECT group_name FROM groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row:
        return row["group_name"]
    try:
        chat = await bot.get_chat(group_id)
        name = chat.title or str(group_id)
        save_group_name(group_id, name)
        return name
    except Exception as e:
        logger.warning("Не удалось получить название группы %s: %s", group_id, e)
        return str(group_id)

def get_all_group_names() -> dict:
    rows = get_db().execute("SELECT group_id, group_name FROM groups").fetchall()
    res = {row["group_id"]: row["group_name"] for row in rows}
    if DEFAULT_GROUP_ID not in res:
        res[DEFAULT_GROUP_ID] = str(DEFAULT_GROUP_ID)
    return res

async def fetch_and_save_group_name(bot, group_id: int) -> str:
    try:
        chat = await bot.get_chat(group_id)
        name = chat.title or str(group_id)
    except Exception as e:
        logger.warning("fetch_and_save_group_name %s: %s", group_id, e)
        name = str(group_id)
    save_group_name(group_id, name)
    return name


# ── CRUD отчёты ───────────────────────────────────────────────────────────────

def save_report(telegram_id, report_date, report_type, slot_time, received_at,
                is_ok, is_late, format_comment, required_action, raw_text="") -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO reports
            (telegram_id, report_date, report_type, slot_time, received_at,
             is_ok, is_late, format_comment, required_action, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (telegram_id, report_date, report_type, slot_time, received_at,
          int(is_ok), int(is_late), format_comment, required_action, raw_text))
    conn.commit()
    return cur.lastrowid

def update_report_text_and_ai(report_id, is_ok, format_comment, required_action, raw_text, received_at):
    conn = get_db()
    conn.execute("""
        UPDATE reports
        SET is_ok=?, format_comment=?, required_action=?, raw_text=?, received_at=?
        WHERE id=?
    """, (int(is_ok), format_comment, required_action, raw_text, received_at, report_id))
    conn.commit()

def get_reports_for_date(report_date: str):
    return get_db().execute(
        "SELECT * FROM reports WHERE report_date = ?", (report_date,)
    ).fetchall()

def get_worker_history_last_week(telegram_id: int) -> str:
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM reports
        WHERE telegram_id = ? AND report_date >= date('now', '-7 days')
        ORDER BY report_date DESC, received_at DESC
    """, (telegram_id,)).fetchall()

    worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if not worker:
        return "Сотрудник не найден."
    if not rows:
        return f"📅 У сотрудника {worker['last_name']} {worker['first_name']} нет отчётов за последние 7 дней."

    lines = [f"📅 История отчётов за неделю — {worker['last_name']} {worker['first_name']}:\n"]
    for r in rows:
        r_type = "Статус" if r["report_type"] == "status" else "Итог дня"
        slot_str = f" за {r['slot_time']}" if r["slot_time"] and r["report_type"] == "status" else ""
        ok_str = "✅ ОК" if r["is_ok"] else f"⚠️ {r['format_comment']}"
        late_str = " ⏰ Опоздание" if r["is_late"] else ""
        lines.append(
            f"📍 {r['report_date']} в {r['received_at']}\n"
            f"   Тип: {r_type}{slot_str}\n"
            f"   Результат: {ok_str}{late_str}\n"
        )
    return "\n".join(lines)


# ── Незарегистрированные пользователи (в БД) ──────────────────────────────────

def save_pending_user(telegram_id: int, first_name: str, last_name: str, username: str, text: str):
    conn = get_db()
    conn.execute("""
        INSERT INTO pending_users (telegram_id, first_name, last_name, username, timestamp, last_text)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            first_name=excluded.first_name, last_name=excluded.last_name,
            username=excluded.username, timestamp=excluded.timestamp, last_text=excluded.last_text
    """, (telegram_id, first_name, last_name, username, datetime.now().isoformat(), text))
    conn.commit()


# ── Rate limiting ─────────────────────────────────────────────────────────────

def check_rate_limit(telegram_id: int) -> bool:
    """Возвращает True если запрос разрешён, False если слишком быстро."""
    conn = get_db()
    row = conn.execute(
        "SELECT last_report FROM rate_limits WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    now_ts = time.time()
    if row and (now_ts - row["last_report"]) < REPORT_COOLDOWN_SEC:
        return False
    conn.execute("""
        INSERT INTO rate_limits (telegram_id, last_report) VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET last_report=excluded.last_report
    """, (telegram_id, now_ts))
    conn.commit()
    return True


# ── Настройки сводок ──────────────────────────────────────────────────────────

def get_scheduled_times() -> list[str]:
    row = get_db().execute(
        "SELECT value FROM settings WHERE key = 'summary_times'"
    ).fetchone()
    if row:
        try:
            return json.loads(row["value"])
        except Exception:
            return ["19:00"]
    return ["19:00"]

def save_scheduled_times(times: list[str]):
    conn = get_db()
    conn.execute("""
        INSERT INTO settings (key, value) VALUES ('summary_times', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (json.dumps(sorted(times)),))
    conn.commit()

def reschedule_summary_jobs(application: Application):
    jq = application.job_queue
    if not jq:
        logger.warning("JobQueue недоступен.")
        return
    for job in jq.get_jobs_by_name("daily_summary"):
        job.schedule_removal()
    for t_str in get_scheduled_times():
        try:
            h, m = map(int, t_str.split(":"))
            jq.run_daily(
                scheduled_summary_callback,
                time=dt_module.time(hour=h, minute=m, tzinfo=LOCAL_TZ),
                days=(0, 1, 2, 3, 4, 5, 6),
                name="daily_summary",
            )
            logger.info("Запланирована сводка на %s", t_str)
        except Exception as e:
            logger.error("Ошибка планирования сводки %s: %s", t_str, e)

async def scheduled_summary_callback(context: ContextTypes.DEFAULT_TYPE):
    date_str = now_local().strftime("%Y-%m-%d")
    text = "⏰ Автоматическая сводка:\n\n" + generate_daily_summary_text(date_str)
    targets = set(ADMIN_IDS)
    if SUMMARY_CHAT_ID:
        targets.add(SUMMARY_CHAT_ID)
    for chat_id in targets:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error("Ошибка отправки автосводки в %s: %s", chat_id, e)


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def require_admin(update: Update) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "Эта кнопка доступна только администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return False
    return True

def menu_for_user(user_id: int):
    return MAIN_MENU if is_admin(user_id) else ReplyKeyboardRemove()

def positions_keyboard(rows):
    positions = sorted({row["position"] for row in rows})
    keyboard = [[p] for p in positions] + [["❌ Отмена"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True), positions

def numbered_workers_keyboard(rows):
    keyboard = [[f"{i}. {r['last_name']} {r['first_name']}"] for i, r in enumerate(rows, 1)]
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ══════════════════════════════════════════════════════════════════════════════
# ИИ (Groq / Whisper / Llama)
# ══════════════════════════════════════════════════════════════════════════════

def normalize_ai_result(data: dict, source_text: str) -> dict:
    text_lower = source_text.lower()
    report_type = str(data.get("report_type", "status")).strip().lower()

    fact_words  = ("факт", "факт дня", "за день", "итог дня", "итоги дня", "сегодня за день", "дневной отчет")
    status_words = ("статус", "сейчас", "на данный момент", "за 10", "за 11", "за 12",
                    "за 13", "за 15", "за 16", "за 17", "за 18")

    if any(w in text_lower for w in fact_words):
        report_type = "daily_fact"
    elif any(w in text_lower for w in status_words):
        report_type = "status"
    elif report_type not in ("status", "daily_fact"):
        report_type = "status"

    is_ok = bool(data.get("is_ok", False))
    issue = str(data.get("issue") or data.get("format_comment") or "").strip()
    required_action = str(data.get("required_action") or "").strip()
    employee_message = str(data.get("employee_message") or "").strip()

    if is_ok:
        format_comment   = "всё ОК"
        required_action  = "ничего не предпринимать"
        employee_message = ""
    else:
        if not issue:
            issue = "есть замечания по отчёту"
        format_comment   = f"не ОК, {issue}"
        required_action  = f"сделал замечание сотруднику: {issue}"
        if not employee_message:
            employee_message = f"В отчёте есть замечание: {issue}. В следующем отчёте исправьте это."

    return {
        "report_type":    report_type,
        "is_ok":          is_ok,
        "format_comment": format_comment,
        "required_action": required_action,
        "employee_message": employee_message,
    }

def find_nearest_slot(schedule: list[str], now: datetime):
    """
    Исправление: возвращает (slot, is_late).
    Если ближайший слот дальше MAX_SLOT_DISTANCE_MIN — возвращает (None, False).
    """
    current_minutes = now.hour * 60 + now.minute
    nearest_slot, nearest_diff = None, None

    for slot in schedule:
        h, m = map(int, slot.split(":"))
        diff = abs(current_minutes - (h * 60 + m))
        if nearest_diff is None or diff < nearest_diff:
            nearest_diff = diff
            nearest_slot = slot

    if nearest_diff is None or nearest_diff > MAX_SLOT_DISTANCE_MIN:
        return None, False

    return nearest_slot, nearest_diff > LATE_THRESHOLD_MIN

def transcribe_audio(file_path: str) -> str:
    if groq_client is None:
        return "Не задан GROQ_API_KEY, аудио не распознано."
    try:
        with open(file_path, "rb") as f:
            transcription = groq_client.audio.transcriptions.create(
                file=(os.path.basename(file_path), f),
                model="whisper-large-v3",
                language="ru",
                response_format="text",
            )
        return transcription.strip()
    except Exception as e:
        logger.error("Ошибка транскрибации аудио: %s", e)
        return f"Ошибка распознавания аудио: {e}"

CHECK_PROMPT_TEMPLATE = """
Ты — опытный прораб строительного объекта.

Твоя задача — проверить отчёт рабочего.
Рабочие могут писать коротко, с ошибками, простыми словами.
Ты должен понимать смысл, а не искать идеальную формулировку.

Главный вопрос:
СДЕЛАЛ ЛИ ЧЕЛОВЕК РАБОТУ ИЛИ НЕТ?

=====================
КАК ОЦЕНИВАТЬ
=====================

Отчёт считается ХОРОШИМ, если понятно:
- какую работу выполнял человек
- с чем он работал
- какой процесс выполнялся
- есть ли проблема

Не требуй обязательно цифры.

Примеры ХОРОШИХ отчётов:
"работал на дробилке" → хорошо, понятно что человек выполнял работу
"стоял на кране, подавал материал" → хорошо
"делал опалубку" → хорошо
"бетон заливали сегодня" → хорошо
"копал траншею 50 метров" → отлично, есть объём

=====================
КОГДА НУЖНЫ ОБЪЁМЫ
=====================
Цифры важны для: земляные работы (метры/кубы), бетон (кубометры), монтаж (количество), кабель (метры).
Но отсутствие цифр НЕ означает плохой отчёт.

=====================
ПЛОХИЕ ОТЧЁТЫ
=====================
Отклонять: "работаю", "в процессе", "нормально", "всё сделал", "на объекте", "занимаюсь"
— если невозможно понять, что именно делал человек.

=====================
ИСПРАВЛЕНИЕ РЕЧИ
=====================
"там это, ковыряли яму" → "Выполнялись земляные работы"
"дробилку гоняли" → "Работа на дробильном оборудовании"

Ты обязан понять смысл сообщения.

=====================
ФОРМАТ
=====================
Ответь только JSON:
{{
  "report_type": "status" или "daily_fact",
  "is_ok": true или false,
  "issue": "что не так",
  "required_action": "что написать сотруднику",
  "employee_message": "короткое сообщение сотруднику"
}}

Отчёт:
{text}
"""

CLEAN_REPORT_PROMPT = """
Ты — технический специалист, который оформляет отчёты строительной бригады.
Тебе дают сообщение рабочего. Рабочий может писать с ошибками, сокращениями и разговорными словами.
Твоя задача: превратить его сообщение в понятный официальный отчёт.

Правила:
1. Не придумывай работу, которой не было.
2. Сохраняй только смысл исходного сообщения.
3. Исправляй ошибки.
4. Убирай слова-паразиты.
5. Делай текст коротким и понятным.

Примеры:
Вход: "с утра там дробилку гоняли щебень делали"
Выход: "Выполнялась работа на дробильном оборудовании, производилась переработка материала."

Вход: "копал там возле склада траншею"
Выход: "Выполнялись земляные работы: разработка траншеи возле склада."

Вход: "всё норм"
Выход: "Отчёт не содержит информации о выполненной работе."

Верни только готовый текст отчёта.

Сообщение рабочего:
{text}
"""

def clean_report(text: str) -> str:
    """Исправление: пропускаем вызов API для коротких/пустых текстов."""
    if groq_client is None or len(text) < MIN_CLEAN_LEN:
        return text
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты преобразуешь сообщения рабочих в официальные отчёты. Верни только готовый текст без комментариев и кавычек."},
                {"role": "user", "content": CLEAN_REPORT_PROMPT.format(text=text)},
            ],
            max_tokens=400,
            temperature=0,
        )
        return response.choices[0].message.content.strip().strip('"').strip("'")
    except Exception as e:
        logger.error("Ошибка clean_report: %s", e)
        return text

def check_status(text: str) -> dict:
    if groq_client is None:
        return normalize_ai_result(
            {"report_type": "status", "is_ok": False, "issue": "GROQ_API_KEY не задан"}, text
        )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай только валидным JSON без Markdown."},
                {"role": "user", "content": CHECK_PROMPT_TEMPLATE.format(text=text)},
            ],
            max_tokens=400,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        return normalize_ai_result(json.loads(raw), text)
    except Exception as e:
        logger.error("Ошибка check_status: %s", e)
        return normalize_ai_result(
            {"report_type": "status", "is_ok": False, "issue": f"Ошибка ИИ: {e}"}, text
        )


# ══════════════════════════════════════════════════════════════════════════════
# Генерация сводки
# ══════════════════════════════════════════════════════════════════════════════

def generate_daily_summary_text(report_date: str) -> str:
    conn = get_db()
    workers = conn.execute(
        "SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name"
    ).fetchall()
    reports = conn.execute(
        "SELECT * FROM reports WHERE report_date = ?", (report_date,)
    ).fetchall()

    reports_by_worker: dict[int, dict] = {}
    for r in reports:
        tid = r["telegram_id"]
        if tid not in reports_by_worker:
            reports_by_worker[tid] = {"status": {}, "daily_fact": []}
        if r["report_type"] == "status":
            reports_by_worker[tid]["status"][r["slot_time"]] = dict(r)
        elif r["report_type"] == "daily_fact":
            reports_by_worker[tid]["daily_fact"].append(dict(r))

    lines = [f"📊 Сводка отчётов за {report_date}", ""]

    workers_by_dept: dict[str, list] = {}
    for w in workers:
        ln = (w["last_name"] or "").lower()
        fn = (w["first_name"] or "").lower()
        dp = (w["position"] or "").lower()
        if any(x in ln or x in fn or x in dp for x in ("отмена", "test", "тест")):
            continue
        dept = w["position"]
        workers_by_dept.setdefault(dept, []).append(w)

    for dept, dept_workers in workers_by_dept.items():
        lines.append(f"🏢 Отдел: {dept}")
        lines.append("──────────────────────────")
        for w in dept_workers:
            tid = w["telegram_id"]
            name = f"{w['last_name']} {w['first_name']}"
            w_reps = reports_by_worker.get(tid, {"status": {}, "daily_fact": []})

            schedule_slots = SCHEDULES.get(w["schedule"], SCHEDULE_A)
            status_segments, issues_list = [], []

            for slot in schedule_slots:
                rep = w_reps["status"].get(slot)
                if rep:
                    icon = "✅" if rep["is_ok"] else "⚠️"
                    late = "⏰" if rep["is_late"] else ""
                    status_segments.append(f"{slot} {icon}{late}")
                    if not rep["is_ok"]:
                        comment = (rep["format_comment"] or "Есть замечание").removeprefix("не ОК, ").removeprefix("не ОК: ")
                        issues_list.append(f"• {slot} — {comment}")
                else:
                    status_segments.append(f"{slot} ❌")

            status_str = " | ".join(status_segments)

            if w["needs_daily_fact"]:
                fact_reps = w_reps["daily_fact"]
                if fact_reps:
                    f_rep = fact_reps[-1]
                    if f_rep["is_ok"]:
                        fact_str = "✅ Сдан"
                    else:
                        comment = (f_rep["format_comment"] or "Есть замечание").removeprefix("не ОК, ").removeprefix("не ОК: ")
                        fact_str = f"⚠️ Замечание ({comment})"
                else:
                    fact_str = "❌ Не отправлен"
            else:
                fact_str = "⚪ Не требуется"

            lines.append(f"👨‍💻 {name}")
            lines.append(f"   ⏱ Статусы:   {status_str}")
            lines.append(f"   📋 Итог дня: {fact_str}")
            if issues_list:
                lines.append("   ⚠️ Замечания:")
                lines.extend(f"     {i}" for i in issues_list)
            lines.append("")
        lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции для отправки сообщений (устранение дублирования)
# ══════════════════════════════════════════════════════════════════════════════

async def copy_media_to_chat(bot, from_chat_id: int, message_id: int, to_chat_id: int) -> int | None:
    """Копирует медиа в указанный чат. Возвращает message_id копии или None."""
    try:
        copied = await bot.copy_message(
            chat_id=to_chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
        )
        return copied.message_id
    except Exception as e:
        logger.warning("Не удалось скопировать медиа в %s: %s", to_chat_id, e)
        return None

async def send_notify(bot, chat_id: int, text: str, inline_kbd, reply_to: int | None = None):
    """Отправляет уведомление об отчёте с inline-кнопками."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=inline_kbd,
            reply_to_message_id=reply_to,
        )
        return True
    except Exception as e:
        logger.error("Ошибка отправки уведомления в %s: %s", chat_id, e)
        return False

async def broadcast_report_notify(
    bot,
    update: Update,
    dest_chat: int,
    notify_text: str,
    inline_kbd,
    is_media: bool,
):
    """
    Отправляет уведомление об отчёте в dest_chat.
    При неудаче — дублирует всем администраторам.
    """
    copied_msg_id = None
    if is_media:
        copied_msg_id = await copy_media_to_chat(
            bot, update.effective_chat.id, update.message.message_id, dest_chat
        )

    success = await send_notify(bot, dest_chat, notify_text, inline_kbd, copied_msg_id)
    if not success:
        for admin_id in ADMIN_IDS:
            adm_copy_id = None
            if is_media:
                adm_copy_id = await copy_media_to_chat(
                    bot, update.effective_chat.id, update.message.message_id, admin_id
                )
            await send_notify(bot, admin_id, notify_text, inline_kbd, adm_copy_id)


# ══════════════════════════════════════════════════════════════════════════════
# Callback-кнопки (изменение оценок)
# ══════════════════════════════════════════════════════════════════════════════

def _build_report_notify_text(report: sqlite3.Row, worker_name: str, gname: str, is_addon: bool, cleaned_text: str) -> str:
    is_ok_emoji = "✅" if report["is_ok"] else "⚠️"
    title = (
        f"📊 {is_ok_emoji} Дополнение к отчёту (обновлен): {worker_name}"
        if is_addon else
        f"📊 {is_ok_emoji} Новый отчёт: {worker_name}"
    )
    raw_label = "🗣 Оригинальный текст (объединённый):" if is_addon else "🗣 Оригинальный текст:"
    return (
        f"{title}\n"
        f"Тип/Статус: {report['slot_time'] or 'Факт дня (Итог)'}\n"
        f"Оценка ИИ: {'ОК' if report['is_ok'] else 'НЕ ОК'}\n"
        f"Комментарий ИИ: {report['format_comment']}\n"
        f"Группа: {gname}\n\n"
        f"📝 Официальный отчёт:\n\"{cleaned_text}\"\n\n"
        f"{raw_label}\n\"{report['raw_text']}\""
    )

def _inline_kbd(report_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Изменить оценку (ОК / НЕ ОК)", callback_data=f"fix_toggle_{report_id}"),
        InlineKeyboardButton("✏️ Изменить комментарий",          callback_data=f"edit_comment_{report_id}"),
    ]])

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("Нет прав администратора.", show_alert=True)
        return

    data = query.data

    # ── Переключение ОК / НЕ ОК ──────────────────────────────────────────────
    if data.startswith("fix_toggle_"):
        report_id = int(data.split("_")[-1])
        conn = get_db()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            conn.close()
            await query.answer("Запись не найдена.", show_alert=True)
            return

        new_ok = 1 - int(report["is_ok"])
        new_comment = "ОК (изменено администратором)" if new_ok else "Замечание (изменено администратором)"
        new_action  = f"Скорректировано администратором @{query.from_user.username or user_id}"

        conn.execute(
            "UPDATE reports SET is_ok=?, format_comment=?, required_action=? WHERE id=?",
            (new_ok, new_comment, new_action, report_id),
        )
        worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (report["telegram_id"],)).fetchone()
        conn.commit()

        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        status_emoji = "✅" if new_ok else "⚠️"

        await query.answer("Оценка скорректирована!")
        new_text = (
            f"🔧 Оценка изменена администратором @{query.from_user.username or user_id}:\n"
            f"Сотрудник: {worker_name}\n"
            f"Дата: {report['report_date']}\n"
            f"Тип: {report['slot_time'] or report['report_type']}\n"
            f"Новый статус: {status_emoji} ({new_comment})"
        )
        try:
            await query.edit_message_text(text=new_text, reply_markup=_inline_kbd(report_id))
        except Exception as e:
            logger.warning("edit_message_text после toggle: %s", e)

    # ── Редактирование комментария ────────────────────────────────────────────
    elif data.startswith("edit_comment_"):
        report_id = int(data.split("_")[-1])
        conn = get_db()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.close()
        if not report:
            await query.answer("Запись не найдена.", show_alert=True)
            return

        context.user_data["editing_comment_report_id"]   = report_id
        context.user_data["editing_comment_chat_id"]     = query.message.chat_id
        context.user_data["editing_comment_message_id"]  = query.message.message_id
        context.user_data["editing_comment_original_text"] = query.message.text

        await query.answer()
        await query.message.reply_text("✏️ Введите новый комментарий ИИ:", reply_markup=CANCEL_KEYBOARD)


# ══════════════════════════════════════════════════════════════════════════════
# Базовые команды
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Привет! Выберите действие:", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Привет! Отправьте видеоотчёт, когда он будет готов.", reply_markup=ReplyKeyboardRemove())

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ID чата: {update.effective_chat.id}",
        reply_markup=menu_for_user(update.effective_user.id),
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Диалог 📋 Сотрудники (список + редактирование)
# ══════════════════════════════════════════════════════════════════════════════

async def list_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел:", reply_markup=kbd)
    return ASK_LIST_DEPARTMENT

async def list_workers_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text(f"Отдел «{position}» не найден.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    context.user_data["list_position"] = position
    context.user_data["list_rows"] = [dict(r) for r in rows]

    group_names = get_all_group_names()
    lines = [f"Сотрудники отдела «{position}»:\n"]
    for i, row in enumerate(rows, 1):
        schedule_str = ", ".join(SCHEDULES.get(row["schedule"], SCHEDULE_A))
        fact = "да" if row["needs_daily_fact"] else "нет"
        gname = group_names.get(row["group_id"], str(row["group_id"]))
        lines.append(
            f"{i}. {row['last_name']} {row['first_name']}\n"
            f"   График: {row['schedule']} ({schedule_str})\n"
            f"   Группа: {gname} | Факт дня: {fact}"
        )
    lines.append("\n👉 Выберите сотрудника по номеру:")
    await update.message.reply_text("\n".join(lines), reply_markup=numbered_workers_keyboard(rows))
    return ASK_LIST_WORKER

async def list_workers_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    rows = context.user_data.get("list_rows", [])
    num_str = raw.split(".")[0].strip()
    if not num_str.isdigit():
        await update.message.reply_text("Выберите сотрудника по номеру.")
        return ASK_LIST_WORKER
    idx = int(num_str) - 1
    if idx < 0 or idx >= len(rows):
        await update.message.reply_text("Номер не найден. Попробуйте ещё раз.")
        return ASK_LIST_WORKER

    worker = rows[idx]
    context.user_data["edit_worker"] = worker
    context.user_data["edit_worker_idx"] = idx

    schedule_str = ", ".join(SCHEDULES.get(worker["schedule"], SCHEDULE_A))
    fact = "да" if worker["needs_daily_fact"] else "нет"
    gname = await get_group_name_async(context.bot, worker["group_id"])
    info = (
        f"👤 {worker['last_name']} {worker['first_name']}\n"
        f"Отдел: {worker['position']}\n"
        f"График: {worker['schedule']} ({schedule_str})\n"
        f"Группа: {gname}\n"
        f"Факт дня: {fact}\n\nЧто хотите сделать?"
    )
    kbd = ReplyKeyboardMarkup([
        ["📅 История за неделю", "✏️ Номер в списке"],
        ["✏️ Изменить фамилию",  "✏️ Изменить имя"],
        ["✏️ Изменить отдел",    "✏️ Изменить график"],
        ["✏️ Изменить группу",   "✏️ Факт дня"],
        ["🔼 Вверх в списке",    "🔽 Вниз в списке"],
        ["❌ Отмена"],
    ], resize_keyboard=True)
    await update.message.reply_text(info, reply_markup=kbd)
    return ASK_EDIT_FIELD

async def list_workers_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    rows   = context.user_data.get("list_rows", [])
    idx    = context.user_data.get("edit_worker_idx", 0)

    if not worker:
        await update.message.reply_text("Ошибка состояния. Начните сначала.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if action == "📅 История за неделю":
        await update.message.reply_text(get_worker_history_last_week(worker["telegram_id"]), reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    if action == "✏️ Номер в списке":
        await update.message.reply_text(
            f"Введите новый порядковый номер для {worker['last_name']} {worker['first_name']}\n"
            f"(сейчас {idx + 1}, всего {len(rows)}):",
            reply_markup=CANCEL_KEYBOARD,
        )
        return ASK_EDIT_SORT_ORDER

    if action in ("🔼 Вверх в списке", "🔽 Вниз в списке"):
        target_idx = idx - 1 if action == "🔼 Вверх в списке" else idx + 1
        if target_idx < 0 or target_idx >= len(rows):
            await update.message.reply_text("Сотрудник уже на краю списка.", reply_markup=MAIN_MENU)
            return ConversationHandler.END
        swap_sort_order(worker["telegram_id"], rows[target_idx]["telegram_id"])
        await update.message.reply_text(f"Порядок изменён для {worker['last_name']}.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    field_map = {
        "✏️ Изменить фамилию": ("last_name",       "Введите новую фамилию:"),
        "✏️ Изменить имя":     ("first_name",       "Введите новое имя:"),
        "✏️ Изменить отдел":   ("position",         "Введите новое название отдела:"),
        "✏️ Изменить группу":  ("group_id",         "Введите новый ID группы Telegram (0 = по умолчанию):"),
        "✏️ Изменить график":  ("schedule",         None),
        "✏️ Факт дня":         ("needs_daily_fact", None),
    }
    if action not in field_map:
        await update.message.reply_text("Выберите действие кнопкой.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    field, prompt = field_map[action]
    context.user_data["edit_field"] = field

    if field == "schedule":
        await update.message.reply_text("Выберите новый график:", reply_markup=SCHEDULE_KEYBOARD)
        return ASK_EDIT_SCHEDULE
    if field == "needs_daily_fact":
        await update.message.reply_text("Нужен ли ежедневный факт дня?", reply_markup=YES_NO_KEYBOARD)
        return ASK_EDIT_DAILY_FACT
    if field == "group_id":
        await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_GROUP_VALUE

    await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
    return ASK_EDIT_VALUE

async def edit_value_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = context.user_data.get("edit_worker")
    field  = context.user_data.get("edit_field")
    update_worker_field(worker["telegram_id"], field, update.message.text.strip())
    await update.message.reply_text("Данные обновлены.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_group_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Введите числовой ID:")
        return ASK_EDIT_GROUP_VALUE
    worker   = context.user_data.get("edit_worker")
    final_id = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    update_worker_field(worker["telegram_id"], "group_id", final_id)
    gname = await fetch_and_save_group_name(context.bot, final_id)
    await update.message.reply_text(f"Группа обновлена: {gname}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_schedule_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        return ASK_EDIT_SCHEDULE
    update_worker_field(context.user_data["edit_worker"]["telegram_id"], "schedule", raw)
    await update.message.reply_text(f"График изменён на {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_daily_fact_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        return ASK_EDIT_DAILY_FACT
    update_worker_field(context.user_data["edit_worker"]["telegram_id"], "needs_daily_fact", 1 if raw == "да" else 0)
    await update.message.reply_text(f"Факт дня изменён: {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_sort_order_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == "❌ Отмена":
        await update.message.reply_text("Изменение отменено.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END
    if not raw.isdigit():
        await update.message.reply_text("Введите положительное целое число:")
        return ASK_EDIT_SORT_ORDER

    worker = context.user_data.get("edit_worker")
    rows   = context.user_data.get("list_rows", [])
    target_num = int(raw)
    num_workers = len(rows)

    if not worker or not rows:
        await update.message.reply_text("Ошибка сессии. Начните сначала.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END
    if target_num < 1 or target_num > num_workers:
        await update.message.reply_text(f"Введите число от 1 до {num_workers}:")
        return ASK_EDIT_SORT_ORDER

    all_dept = [dict(r) for r in get_workers_by_position(worker["position"])]
    worker_to_move = next((w for w in all_dept if w["telegram_id"] == worker["telegram_id"]), None)
    if worker_to_move:
        all_dept = [w for w in all_dept if w["telegram_id"] != worker["telegram_id"]]
        all_dept.insert(target_num - 1, worker_to_move)
        conn = get_db()
        for i, w in enumerate(all_dept):
            conn.execute("UPDATE workers SET sort_order=? WHERE telegram_id=?", (i, w["telegram_id"]))
        conn.commit()
        await update.message.reply_text(
            f"✅ Порядок {worker['last_name']} {worker['first_name']} изменён на #{target_num}.",
            reply_markup=MAIN_MENU,
        )
    else:
        await update.message.reply_text("Сотрудник не найден в отделе.", reply_markup=MAIN_MENU)

    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Диалог ➕ Добавление сотрудника
# ══════════════════════════════════════════════════════════════════════════════

async def add_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Введите Telegram ID сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_WORKER_ID

async def add_worker_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Введите числовой ID:")
        return ASK_WORKER_ID
    context.user_data["new_worker_id"] = int(raw)
    await update.message.reply_text("Введите фамилию:", reply_markup=CANCEL_KEYBOARD)
    return ASK_LASTNAME

async def add_worker_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_name"] = update.message.text.strip()
    await update.message.reply_text("Введите имя:", reply_markup=CANCEL_KEYBOARD)
    return ASK_FIRSTNAME

async def add_worker_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["first_name"] = update.message.text.strip()
    await update.message.reply_text("Введите отдел сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_POSITION

async def add_worker_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text.strip()
    await update.message.reply_text("Введите ID группы Telegram (0 = по умолчанию):", reply_markup=CANCEL_KEYBOARD)
    return ASK_GROUP

async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        return ASK_GROUP
    context.user_data["group_id"] = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    await update.message.reply_text("Выберите график (A или B):", reply_markup=SCHEDULE_KEYBOARD)
    return ASK_SCHEDULE

async def add_worker_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        return ASK_SCHEDULE
    context.user_data["schedule"] = raw
    await update.message.reply_text("Нужно ли присылать ежедневный факт дня?", reply_markup=YES_NO_KEYBOARD)
    return ASK_NEEDS_DAILY_FACT

async def add_worker_needs_daily_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        return ASK_NEEDS_DAILY_FACT
    upsert_worker(
        telegram_id      = context.user_data["new_worker_id"],
        last_name        = context.user_data["last_name"],
        first_name       = context.user_data["first_name"],
        position         = context.user_data["position"],
        group_id         = context.user_data["group_id"],
        schedule         = context.user_data["schedule"],
        needs_daily_fact = (raw == "да"),
    )
    await fetch_and_save_group_name(context.bot, context.user_data["group_id"])
    await update.message.reply_text("✅ Сотрудник добавлен!", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Диалог ➖ Удаление сотрудника
# ══════════════════════════════════════════════════════════════════════════════

async def delete_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел:", reply_markup=kbd)
    return ASK_REMOVE_DEPARTMENT

async def delete_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_workers_by_position(update.message.text.strip())
    if not rows:
        return ConversationHandler.END
    context.user_data["remove_rows"] = [dict(r) for r in rows]
    await update.message.reply_text("Выберите кого удалить:", reply_markup=numbered_workers_keyboard(rows))
    return ASK_REMOVE_WORKER

async def delete_worker_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw  = update.message.text.strip()
    rows = context.user_data.get("remove_rows", [])
    num_str = raw.split(".")[0].strip()
    if not num_str.isdigit():
        return ASK_REMOVE_WORKER
    idx = int(num_str) - 1
    if idx < 0 or idx >= len(rows):
        return ASK_REMOVE_WORKER
    worker = rows[idx]
    delete_worker(worker["telegram_id"])
    await update.message.reply_text(f"Сотрудник {worker['last_name']} удалён.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Просмотр отдела и сводка
# ══════════════════════════════════════════════════════════════════════════════

async def department_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("Нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел:", reply_markup=kbd)
    return ASK_DEPARTMENT

async def department_workers_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text("Сотрудники не найдены.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    lines = [f"📋 Отдел: {position}"]
    lines += [f"• {r['last_name']} {r['first_name']} (ID: {r['telegram_id']})" for r in rows]
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def send_summary_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    date_str = now_local().strftime("%Y-%m-%d")
    text = generate_daily_summary_text(date_str)
    await update.message.reply_text(text, reply_markup=MAIN_MENU)
    if SUMMARY_CHAT_ID and SUMMARY_CHAT_ID != update.effective_chat.id:
        try:
            await context.bot.send_message(chat_id=SUMMARY_CHAT_ID, text=text)
        except Exception as e:
            logger.error("Ошибка отправки сводки в %s: %s", SUMMARY_CHAT_ID, e)


# ══════════════════════════════════════════════════════════════════════════════
# Настройка расписания сводок
# ══════════════════════════════════════════════════════════════════════════════

async def summary_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    times = get_scheduled_times()
    times_str = ", ".join(times) if times else "не настроено"
    kbd = ReplyKeyboardMarkup([
        ["➕ Добавить время", "➖ Удалить время"],
        ["❌ Назад"],
    ], resize_keyboard=True)
    await update.message.reply_text(
        f"⏰ Расписание сводки: {times_str}\n\nВыберите действие:",
        reply_markup=kbd,
    )
    return ASK_REPORT_TIME

async def summary_time_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.strip()
    if action == "❌ Назад":
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    if action == "➕ Добавить время":
        await update.message.reply_text("Введите время в формате ЧЧ:ММ (например, 19:30):", reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_SCHEDULE
    if action == "➖ Удалить время":
        times = get_scheduled_times()
        if not times:
            await update.message.reply_text("Расписание пустое.", reply_markup=MAIN_MENU)
            return ConversationHandler.END
        kbd = ReplyKeyboardMarkup([[t] for t in times] + [["❌ Отмена"]], resize_keyboard=True)
        await update.message.reply_text("Выберите время для удаления:", reply_markup=kbd)
        return ASK_ORDER_DEPARTMENT
    await update.message.reply_text("Нажмите кнопку.")
    return ASK_REPORT_TIME

async def summary_time_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    parts = raw.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await update.message.reply_text("Некорректный формат. Пример: 19:30")
        return ASK_EDIT_SCHEDULE
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        await update.message.reply_text("Часы 0-23, минуты 0-59.")
        return ASK_EDIT_SCHEDULE
    time_str = f"{h:02d}:{m:02d}"
    times = get_scheduled_times()
    if time_str in times:
        await update.message.reply_text("Это время уже в расписании.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    times.append(time_str)
    save_scheduled_times(times)
    reschedule_summary_jobs(context.application)
    await update.message.reply_text(f"✅ Время {time_str} добавлено в расписание!", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def summary_time_del_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == "❌ Отмена":
        await update.message.reply_text("Удаление отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    times = get_scheduled_times()
    if raw not in times:
        await update.message.reply_text("Выберите из списка:")
        return ASK_ORDER_DEPARTMENT
    times.remove(raw)
    save_scheduled_times(times)
    reschedule_summary_jobs(context.application)
    await update.message.reply_text(f"✅ Время {raw} удалено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Приём отчётов — разбит на подфункции
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_admin_comment_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Обрабатывает редактирование комментария ИИ администратором.
    Возвращает True если это был режим редактирования.
    """
    if not (is_admin(update.effective_user.id) and context.user_data.get("editing_comment_report_id")):
        return False

    report_id    = context.user_data.pop("editing_comment_report_id")
    orig_chat_id = context.user_data.pop("editing_comment_chat_id", None)
    orig_msg_id  = context.user_data.pop("editing_comment_message_id", None)

    new_comment = (update.message.text or "").strip()
    if not new_comment or new_comment == "❌ Отмена":
        await update.message.reply_text("Редактирование отменено.", reply_markup=MAIN_MENU)
        return True

    conn = get_db()
    report = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not report:
        conn.close()
        await update.message.reply_text("Запись не найдена.", reply_markup=MAIN_MENU)
        return True

    new_action = f"Комментарий изменён администратором: {new_comment}"
    conn.execute(
        "UPDATE reports SET format_comment=?, required_action=? WHERE id=?",
        (new_comment, new_action, report_id),
    )
    report = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    worker = conn.execute("SELECT * FROM workers WHERE telegram_id=?", (report["telegram_id"],)).fetchone()
    conn.commit()

    worker_name  = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
    status_emoji = "✅" if report["is_ok"] else "⚠️"
    user_id      = update.effective_user.id

    await update.message.reply_text(
        f"✅ Комментарий к отчёту {worker_name} обновлён:\n\"{new_comment}\"",
        reply_markup=MAIN_MENU,
    )

    if orig_chat_id and orig_msg_id:
        try:
            gname = await get_group_name_async(context.bot, (worker["group_id"] if worker else 0) or DEFAULT_GROUP_ID)
            is_addon = "[Дополнение]:" in (report["raw_text"] or "")
            cleaned  = clean_report(report["raw_text"] or "")

            updated_text = _build_report_notify_text(report, worker_name, gname, is_addon, cleaned)
            await context.bot.edit_message_text(
                chat_id=orig_chat_id,
                message_id=orig_msg_id,
                text=updated_text,
                reply_markup=_inline_kbd(report_id),
            )
        except Exception as e:
            logger.warning("Ошибка обновления сообщения после правки комментария: %s", e)
    return True

async def _extract_text_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str | None]:
    """
    Извлекает текст из сообщения (текст / голос / видео).
    Возвращает (text_content, tmp_path). tmp_path нужно удалить в finally.
    """
    if update.message.text:
        return update.message.text.strip(), None

    file_obj = update.message.voice or update.message.video or update.message.video_note
    if not file_obj:
        return "", None

    await update.message.reply_text("🎙 Отчёт получен, транскрибируем...")
    tg_file = await context.bot.get_file(file_obj.file_id)
    ext = "ogg" if update.message.voice else "mp4"

    os.makedirs("tmp", exist_ok=True)
    tmp_path = f"tmp/file_{update.effective_user.id}_{int(time.time())}.{ext}"
    await tg_file.download_to_drive(tmp_path)

    text = transcribe_audio(tmp_path)
    return text, tmp_path

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик входящих отчётов."""

    # 1. Редактирование комментария администратором
    if await _handle_admin_comment_edit(update, context):
        return

    user_id = update.effective_user.id

    # 2. Администраторы не отправляют отчёты — игнорируем их сообщения
    #    (кроме случая редактирования комментария, который обработан выше)
    if is_admin(user_id):
        return

    # 3. Rate limiting
    if not check_rate_limit(user_id):
        await update.message.reply_text(
            f"⏳ Вы отправляете отчёты слишком часто. Подождите {REPORT_COOLDOWN_SEC} секунд."
        )
        return

    tmp_path = None
    try:
        # 3. Извлечение текста
        text_content, tmp_path = await _extract_text_from_message(update, context)
        if not text_content:
            await update.message.reply_text("❌ Не удалось распознать содержимое отчёта.")
            return

        is_media = bool(update.message.voice or update.message.video or update.message.video_note)

        # 4. Незарегистрированный пользователь
        worker = get_worker(user_id)
        if not worker:
            u = update.effective_user
            save_pending_user(user_id, u.first_name or "", u.last_name or "", u.username or "", text_content)

            admin_msg = (
                f"👤 Отчёт от незарегистрированного пользователя!\n"
                f"TG ID: {user_id}\n"
                f"Имя: {u.first_name} {u.last_name} (@{u.username})\n"
                f"Текст:\n\"{text_content[:300]}\"\n\n"
                f"Добавьте его через меню, указав ID."
            )
            for admin_id in ADMIN_IDS:
                try:
                    adm_copy = await copy_media_to_chat(context.bot, update.effective_chat.id, update.message.message_id, admin_id) if is_media else None
                    await context.bot.send_message(chat_id=admin_id, text=admin_msg, reply_to_message_id=adm_copy)
                except Exception as e:
                    logger.warning("Ошибка уведомления админа %s: %s", admin_id, e)

            await update.message.reply_text("❌ Вы не зарегистрированы. Ваш отчёт передан администраторам.")
            return

        # 5. ИИ-классификация и определение слота
        ai_res_pre = check_status(text_content)
        report_type = ai_res_pre["report_type"]
        now = now_local()
        date_str = now.strftime("%Y-%m-%d")
        sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
        nearest_slot, is_late = find_nearest_slot(sched_list, now)

        # 6. Дополнение или новый отчёт
        conn = get_db()
        if report_type == "status":
            existing = conn.execute(
                "SELECT * FROM reports WHERE telegram_id=? AND report_date=? AND report_type='status' AND slot_time=?",
                (user_id, date_str, nearest_slot),
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT * FROM reports WHERE telegram_id=? AND report_date=? AND report_type='daily_fact'",
                (user_id, date_str),
            ).fetchone()

        is_addon = bool(existing)
        if is_addon:
            existing_raw = existing["raw_text"] or ""
            text_content = f"{existing_raw}\n[Дополнение]: {text_content}" if existing_raw else text_content
            ai_res = check_status(text_content)
            cleaned_text = clean_report(text_content)
            report_id = existing["id"]
            update_report_text_and_ai(
                report_id=report_id,
                is_ok=ai_res["is_ok"],
                format_comment=ai_res["format_comment"],
                required_action=ai_res["required_action"],
                raw_text=text_content,
                received_at=now.strftime("%H:%M:%S"),
            )
        else:
            ai_res = ai_res_pre
            cleaned_text = clean_report(text_content)
            report_id = save_report(
                telegram_id=user_id,
                report_date=date_str,
                report_type=ai_res["report_type"],
                slot_time=nearest_slot if ai_res["report_type"] == "status" else None,
                received_at=now.strftime("%H:%M:%S"),
                is_ok=ai_res["is_ok"],
                is_late=is_late if ai_res["report_type"] == "status" else False,
                format_comment=ai_res["format_comment"],
                required_action=ai_res["required_action"],
                raw_text=text_content,
            )

        # 7. Ответ сотруднику
        if ai_res["is_ok"]:
            reply = "🔄 Дополнение принято ИИ без замечаний!" if is_addon else "✅ Отчёт принят ИИ без замечаний!"
        else:
            prefix = "⚠️ Дополненный отчёт:" if is_addon else "⚠️ Оценка отчёта:"
            reply = f"{prefix} {ai_res['employee_message']}"
        await update.message.reply_text(reply)

        # 8. Уведомление в группу
        dest_chat = worker["group_id"] or DEFAULT_GROUP_ID
        gname = await get_group_name_async(context.bot, dest_chat)

        # Исправление бага с is_addon f-string: строим текст явно
        report_row = get_db().execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        notify_text = _build_report_notify_text(
            report_row,
            f"{worker['last_name']} {worker['first_name']}",
            gname,
            is_addon,
            cleaned_text,
        )

        await broadcast_report_notify(
            bot=context.bot,
            update=update,
            dest_chat=dest_chat,
            notify_text=notify_text,
            inline_kbd=_inline_kbd(report_id),
            is_media=is_media,
        )

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as e:
                logger.warning("Ошибка удаления tmp файла %s: %s", tmp_path, e)


# ══════════════════════════════════════════════════════════════════════════════
# Инициализация и запуск
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application):
    try:
        chat = await application.bot.get_chat(DEFAULT_GROUP_ID)
        name = chat.title or str(DEFAULT_GROUP_ID)
        save_group_name(DEFAULT_GROUP_ID, name)
        logger.info("Группа по умолчанию кэширована: %s", name)
    except Exception as e:
        logger.warning("Не удалось получить DEFAULT_GROUP_ID %s: %s", DEFAULT_GROUP_ID, e)
    reschedule_summary_jobs(application)

def main():
    init_db()

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Простые команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"),     get_chat_id))
    application.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), send_summary_now))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    TIMEOUT = {"conversation_timeout": CONVERSATION_TIMEOUT}

    list_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📋 Сотрудники$"), list_workers)],
        states={
            ASK_LIST_DEPARTMENT: [MessageHandler(DIALOG_TEXT, list_workers_department)],
            ASK_LIST_WORKER:     [MessageHandler(DIALOG_TEXT, list_workers_select)],
            ASK_EDIT_FIELD:      [MessageHandler(DIALOG_TEXT, list_workers_action)],
            ASK_EDIT_VALUE:      [MessageHandler(DIALOG_TEXT, edit_value_finish)],
            ASK_EDIT_GROUP_VALUE:[MessageHandler(DIALOG_TEXT, edit_group_finish)],
            ASK_EDIT_SCHEDULE:   [MessageHandler(DIALOG_TEXT, edit_schedule_finish)],
            ASK_EDIT_DAILY_FACT: [MessageHandler(DIALOG_TEXT, edit_daily_fact_finish)],
            ASK_EDIT_SORT_ORDER: [MessageHandler(DIALOG_TEXT, edit_sort_order_finish)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
        **TIMEOUT,
    )

    add_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить сотрудника$"), add_worker_start)],
        states={
            ASK_WORKER_ID:       [MessageHandler(DIALOG_TEXT, add_worker_id)],
            ASK_LASTNAME:        [MessageHandler(DIALOG_TEXT, add_worker_lastname)],
            ASK_FIRSTNAME:       [MessageHandler(DIALOG_TEXT, add_worker_firstname)],
            ASK_POSITION:        [MessageHandler(DIALOG_TEXT, add_worker_position)],
            ASK_GROUP:           [MessageHandler(DIALOG_TEXT, add_worker_group)],
            ASK_SCHEDULE:        [MessageHandler(DIALOG_TEXT, add_worker_schedule)],
            ASK_NEEDS_DAILY_FACT:[MessageHandler(DIALOG_TEXT, add_worker_needs_daily_fact)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
        **TIMEOUT,
    )

    delete_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить сотрудника$"), delete_worker_start)],
        states={
            ASK_REMOVE_DEPARTMENT:[MessageHandler(DIALOG_TEXT, delete_worker_department)],
            ASK_REMOVE_WORKER:    [MessageHandler(DIALOG_TEXT, delete_worker_finish)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
        **TIMEOUT,
    )

    view_dept_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🏢 Сотрудники отдела$"), department_workers_start)],
        states={
            ASK_DEPARTMENT:[MessageHandler(DIALOG_TEXT, department_workers_show)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
        **TIMEOUT,
    )

    summary_scheduler_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏰ Время сводки$"), summary_time_start)],
        states={
            ASK_REPORT_TIME:    [MessageHandler(DIALOG_TEXT, summary_time_action)],
            ASK_EDIT_SCHEDULE:  [MessageHandler(DIALOG_TEXT, summary_time_add_finish)],
            ASK_ORDER_DEPARTMENT:[MessageHandler(DIALOG_TEXT, summary_time_del_finish)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
        **TIMEOUT,
    )

    for handler in (list_handler, add_handler, delete_handler, view_dept_handler, summary_scheduler_handler):
        application.add_handler(handler)

    application.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.TEXT & ~filters.COMMAND,
        handle_report,
    ))

    logger.info("Бот запущен.")
    application.run_polling()

if __name__ == "__main__":
    main()
