import json
import os
import sqlite3
import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from groq import Groq
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# 1. Список администраторов вместо одного ID
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x.strip().isdigit()}
if not ADMIN_IDS and os.environ.get("ADMIN_ID"):
    ADMIN_IDS = {int(os.environ.get("ADMIN_ID"))}

SUMMARY_CHAT_ID = int(os.environ.get("SUMMARY_CHAT_ID", "0")) or (list(ADMIN_IDS)[0] if ADMIN_IDS else 0)

LOCAL_TZ = ZoneInfo("Europe/Chisinau")

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

LATE_THRESHOLD_MIN = 15
SCHEDULE_A = ["10:00", "12:00", "15:00", "17:00"]
SCHEDULE_B = ["11:00", "13:00", "16:00", "18:00"]
SCHEDULES = {"A": SCHEDULE_A, "B": SCHEDULE_B}

# Состояния ConversationHandler
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
) = range(18)

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Сотрудники", "📊 Сводка сейчас"],
        ["➕ Добавить сотрудника", "➖ Удалить сотрудника"],
        ["⏰ Время сводки", "🆔 ID чата"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
SCHEDULE_KEYBOARD = ReplyKeyboardMarkup([["A", "B"], ["❌ Отмена"]], resize_keyboard=True)
YES_NO_KEYBOARD = ReplyKeyboardMarkup([["Да", "Нет"], ["❌ Отмена"]], resize_keyboard=True)
CANCEL_TEXT = "❌ Отмена"
DIALOG_TEXT = filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^{CANCEL_TEXT}$")

# ══════════════════════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
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
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # 12. Сохранение системных настроек в базу данных
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            group_id INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL
        )
        """
    )
    # 4. и 5. Исправление дублей и явное разделение на status и daily_fact
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
            UNIQUE(telegram_id, report_date, report_type, slot_time) ON CONFLICT REPLACE
        )
        """
    )
    
    # Миграция колонок на всякий случай
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    for col, definition in [
        ("schedule", "TEXT NOT NULL DEFAULT 'A'"),
        ("needs_daily_fact", "INTEGER NOT NULL DEFAULT 1"),
        ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {col} {definition}")
            
    conn.commit()
    conn.close()

def get_worker(telegram_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row

def get_all_workers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name").fetchall()
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

def upsert_worker(telegram_id: int, last_name: str, first_name: str, position: str, group_id: int, schedule: str, needs_daily_fact: bool, sort_order: int = 0):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            last_name=excluded.last_name, first_name=excluded.first_name, position=excluded.position,
            group_id=excluded.group_id, schedule=excluded.schedule, needs_daily_fact=excluded.needs_daily_fact,
            sort_order=excluded.sort_order
        """,
        (telegram_id, last_name, first_name, position, group_id, schedule, int(needs_daily_fact), sort_order),
    )
    conn.commit()
    conn.close()

def update_worker_field(telegram_id: int, field: str, value):
    conn = get_db()
    # 9. Сброс индекса сортировки при переводе в другой отдел
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
        "INSERT INTO groups (group_id, group_name) VALUES (?, ?) ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name",
        (group_id, group_name),
    )
    conn.commit()
    conn.close()

def get_group_name(group_id: int) -> str:
    conn = get_db()
    row = conn.execute("SELECT group_name FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    conn.close()
    return row["group_name"] if row else str(group_id)

def get_all_group_names() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT group_id, group_name FROM groups").fetchall()
    conn.close()
    return {row["group_id"]: row["group_name"] for row in rows}

async def fetch_and_save_group_name(bot, group_id: int) -> str:
    try:
        chat = await bot.get_chat(group_id)
        name = chat.title or str(group_id)
    except Exception:
        name = str(group_id)
    save_group_name(group_id, name)
    return name

# 6. История отчетов по сотруднику
def get_worker_history(telegram_id: int, limit: int = 7):
    conn = get_db()
    rows = conn.execute(
        "SELECT report_date, report_type, slot_time, is_ok, format_comment FROM reports WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
        (telegram_id, limit)
    ).fetchall()
    conn.close()
    return rows

# 7. Изменение оценки ИИ вручную администратором
def update_report_status_by_id(report_id: int, is_ok: bool, comment: str, action: str):
    conn = get_db()
    conn.execute(
        "UPDATE reports SET is_ok = ?, format_comment = ?, required_action = ? WHERE id = ?",
        (int(is_ok), comment, action, report_id)
    )
    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def require_admin(update: Update) -> bool:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта кнопка доступна только администратору.", reply_markup=ReplyKeyboardRemove())
        return False
    return True

def menu_for_user(user_id: int):
    return MAIN_MENU if is_admin(user_id) else ReplyKeyboardRemove()

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
# ИИ АНАЛИЗАТОР
# ══════════════════════════════════════════════════════════════════════════════

def normalize_ai_result(data: dict, source_text: str) -> dict:
    text_lower = source_text.lower()
    report_type = str(data.get("report_type", "status")).strip().lower()
    fact_words = ("факт", "факт дня", "за день", "итог дня", "итоги дня", "сегодня за день", "дневной отчет")

    if any(word in text_lower for word in fact_words):
        report_type = "daily_fact"
    else:
        report_type = "status"

    is_ok = bool(data.get("is_ok", False))
    issue = str(data.get("issue") or data.get("format_comment") or "").strip()
    required_action = str(data.get("required_action") or "").strip()
    employee_message = str(data.get("employee_message") or "").strip()

    if is_ok:
        format_comment = "всё ОК"
        required_action = "ничего не предпринимать"
    else:
        if not issue: issue = "есть замечания по отчету"
        format_comment = f"не ОК, {issue}"
        required_action = f"сделал замечание сотруднику: {issue}"
        if not employee_message: employee_message = f"В отчете есть замечание: {issue}."

    return {
        "report_type": report_type,
        "is_ok": is_ok,
        "format_comment": format_comment,
        "required_action": required_action,
        "employee_message": employee_message,
    }

def find_nearest_slot(schedule: list[str], now: datetime):
    current_minutes = now.hour * 60 + now.minute
    nearest_slot, nearest_diff = None, None
    for slot in schedule:
        hour, minute = map(int, slot.split(":"))
        diff = abs(current_minutes - (hour * 60 + minute))
        if nearest_diff is None or diff < nearest_diff:
            nearest_diff = diff
            nearest_slot = slot
    return nearest_slot, (nearest_diff is not None and nearest_diff > LATE_THRESHOLD_MIN)

def transcribe_audio(file_path: str) -> str:
    if groq_client is None: return "Не задан GROQ_API_KEY."
    try:
        with open(file_path, "rb") as f:
            return groq_client.audio.transcriptions.create(
                file=(os.path.basename(file_path), f), model="whisper-large-v3", language="ru", response_format="text"
            ).strip()
    except Exception as e:
        return f"Ошибка распознавания: {e}"

# 10. Замена на мощную контекстную модель Llama-3.3-70b
CHECK_PROMPT_TEMPLATE = """
Ты — строгий проверяющий строительных видеоотчётов.
Верни только плоский JSON:
{{
  "report_type": "status" или "daily_fact",
  "is_ok": true или false,
  "issue": "замечание или пустая строка",
  "required_action": "действие",
  "employee_message": "сообщение работнику"
}}
Текст отчета: {text}
"""

def check_status(text: str) -> dict:
    if groq_client is None: return normalize_ai_result({"is_ok": False}, text)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай только валидным JSON без markdown."},
                {"role": "user", "content": CHECK_PROMPT_TEMPLATE.format(text=text)},
            ],
            temperature=0,
            response_format={"type": "json_object"}
        )
        return normalize_ai_result(json.loads(response.choices[0].message.content.strip()), text)
    except Exception as e:
        return normalize_ai_result({"is_ok": False, "issue": f"Ошибка ИИ: {e}"}, text)

# ══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ ИНТЕРФЕЙСА УПРАВЛЕНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Привет! Отправьте видеоотчет, когда он будет готов.", reply_markup=ReplyKeyboardRemove())

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID чата: {update.effective_chat.id}", reply_markup=menu_for_user(update.effective_user.id))

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

    # 11. Сообщения объединены в один вызов для улучшения UX
    await update.message.reply_text("\n".join(lines) + "\n\nВыберите сотрудника для действий:", reply_markup=numbered_workers_keyboard(rows))
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
    gname = get_group_name(worker["group_id"])
    info = f"👤 {worker['last_name']} {worker['first_name']}\nОтдел: {worker['position']}\nГрафик: {worker['schedule']} ({schedule_str})\nГруппа: {gname}\nФакт дня: {'да' if worker['needs_daily_fact'] else 'нет'}"

    kbd = ReplyKeyboardMarkup(
        [
            ["✏️ Изменить фамилию", "✏️ Изменить имя"],
            ["✏️ Изменить отдел", "✏️ Изменить график"],
            ["✏️ Изменить группу", "✏️ Факт дня"],
            ["📊 История отчетов", "🔼 Вверх", "🔽 Вниз"],
            ["❌ Отмена"],
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

    if not worker: return ConversationHandler.END

    if action == "📊 История отчетов":
        history = get_worker_history(worker["telegram_id"])
        if not history:
            await update.message.reply_text("История пуста.", reply_markup=MAIN_MENU)
        else:
            text = f"📋 Последние отчеты {worker['last_name']}:\n\n"
            for h in history:
                text += f"{'✅' if h['is_ok'] else '❌'} [{h['report_date']}] {h['report_type']} ({h['slot_time'] or 'Итог'}): {h['format_comment']}\n"
            await update.message.reply_text(text, reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    if action in ("🔼 Вверх", "🔽 Вниз"):
        target_idx = idx - 1 if action == "🔼 Вверх" else idx + 1
        if target_idx < 0 or target_idx >= len(rows):
            await update.message.reply_text("Предел списка.", reply_markup=MAIN_MENU)
            return ConversationHandler.END
        swap_sort_order(worker["telegram_id"], rows[target_idx]["telegram_id"])
        await update.message.reply_text("Порядок изменен.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    field_map = {
        "✏️ Изменить фамилию": ("last_name", "Новая фамилия:"),
        "✏️ Изменить имя": ("first_name", "Новое имя:"),
        "✏️ Изменить отдел": ("position", "Новый отдел (позиция сбросится):"),
        "✏️ Изменить группу": ("group_id", "Новый ID группы (0 = дефолт):"),
        "✏️ Изменить график": ("schedule", None),
        "✏️ Факт дня": ("needs_daily_fact", None),
    }

    if action not in field_map: return ConversationHandler.END
    field, prompt = field_map[action]
    context.user_data["edit_field"] = field

    if field == "schedule":
        await update.message.reply_text("Выберите график:", reply_markup=SCHEDULE_KEYBOARD)
        return ASK_EDIT_SCHEDULE
    if field == "needs_daily_fact":
        await update.message.reply_text("Нужен ли факт дня?", reply_markup=YES_NO_KEYBOARD)
        return ASK_EDIT_DAILY_FACT
    if field == "group_id":
        await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_GROUP_VALUE

    await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
    return ASK_EDIT_VALUE

async def edit_value_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], context.user_data.get("edit_field"), value)
    await update.message.reply_text("Изменения внесены.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_group_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        group_id = int(update.message.text.strip())
    except ValueError: return ASK_EDIT_GROUP_VALUE
    worker = context.user_data.get("edit_worker")
    final_id = DEFAULT_GROUP_ID if group_id == 0 else group_id
    update_worker_field(worker["telegram_id"], "group_id", final_id)
    gname = await fetch_and_save_group_name(context.bot, final_id)
    await update.message.reply_text(f"Группа привязана: {gname}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_schedule_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES: return ASK_EDIT_SCHEDULE
    update_worker_field(context.user_data.get("edit_worker")["telegram_id"], "schedule", raw)
    await update.message.reply_text("График обновлен.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_daily_fact_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"): return ASK_EDIT_DAILY_FACT
    update_worker_field(context.user_data.get("edit_worker")["telegram_id"], "needs_daily_fact", 1 if raw == "да" else 0)
    await update.message.reply_text("Настройка сохранена.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ИНТЕГРАЦИЯ ДОБАВЛЕНИЯ И УДАЛЕНИЯ СОТРУДНИКОВ
# ══════════════════════════════════════════════════════════════════════════════

async def add_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    context.user_data.clear()
    
    # 3. Безопасное получение данных из словаря незарегистрированных пользователей
    pending_users = context.application.bot_data.get("pending_users", {})
    if pending_users:
        uid, p_user = pending_users.popitem()
        context.user_data["new_worker_id"] = p_user["telegram_id"]
        await update.message.reply_text(f"ID заполнен из очереди: {p_user['telegram_id']} ({p_user['name']})\nВведите фамилию:", reply_markup=CANCEL_KEYBOARD)
        return ASK_LASTNAME

    await update.message.reply_text("Введите Telegram ID сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_WORKER_ID

async def add_worker_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit(): return ASK_WORKER_ID
    context.user_data["new_worker_id"] = int(raw)
    await update.message.reply_text("Введите фамилию:", reply_markup=CANCEL_KEYBOARD)
    return ASK_LASTNAME

async def add_worker_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_name"] = update.message.text.strip()
    await update.message.reply_text("Введите имя:", reply_markup=CANCEL_KEYBOARD)
    return ASK_FIRSTNAME

async def add_worker_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["first_name"] = update.message.text.strip()
    await update.message.reply_text("Введите отдел/должность:", reply_markup=CANCEL_KEYBOARD)
    return ASK_POSITION

async def add_worker_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text.strip()
    await update.message.reply_text("Введите ID рабочей группы (0 = по умолчанию):", reply_markup=CANCEL_KEYBOARD)
    return ASK_GROUP

async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        gid = int(update.message.text.strip())
    except ValueError: return ASK_GROUP
    context.user_data["group_id"] = DEFAULT_GROUP_ID if gid == 0 else gid
    await update.message.reply_text("Выберите график:", reply_markup=SCHEDULE_KEYBOARD)
    return ASK_SCHEDULE

async def add_worker_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES: return ASK_SCHEDULE
    context.user_data["schedule"] = raw
    await update.message.reply_text("Нужен ли факт дня? (Да/Нет):", reply_markup=YES_NO_KEYBOARD)
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
        needs_daily_fact=(raw == "да")
    )
    await update.message.reply_text("Сотрудник успешно добавлен!", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def remove_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("База пуста.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    await update.message.reply_text("Введите Telegram ID сотрудника для удаления:", reply_markup=CANCEL_KEYBOARD)
    return ASK_REMOVE_WORKER

async def remove_worker_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError: return ASK_REMOVE_WORKER
    if delete_worker(uid):
        await update.message.reply_text("Сотрудник удален.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("ID не найден.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# ПРИЕМ И ПЕРЕОПРЕДЕЛЕНИЕ ОБРАБОТКИ ОТЧЕТОВ
# ══════════════════════════════════════════════════════════════════════════════

async def handle_report_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.id)

    if "pending_users" not in context.application.bot_data:
        context.application.bot_data["pending_users"] = {}

    if not worker:
        context.application.bot_data["pending_users"][user.id] = {"telegram_id": user.id, "name": user.full_name}
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"🤖 Неизвестный пользователь прислал отчет!\nID: {user.id}\nИмя: {user.full_name}")
            except Exception: pass
        await update.message.reply_text("Вы не зарегистрированы в системе.")
        return

    attachment = update.message.video or update.message.video_note or update.message.voice or update.message.audio
    if not attachment: return

    msg = await update.message.reply_text("⏳ Расшифровка медиа и анализ ИИ...")
    os.makedirs("tmp", exist_ok=True)
    temp_path = f"tmp/file_{user.id}_{int(datetime.now().timestamp())}.mp4"

    try:
        tg_file = await attachment.get_file()
        await tg_file.download_to_drive(temp_path)
        
        text = transcribe_audio(temp_path)
        if not text or "Ошибка" in text:
            await msg.edit_text("❌ Не удалось распознать аудиодорожку.")
            return

        ai = check_status(text)
        now = now_local()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        slot_time = None
        is_late = False
        if ai["report_type"] == "status":
            slot_time, is_late = find_nearest_slot(SCHEDULES.get(worker["schedule"], SCHEDULE_A), now)

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO reports (telegram_id, report_date, report_type, slot_time, received_at, is_ok, is_late, format_comment, required_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user.id, date_str, ai["report_type"], slot_time, time_str, int(ai["is_ok"]), int(is_late), ai["format_comment"], ai["required_action"])
        )
        report_id = cursor.lastrowid
        conn.commit()
        conn.close()

        await msg.edit_text("✅ Отчет успешно принят и сохранен!" if ai["is_ok"] else f"⚠️ Замечание ИИ: {ai['employee_message']}")

        # 7. Инлайн-кнопки для оперативного изменения решения ИИ админом
        inline_kbd = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"force_ok_{report_id}"),
                InlineKeyboardButton("❌ Замечание", callback_data=f"force_fail_{report_id}")
            ]
        ])

        header = f"📊 **Отчет: {worker['last_name']} {worker['first_name']}** ({worker['position']})\n"
        body = f"• Тип: {ai['report_type']} " + (f"[{slot_time}]" if slot_time else "[Итог]") + f"\n• Оценка ИИ: {'✅ ОК' if ai['is_ok'] else '❌ ' + ai['format_comment']}\n• Текст: _\"{text}\"_"
        
        await context.bot.send_message(chat_id=worker["group_id"], text=header + body, parse_mode="Markdown", reply_markup=inline_kbd)

    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        # 2. Гарантированная чистка диска от временных файлов
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except Exception: pass

async def handle_report_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not (query.data.startswith("force_ok_") or query.data.startswith("force_fail_")): return

    report_id = int(query.data.split("_")[-1])
    is_ok = query.data.startswith("force_ok_")

    if is_ok:
        update_report_status_by_id(report_id, True, "всё ОК (исправлено админом)", "Администратор подтвердил отчет")
        new_text = query.message.text + "\n\n♻️ **Статус переопределен: Одобрено Руководителем**"
    else:
        update_report_status_by_id(report_id, False, "не ОК (выставлено админом)", "Администратор выставил замечание")
        new_text = query.message.text + "\n\n♻️ **Статус переопределен: Выставлено Замечание**"

    await query.edit_message_text(text=new_text, reply_markup=None)

# ══════════════════════════════════════════════════════════════════════════════
# ФОРМИРОВАНИЕ И ПЛАНИРОВАНИЕ СВОДКИ
# ══════════════════════════════════════════════════════════════════════════════

def build_summary_text(date_str: str) -> str:
    workers = get_all_workers()
    conn = get_db()
    reports = conn.execute("SELECT * FROM reports WHERE report_date = ?", (date_str,)).fetchall()
    conn.close()

    rep_map = {}
    for r in reports:
        if r["telegram_id"] not in rep_map: rep_map[r["telegram_id"]] = {"status": {}, "daily_fact": None}
        if r["report_type"] == "status": rep_map[r["telegram_id"]]["status"][r["slot_time"]] = r
        else: rep_map[r["telegram_id"]]["daily_fact"] = r

    text = f"📊 **Сводная статистика за {date_str}**\n\n"
    current_pos = None
    for w in workers:
        if w["position"] != current_pos:
            current_pos = w["position"]
            text += f"🏗 **Отдел: {current_pos}**\n"

        w_rep = rep_map.get(w["telegram_id"], {"status": {}, "daily_fact": None})
        status_items = []
        for slot in SCHEDULES.get(w["schedule"], SCHEDULE_A):
            r = w_rep["status"].get(slot)
            if r: status_items.append(f"{slot}:{'⏳' if r['is_late'] else ('✅' if r['is_ok'] else '⚠️')}")
            else: status_items.append(f"{slot}:❌")

        fact_str = "✅ Итог" if (w_rep["daily_fact"] and w_rep["daily_fact"]["is_ok"]) else ("⚠️ Зам." if w_rep["daily_fact"] else "❌ нет")
        text += f"• {w['last_name']} {w['first_name']}\n  ⏱ Спуты: {' | '.join(status_items)}\n  📋 {fact_str if w['needs_daily_fact'] else '➖'}\n"
    return text

async def generate_summary_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return
    text = build_summary_text(now_local().strftime("%Y-%m-%d"))
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU)

async def set_summary_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    await update.message.reply_text("Введите время автосводки (ЧЧ:ММ):", reply_markup=CANCEL_KEYBOARD)
    return ASK_REPORT_TIME

async def set_summary_time_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try: datetime.strptime(raw, "%H:%M")
    except ValueError: return ASK_REPORT_TIME

    conn = get_db()
    conn.execute("INSERT INTO settings (key, value) VALUES ('summary_time', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Время автоматической сводки сохранено: {raw}", reply_markup=MAIN_MENU)
    # Перезапуск задачи планировщика
    await setup_scheduler(context.application)
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=menu_for_user(update.effective_user.id))
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# АВТОМАТИЧЕСКИЙ ПЛАНИРОВЩИК (JOB QUEUE)
# ══════════════════════════════════════════════════════════════════════════════

async def cron_summary_job(context: ContextTypes.DEFAULT_TYPE):
    text = build_summary_text(now_local().strftime("%Y-%m-%d"))
    if SUMMARY_CHAT_ID:
        try: await context.bot.send_message(chat_id=SUMMARY_CHAT_ID, text=text, parse_mode="Markdown")
        except Exception: pass

async def setup_scheduler(application: Application):
    # Очистка старых задач
    current_jobs = application.job_queue.get_jobs_by_name("auto_summary")
    for job in current_jobs: job.schedule_removal()

    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = 'summary_time'").fetchone()
    conn.close()
    
    if row:
        try:
            t = datetime.strptime(row["value"], "%H:%M").time()
            # 12. Время автосводки теперь гарантированно восстанавливается после перезапуска процесса
            application.job_queue.run_daily(cron_summary_job, time=dtime(t.hour, t.minute, tzinfo=LOCAL_TZ), name="auto_summary")
            logger.info(f"Планировщик сводок успешно инициализирован на время: {row['value']}")
        except Exception as e:
            logger.error(f"Не удалось запустить планировщик: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК ПРИЛОЖЕНИЯ
# ══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()

    # 8. Разовое кэширование названия дефолтной группы
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        proxy = Application.builder().token(TOKEN).build()
        loop.run_until_complete(fetch_and_save_group_name(proxy.bot, DEFAULT_GROUP_ID))
    except Exception: pass

    app = Application.builder().token(TOKEN).build()

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
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel_handler)],
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
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel_handler)],
    )

    remove_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить сотрудника$"), remove_worker_start)],
        states={ASK_REMOVE_WORKER: [MessageHandler(DIALOG_TEXT, remove_worker_finish)]},
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel_handler)],
    )

    time_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏰ Время сводки$"), set_summary_time_start)],
        states={ASK_REPORT_TIME: [MessageHandler(DIALOG_TEXT, set_summary_time_finish)]},
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel_handler)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))
    app.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), generate_summary_now))
    app.add_handler(list_handler)
    app.add_handler(add_handler)
    app.add_handler(remove_handler)
    app.add_handler(time_handler)
    app.add_handler(CallbackQueryHandler(handle_report_correction))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO, handle_report_video))

    # Накатываем задачи планировщика при старте приложения
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_until_complete(setup_scheduler(app))
    except Exception: pass

    logger.info("Бот полностью готов к развертыванию.")
    app.run_polling()

if __name__ == "__main__":
    main()
