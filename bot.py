import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from groq import Groq
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Настройки и переменные окружения ─────────────────────────────────────────
TOKEN = os.environ.get("TELEGRAM_TOKEN")
# Считываем ADMIN_IDS из Railway и превращаем в список чисел для проверки
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().lstrip("-").isdigit()]

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
    ASK_ORDER_WORKER,
    ASK_ORDER_DIRECTION,
) = range(21)

# Клавиатуры
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
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    for col, definition in [
        ("schedule", "TEXT NOT NULL DEFAULT 'A'"),
        ("needs_daily_fact", "INTEGER NOT NULL DEFAULT 1"),
        ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
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
            required_action TEXT
        )
        """
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

def upsert_worker(telegram_id: int, last_name: str, first_name: str, position: str, group_id: int, schedule: str, needs_daily_fact: bool, sort_order: int = 0):
    conn = get_db()
    conn.execute(
        """
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
        """,
        (telegram_id, last_name, first_name, position, group_id, schedule, int(needs_daily_fact), sort_order),
    )
    conn.commit()
    conn.close()

def update_worker_field(telegram_id: int, field: str, value):
    allowed = {"last_name", "first_name", "position", "group_id", "schedule", "needs_daily_fact", "sort_order"}
    if field not in allowed:
        raise ValueError(f"Недопустимое поле: {field}")
    conn = get_db()
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

def save_report(telegram_id: int, report_date: str, report_type: str, slot_time: str | None, received_at: str, is_ok: bool, is_late: bool, format_comment: str, required_action: str):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO reports (telegram_id, report_date, report_type, slot_time, received_at, is_ok, is_late, format_comment, required_action)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, report_date, report_type, slot_time, received_at, int(is_ok), int(is_late), format_comment, required_action),
    )
    conn.commit()
    conn.close()

def get_reports_for_date(report_date: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM reports WHERE report_date = ?", (report_date,)).fetchall()
    conn.close()
    return rows


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

    is_ok = bool(data.get("is_ok", False))
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
Ты — строгий, но справедливый проверяющий видеоотчётов сотрудников строительной или смежной бригады.

━━━ ДВА ТИПА ОТЧЁТА ━━━
1. «status» — текущий статус за конкретное время суток.
2. «daily_fact» — итог за весь день.

━━━ ФОРМАТ ОТВЕТА ━━━
Верни только JSON без Markdown и без пояснений:
{{
  "report_type": "status" или "daily_fact",
  "is_ok": true или false,
  "issue": "короткое замечание или пустая строка",
  "required_action": "ничего не предпринимать или конкретное действие",
  "employee_message": "сообщение сотруднику или пустая строка"
}}

━━━ РАСШИФРОВКА ОТЧЁТА ━━━
{text}
"""

def check_status(text: str) -> dict:
    if groq_client is None:
        return normalize_ai_result({"report_type": "status", "is_ok": False, "issue": "GROQ_API_KEY не задан"}, text)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
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


# ══════════════════════════════════════════════════════════════════════════════
# Базовые хэндлеры команд
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Привет! Отправьте видеоотчет, когда он будет готов.", reply_markup=ReplyKeyboardRemove())

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID чата: {update.effective_chat.id}", reply_markup=menu_for_user(update.effective_user.id))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
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
    gname = get_group_name(worker["group_id"])
    info = f"👤 {worker['last_name']} {worker['first_name']}\nОтдел: {worker['position']}\nГрафик: {worker['schedule']} ({schedule_str})\nГруппа: {gname}\nФакт дня: {fact}\n\nЧто хотите сделать?"

    kbd = ReplyKeyboardMarkup(
        [["✏️ Изменить фамилию", "✏️ Изменить имя"], ["✏️ Изменить отдел", "✏️ Изменить график"], ["✏️ Изменить группу", "✏️ Факт дня"], ["🔼 Вверх в списке", "🔽 Вниз в списке"], ["❌ Отмена"]],
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
    delete_worker(worker["telegram_id"])
    await update.message.reply_text(f"Сотрудник {worker['last_name']} успешно удален.", reply_markup=MAIN_MENU)
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
    await update.message.reply_text("📊 Функция формирования сводки запущена вручную...")


# ══════════════════════════════════════════════════════════════════════════════
# Прием отчетов от сотрудников (Голос / Видео / Текст)
# ══════════════════════════════════════════════════════════════════════════════

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    worker = get_worker(user_id)
    if not worker:
        await update.message.reply_text("Ошибка: Вы не зарегистрированы в системе. Обратитесь к администратору.")
        return

    text_content = ""
    if update.message.text:
        text_content = update.message.text.strip()
    else:
        # Распознавание аудио/видео сообщений
        file_obj = None
        if update.message.voice: file_obj = update.message.voice
        elif update.message.video: file_obj = update.message.video
        elif update.message.video_note: file_obj = update.message.video_note

        if file_obj:
            await update.message.reply_text("🎙 Отчет принят на анализ ИИ, ожидайте...")
            tg_file = await context.bot.get_file(file_obj.file_id)
            ext = "mp4" if update.message.video or update.message.video_note else "ogg"
            path = f"file_{user_id}_{int(datetime.now().timestamp())}.{ext}"
            await tg_file.download_to_drive(path)
            text_content = transcribe_audio(path)
            if os.path.exists(path): os.remove(path)

    if not text_content:
        await update.message.reply_text("Не удалось распознать текст отчета.")
        return

    # Анализ промптом Llama
    ai_res = check_status(text_content)
    now = now_local()
    sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
    nearest_slot, is_late = find_nearest_slot(sched_list, now)

    save_report(
        telegram_id=user_id,
        report_date=now.strftime("%Y-%m-%d"),
        report_type=ai_res["report_type"],
        slot_time=nearest_slot,
        received_at=now.strftime("%H:%M:%S"),
        is_ok=ai_res["is_ok"],
        is_late=is_late,
        format_comment=ai_res["format_comment"],
        required_action=ai_res["required_action"]
    )

    if ai_res["is_ok"]:
        await update.message.reply_text("✅ Отчёт принят! Спасибо.")
    else:
        await update.message.reply_text(f"⚠️ {ai_res['employee_message']}")


# ══════════════════════════════════════════════════════════════════════════════
# Инициализация и запуск приложения
# ══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()
    if not TOKEN:
        print("Критическая ошибка: не задан TELEGRAM_TOKEN")
        return

    application = Application.builder().token(TOKEN).build()

    # Точечные команды и кнопки меню (срабатывают моментально)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))
    application.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), send_summary_now))

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

    # 1. Сначала регистрируем админские интерактивные диалоги
    application.add_handler(list_handler)
    application.add_handler(add_handler)
    application.add_handler(delete_handler)
    application.add_handler(view_dept_handler)

    # 2. В самом конце — хэндлер для приема аудио/видео/текстовых отчетов сотрудников
    application.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.TEXT & ~filters.COMMAND, 
        handle_report
    ))

    print("Бот успешно инициализирован и запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
    
