import json
import os
import sqlite3
import asyncio
import logging
import csv
import io
from logging.handlers import RotatingFileHandler
from datetime import datetime
import datetime as dt_module
from zoneinfo import ZoneInfo
from openpyxl import load_workbook
import hashlib

from groq import Groq
from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
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

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Разбивает длинное сообщение на части, не ломая предложения."""
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    chunks.append(text)
    return chunks


# ── Настройки и переменные окружения ─────────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Чтение ADMIN_IDS из переменных окружения
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = []
if ADMIN_IDS_RAW:
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.replace("-", "").isdigit():
            ADMIN_IDS.append(int(x))

DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
SUMMARY_CHAT_ID = int(os.environ.get("SUMMARY_CHAT_ID", "0")) or (ADMIN_IDS[0] if ADMIN_IDS else 0)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# ── Часовой пояс ──────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("Europe/Chisinau")  # UTC+2 / UTC+3 (летнее время)

def now_local() -> datetime:
    """Текущее время в локальном часовом поясе."""
    return datetime.now(LOCAL_TZ)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Словарь для хранения данных незарегистрированных пользователей {telegram_id: данные}
pending_unregistered_users = {}

LATE_THRESHOLD_MIN = 15
SCHEDULE_A = ["10:00", "12:00", "15:00", "17:00"]
SCHEDULE_B = ["11:00", "13:00", "16:00", "18:00"]
SCHEDULES = {"A": SCHEDULE_A, "B": SCHEDULE_B}

# Состояния диалогов
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
    ASK_EDIT_SORT_ORDER,
    ASK_CONFIRM_DELETE,
    ASK_EDIT_STATUS_WORK,
    ASK_EXPORT_TYPE,
    ASK_EXPORT_DEPARTMENT,
    ASK_EXPORT_DEPARTMENT_SELECT,
) = range(25)

ASK_IMPORT_FILE = 25

# Клавиатуры
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Сотрудники", "📊 Сводка сейчас"],
        ["➕ Добавить сотрудника", "➖ Удалить сотрудника"],
        ["🏢 Сотрудники отдела", "⏰ Время сводки"],
        ["🆔 ID чата", "📣 Напомнить всем"],
        ["📥 Выгрузить отчеты", "📥 Импорт сотрудников"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
SCHEDULE_KEYBOARD = ReplyKeyboardMarkup([["A", "B"], ["❌ Отмена"]], resize_keyboard=True)
YES_NO_KEYBOARD = ReplyKeyboardMarkup([["Да", "Нет"], ["❌ Отмена"]], resize_keyboard=True)
CANCEL_TEXT = "❌ Отмена"
DIALOG_TEXT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{CANCEL_TEXT}$")


# ══════════════════════════════════════════════════════════════════════════════
# База данных
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workers (
            telegram_id INTEGER PRIMARY KEY,
            last_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            position TEXT NOT NULL DEFAULT 'Не указано',
            group_id INTEGER NOT NULL,
            schedule TEXT NOT NULL DEFAULT 'A',
            needs_daily_fact INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    for col, definition in [
        ("schedule", "TEXT NOT NULL DEFAULT 'A'"),
        ("needs_daily_fact", "INTEGER NOT NULL DEFAULT 1"),
        ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {col} {definition}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            report_type TEXT NOT NULL,
            slot_time TEXT,
            received_at TEXT NOT NULL,
            is_ok INTEGER NOT NULL,
            is_late INTEGER NOT NULL DEFAULT 0,
            format_comment TEXT,
            required_action TEXT,
            raw_text TEXT NOT NULL DEFAULT ''
        )
        """
    )

    cols_reports = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
    if "raw_text" not in cols_reports:
        conn.execute("ALTER TABLE reports ADD COLUMN raw_text TEXT NOT NULL DEFAULT ''")

    # Таблица для настроек (для сохранения расписания сводок)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )

    # Индексы
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_worker_date ON reports(telegram_id, report_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workers_pos ON workers(position)")

    # Очистка тестовых или отмененных данных сотрудника ("Отмена")
    conn.execute(
        "DELETE FROM workers WHERE last_name LIKE '%Отмена%' OR first_name LIKE '%Отмена%' OR position LIKE '%Отмена%'"
    )

    conn.commit()
    conn.close()

def get_worker(telegram_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row

def get_all_workers():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name"
    ).fetchall()
    conn.close()
    return rows

def get_workers_by_position(position: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers WHERE lower(position) = lower(?) ORDER BY sort_order, last_name, first_name",
        (position,),
    ).fetchall()
    conn.close()
    return rows

def upsert_worker(telegram_id: int, last_name: str, first_name: str, position: str, group_id: int, schedule: str, needs_daily_fact: bool, sort_order: int = 0, is_active: int = 1):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            last_name=excluded.last_name,
            first_name=excluded.first_name,
            position=excluded.position,
            group_id=excluded.group_id,
            schedule=excluded.schedule,
            needs_daily_fact=excluded.needs_daily_fact,
            sort_order=excluded.sort_order
        """,
        (telegram_id, last_name, first_name, position, group_id, schedule, int(needs_daily_fact), sort_order, is_active),
    )
    conn.commit()
    conn.close()

def read_excel(file_path: str):
    wb = load_workbook(file_path, data_only=True)
    sheet = wb.active
    
    headers = []
    for cell in sheet[1]:
        if cell.value is not None:
            headers.append(str(cell.value).strip().lower())
        else:
            headers.append("")
            
    workers = []
    
    idx_id = -1
    idx_lastname = -1
    idx_firstname = -1
    idx_name = -1
    idx_position = -1
    idx_group = -1
    idx_schedule = -1
    idx_daily_fact = -1
    
    for idx, h in enumerate(headers):
        if h in ("telegram_id", "id", "tg id", "telegram", "телеграм id", "тг id"):
            idx_id = idx
        elif h in ("last_name", "lastname", "фамилия"):
            idx_lastname = idx
        elif h in ("first_name", "firstname", "имя"):
            idx_firstname = idx
        elif h in ("name", "фио", "сотрудник", "имя фамилия"):
            idx_name = idx
        elif h in ("position", "должность", "отдел", "специальность"):
            idx_position = idx
        elif h in ("group_id", "группа", "id группы", "чат", "чат id", "brigade"):
            idx_group = idx
        elif h in ("schedule", "график", "расписание"):
            idx_schedule = idx
        elif h in ("needs_daily_fact", "факт дня", "ежедневный факт"):
            idx_daily_fact = idx
            
    for row_idx in range(2, sheet.max_row + 1):
        row_values = []
        for col_idx in range(1, len(headers) + 1):
            val = sheet.cell(row=row_idx, column=col_idx).value
            row_values.append(val)
        
        if not any(v is not None for v in row_values):
            continue
            
        tg_id_val = row_values[idx_id] if idx_id != -1 and idx_id < len(row_values) else None
        lastname_val = row_values[idx_lastname] if idx_lastname != -1 and idx_lastname < len(row_values) else None
        firstname_val = row_values[idx_firstname] if idx_firstname != -1 and idx_firstname < len(row_values) else None
        name_val = row_values[idx_name] if idx_name != -1 and idx_name < len(row_values) else None
        pos_val = row_values[idx_position] if idx_position != -1 and idx_position < len(row_values) else None
        group_val = row_values[idx_group] if idx_group != -1 and idx_group < len(row_values) else None
        schedule_val = row_values[idx_schedule] if idx_schedule != -1 and idx_schedule < len(row_values) else None
        daily_fact_val = row_values[idx_daily_fact] if idx_daily_fact != -1 and idx_daily_fact < len(row_values) else None
        
        # We also check if user has name/position/brigade/object columns which we map gracefully:
        # object column could be mapped to position/department if not specified
        idx_object = -1
        for idx, h in enumerate(headers):
            if h in ("object", "объект"):
                idx_object = idx
                break
        if idx_object != -1 and idx_object < len(row_values) and row_values[idx_object] is not None:
            obj_val = str(row_values[idx_object]).strip()
            if pos_val is None:
                pos_val = obj_val
            else:
                pos_val = f"{str(pos_val).strip()} ({obj_val})"

        tg_id = None
        if tg_id_val is not None:
            try:
                tg_id = int(float(str(tg_id_val).strip()))
            except ValueError:
                pass
                
        last_name = ""
        first_name = ""
        if lastname_val is not None:
            last_name = str(lastname_val).strip()
        if firstname_val is not None:
            first_name = str(firstname_val).strip()
            
        if not last_name and not first_name and name_val is not None:
            parts = str(name_val).strip().split()
            if len(parts) >= 2:
                last_name = parts[0]
                first_name = " ".join(parts[1:])
            elif len(parts) == 1:
                first_name = parts[0]
                last_name = "Сотрудник"
                
        if not last_name:
            last_name = "Не указана"
        if not first_name:
            first_name = "Не указано"
            
        position = str(pos_val).strip() if pos_val is not None else "Не указано"
        
        group_id = DEFAULT_GROUP_ID
        if group_val is not None:
            try:
                # If they wrote e.g. "Бригада 1", it isn't an integer, so we ignore or map to DEFAULT_GROUP_ID
                group_id = int(float(str(group_val).strip()))
            except ValueError:
                pass
                
        schedule = str(schedule_val).strip().upper() if schedule_val is not None else "A"
        if schedule not in ("A", "B"):
            schedule = "A"
            
        needs_daily_fact = True
        if daily_fact_val is not None:
            df_str = str(daily_fact_val).strip().lower()
            if df_str in ("0", "false", "нет", "no"):
                needs_daily_fact = False
                
        # Generate a fallback tg_id if not present
        if tg_id is None:
            name_str = f"{last_name} {first_name} {position}"
            tg_id_hash = int(hashlib.md5(name_str.encode('utf-8')).hexdigest()[:7], 16)
            tg_id = -tg_id_hash

        workers.append({
            "telegram_id": tg_id,
            "last_name": last_name,
            "first_name": first_name,
            "position": position,
            "group_id": group_id,
            "schedule": schedule,
            "needs_daily_fact": needs_daily_fact
        })
        
    return workers

def add_worker_excel(worker):
    upsert_worker(
        telegram_id=worker["telegram_id"],
        last_name=worker["last_name"],
        first_name=worker["first_name"],
        position=worker["position"],
        group_id=worker["group_id"],
        schedule=worker["schedule"],
        needs_daily_fact=worker["needs_daily_fact"],
    )

def update_worker_field(telegram_id: int, field: str, value):
    allowed = {"last_name", "first_name", "position", "group_id", "schedule", "needs_daily_fact", "sort_order", "is_active"}
    if field not in allowed:
        raise ValueError(f"Недопустимое поле: {field}")
    conn = get_db()
    
    # Решение проблемы 9: Сброс sort_order в 0 при смене отдела (position)
    if field == "position":
        conn.execute("UPDATE workers SET position = ?, sort_order = 0 WHERE telegram_id = ?", (value, telegram_id))
    else:
        conn.execute(f"UPDATE workers SET {field} = ? WHERE telegram_id = ?", (value, telegram_id))
        
    conn.commit()
    conn.close()

def swap_sort_order(id1: int, id2: int):
    conn = get_db()
    r1 = conn.execute("SELECT sort_order FROM workers WHERE telegram_id = ?", (id1,)).fetchone()
    r2 = conn.execute("SELECT sort_order FROM workers WHERE telegram_id = ?", (id2,)).fetchone()
    if r1 and r2:
        conn.execute("UPDATE workers SET sort_order = ? WHERE telegram_id = ?", (r2["sort_order"], id1))
        conn.execute("UPDATE workers SET sort_order = ? WHERE telegram_id = ?", (r1["sort_order"], id2))
        conn.commit()
    conn.close()

def delete_worker(telegram_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM workers WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted

def save_group_name(group_id: int, group_name: str):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO groups (group_id, group_name) VALUES (?, ?)
        ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name
        """,
        (group_id, group_name),
    )
    conn.commit()
    conn.close()

def get_group_name(group_id: int) -> str:
    conn = get_db()
    row = conn.execute("SELECT group_name FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    conn.close()
    return row["group_name"] if row else str(group_id)

async def get_group_name_async(bot, group_id: int) -> str:
    """Решение проблемы 8: автоматическое подтягивание названия некэшированной группы из Telegram."""
    conn = get_db()
    row = conn.execute("SELECT group_name FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    conn.close()
    if row:
        return row["group_name"]
    try:
        chat = await bot.get_chat(group_id)
        name = chat.title or str(group_id)
        save_group_name(group_id, name)
        return name
    except Exception:
        return str(group_id)

def get_all_group_names() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT group_id, group_name FROM groups").fetchall()
    conn.close()
    res = {row["group_id"]: row["group_name"] for row in rows}
    if DEFAULT_GROUP_ID not in res:
        res[DEFAULT_GROUP_ID] = str(DEFAULT_GROUP_ID)
    return res

async def fetch_and_save_group_name(bot, group_id: int) -> str:
    try:
        chat = await bot.get_chat(group_id)
        name = chat.title or str(group_id)
    except Exception:
        name = str(group_id)
    save_group_name(group_id, name)
    return name

def save_report(telegram_id: int, report_date: str, report_type: str, slot_time: str | None, received_at: str, is_ok: bool, is_late: bool, format_comment: str, required_action: str, raw_text: str = "") -> int:
    """Слегка изменена для возврата ID новой записи (для исправления оценок ИИ)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reports (telegram_id, report_date, report_type, slot_time, received_at, is_ok, is_late, format_comment, required_action, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, report_date, report_type, slot_time, received_at, int(is_ok), int(is_late), format_comment, required_action, raw_text),
    )
    conn.commit()
    inserted_id = cur.lastrowid
    conn.close()
    return inserted_id

def update_report_text_and_ai(report_id: int, is_ok: bool, format_comment: str, required_action: str, raw_text: str, received_at: str):
    conn = get_db()
    conn.execute(
        """
        UPDATE reports
        SET is_ok = ?, format_comment = ?, required_action = ?, raw_text = ?, received_at = ?
        WHERE id = ?
        """,
        (int(is_ok), format_comment, required_action, raw_text, received_at, report_id)
    )
    conn.commit()
    conn.close()

def get_reports_for_date(report_date: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM reports WHERE report_date = ?", (report_date,)).fetchall()
    conn.close()
    return rows

def check_duplicate_report(telegram_id: int, report_date: str, report_type: str, slot_time: str | None) -> bool:
    """Решение проблемы 5: Защита от дублей. Возвращает True, если отчет уже существует."""
    conn = get_db()
    if report_type == "status":
        row = conn.execute(
            "SELECT id FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = ? AND slot_time = ?",
            (telegram_id, report_date, report_type, slot_time),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = ?",
            (telegram_id, report_date, report_type),
        ).fetchone()
    conn.close()
    return row is not None

def get_worker_history_last_week(telegram_id: int) -> str:
    """Решение проблемы 6: Логирование истории отчетов сотрудника за неделю."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM reports 
        WHERE telegram_id = ? 
        AND report_date >= date('now', '-7 days')
        ORDER BY report_date DESC, received_at DESC
        """,
        (telegram_id,)
    ).fetchall()
    
    worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    
    if not worker:
        return "Сотрудник не найден."
        
    if not rows:
        return f"📅 У сотрудника {worker['last_name']} {worker['first_name']} нет отчетов за последние 7 дней."
        
    lines = [f"📅 История отчетов за прошедшую неделю для {worker['last_name']} {worker['first_name']}:\n"]
    for r in rows:
        r_type = "Статус" if r["report_type"] == "status" else "Итог дня"
        slot_str = f" за {r['slot_time']}" if r["slot_time"] and r["report_type"] == "status" else ""
        ok_str = "✅ ОК" if r["is_ok"] else f"⚠️ Замечание: {r['format_comment']}"
        late_str = " ⏰ Опоздание" if r["is_late"] else ""
        lines.append(
            f"📍 {r['report_date']} в {r['received_at']}\n"
            f"   Тип: {r_type}{slot_str}\n"
            f"   Результат: {ok_str}{late_str}\n"
        )
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Решение проблемы 12 (Хранение расписания сводок в БД)
# ══════════════════════════════════════════════════════════════════════════════

def get_scheduled_times() -> list[str]:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'summary_times'").fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["value"])
        except Exception:
            return ["19:00"]
    return ["19:00"]

def save_scheduled_times(times: list[str]):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('summary_times', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(sorted(times)),)
    )
    conn.commit()
    conn.close()

def reschedule_summary_jobs(application: Application):
    job_queue = application.job_queue
    if not job_queue:
        print("JobQueue недоступен.")
        return

    # Удаление существующих задач сводки
    for job in job_queue.get_jobs_by_name("daily_summary"):
        job.schedule_removal()

    times = get_scheduled_times()
    for t_str in times:
        try:
            hour, minute = map(int, t_str.split(":"))
            time_obj = dt_module.time(hour=hour, minute=minute, tzinfo=LOCAL_TZ)
            job_queue.run_daily(
                scheduled_summary_callback,
                time=time_obj,
                days=(0, 1, 2, 3, 4, 5, 6),
                name="daily_summary"
            )
            print(f"Запланирована сводка на {t_str}")
        except Exception as e:
            print(f"Критическая ошибка при планировании сводки на {t_str}: {e}")

async def scheduled_summary_callback(context: ContextTypes.DEFAULT_TYPE):
    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    summary_text = f"⏰ Автоматическая запланированная сводка:\n\n" + generate_daily_summary_text(date_str)
    
    if SUMMARY_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=SUMMARY_CHAT_ID, text=summary_text)
        except Exception as e:
            print(f"Ошибка при отправке автоматической сводки в {SUMMARY_CHAT_ID}: {e}")
            
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=summary_text)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции авторизации и меню
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
    if update.effective_chat.type != "private":
        try:
            await update.message.reply_text(
                "⚠️ Эта функция доступна только в личных сообщениях с ботом.",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception:
            pass
        return False
    return True

def menu_for_user(user_id: int, chat_type: str = "private"):
    return MAIN_MENU if (is_admin(user_id) and chat_type == "private") else ReplyKeyboardRemove()

def positions_keyboard(rows):
    positions = sorted({row["position"] for row in rows})
    keyboard = [[p] for p in positions]
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True), positions

def numbered_workers_keyboard(rows):
    keyboard = []
    for i, row in enumerate(rows, 1):
        keyboard.append([f"{i}. {row['last_name']} {row['first_name']}"])
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ══════════════════════════════════════════════════════════════════════════════
# Работа с ИИ (Groq / Whisper / Llama)
# ══════════════════════════════════════════════════════════════════════════════

def normalize_ai_result(data: dict, source_text: str) -> dict:
    text_lower = source_text.lower()
    report_type = str(data.get("report_type", "status")).strip().lower()

    fact_words = ("факт", "факт дня", "за день", "итог дня", "итоги дня", "сегодня за день", "дневной отчет")
    status_words = ("статус", "сейчас", "на данный момент", "за 10", "за 11", "за 12", "за 13", "за 15", "за 16", "за 17", "за 18")

    if any(word in text_lower for word in fact_words):
        report_type = "daily_fact"
    elif any(word in text_lower for word in status_words):
        report_type = "status"
    elif report_type not in ("status", "daily_fact"):
        report_type = "status"

    raw_ok = data.get("is_ok", False)
    if isinstance(raw_ok, str):
        is_ok = raw_ok.strip().lower() in ("true", "1", "yes", "да")
    else:
        is_ok = bool(raw_ok)
    issue = str(data.get("issue") or data.get("format_comment") or "").strip()
    required_action = str(data.get("required_action") or "").strip()
    employee_message = str(data.get("employee_message") or "").strip()

    if is_ok:
        issue = ""
        format_comment = "всё ОК"
        required_action = "ничего не предпринимать"
        employee_message = ""
    else:
        if not issue:
            issue = "есть замечания по отчету"
        format_comment = f"не ОК, {issue}"
        required_action = f"сделал замечание сотруднику: {issue}"
        if not employee_message:
            employee_message = f"В отчете есть замечание: {issue}. В следующем отчете исправьте это."

    return {
        "report_type": report_type,
        "is_ok": is_ok,
        "format_comment": format_comment,
        "required_action": required_action,
        "employee_message": employee_message,
    }

def find_nearest_slot(schedule: list[str], now: datetime):
    current_minutes = now.hour * 60 + now.minute
    nearest_slot = None
    nearest_diff = None

    for slot in schedule:
        hour, minute = map(int, slot.split(":"))
        slot_minutes = hour * 60 + minute
        diff = abs(current_minutes - slot_minutes)
        if nearest_diff is None or diff < nearest_diff:
            nearest_diff = diff
            nearest_slot = slot

    is_late = nearest_diff is not None and nearest_diff > LATE_THRESHOLD_MIN
    return nearest_slot, is_late

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

"работал на дробилке"
→ хорошо, понятно что человек выполнял работу


"стоял на кране, подавал материал"
→ хорошо


"делал опалубку"
→ хорошо


"бетон заливали сегодня"
→ хорошо


"копал траншею 50 метров"
→ отлично, есть объём


=====================
КОГДА НУЖНЫ ОБЪЁМЫ
=====================

Цифры важны там, где работа измеряется:

земляные работы:
- метры
- кубы

бетон:
- кубометры

монтаж:
- количество элементов

кабель:
- метры


Но отсутствие цифр НЕ означает плохой отчёт.

Например:

"копал траншею"

может быть принят,
если понятно, что человек реально выполнял работу.


=====================
ПЛОХИЕ ОТЧЁТЫ
=====================

Отклонять:

"работаю"
"в процессе"
"нормально"
"всё сделал"
"на объекте"
"занимаюсь"

если невозможно понять, что именно делал человек.


=====================
ИСПРАВЛЕНИЕ РЕЧИ
=====================

Рабочие могут писать:

"там это, ковыряли яму"
понимай как:
"Выполнялись земляные работы"


"дробилку гоняли"
понимай как:
"Работа на дробильном оборудовании"


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

Тебе дают сообщение рабочего.
Рабочий может писать с ошибками, сокращениями и разговорными словами.

Твоя задача:
превратить его сообщение в понятный официальный отчёт.

Правила:

1. Не придумывай работу, которой не было.
2. Сохраняй только смысл исходного сообщения.
3. Исправляй ошибки.
4. Убирай слова-паразиты.
5. Делай текст коротким и понятным.


Примеры:


Вход:
"с утра там дробилку гоняли щебень делали"

Выход:
"Выполнялась работа на дробильном оборудовании, производилась переработка материала."


Вход:
"копал там возле склада траншею"

Выход:
"Выполнялись земляные работы: разработка траншеи возле склада."


Вход:
"бетон лили колонны"

Выход:
"Выполнялись бетонные работы: заливка колонн."


Вход:
"всё норм"

Выход:
"Отчёт не содержит информации о выполненной работе."


Верни только готовый текст отчёта.


Сообщение рабочего:

{text}
"""

def clean_report(text: str) -> str:
    if groq_client is None:
        return text
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты преобразуешь сообщения рабочих в официальные отчеты строительной бригады. Верни только готовый текст отчета без каких-либо комментариев и кавычек."},
                {"role": "user", "content": CLEAN_REPORT_PROMPT.format(text=text)},
            ],
            max_tokens=400,
            temperature=0,
        )
        return response.choices[0].message.content.strip().strip('"').strip("'")
    except Exception as e:
        print(f"Ошибка при очистке отчета: {e}")
        return text

def check_status(text: str) -> dict:
    if groq_client is None:
        return normalize_ai_result({"report_type": "status", "is_ok": False, "issue": "GROQ_API_KEY не задан"}, text)
    try:
        response = groq_client.chat.completions.create(
            # Решение проблемы 10: смена модели на llama-3.3-70b-versatile
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
        return normalize_ai_result({"report_type": "status", "is_ok": False, "issue": f"Ошибка ИИ: {e}"}, text)


async def transcribe_audio_async(file_path: str) -> str:
    return await asyncio.to_thread(transcribe_audio, file_path)


async def clean_report_async(text: str) -> str:
    return await asyncio.to_thread(clean_report, text)


async def check_status_async(text: str) -> dict:
    return await asyncio.to_thread(check_status, text)


# ══════════════════════════════════════════════════════════════════════════════
# Решение проблемы 4 (Детализированная сводка, различающая статус и факт дня)
# ══════════════════════════════════════════════════════════════════════════════

def generate_daily_summary_text(report_date: str) -> str:
    conn = get_db()
    workers = conn.execute("SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name").fetchall()
    reports = conn.execute("SELECT * FROM reports WHERE report_date = ?", (report_date,)).fetchall()
    conn.close()

    reports_by_worker = {}
    for r in reports:
        tid = r["telegram_id"]
        r_dict = dict(r)
        if tid not in reports_by_worker:
            reports_by_worker[tid] = {"status": {}, "daily_fact": []}
        
        if r_dict["report_type"] == "status":
            slot = r_dict["slot_time"]
            reports_by_worker[tid]["status"][slot] = r_dict
        elif r_dict["report_type"] == "daily_fact":
            reports_by_worker[tid]["daily_fact"].append(r_dict)

    summary_lines = [
        f"📊 Сводка отчетов за {report_date}",
        ""
    ]
    
    workers_by_dept = {}
    for w in workers:
        lastname_lower = (w["last_name"] or "").lower()
        firstname_lower = (w["first_name"] or "").lower()
        dept_lower = (w["position"] or "").lower()
        
        # Исключаем тестовые записи ("отмена" или "тест")
        if any(x in lastname_lower or x in firstname_lower or x in dept_lower for x in ("отмена", "test", "тест")):
            continue
            
        dept = w["position"]
        if dept not in workers_by_dept:
            workers_by_dept[dept] = []
        workers_by_dept[dept].append(w)

    for dept, dept_workers in workers_by_dept.items():
        if not dept_workers:
            continue
        summary_lines.append(f"🏢 Отдел: {dept}")
        summary_lines.append("──────────────────────────")
        for w in dept_workers:
            tid = w["telegram_id"]
            name = f"{w['last_name']} {w['first_name']}"
            
            if not w["is_active"]:
                summary_lines.append(f"👨‍💻 {name}")
                summary_lines.append("   🏝 В отпуске / на больничном")
                summary_lines.append("")
                continue
                
            w_reports = reports_by_worker.get(tid, {"status": {}, "daily_fact": []})
            
            # 1. Почасовые статусы
            schedule_slots = SCHEDULES.get(w["schedule"], SCHEDULE_A)
            status_segments = []
            issues_list = []
            
            for slot in schedule_slots:
                rep = w_reports["status"].get(slot)
                if rep:
                    status_icon = "✅" if rep["is_ok"] else "⚠️"
                    late_icon = "⏰" if rep["is_late"] else ""
                    status_segments.append(f"{slot} {status_icon}{late_icon}")
                    if not rep["is_ok"]:
                        comment = rep["format_comment"] or "Есть замечание"
                        if comment.startswith("не ОК, "):
                            comment = comment[len("не ОК, "):]
                        elif comment.startswith("не ОК: "):
                            comment = comment[len("не ОК: "):]
                        issues_list.append(f"• {slot} — {comment}")
                else:
                    status_segments.append(f"{slot} ❌")
            
            status_str = " | ".join(status_segments)
            
            # 2. Факт дня (daily_fact)
            if w["needs_daily_fact"]:
                fact_reps = w_reports["daily_fact"]
                if fact_reps:
                    f_rep = fact_reps[-1]
                    if f_rep["is_ok"]:
                        fact_str = "✅ Сдан"
                    else:
                        comment = f_rep["format_comment"] or "Есть замечание"
                        if comment.startswith("не ОК, "):
                            comment = comment[len("не ОК, "):]
                        elif comment.startswith("не ОК: "):
                            comment = comment[len("не ОК: "):]
                        fact_str = f"⚠️ Замечание ({comment})"
                else:
                    fact_str = "❌ Не отправлен"
            else:
                fact_str = "⚪ Не требуется"

            summary_lines.append(f"👨‍💻 {name}")
            summary_lines.append(f"   ⏱ Статусы:  {status_str}")
            summary_lines.append(f"   📋 Итог дня: {fact_str}")
            if issues_list:
                summary_lines.append("   ⚠️ Замечания по статусам:")
                for issue in issues_list:
                    summary_lines.append(f"     {issue}")
            summary_lines.append("")
        summary_lines.append("")

    return "\n".join(summary_lines)


# ══════════════════════════════════════════════════════════════════════════════
# Решение проблемы 7 (Обработчик Callback-кнопки переключения результатов)
# ══════════════════════════════════════════════════════════════════════════════

def format_show_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return date_str

def format_status_or_fact_line(report_type: str, slot_time: str | None, report_date: str) -> str:
    formatted_date = format_show_date(report_date)
    if report_type == "daily_fact":
        return f"Факт за {formatted_date}"
    else:
        slot_str = slot_time or "Неизвестно"
        return f"Статус за {slot_str} за {formatted_date}"

def update_message_metadata(original_text: str, is_ok: bool | None = None, comment: str | None = None, status_val: str | None = None, is_manual: bool = False) -> str:
    lines = original_text.split("\n")
    for i, line in enumerate(lines):
        if (line.startswith("Статус:") or line.startswith("Статус за") or line.startswith("Факт за")) and status_val is not None:
            lines[i] = status_val
        elif (line.startswith("Оценка ИИ:") or line.startswith("Оценка:")) and is_ok is not None:
            label = "Оценка" if (is_manual or line.startswith("Оценка:")) else "Оценка ИИ"
            lines[i] = f"{label}: {'ОК' if is_ok else 'НЕ ОК'}"
        elif (line.startswith("Комментарий ИИ:") or line.startswith("Комментарий:")) and comment is not None:
            label = "Комментарий" if (is_manual or line.startswith("Комментарий:")) else "Комментарий ИИ"
            lines[i] = f"{label}: {comment}"
    return "\n".join(lines)

def update_message_text_fields(original_text: str, is_ok: bool, new_comment: str) -> str:
    return update_message_metadata(original_text, is_ok=is_ok, comment=new_comment, is_manual=True)

def make_report_keyboard(report_id: int, report_type: str | None = None) -> InlineKeyboardMarkup:
    if report_type is None:
        try:
            conn = get_db()
            row = conn.execute("SELECT report_type FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            report_type = row["report_type"] if row else "status"
        except Exception:
            report_type = "status"
            
    type_btn_text = "📋 Сделать Итогом дня" if report_type == "status" else "⏱ Сделать Статусом"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Изменить оценку (ОК / НЕ ОК)", callback_data=f"fix_toggle_{report_id}"),
            InlineKeyboardButton("✏️ Изменить комментарий", callback_data=f"edit_comment_{report_id}")
        ],
        [
            InlineKeyboardButton(type_btn_text, callback_data=f"toggle_type_{report_id}")
        ]
    ])

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("У вас нет прав администратора для корректировки оценок.", show_alert=True)
        return
        
    data = query.data
    if data.startswith("fix_toggle_"):
        report_id = int(data.split("_")[-1])
        
        conn = get_db()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            conn.close()
            await query.answer("Запись отчета в БД не найдена.", show_alert=True)
            return
            
        new_ok = 1 if report["is_ok"] == 0 else 0
        new_comment = "ОК (изменено администратором вручную)" if new_ok == 1 else "Замечание (изменено администратором вручную)"
        new_action = f"Скорректировано вручную администратором @{query.from_user.username or user_id}"
        
        conn.execute(
            "UPDATE reports SET is_ok = ?, format_comment = ?, required_action = ? WHERE id = ?",
            (new_ok, new_comment, new_action, report_id)
        )
        
        worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (report["telegram_id"],)).fetchone()
        conn.commit()
        conn.close()
        
        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        status_emoji = "✅" if new_ok == 1 else "⚠️"
        
        await query.answer("Оценка скорректирована!")
        
        # Обновляем текст сообщения, сохраняя историю
        original_text = query.message.text or ""
        if "Официальный отчет:" in original_text:
            new_text = update_message_text_fields(original_text, new_ok == 1, new_comment)
        else:
            new_text = (
                f"🔧 Оценка отчета изменена вручную администратором @{query.from_user.username or user_id}:\n"
                f"Сотрудник: {worker_name}\n"
                f"Дата отчета: {report['report_date']}\n"
                f"Статус: {report['slot_time'] or report['report_type']}\n"
                f"Новый статус: {status_emoji} ({new_comment})"
            )
        
        kbd = make_report_keyboard(report_id, report["report_type"] if report else None)
        
        try:
            await query.edit_message_text(text=new_text, reply_markup=kbd)
        except Exception as e:
            print(f"Ошибка обновления интерактивной кнопки: {e}")

    elif data.startswith("edit_comment_"):
        report_id = int(data.split("_")[-1])
        
        conn = get_db()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.close()
        
        if not report:
            await query.answer("Запись отчета в БД не найдена.", show_alert=True)
            return
            
        context.user_data["editing_comment_report_id"] = report_id
        context.user_data["editing_comment_chat_id"] = query.message.chat_id
        context.user_data["editing_comment_message_id"] = query.message.message_id
        context.user_data["editing_comment_original_text"] = query.message.text
        
        await query.answer()
        try:
            prompt_msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="✏️ Введите новый комментарий ИИ:",
                reply_markup=ForceReply(selective=True)
            )
            context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        except Exception as e:
            print(f"Ошибка отправки ForceReply: {e}")

    elif data.startswith("toggle_type_"):
        report_id = int(data.split("_")[-1])
        
        conn = get_db()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            conn.close()
            await query.answer("Запись отчета в БД не найдена.", show_alert=True)
            return
            
        worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (report["telegram_id"],)).fetchone()
        
        current_type = report["report_type"]
        if current_type == "status":
            new_type = "daily_fact"
            new_slot = None
            is_late = 0
            status_display_val = format_status_or_fact_line(new_type, new_slot, report["report_date"])
        else:
            new_type = "status"
            schedule_slots = SCHEDULES.get(worker["schedule"] if worker else "A", SCHEDULE_A)
            try:
                h, m, s = map(int, (report["received_at"] or "00:00:00").split(":"))
                now_dt = now_local().replace(hour=h, minute=m, second=s)
            except Exception:
                now_dt = now_local()
            new_slot, wait_is_late = find_nearest_slot(schedule_slots, now_dt)
            status_display_val = format_status_or_fact_line(new_type, new_slot, report["report_date"])
            is_late = int(wait_is_late)
            
        conn.execute(
            "UPDATE reports SET report_type = ?, slot_time = ?, is_late = ? WHERE id = ?",
            (new_type, new_slot, is_late, report_id)
        )
        conn.commit()
        conn.close()
        
        await query.answer("Тип отчета изменен!")
        
        original_text = query.message.text or ""
        new_text = update_message_metadata(original_text, status_val=status_display_val)
        
        kbd = make_report_keyboard(report_id, new_type)
        try:
            await query.edit_message_text(text=new_text, reply_markup=kbd)
        except Exception as e:
            print(f"Ошибка обновления типа отчета в сообщении: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Базовые хэндлеры команд
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if is_admin(update.effective_user.id) and chat_type == "private":
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Привет! Отправьте видеоотчет, когда он будет готов.", reply_markup=ReplyKeyboardRemove())

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID чата: {update.effective_chat.id}", reply_markup=menu_for_user(update.effective_user.id, update.effective_chat.type))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    chat_type = update.effective_chat.type
    if is_admin(update.effective_user.id) and chat_type == "private":
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def import_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    await update.message.reply_text(
        "📎 Пожалуйста, отправьте файл Excel (.xlsx) со списком сотрудников.\n\n"
        "Файл должен содержать заголовки: name (или ФИО), position (или должность), и т.д. "
        "А также telegram_id (или ID), если хотите связать аккаунты напрямую.",
        reply_markup=CANCEL_KEYBOARD
    )
    return ASK_IMPORT_FILE

async def import_workers_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Пожалуйста, отправьте файл типа документ (.xlsx).")
        return ASK_IMPORT_FILE
        
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Формат файла не поддерживается. Пожалуйста, отправьте файл .xlsx")
        return ASK_IMPORT_FILE
        
    await update.message.reply_text("⏳ Получение файла и импорт данных, пожалуйста, подождите...")
    
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        local_path = "workers_temp_import.xlsx"
        await tg_file.download_to_drive(local_path)
        
        workers = read_excel(local_path)
        if not workers:
            await update.message.reply_text("⚠️ Не обнаружено записей в файле или структура не распознана. Проверьте заголовки.", reply_markup=MAIN_MENU)
            if os.path.exists(local_path):
                os.remove(local_path)
            return ConversationHandler.END
            
        success_count = 0
        for w in workers:
            try:
                add_worker_excel(w)
                success_count += 1
            except Exception as ex:
                logging.error(f"Error importing worker {w}: {ex}")
                
        # Clean up temp file
        if os.path.exists(local_path):
            os.remove(local_path)
            
        await update.message.reply_text(
            f"✅ Импорт успешно завершен!\n"
            f"Загружено/обновлено сотрудников: {success_count}.",
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END
        
    except Exception as e:
        logging.error(f"Excel import error: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка во время импорта: {e}", reply_markup=MAIN_MENU)
        return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Диалог 📋 Список и редактирование сотрудников
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
        lines.append(f"{i}. {row['last_name']} {row['first_name']}\n   График: {row['schedule']} ({schedule_str})\n   Группа: {gname} | Факт дня: {fact}")

    # Решение проблемы 11: Объединение уведомления со списком сотрудников
    lines.append("\n👉 Выберите сотрудника по номеру из списка:")
    await update.message.reply_text("\n".join(lines), reply_markup=numbered_workers_keyboard(rows))
    return ASK_LIST_WORKER

async def list_workers_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    rows = context.user_data.get("list_rows", [])
    num_str = raw.split(".")[0].strip()
    if not num_str.isdigit():
        await update.message.reply_text("Выберите сотрудника по номеру из списка.")
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
    active_str = "Активен" if worker["is_active"] else "В отпуске / на больничном"
    info = f"👤 {worker['last_name']} {worker['first_name']}\nОтдел: {worker['position']}\nГрафик: {worker['schedule']} ({schedule_str})\nГруппа: {gname}\nФакт дня: {fact}\nСтатус работы: {active_str}\n\nЧто хотите сделать?"

    # Добавление кнопки Истории еженедельных оценок (проблема 6)
    kbd = ReplyKeyboardMarkup(
        [
            ["📅 История за неделю", "✏️ Номер в списке"],
            ["✏️ Изменить фамилию", "✏️ Изменить имя"],
            ["✏️ Изменить отдел", "✏️ Изменить график"],
            ["✏️ Изменить группу", "✏️ Факт дня"],
            ["✏️ Статус работы", "🔼 Вверх в списке"],
            ["🔽 Вниз в списке", "❌ Отмена"]
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(info, reply_markup=kbd)
    return ASK_EDIT_FIELD

async def list_workers_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    rows = context.user_data.get("list_rows", [])
    idx = context.user_data.get("edit_worker_idx", 0)

    if not worker:
        await update.message.reply_text("Ошибка состояния. Начните сначала.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if action == "📅 История за неделю":
        history_text = get_worker_history_last_week(worker["telegram_id"])
        await update.message.reply_text(history_text, reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    if action == "✏️ Номер в списке":
        await update.message.reply_text(
            f"Введите новый порядковый номер для {worker['last_name']} {worker['first_name']}\n"
            f"(сейчас он под номером {idx + 1}, всего в отделе {len(rows)} сотрудников):",
            reply_markup=CANCEL_KEYBOARD
        )
        return ASK_EDIT_SORT_ORDER

    if action in ("🔼 Вверх в списке", "🔽 Вниз в списке"):
        target_idx = idx - 1 if action == "🔼 Вверх в списке" else idx + 1
        if target_idx < 0 or target_idx >= len(rows):
            await update.message.reply_text("Сотрудник уже на краю списка.", reply_markup=MAIN_MENU)
            return ConversationHandler.END
        swap_sort_order(worker["telegram_id"], rows[target_idx]["telegram_id"])
        await update.message.reply_text(f"Порядок изменен для {worker['last_name']}.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    field_map = {
        "✏️ Изменить фамилию": ("last_name", "Введите новую фамилию:"),
        "✏️ Изменить имя": ("first_name", "Введите новое имя:"),
        "✏️ Изменить отдел": ("position", "Введите новое название отдела:"),
        "✏️ Изменить группу": ("group_id", "Введите новый ID группы Telegram (0 = по умолчанию):"),
        "✏️ Изменить график": ("schedule", None),
        "✏️ Факт дня": ("needs_daily_fact", None),
        "✏️ Статус работы": ("is_active", None),
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
    if field == "is_active":
        await update.message.reply_text(
            "Выберите новый статус работы:",
            reply_markup=ReplyKeyboardMarkup([["Активен", "В отпуске / на больничном"], ["❌ Отмена"]], resize_keyboard=True)
        )
        return ASK_EDIT_STATUS_WORK
    if field == "group_id":
        await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_GROUP_VALUE

    await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
    return ASK_EDIT_VALUE

async def edit_value_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    field = context.user_data.get("edit_field")
    update_worker_field(worker["telegram_id"], field, value)
    await update.message.reply_text("Данные обновлены.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_group_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Введите числовой ID:")
        return ASK_EDIT_GROUP_VALUE
    worker = context.user_data.get("edit_worker")
    final_id = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    update_worker_field(worker["telegram_id"], "group_id", final_id)
    gname = await fetch_and_save_group_name(context.bot, final_id)
    await update.message.reply_text(f"Группа обновлена на: {gname}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_schedule_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES: return ASK_EDIT_SCHEDULE
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "schedule", raw)
    await update.message.reply_text(f"График изменен на {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_daily_fact_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"): return ASK_EDIT_DAILY_FACT
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "needs_daily_fact", 1 if raw == "да" else 0)
    await update.message.reply_text(f"Параметр Факт Дня изменен на: {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_status_work_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw == "❌ отмена":
        await update.message.reply_text("Изменение отменено.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END
    if raw not in ("активен", "в отпуске / на больничном"):
        return ASK_EDIT_STATUS_WORK
    worker = context.user_data.get("edit_worker")
    val = 1 if raw == "активен" else 0
    update_worker_field(worker["telegram_id"], "is_active", val)
    await update.message.reply_text(f"Статус работы изменен на: {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_sort_order_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == "❌ Отмена":
        await update.message.reply_text("Изменение отменено.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END
        
    if not raw.isdigit():
        await update.message.reply_text("Пожалуйста, введите положительное целое число для порядкового номера:")
        return ASK_EDIT_SORT_ORDER
        
    target_num = int(raw)
    worker = context.user_data.get("edit_worker")
    rows = context.user_data.get("list_rows", [])
    
    if not worker or not rows:
        await update.message.reply_text("Ошибка сессии. Начните сначала.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END
        
    num_workers = len(rows)
    if target_num < 1 or target_num > num_workers:
        await update.message.reply_text(f"Недопустимый порядковый номер. Пожалуйста, введите число от 1 до {num_workers}:")
        return ASK_EDIT_SORT_ORDER
        
    target_idx = target_num - 1 # 0-based target index
    
    position = worker["position"]
    all_dept_workers = [dict(r) for r in get_workers_by_position(position)]
    
    target_worker_id = worker["telegram_id"]
    worker_to_move = None
    for idx, w in enumerate(all_dept_workers):
        if w["telegram_id"] == target_worker_id:
            worker_to_move = all_dept_workers.pop(idx)
            break
            
    if worker_to_move is not None:
        all_dept_workers.insert(target_idx, worker_to_move)
        
        conn = get_db()
        for i, w in enumerate(all_dept_workers):
            conn.execute("UPDATE workers SET sort_order = ? WHERE telegram_id = ?", (i, w["telegram_id"]))
        conn.commit()
        conn.close()
        
        name = f"{worker['last_name']} {worker['first_name']}"
        await update.message.reply_text(
            f"✅ Порядковый номер сотрудника {name} успешно изменен на {target_num}.",
            reply_markup=MAIN_MENU
        )
    else:
        await update.message.reply_text("Сотрудник не найден в текущем списке отдела.", reply_markup=MAIN_MENU)
        
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
    await update.message.reply_text("Введите должность или отдел сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_POSITION

async def add_worker_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text.strip()
    await update.message.reply_text("Введите ID группы Telegram (или 0 для группы по умолчанию):", reply_markup=CANCEL_KEYBOARD)
    return ASK_GROUP

async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit(): return ASK_GROUP
    context.user_data["group_id"] = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    await update.message.reply_text("Выберите график отчетов (A или B):", reply_markup=SCHEDULE_KEYBOARD)
    return ASK_SCHEDULE

async def add_worker_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES: return ASK_SCHEDULE
    context.user_data["schedule"] = raw
    await update.message.reply_text("Нужно ли присылать ежедневный факт дня? (Да/Нет)", reply_markup=YES_NO_KEYBOARD)
    return ASK_NEEDS_DAILY_FACT

async def add_worker_needs_daily_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"): return ASK_NEEDS_DAILY_FACT

    upsert_worker(
        telegram_id=context.user_data["new_worker_id"],
        last_name=context.user_data["last_name"],
        first_name=context.user_data["first_name"],
        position=context.user_data["position"],
        group_id=context.user_data["group_id"],
        schedule=context.user_data["schedule"],
        needs_daily_fact=(raw == "да"),
    )
    await fetch_and_save_group_name(context.bot, context.user_data["group_id"])
    await update.message.reply_text("Сотрудник успешно добавлен в базу!", reply_markup=MAIN_MENU)
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
    await update.message.reply_text("Выберите отдел сотрудника для удаления:", reply_markup=kbd)
    return ASK_REMOVE_DEPARTMENT

async def delete_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows: return ConversationHandler.END
    context.user_data["remove_rows"] = [dict(r) for r in rows]
    await update.message.reply_text("Выберите кого удалить:", reply_markup=numbered_workers_keyboard(rows))
    return ASK_REMOVE_WORKER

async def delete_worker_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    rows = context.user_data.get("remove_rows", [])
    num_str = raw.split(".")[0].strip()
    if not num_str.isdigit(): return ASK_REMOVE_WORKER
    idx = int(num_str) - 1
    if idx < 0 or idx >= len(rows): return ASK_REMOVE_WORKER

    worker = rows[idx]
    context.user_data["worker_to_delete"] = worker
    kbd = ReplyKeyboardMarkup([["Да, удалить", "Нет, отмена"]], resize_keyboard=True)
    await update.message.reply_text(
        f"⚠️ Вы уверены, что хотите удалить сотрудника {worker['last_name']} {worker['first_name']} (ID: {worker['telegram_id']})?",
        reply_markup=kbd
    )
    return ASK_CONFIRM_DELETE

async def delete_worker_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "Да, удалить":
        worker = context.user_data.get("worker_to_delete")
        if worker:
            delete_worker(worker["telegram_id"])
            await update.message.reply_text(f"✅ Сотрудник {worker['last_name']} успешно удален.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Удаление сотрудника отменено.", reply_markup=MAIN_MENU)
        
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Просмотр сотрудников отдела и Сводка
# ══════════════════════════════════════════════════════════════════════════════

async def department_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел для просмотра:", reply_markup=kbd)
    return ASK_DEPARTMENT

async def department_workers_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text("Сотрудники не найдены.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    
    lines = [f"📋 Отдел: {position}"]
    for r in rows:
        lines.append(f"• {r['last_name']} {r['first_name']} (ID: {r['telegram_id']})")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def send_summary_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    summary_text = generate_daily_summary_text(date_str)
    
    await update.message.reply_text(summary_text, reply_markup=MAIN_MENU)
    
    if SUMMARY_CHAT_ID and SUMMARY_CHAT_ID != update.effective_chat.id:
        try:
            await context.bot.send_message(chat_id=SUMMARY_CHAT_ID, text=summary_text)
        except Exception as e:
            print(f"Не удалось отправить сводку в чат {SUMMARY_CHAT_ID}: {e}")


async def myreports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (user_id,)).fetchone()
    if not worker:
        await update.message.reply_text("Вы не зарегистрированы в системе. Обратитесь к администратору.")
        conn.close()
        return

    # Извлекаем отчеты за последние 7 дней для этого сотрудника
    now = now_local()
    seven_days_ago = (now - dt_module.timedelta(days=7)).strftime("%Y-%m-%d")
    reports = conn.execute(
        "SELECT * FROM reports WHERE telegram_id = ? AND report_date >= ? ORDER BY report_date DESC, received_at DESC",
        (user_id, seven_days_ago)
    ).fetchall()
    conn.close()

    if not reports:
        await update.message.reply_text("У вас нет отчетов за последние 7 дней.")
        return

    lines = [f"📋 Ваши отчеты за последние 7 дней ({worker['last_name']} {worker['first_name']}):\n"]
    current_date = None
    for r in reports:
        r_dict = dict(r)
        rep_date = r_dict["report_date"]
        if rep_date != current_date:
            current_date = rep_date
            lines.append(f"\n📅 {current_date}:")
        
        slot_time = r_dict["slot_time"]
        rep_type_str = f"Статус {slot_time}" if r_dict["report_type"] == "status" else "Факт дня (Итог)"
        is_ok_str = "✅ ОК" if r_dict["is_ok"] else "⚠️ НЕ ОК"
        comment_str = f"AI: {r_dict['format_comment']}" if r_dict["format_comment"] else "Проверка не требуется"
        
        lines.append(
            f"• {r_dict['received_at']} - {rep_type_str} | {is_ok_str}\n"
            f"  {comment_str}\n"
            f"  Текст: \"{r_dict['raw_text']}\""
        )
    
    full_text = "\n".join(lines)
    for part in split_message(full_text):
        await update.message.reply_text(part)


async def generate_and_send_csv(update: Update, context: ContextTypes.DEFAULT_TYPE, dept: str = None, only_facts: bool = False):
    await update.message.reply_text("⏳ Формирую выгрузку отчетов в формате CSV...")
    
    conn = get_db()
    conditions = []
    params = []
    
    if dept is not None:
        conditions.append("lower(w.position) = lower(?)")
        params.append(dept)
        
    if only_facts:
        conditions.append("r.report_type = 'daily_fact'")
        
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    
    query = f"""
        SELECT w.last_name, w.first_name, w.position, r.is_ok, r.format_comment, r.report_type, r.slot_time, r.received_at, r.report_date
        FROM reports r
        LEFT JOIN workers w ON r.telegram_id = w.telegram_id
        {where_clause}
        ORDER BY r.report_date DESC, r.received_at DESC
    """
    reports = conn.execute(query, params).fetchall()
    conn.close()
    
    if not reports:
        criteria_msg = "для выгрузки по данному критерию."
        if only_facts:
            criteria_msg = "по фактам дня."
        await update.message.reply_text(f"В базе данных пока нет ни одного отчета {criteria_msg}", reply_markup=MAIN_MENU)
        return
        
    output = io.StringIO()
    # Записываем BOM для Excel, чтобы он корректно отображал UTF-8 символы
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';', lineterminator='\n')
    
    # Заголовки - теперь включаем Время сдачи
    writer.writerow([
        "Имя", "Фамилия", "Должность", "Отдел", "Дата", "Тип отчета", "Слот", "Время сдачи", "Статус", "Причина замечания"
    ])
    
    for r in reports:
        last_name = r["last_name"] or "Неизвестный"
        first_name = r["first_name"] or ""
        position = r["position"] or "Не указано"
        
        status_str = "Сдал" if r["is_ok"] == 1 else "Не сдал"
        
        comment_str = ""
        if r["is_ok"] == 0:
            comment_str = r["format_comment"] or "Без комментария"
            if comment_str.startswith("не ОК, "):
                comment_str = comment_str[len("не ОК, "):]
            elif comment_str.startswith("не ОК: "):
                comment_str = comment_str[len("не ОК: "):]
        
        r_type_rus = "Статус" if r["report_type"] == "status" else "Факт дня"
        slot_str = r["slot_time"] or "-"
        received_at_str = r["received_at"] or "-"
        report_date_str = r["report_date"] or "-"
        
        writer.writerow([
            first_name,
            last_name,
            position,
            position,
            report_date_str,
            r_type_rus,
            slot_str,
            received_at_str,
            status_str,
            comment_str
        ])
        
    csv_data = output.getvalue().encode('utf-8')
    bio = io.BytesIO(csv_data)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    prefix = "facts" if only_facts else "reports"
    
    if dept is None:
        bio.name = f"{prefix}_all_{timestamp}.csv"
        caption = "📊 Общая выгрузка фактов дня успешно сформирована!" if only_facts else "📊 Общая выгрузка всех отчетов успешно сформирована!"
    else:
        safe_dept = "".join(c for c in dept if c.isalnum() or c in (" ", "_", "-")).strip()
        bio.name = f"{prefix}_dept_{safe_dept}_{timestamp}.csv"
        caption = f"📊 Выгрузка фактов дня для отдела «{dept}» успешно сформирована!" if only_facts else f"📊 Выгрузка отчетов для отдела «{dept}» успешно сформирована!"
        
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=bio,
        filename=bio.name,
        caption=caption,
        reply_markup=MAIN_MENU
    )


async def export_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END
        
    kbd = ReplyKeyboardMarkup([
        ["📊 Общая выгрузка", "🏢 Выгрузка по отделу"],
        ["❌ Отмена"]
    ], resize_keyboard=True)
    
    await update.message.reply_text(
        "Выберите тип выгрузки отчетов:",
        reply_markup=kbd
    )
    return ASK_EXPORT_TYPE


async def export_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "❌ Отмена":
        await update.message.reply_text("Выгрузка отменена.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    if choice == "📊 Общая выгрузка":
        await generate_and_send_csv(update, context, dept=None)
        return ConversationHandler.END
        
    if choice == "🏢 Выгрузка по отделу":
        conn = get_db()
        rows = conn.execute("SELECT DISTINCT position FROM workers ORDER BY position").fetchall()
        conn.close()
        
        depts = [r["position"] for r in rows if r["position"] and r["position"] != "Не указано"]
        if not depts:
            await update.message.reply_text(
                "В базе данных нет зарегистрированных отделов у сотрудников.",
                reply_markup=MAIN_MENU
            )
            return ConversationHandler.END
            
        kbd_rows = [[d] for d in depts]
        kbd_rows.append(["❌ Отмена"])
        kbd = ReplyKeyboardMarkup(kbd_rows, resize_keyboard=True)
        
        await update.message.reply_text(
            "Выберите отдел для выгрузки:",
            reply_markup=kbd
        )
        return ASK_EXPORT_DEPARTMENT
        
    await update.message.reply_text("Пожалуйста, выберите один из вариантов на клавиатуре.")
    return ASK_EXPORT_TYPE


async def export_department_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "❌ Отмена":
        await update.message.reply_text("Выгрузка отменена.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    await generate_and_send_csv(update, context, dept=choice)
    return ConversationHandler.END


async def check_missing_reports_job(context: ContextTypes.DEFAULT_TYPE):
    now = now_local()
    
    # Слот наступает 30 минут назад
    slot_dt = now - dt_module.timedelta(minutes=30)
    expected_slot = slot_dt.strftime("%H:%M")
    
    conn = get_db()
    workers = conn.execute("SELECT * FROM workers WHERE is_active = 1").fetchall()
    date_str = now.strftime("%Y-%m-%d")
    
    reports = conn.execute(
        "SELECT telegram_id, slot_time FROM reports WHERE report_date = ? AND report_type = 'status'",
        (date_str,)
    ).fetchall()
    conn.close()
    
    submitted_worker_slots = {(r["telegram_id"], r["slot_time"]) for r in reports}
    
    for w in workers:
        sched_slots = SCHEDULES.get(w["schedule"], SCHEDULE_A)
        if expected_slot in sched_slots:
            tid = w["telegram_id"]
            if (tid, expected_slot) not in submitted_worker_slots:
                # Отправляем в ЛС сотруднику
                try:
                    await context.bot.send_message(
                        chat_id=tid,
                        text=f"⏰ Напоминание! Вы забыли отправить отчет за слот **{expected_slot}** в систему.\n\nПожалуйста, отправьте его прямо сейчас голосовым сообщением, кружком, видео или текстом."
                    )
                except Exception as e:
                    logger.warning(f"Ошибка личного уведомления о пропуске слота {tid}: {e}")
                
                # Отправляем в рабочую группу
                group_id = w["group_id"] or DEFAULT_GROUP_ID
                try:
                    await context.bot.send_message(
                        chat_id=group_id,
                        text=f"⚠️ {w['last_name']} {w['first_name']} не предоставил вовремя отчет за статус {expected_slot}."
                    )
                except Exception as e:
                    logger.warning(f"Ошибка отправки предупреждения нарушителя в группу {group_id}: {e}")


async def remind_all_missing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    
    now = now_local()
    current_minutes = now.hour * 60 + now.minute
    date_str = now.strftime("%Y-%m-%d")
    
    conn = get_db()
    workers = conn.execute("SELECT * FROM workers WHERE is_active = 1").fetchall()
    reports = conn.execute(
        "SELECT telegram_id, slot_time FROM reports WHERE report_date = ? AND report_type = 'status'",
        (date_str,)
    ).fetchall()
    conn.close()
    
    submitted_worker_slots = {(r["telegram_id"], r["slot_time"]) for r in reports}
    reminded_count = 0
    
    for w in workers:
        sched_slots = SCHEDULES.get(w["schedule"], SCHEDULE_A)
        tid = w["telegram_id"]
        overdue_slots = []
        
        for slot in sched_slots:
            hour, minute = map(int, slot.split(":"))
            slot_minutes = hour * 60 + minute
            if slot_minutes <= current_minutes:
                if (tid, slot) not in submitted_worker_slots:
                    overdue_slots.append(slot)
                    
        if overdue_slots:
            slots_str = ", ".join(overdue_slots)
            try:
                # Напоминание сотруднику
                await context.bot.send_message(
                    chat_id=tid,
                    text=f"⏰ Срочное напоминание! Вы пропустили отправку отчетов за слоты (сегменты): **{slots_str}**.\n\nПожалуйста, немедленно отправьте отчет в бот!"
                )
                reminded_count += 1
                
                # Сообщение в чат бригады
                group_id = w["group_id"] or DEFAULT_GROUP_ID
                await context.bot.send_message(
                    chat_id=group_id,
                    text=f"⚠️ {w['last_name']} {w['first_name']} пропустил отправку отчетов за слоты: {slots_str}. Напоминание направлено в ЛС."
                )
            except Exception as e:
                logger.warning(f"Ошибка ручной отправки напоминаний для {tid}: {e}")
                
    await update.message.reply_text(
        f"✅ Проверка завершена. Разослано напоминаний должникам: {reminded_count}.",
        reply_markup=MAIN_MENU
    )


# ══════════════════════════════════════════════════════════════════════════════
# Настройка расписания автоматической сводки (Время сводки)
# ══════════════════════════════════════════════════════════════════════════════

async def summary_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    times = get_scheduled_times()
    times_str = ", ".join(times) if times else "не настроено"
    
    text = (
        f"⏰ Текущее автоматическое расписание сводки: {times_str}\n\n"
        f"Выберите действие:"
    )
    kbd = ReplyKeyboardMarkup([
        ["➕ Добавить время", "➖ Удалить время"],
        ["❌ Назад"]
    ], resize_keyboard=True)
    
    await update.message.reply_text(text, reply_markup=kbd)
    return ASK_REPORT_TIME

async def summary_time_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = update.message.text.strip()
    
    if action == "❌ Назад":
        await update.message.reply_text("Возврат в главное меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    if action == "➕ Добавить время":
        await update.message.reply_text(
            "Введите новое время в формате ЧЧ:ММ (например, 19:30):",
            reply_markup=CANCEL_KEYBOARD
        )
        return ASK_EDIT_SCHEDULE
        
    if action == "➖ Удалить время":
        times = get_scheduled_times()
        if not times:
            await update.message.reply_text("В расписании пока нет сохраненного времени сводок.", reply_markup=MAIN_MENU)
            return ConversationHandler.END
            
        kbd = ReplyKeyboardMarkup([[t] for t in times] + [["❌ Отмена"]], resize_keyboard=True)
        await update.message.reply_text(
            "Выберите время для удаления:",
            reply_markup=kbd
        )
        return ASK_ORDER_DEPARTMENT
        
    await update.message.reply_text("Пожалуйста, нажмите на одну из предложенных кнопок.")
    return ASK_REPORT_TIME

async def summary_time_add_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    parts = raw.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        await update.message.reply_text("Некорректный формат времени. Попробуйте еще раз (ЧЧ:ММ, например, 19:30):")
        return ASK_EDIT_SCHEDULE
        
    hour, minute = int(parts[0]), int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await update.message.reply_text("Неподходящие часы/минуты. Диапазоны: 00-23 и 00-59:")
        return ASK_EDIT_SCHEDULE
        
    time_str = f"{hour:02d}:{minute:02d}"
    times = get_scheduled_times()
    if time_str in times:
        await update.message.reply_text("Это время уже содержится в расписании сводки.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    times.append(time_str)
    save_scheduled_times(times)
    reschedule_summary_jobs(context.application)
    
    await update.message.reply_text(f"✅ Время {time_str} успешно внесено в расписание!", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def summary_time_del_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw == "❌ Отмена":
        await update.message.reply_text("Удаление отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    times = get_scheduled_times()
    if raw not in times:
        await update.message.reply_text("Пожалуйста, выберите существующий элемент из списка:")
        return ASK_ORDER_DEPARTMENT
        
    times.remove(raw)
    save_scheduled_times(times)
    reschedule_summary_jobs(context.application)
    
    await update.message.reply_text(f"✅ Время сводки {raw} успешно удалено из расписания!", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Прием отчетов от сотрудников (Голос / Видео / Текст)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Проверка, не редактирует ли администратор комментарий ИИ к отчету
    if is_admin(user_id) and context.user_data.get("editing_comment_report_id"):
        report_id = context.user_data["editing_comment_report_id"]
        original_chat_id = context.user_data.get("editing_comment_chat_id")
        original_msg_id = context.user_data.get("editing_comment_message_id")
        original_text = context.user_data.get("editing_comment_original_text", "")
        
        # Сброс состояния
        del context.user_data["editing_comment_report_id"]
        context.user_data.pop("editing_comment_chat_id", None)
        context.user_data.pop("editing_comment_message_id", None)
        context.user_data.pop("editing_comment_original_text", None)
        prompt_message_id = context.user_data.pop("editing_comment_prompt_message_id", None)
        
        # Попытка удалить сообщение-запрос и ответ администратора
        if prompt_message_id and original_chat_id:
            try:
                await context.bot.delete_message(chat_id=original_chat_id, message_id=prompt_message_id)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass
            
        new_comment = update.message.text.strip() if update.message.text else ""
        if not new_comment or new_comment.lower() in ("отмена", "❌ отмена"):
            return
            
        conn = get_db()
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not report:
            conn.close()
            return
            
        # Обновляем комментарий в БД
        new_action = f"Комментарий изменен администратором вручную: {new_comment}"
        conn.execute(
            "UPDATE reports SET format_comment = ?, required_action = ? WHERE id = ?",
            (new_comment, new_action, report_id)
        )
        # Получаем обновленное состояние
        report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        worker = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (report["telegram_id"],)).fetchone()
        conn.commit()
        conn.close()
        
        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        status_emoji = "✅" if report["is_ok"] == 1 else "⚠️"
        
        # Обновляем оригинальное сообщение в группе / чате
        if original_chat_id and original_msg_id:
            try:
                # Если исходное сообщение в укороченном формате
                if "Официальный отчет:" not in (original_text or ""):
                    updated_text = (
                        f"🔧 Оценка отчета изменена вручную администратором @{update.effective_user.username or user_id}:\n"
                        f"Сотрудник: {worker_name}\n"
                        f"Дата отчета: {report['report_date']}\n"
                        f"Статус: {report['slot_time'] or report['report_type']}\n"
                        f"Новый статус: {status_emoji} ({new_comment})"
                    )
                else:
                    updated_text = update_message_text_fields(original_text, report["is_ok"] == 1, new_comment)
                
                kbd = make_report_keyboard(report_id, report["report_type"] if report else None)
                await context.bot.edit_message_text(
                    chat_id=original_chat_id,
                    message_id=original_msg_id,
                    text=updated_text,
                    reply_markup=kbd
                )
            except Exception as e:
                print(f"Ошибка при обновлении сообщения после редактирования комментария: {e}")
        return

    worker = get_worker(user_id)
    
    text_content = ""
    # Решение проблемы 2: Обеспечение гарантированного создания каталога tmp/ и очистки через try/finally
    tmp_path = None
    
    try:
        if update.message.text:
            text_content = update.message.text.strip()
        else:
            file_obj = None
            if update.message.voice: file_obj = update.message.voice
            elif update.message.video: file_obj = update.message.video
            elif update.message.video_note: file_obj = update.message.video_note

            if file_obj:
                await update.message.reply_text("🎙 Отчет получен и отправлен на транскрибацию ИИ, ожидайте оценки...")
                tg_file = await context.bot.get_file(file_obj.file_id)
                ext = "mp4" if update.message.video or update.message.video_note else "ogg"
                
                os.makedirs("tmp", exist_ok=True)
                tmp_path = f"tmp/file_{user_id}_{int(datetime.now().timestamp())}.{ext}"
                
                await tg_file.download_to_drive(tmp_path)
                text_content = await transcribe_audio_async(tmp_path)

        if not text_content:
            await update.message.reply_text("Ошибка: Не удалось распознать аудио или медиа отчета.")
            return

        if text_content.startswith("Ошибка распознавания аудио"):
            await update.message.reply_text("❌ При распознавании аудио произошла ошибка. Пожалуйста, отправьте текстовый отчет или попробуйте перезаписать.")
            return

        is_media = bool(update.message.voice or update.message.video or update.message.video_note)

        # Решение проблемы 3: Сохранение данных незарегистрированных сотрудников по TELEGRAM_ID
        if not worker:
            user_info = {
                "first_name": update.effective_user.first_name or "",
                "last_name": update.effective_user.last_name or "",
                "username": update.effective_user.username or "",
                "timestamp": datetime.now().isoformat(),
                "text": text_content
            }
            pending_unregistered_users[user_id] = user_info
            
            # Оповещение администраторов
            admin_msg = (
                f"👤 Обнаружен отчет от незарегистрированного сотрудника!\n"
                f"TG ID: {user_id}\n"
                f"Имя в Telegram: {user_info['first_name']} {user_info['last_name']} (@{user_info['username']})\n"
                f"Текст:\n\"{text_content[:300]}\"\n\n"
                f"Вы можете добавить его в базу через меню, указав ID."
            )
            for admin_id in ADMIN_IDS:
                try:
                    admin_copied_msg_id = None
                    if is_media:
                        try:
                            admin_copied = await context.bot.copy_message(
                                chat_id=admin_id,
                                from_chat_id=update.effective_chat.id,
                                message_id=update.message.message_id
                            )
                            admin_copied_msg_id = admin_copied.message_id
                        except Exception as copy_err:
                            print(f"Ошибка копирования медиа незарегистрированного пользователя администратору {admin_id}: {copy_err}")
                    
                    if admin_copied_msg_id:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=admin_msg,
                            reply_to_message_id=admin_copied_msg_id
                        )
                    else:
                        await context.bot.send_message(chat_id=admin_id, text=admin_msg)
                except Exception:
                    pass

            await update.message.reply_text(
                "Ошибка: Вы не зарегистрированы в системе авторизации бота.\n"
                "Ваш отчет отправлен администраторам как временный."
            )
            return

        # Анализ промптом Llama для определения типа отчета
        ai_res_pre = await check_status_async(text_content)
        report_type = ai_res_pre["report_type"]
        now = now_local()
        date_str = now.strftime("%Y-%m-%d")
        sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
        nearest_slot, is_late = find_nearest_slot(sched_list, now)

        # Проверяем, существует ли отчет у этого сотрудника за этот слот (status) или день (daily_fact)
        conn = get_db()
        if report_type == "status":
            existing_report = conn.execute(
                "SELECT * FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'status' AND slot_time = ?",
                (user_id, date_str, nearest_slot)
            ).fetchone()
        else:
            existing_report = conn.execute(
                "SELECT * FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'daily_fact'",
                (user_id, date_str)
            ).fetchone()
        conn.close()

        is_addon = False
        if existing_report:
            is_addon = True
            existing_raw = existing_report["raw_text"] or ""
            # Склеиваем предыдущий текст и новое дополнение
            text_content = f"{existing_raw}\n[Дополнение]: {text_content}" if existing_raw else text_content
            # Прогоняем КЛАССИФИКАЦИЮ и АНАЛИЗ заново для объединенного контента
            ai_res = await check_status_async(text_content)
            cleaned_text = await clean_report_async(text_content)
            report_id = existing_report["id"]
            
            # Обновляем существующий отчет в БД
            update_report_text_and_ai(
                report_id=report_id,
                is_ok=ai_res["is_ok"],
                format_comment=ai_res["format_comment"],
                required_action=ai_res["required_action"],
                raw_text=text_content,
                received_at=now.strftime("%H:%M:%S")
            )
        else:
            ai_res = ai_res_pre
            cleaned_text = await clean_report_async(text_content)
            report_id = save_report(
                telegram_id=user_id,
                report_date=date_str,
                report_type=ai_res["report_type"],
                slot_time=nearest_slot if ai_res["report_type"] == "status" else None,
                received_at=now.strftime("%H:%M:%S"),
                is_ok=ai_res["is_ok"],
                is_late=is_late if ai_res["report_type"] == "status" else 0,
                format_comment=ai_res["format_comment"],
                required_action=ai_res["required_action"],
                raw_text=text_content
            )

        w_name = f"{worker['last_name']} {worker['first_name']}"
        
        # Информирование сотрудника
        if ai_res["is_ok"]:
            if is_addon:
                await update.message.reply_text("🔄 Дополнение к отчёту успешно проверено ИИ и принято без замечаний! Спасибо.")
            else:
                await update.message.reply_text("✅ Отчёт успешно проверен ИИ и принят без замечаний! Спасибо.")
        else:
            if is_addon:
                await update.message.reply_text(f"⚠️ Оценка дополненного отчета: {ai_res['employee_message']}")
            else:
                await update.message.reply_text(f"⚠️ Оценка отчета: {ai_res['employee_message']}")

        # Решение проблемы 7: Кнопка «Исправить оценку» во всех отчетах для администраторов или в группе
        dest_chat = worker["group_id"] or DEFAULT_GROUP_ID
        
        # Получаем красивое название группы
        gname = await get_group_name_async(context.bot, dest_chat)
        
        title_text = f"Дополнение к отчету (отчет обновлен): {w_name}" if is_addon else f"Новый отчет: {w_name}"

        orig_label = "🗣 Оригинальный текст (объединенный):" if is_addon else "🗣 Оригинальный текст:"
        notify_text = (
            f"{title_text}\n"
            f"{format_status_or_fact_line(ai_res['report_type'], nearest_slot if ai_res['report_type'] == 'status' else None, date_str)}\n"
            f"Оценка ИИ: {'ОК' if ai_res['is_ok'] else 'НЕ ОК'}\n"
            f"Комментарий ИИ: {ai_res['format_comment']}\n\n"
            f"📝 Официальный отчет:\n\"{cleaned_text}\"\n\n"
            f"{orig_label}\n\"{text_content}\""
        )
        
        inline_kbd = make_report_keyboard(report_id, ai_res["report_type"])
        
        copied_msg_id = None
        if is_media:
            try:
                copied_msg = await context.bot.copy_message(
                    chat_id=dest_chat,
                    from_chat_id=update.effective_chat.id,
                    message_id=update.message.message_id
                )
                copied_msg_id = copied_msg.message_id
            except Exception as e:
                print(f"Ошибка копирования медиа в чат {dest_chat}: {e}")

        try:
            if copied_msg_id:
                await context.bot.send_message(
                    chat_id=dest_chat,
                    text=notify_text,
                    reply_markup=inline_kbd,
                    reply_to_message_id=copied_msg_id
                )
            else:
                await context.bot.send_message(
                    chat_id=dest_chat,
                    text=notify_text,
                    reply_markup=inline_kbd
                )
        except Exception as e:
            # Предохранитель: если отправка в группу сломалась, дублируем всем админам
            print(f"Ошибка отправки оценки в чат {dest_chat}: {e}")
            for admin_id in ADMIN_IDS:
                try:
                    admin_copied_msg_id = None
                    if is_media:
                        try:
                            admin_copied = await context.bot.copy_message(
                                chat_id=admin_id,
                                from_chat_id=update.effective_chat.id,
                                message_id=update.message.message_id
                            )
                            admin_copied_msg_id = admin_copied.message_id
                        except Exception:
                            pass
                    
                    if admin_copied_msg_id:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=notify_text,
                            reply_markup=inline_kbd,
                            reply_to_message_id=admin_copied_msg_id
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=notify_text,
                            reply_markup=inline_kbd
                        )
                except Exception:
                    pass
    
    finally:
        # Решение проблемы 2: безусловная чистка временного файла
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as rm_e:
                print(f"Ошибка удаления файла {tmp_path}: {rm_e}")


# ══════════════════════════════════════════════════════════════════════════════
# Инициализация и запуск приложения
# ══════════════════════════════════════════════════════════════════════════════

async def post_init(application: Application):
    """Срабатывает после запуска приложения Telegram."""
    # Решение проблемы 8: автоматическое сохранение имени группы по умолчанию при старте
    try:
        chat = await application.bot.get_chat(DEFAULT_GROUP_ID)
        name = chat.title or str(DEFAULT_GROUP_ID)
        save_group_name(DEFAULT_GROUP_ID, name)
        print(f"Группа по умолчанию кэширована: {name}")
    except Exception as e:
        print(f"Не удалось получить название DEFAULT_GROUP_ID {DEFAULT_GROUP_ID}: {e}")

    # Решение проблемы 12: Восстановление автосводок из БД при старте / перезапуске
    reschedule_summary_jobs(application)

    # Периодическая фоновая проверка забытых/пропущенных отчетов (через 30 минут после слотов)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_missing_reports_job, interval=60, first=10)

def main():
    init_db()
    if not TOKEN:
        print("Критическая ошибка: не задан TELEGRAM_TOKEN")
        return

    # Запуск бота с подключением к функции post_init для восстановления кеша и сводок
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Точечные команды и кнопки меню (срабатывают моментально)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myreports", myreports))
    application.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))
    application.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), send_summary_now))
    application.add_handler(MessageHandler(filters.Regex("^📣 Напомнить всем$"), remind_all_missing))
    
    # Callback-кнопки (для изменения оценок)
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Диалоговые обработчики (ConversationHandlers)
    list_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📋 Сотрудники$"), list_workers)],
        states={
            ASK_LIST_DEPARTMENT: [MessageHandler(DIALOG_TEXT, list_workers_department)],
            ASK_LIST_WORKER: [MessageHandler(DIALOG_TEXT, list_workers_select)],
            ASK_EDIT_FIELD: [MessageHandler(DIALOG_TEXT, list_workers_action)],
            ASK_EDIT_VALUE: [MessageHandler(DIALOG_TEXT, edit_value_finish)],
            ASK_EDIT_GROUP_VALUE: [MessageHandler(DIALOG_TEXT, edit_group_finish)],
            ASK_EDIT_SCHEDULE: [MessageHandler(DIALOG_TEXT, edit_schedule_finish)],
            ASK_EDIT_DAILY_FACT: [MessageHandler(DIALOG_TEXT, edit_daily_fact_finish)],
            ASK_EDIT_STATUS_WORK: [MessageHandler(DIALOG_TEXT, edit_status_work_finish)],
            ASK_EDIT_SORT_ORDER: [MessageHandler(DIALOG_TEXT, edit_sort_order_finish)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    add_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить сотрудника$"), add_worker_start)],
        states={
            ASK_WORKER_ID: [MessageHandler(DIALOG_TEXT, add_worker_id)],
            ASK_LASTNAME: [MessageHandler(DIALOG_TEXT, add_worker_lastname)],
            ASK_FIRSTNAME: [MessageHandler(DIALOG_TEXT, add_worker_firstname)],
            ASK_POSITION: [MessageHandler(DIALOG_TEXT, add_worker_position)],
            ASK_GROUP: [MessageHandler(DIALOG_TEXT, add_worker_group)],
            ASK_SCHEDULE: [MessageHandler(DIALOG_TEXT, add_worker_schedule)],
            ASK_NEEDS_DAILY_FACT: [MessageHandler(DIALOG_TEXT, add_worker_needs_daily_fact)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    delete_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить сотрудника$"), delete_worker_start)],
        states={
            ASK_REMOVE_DEPARTMENT: [MessageHandler(DIALOG_TEXT, delete_worker_department)],
            ASK_REMOVE_WORKER: [MessageHandler(DIALOG_TEXT, delete_worker_finish)],
            ASK_CONFIRM_DELETE: [MessageHandler(DIALOG_TEXT, delete_worker_confirm)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    view_dept_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🏢 Сотрудники отдела$"), department_workers_start)],
        states={
            ASK_DEPARTMENT: [MessageHandler(DIALOG_TEXT, department_workers_show)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    # Решение проблемы 12: хэндлер для настройки времени сводки
    summary_scheduler_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏰ Время сводки$"), summary_time_start)],
        states={
            ASK_REPORT_TIME: [MessageHandler(DIALOG_TEXT, summary_time_action)],
            ASK_EDIT_SCHEDULE: [MessageHandler(DIALOG_TEXT, summary_time_add_finish)],
            ASK_ORDER_DEPARTMENT: [MessageHandler(DIALOG_TEXT, summary_time_del_finish)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    export_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📥 Выгрузить отчеты$"), export_start)],
        states={
            ASK_EXPORT_TYPE: [MessageHandler(DIALOG_TEXT, export_type_selected)],
            ASK_EXPORT_DEPARTMENT: [MessageHandler(DIALOG_TEXT, export_department_selected)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    import_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📥 Импорт сотрудников$"), import_workers_start)],
        states={
            ASK_IMPORT_FILE: [MessageHandler(filters.Document.ALL, import_workers_file)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )

    # Регистрация диалогов
    application.add_handler(list_handler)
    application.add_handler(add_handler)
    application.add_handler(delete_handler)
    application.add_handler(view_dept_handler)
    application.add_handler(summary_scheduler_handler)
    application.add_handler(export_handler)
    application.add_handler(import_handler)

    # Хэндлер для приема аудио/видео/текстовых отчетов сотрудников (регистрируется в самом конце)
    application.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.TEXT & ~filters.COMMAND, 
        handle_report
    ))

    print("Бот успешно инициализирован и запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
