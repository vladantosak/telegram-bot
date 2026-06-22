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

# Настройки и конфигурация из окружения
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# 1. Исправление: Поддержка списка администраторов через запятую
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

# Состояния разговорных обработчиков
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
    
    # 12. Исправление: Таблица для сохранения системных настроек (время сводки и т.д.)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

    # 5. Исправление: Добавлен UNIQUE constraint для исключения дубликатов в рамках одного слота/типа за день
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

def update_worker_field(telegram_id: int, field: str, value):
    allowed = {"last_name", "first_name", "position", "group_id", "schedule", "needs_daily_fact", "sort_order"}
    if field not in allowed:
        raise ValueError(f"Недопустимое поле: {field}")
    
    conn = get_db()
    # 9. Исправление: Если меняется отдел (position), сбрасываем sort_order в 0
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

# 6. Улучшение: Получение истории последних 7 отчетов сотрудника
def get_worker_history(telegram_id: int, limit: int = 7):
    conn = get_db()
    rows = conn.execute(
        "SELECT report_date, report_type, slot_time, is_ok, format_comment FROM reports WHERE telegram_id = ? ORDER BY received_at DESC LIMIT ?",
        (telegram_id, limit)
    ).fetchall()
    conn.close()
    return rows

# 7. Улучшение: Прямое принудительное исправление статуса отчета по его ID (Кнопки ИИ проверки)
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

def positions_keyboard(rows, extra=None):
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
# ИИ / Анализ
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

# 10. Улучшение: Использование более точной промышленной модели llama-3.3-70b-versatile
CHECK_PROMPT_TEMPLATE = """
Ты — строгий, но справедливый проверяющий видеоотчётов сотрудников строительной бригады.

━━━ ДВА ТИПА ОТЧЁТА ━━━
1. «status» — текущий статус за конкретное время суток.
2. «daily_fact» — итог за весь день.

Объём работы НЕ обязательно должен быть числом. Если ясно, ЧТО именно делалось («шпаклевал стену», «работал на экскаваторе») — объём считается указанным.

Верни только JSON без Markdown:
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

    prompt = CHECK_PROMPT_TEMPLATE.format(text=text)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай только валидным JSON без Markdown."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return normalize_ai_result(data, text)
    except Exception as e:
        return normalize_ai_result({"report_type": "status", "is_ok": False, "issue": f"Ошибка ИИ: {e}"}, text)

# ══════════════════════════════════════════════════════════════════════════════
# Обработчики интерфейса управления
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
    await update.message.reply_text("Выберите отдел для управления сотрудниками:", reply_markup=kbd)
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

    # 11. Исправление: Текст и инлайн-интерфейс объединены в один аккуратный вызов клавиатуры
    await update.message.reply_text("\n".join(lines) + "\n\nВыберите сотрудника для редактирования:", reply_markup=numbered_workers_keyboard(rows))
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
    
    info = (
        f"👤 {worker['last_name']} {worker['first_name']}\n"
        f"Отдел: {worker['position']}\n"
        f"График: {worker['schedule']} ({schedule_str})\n"
        f"Группа: {gname}\n"
        f"Факт дня: {fact}\n\n"
        "Доступные действия:"
    )

    kbd = ReplyKeyboardMarkup(
        [
            ["✏️ Изменить фамилию", "✏️ Изменить имя"],
            ["✏️ Изменить отдел", "✏️ Изменить график"],
            ["✏️ Изменить группу", "✏️ Факт дня"],
            ["📊 История отчетов", "🔼 Вверх в списке"],
            ["🔽 Вниз в списке", "❌ Отмена"],
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
        await update.message.reply_text("Ошибка сессии. Начните сначала.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    # 6. Улучшение: Показ истории по сотруднику при выборе кнопки
    if action == "📊 История отчетов":
        history = get_worker_history(worker["telegram_id"])
        if not history:
            await update.message.reply_text("История отчетов пуста.", reply_markup=MAIN_MENU)
        else:
            text = f"📋 Последние отчеты: {worker['last_name']} {worker['first_name']}:\n\n"
            for h in history:
                status_icon = "✅" if h["is_ok"] else "❌"
                t_type = "Факт" if h["report_type"] == "daily_fact" else f"Статус ({h['slot_time']})"
                text += f"{status_icon} [{h['report_date']}] {t_type}: {h['format_comment']}\n"
            await update.message.reply_text(text, reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    if action in ("🔼 Вверх в списке", "🔽 Вниз в списке"):
        target_idx = idx - 1 if action == "🔼 Вверх в списке" else idx + 1
        if target_idx < 0 or target_idx >= len(rows):
            await update.message.reply_text("Сотрудник уже на краю списка.", reply_markup=MAIN_MENU)
            return ConversationHandler.END

        swap_sort_order(worker["telegram_id"], rows[target_idx]["telegram_id"])
        await update.message.reply_text(f"Порядок сортировки изменен.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    field_map = {
        "✏️ Изменить фамилию": ("last_name", "Введите новую фамилию:"),
        "✏️ Изменить имя": ("first_name", "Введите новое имя:"),
        "✏️ Изменить отдел": ("position", "Введите новое название отдела (сортировка сбросится):"),
        "✏️ Изменить группу": ("group_id", f"Введите новый ID группы Telegram (0 = по умолчанию):"),
        "✏️ Изменить график": ("schedule", None),
        "✏️ Факт дня": ("needs_daily_fact", None),
    }

    if action not in field_map:
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    field, prompt = field_map[action]
    context.user_data["edit_field"] = field

    if field == "schedule":
        await update.message.reply_text("Выберите новый график:", reply_markup=SCHEDULE_KEYBOARD)
        return ASK_EDIT_SCHEDULE
    if field == "needs_daily_fact":
        await update.message.reply_text("Нужен ли сотруднику ежедневный факт дня?", reply_markup=YES_NO_KEYBOARD)
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
    await update.message.reply_text(f"Успешно изменено.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_group_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        group_id = int(raw)
    except ValueError:
        await update.message.reply_text("Введите корректный ID.")
        return ASK_EDIT_GROUP_VALUE

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
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "schedule", raw)
    await update.message.reply_text(f"График обновлен на {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_daily_fact_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"): return ASK_EDIT_DAILY_FACT
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "needs_daily_fact", 1 if raw == "да" else 0)
    await update.message.reply_text(f"Изменения сохранены.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# Обработчик входящих видео и аудио отчетов
# ══════════════════════════════════════════════════════════════════════════════

async def handle_report_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.id)
    
    # 3. Исправление: Ввод потокобезопасного хранения во внутренний словарь bot_data
    if "pending_users" not in context.application.bot_data:
        context.application.bot_data["pending_users"] = {}

    if not worker:
        context.application.bot_data["pending_users"][user.id] = {
            "telegram_id": user.id,
            "name": user.full_name,
            "username": f"@{user.username}" if user.username else "нет"
        }
        # Уведомление админам о новом незарегистрированном пользователе
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"🤖 Неизвестный пользователь прислал файл!\nID: {user.id}\nИмя: {user.full_name}\n\nНажмите '➕ Добавить сотрудника' для быстрой привязки."
                )
            except Exception:
                pass
        await update.message.reply_text("Вы не зарегистрированы в системе. Администратор уведомлен.")
        return

    # Загрузка и обработка аудио/видео файлов
    attachment = update.message.video or update.message.video_note or update.message.voice or update.message.audio
    if not attachment:
        await update.message.reply_text("Пожалуйста, отправьте видеоотчет или голосовое сообщение.")
        return

    msg = await update.message.reply_text("⏳ Получение файла и расшифровка ИИ...")
    
    # Генерация уникального локального пути в tmp
    os.makedirs("tmp", exist_ok=True)
    temp_file_path = f"tmp/file_{user.id}_{int(datetime.now().timestamp())}.mp4"

    try:
        # Получение объекта файла
        if update.message.video_note:
            tg_file = await update.message.video_note.get_file()
        elif update.message.video:
            tg_file = await update.message.video.get_file()
        elif update.message.voice:
            tg_file = await update.message.voice.get_file()
        else:
            tg_file = await attachment.get_file()

        await tg_file.download_to_drive(temp_file_path)

        # Распознавание речи через Whisper
        text_transcription = transcribe_audio(temp_file_path)
        if not text_transcription or "Ошибка" in text_transcription:
            await msg.edit_text(f"❌ Ошибка аудио: {text_transcription}")
            return

        # Анализ параметров текста бизнес-логикой
        ai_analysis = check_status(text_transcription)
        now = now_local()
        current_date_str = now.strftime("%Y-%m-%d")
        current_time_str = now.strftime("%H:%M:%S")

        slot_time = None
        is_late = False

        if ai_analysis["report_type"] == "status":
            worker_schedule = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
            slot_time, is_late = find_nearest_slot(worker_schedule, now)

        # Сохранение в базу данных
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO reports (telegram_id, report_date, report_type, slot_time, received_at, is_ok, is_late, format_comment, required_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user.id, current_date_str, ai_analysis["report_type"], slot_time, current_time_str, int(ai_analysis["is_ok"]), int(is_late), ai_analysis["format_comment"], ai_analysis["required_action"])
        )
        report_db_id = cursor.lastrowid
        conn.commit()
        conn.close()

        # Формирование ответа сотруднику
        if ai_analysis["is_ok"]:
            late_text = " (С опозданием!)" if is_late else ""
            await msg.edit_text(f"✅ Отчет успешно принят! Тип: {ai_analysis['report_type']}{late_text}")
        else:
            await msg.edit_text(f"⚠️ Отчет принят, но ИИ выявил замечание: {ai_analysis['employee_message']}")

        # 7. Улучшение: Инлайн-клавиатура для ручного переопределения оценки ИИ администратором в группе
        inline_kbd = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"force_ok_{report_db_id}"),
                InlineKeyboardButton("❌ Замечание", callback_data=f"force_fail_{report_db_id}")
            ]
        ])

        # Дублирование отчета в целевую рабочую группу
        report_header = f"📊 **Отчет: {worker['last_name']} {worker['first_name']}** ({worker['position']})\n"
        report_body = (
            f"• Тип: {ai_analysis['report_type']} " + (f"[{slot_time}]" if slot_time else "") + "\n"
            f"• Время: {current_time_str}\n"
            f"• Оценка ИИ: " + ("✅ ОК" if ai_analysis["is_ok"] else f"❌ {ai_analysis['format_comment']}") + "\n"
            f"• Текст: _\"{text_transcription}\"_\n"
        )
        
        await context.bot.send_message(
            chat_id=worker["group_id"],
            text=report_header + report_body,
            parse_mode="Markdown",
            reply_markup=inline_kbd
        )

    except Exception as general_error:
        logger.error(f"Ошибка при обработке файла: {general_error}")
        await msg.edit_text("Произошла критическая ошибка при обработке медиафайла.")
    finally:
        # 2. Исправление: Принудительное удаление временного файла с диска во избежание переполнения памяти
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as e:
                logger.error(f"Не удалось удалить файл {temp_file_path}: {e}")

# 7. Улучшение: Callback query обработчик для мгновенного изменения оценки ИИ
async def handle_report_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if not (data.startswith("force_ok_") or data.startswith("force_fail_")):
        return

    report_id = int(data.split("_")[-1])
    is_ok = data.startswith("force_ok_")

    if is_ok:
        update_report_status_by_id(report_id, True, "всё ОК (исправлено вручную)", "ничего не предпринимать")
        new_text = query.message.text + "\n\n♻️ **Статус изменен вручную: Одобрено Руководителем**"
    else:
        update_report_status_by_id(report_id, False, "не ОК, выставлено вручную", "руководитель выставил замечание")
        new_text = query.message.text + "\n\n♻️ **Статус изменен вручную: Выставлено Замечание**"

    await query.edit_message_text(text=new_text, reply_markup=None)

# 4. Исправление: Сводка теперь четко распределяет и группирует status и daily_fact отдельно
async def generate_summary_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return

    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    workers = get_all_workers()
    
    conn = get_db()
    reports = conn.execute("SELECT * FROM reports WHERE report_date = ?", (date_str,)).fetchall()
    conn.close()

    rep_map = {}
    for r in reports:
        if r["telegram_id"] not in rep_map:
            rep_map[r["telegram_id"]] = {"status": {}, "daily_fact": None}
        if r["report_type"] == "status":
            rep_map[r["telegram_id"]]["status"][r["slot_time"]] = r
        else:
            rep_map[r["telegram_id"]]["daily_fact"] = r

    text = f"📊 **Сводная статистика за {date_str}**\n\n"
    
    current_pos = None
    for w in workers:
        if w["position"] != current_pos:
            current_pos = w["position"]
            text += f"🏗 **Отдел: {current_pos}**\n"

        w_reports = rep_map.get(w["telegram_id"], {"status": {}, "daily_fact": None})
        
        # Сборка статусов
        sched = SCHEDULES.get(w["schedule"], SCHEDULE_A)
        status_line_items = []
        for slot in sched:
            r = w_reports["status"].get(slot)
            if r:
                icon = "✅" if r["is_ok"] else "⚠️"
                if r["is_late"]: icon = "⏳"
                status_line_items.append(f"{slot}:{icon}")
            else:
                status_line_items.append(f"{slot}:❌")
        
        status_str = " | ".join(status_line_items)

        # Сборка факта дня
        if w["needs_daily_fact"]:
            f_rep = w_reports["daily_fact"]
            if f_rep:
                fact_str = "✅ Итог: ОК" if f_rep["is_ok"] else "⚠️ Итог: Зам."
            else:
                fact_str = "❌ Итог: нет"
        else:
            fact_str = "➖"

        text += f"• {w['last_name']} {w['first_name']}\n  ⏱ Спуты: {status_str}\n  📋 {fact_str}\n"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=MAIN_MENU)

# 12. Исправление: Логика сохранения времени сводки в персистентную БД
async def set_summary_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update): return ConversationHandler.END
    await update.message.reply_text("Введите время в формате ЧЧ:ММ для автоматической сводки ежедневно:", reply_markup=CANCEL_KEYBOARD)
    return ASK_REPORT_TIME

async def set_summary_time_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        await update.message.reply_text("Неверный формат. Попробуйте еще раз (например 19:30):")
        return ASK_REPORT_TIME

    conn = get_db()
    conn.execute("INSERT INTO settings (key, value) VALUES ('summary_time', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"Время автоматической сводки сохранено: {raw}", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=menu_for_user(update.effective_user.id))
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# Инициализация и старт приложения
# ══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()

    # 8. Улучшение: Динамический разовый запрос названия дефолтной группы при старте для исключения ID в логах
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        proxy_app = Application.builder().token(TOKEN).build()
        loop.run_until_complete(fetch_and_save_group_name(proxy_app.bot, DEFAULT_GROUP_ID))
    except Exception as err:
        logger.warning(f"Не удалось подтянуть имя группы по умолчанию при старте: {err}")

    app = Application.builder().token(TOKEN).build()

    # Разговорный обработчик для вывода списка и редактирования полей
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

    # Разговорный обработчик настройки времени
    time_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏰ Время сводки$"), set_summary_time_start)],
        states={ASK_REPORT_TIME: [MessageHandler(DIALOG_TEXT, set_summary_time_finish)]},
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel_handler)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))
    app.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), generate_summary_now))
    app.add_handler(list_handler)
    app.add_handler(time_handler)
    
    # Специфический инлайн-обработчик ручного изменения ИИ проверок
    app.add_handler(CallbackQueryHandler(handle_report_correction))

    # Логика захвата любых входящих медиа-отчетов (Аудио, Видео, Голосовые, Заметки)
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO, handle_report_video))

    logger.info("Бот успешно запущен и готов к работе.")
    app.run_polling()

if __name__ == "__main__":
    main()
