import json
import os
import sqlite3
from datetime import datetime, time as dtime
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


TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
SUMMARY_CHAT_ID = int(os.environ.get("SUMMARY_CHAT_ID", "0")) or ADMIN_ID
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
    # Новые состояния для просмотра/редактирования сотрудников
    ASK_LIST_DEPARTMENT,
    ASK_LIST_WORKER,
    ASK_EDIT_FIELD,
    ASK_EDIT_VALUE,
    ASK_EDIT_SCHEDULE,
    ASK_EDIT_DAILY_FACT,
    ASK_EDIT_GROUP_VALUE,
    # Смена порядка
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
# БД
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


def upsert_worker(
    telegram_id: int,
    last_name: str,
    first_name: str,
    position: str,
    group_id: int,
    schedule: str,
    needs_daily_fact: bool,
    sort_order: int = 0,
):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers
            (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order)
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
    """Меняет местами sort_order двух сотрудников."""
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


def save_report(
    telegram_id: int,
    report_date: str,
    report_type: str,
    slot_time: str | None,
    received_at: str,
    is_ok: bool,
    is_late: bool,
    format_comment: str,
    required_action: str,
):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO reports
            (telegram_id, report_date, report_type, slot_time, received_at,
             is_ok, is_late, format_comment, required_action)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            telegram_id, report_date, report_type, slot_time, received_at,
            int(is_ok), int(is_late), format_comment, required_action,
        ),
    )
    conn.commit()
    conn.close()


def get_reports_for_date(report_date: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM reports WHERE report_date = ?", (report_date,)).fetchall()
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательное
# ══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


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


def positions_keyboard(rows, extra=None):
    positions = sorted({row["position"] for row in rows})
    keyboard = [[p] for p in positions]
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True), positions


def numbered_workers_keyboard(rows):
    """Клавиатура с пронумерованными сотрудниками."""
    keyboard = []
    for i, row in enumerate(rows, 1):
        keyboard.append([f"{i}. {row['last_name']} {row['first_name']}"])
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ══════════════════════════════════════════════════════════════════════════════
# ИИ / промпт
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


# ── Улучшенный промпт ─────────────────────────────────────────────────────────
CHECK_PROMPT_TEMPLATE = """
Ты — строгий, но справедливый проверяющий видеоотчётов сотрудников строительной или смежной бригады.

━━━ ДВА ТИПА ОТЧЁТА ━━━

1. «status» — текущий статус за конкретное время суток.
   Сотрудник говорит: «статус», «на данный момент», «сейчас», «за 10:00», «за 12:00» и т.д.
   Отчёт считается хорошим, если понятно:
     • что именно делал / проверял / контролировал,
     • каков объём выполненной работы (количество, площадь, длина, погонные метры, единицы техники и т.д.),
     • есть ли проблемы.

2. «daily_fact» — итог за весь день.
   Сотрудник говорит: «факт», «факт за день», «итог дня», «за день», «сегодня за день».
   Оцениваем дневной итог: объём, что сделано, проблемы.

━━━ КАК ОПРЕДЕЛЯТЬ ОБЪЁМ ━━━

Объём НЕ обязательно должен быть числом. Учитывай контекст профессии:

• Если сотрудник говорит «шпаклевал стену» — это конкретный объект. Засчитывай как объём.
• Если сотрудник говорит «работал на экскаваторе» или «копал котлован» — это конкретный вид работы. Засчитывай.
• Если сотрудник говорит «заливали фундамент», «клали кирпич», «штукатурили», «красили» — засчитывай.
• Если сотрудник говорит только «всё нормально», «работаем», «без изменений» без какой-либо конкретики — это НЕ объём, замечание.
• Если сотрудник называет конкретный объект, участок, задачу или вид работы — засчитывай как достаточный объём.

Правило: если из слов сотрудника ясно, ЧТО именно делалось (хотя бы на уровне «шпаклевал стену» или «работал на экскаваторе») — объём считается указанным.
Замечание ставится только если непонятно вообще ничего конкретного о сути работы.

━━━ ОЦЕНКА ━━━

• is_ok=true — если понятно что делалось, нет серьёзных проблем, действий руководителя не нужно.
• is_ok=false — если нет ни слова о конкретной работе, есть проблема требующая реакции, или отчёт состоит только из «всё хорошо» без деталей.

Если is_ok=true:
  required_action = "ничего не предпринимать"
  issue = ""
  employee_message = ""

Если is_ok=false:
  issue — короткое замечание в прошедшем времени (например: «не упомянул вид работы»)
  required_action — что сделал/должен сделать руководитель (например: «напомнил сотруднику указывать суть работы»)
  employee_message — понятное сообщение сотруднику (например: «Вы не указали, что именно делали. В следующем отчёте упомяните вид работы.»)

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
        return normalize_ai_result(
            {
                "report_type": "status",
                "is_ok": False,
                "issue": "GROQ_API_KEY не задан, проверка ИИ недоступна",
                "required_action": "Проверить отчет вручную",
                "employee_message": "Отчет получен, но автоматическая проверка сейчас недоступна.",
            },
            text,
        )

    prompt = CHECK_PROMPT_TEMPLATE.format(text=text)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
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
        return normalize_ai_result(
            {
                "report_type": "status",
                "is_ok": False,
                "issue": f"Ошибка проверки ИИ: {e}",
                "required_action": "Проверить отчет вручную",
                "employee_message": "Отчет получен, но его нужно проверить вручную.",
            },
            text,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Команды / базовые обработчики
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(
            "Привет! Отправьте видеоотчет, когда он будет готов.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ID чата: {update.effective_chat.id}",
        reply_markup=menu_for_user(update.effective_user.id),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 📋 Список сотрудников — по отделам с нумерацией и редактированием
# ══════════════════════════════════════════════════════════════════════════════

async def list_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: показываем список отделов."""
    if not await require_admin(update):
        return ConversationHandler.END

    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел:", reply_markup=kbd)
    return ASK_LIST_DEPARTMENT


async def list_workers_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: показываем пронумерованных сотрудников отдела."""
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text(f"Отдел «{position}» не найден.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    context.user_data["list_position"] = position
    context.user_data["list_rows"] = [dict(r) for r in rows]

    lines = [f"Сотрудники отдела «{position}»:\n"]
    for i, row in enumerate(rows, 1):
        schedule_str = ", ".join(SCHEDULES.get(row["schedule"], SCHEDULE_A))
        fact = "да" if row["needs_daily_fact"] else "нет"
        lines.append(
            f"{i}. {row['last_name']} {row['first_name']}\n"
            f"   График: {row['schedule']} ({schedule_str})\n"
            f"   Группа: {row['group_id']} | Факт дня: {fact}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=numbered_workers_keyboard(rows),
    )
    await update.message.reply_text(
        "Выберите сотрудника для действий (редактировать / переместить в другой отдел / изменить порядок):",
        reply_markup=numbered_workers_keyboard(rows),
    )
    return ASK_LIST_WORKER


async def list_workers_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: выбор сотрудника и действия."""
    raw = update.message.text.strip()
    rows = context.user_data.get("list_rows", [])

    # Парсим «1. Иванов Иван» → берём номер
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
    info = (
        f"👤 {worker['last_name']} {worker['first_name']}\n"
        f"Отдел: {worker['position']}\n"
        f"График: {worker['schedule']} ({schedule_str})\n"
        f"Группа: {worker['group_id']}\n"
        f"Факт дня: {fact}\n\n"
        "Что хотите сделать?"
    )

    kbd = ReplyKeyboardMarkup(
        [
            ["✏️ Изменить фамилию", "✏️ Изменить имя"],
            ["✏️ Изменить отдел", "✏️ Изменить график"],
            ["✏️ Изменить группу", "✏️ Факт дня"],
            ["🔼 Вверх в списке", "🔽 Вниз в списке"],
            ["❌ Отмена"],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text(info, reply_markup=kbd)
    return ASK_EDIT_FIELD


async def list_workers_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4: обрабатываем выбранное действие."""
    action = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    rows = context.user_data.get("list_rows", [])
    idx = context.user_data.get("edit_worker_idx", 0)

    if not worker:
        await update.message.reply_text("Ошибка состояния. Начните сначала.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    # ── Смена порядка ──────────────────────────────────────────────────────
    if action in ("🔼 Вверх в списке", "🔽 Вниз в списке"):
        if action == "🔼 Вверх в списке":
            target_idx = idx - 1
        else:
            target_idx = idx + 1

        if target_idx < 0 or target_idx >= len(rows):
            await update.message.reply_text(
                "Сотрудник уже на краю списка.", reply_markup=MAIN_MENU
            )
            return ConversationHandler.END

        swap_sort_order(worker["telegram_id"], rows[target_idx]["telegram_id"])
        await update.message.reply_text(
            f"Позиция сотрудника {worker['last_name']} {worker['first_name']} изменена.",
            reply_markup=MAIN_MENU,
        )
        context.user_data.clear()
        return ConversationHandler.END

    # ── Редактирование полей ───────────────────────────────────────────────
    field_map = {
        "✏️ Изменить фамилию": ("last_name", "Введите новую фамилию:"),
        "✏️ Изменить имя": ("first_name", "Введите новое имя:"),
        "✏️ Изменить отдел": ("position", "Введите новое название отдела/должности:"),
        "✏️ Изменить группу": ("group_id", f"Введите новый ID группы Telegram (0 = по умолчанию {DEFAULT_GROUP_ID}):"),
        "✏️ Изменить график": ("schedule", None),
        "✏️ Факт дня": ("needs_daily_fact", None),
    }

    if action not in field_map:
        await update.message.reply_text("Выберите действие кнопкой.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    field, prompt = field_map[action]
    context.user_data["edit_field"] = field

    if field == "schedule":
        await update.message.reply_text(
            "Выберите новый график:\nA: 10:00, 12:00, 15:00, 17:00\nB: 11:00, 13:00, 16:00, 18:00",
            reply_markup=SCHEDULE_KEYBOARD,
        )
        return ASK_EDIT_SCHEDULE

    if field == "needs_daily_fact":
        await update.message.reply_text(
            "Нужен ли сотруднику ежедневный факт дня?",
            reply_markup=YES_NO_KEYBOARD,
        )
        return ASK_EDIT_DAILY_FACT

    if field == "group_id":
        await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_GROUP_VALUE

    await update.message.reply_text(prompt, reply_markup=CANCEL_KEYBOARD)
    return ASK_EDIT_VALUE


async def edit_value_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняем текстовое поле (фамилия, имя, отдел)."""
    value = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    field = context.user_data.get("edit_field")

    update_worker_field(worker["telegram_id"], field, value)
    await update.message.reply_text(f"Обновлено: {field} → «{value}»", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


async def edit_group_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняем ID группы."""
    raw = update.message.text.strip()
    try:
        group_id = int(raw)
    except ValueError:
        await update.message.reply_text("Введите числовой ID или 0:", reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_GROUP_VALUE

    worker = context.user_data.get("edit_worker")
    final_id = DEFAULT_GROUP_ID if group_id == 0 else group_id
    update_worker_field(worker["telegram_id"], "group_id", final_id)
    await update.message.reply_text(f"Группа обновлена: {final_id}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


async def edit_schedule_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        await update.message.reply_text("Выберите A или B:", reply_markup=SCHEDULE_KEYBOARD)
        return ASK_EDIT_SCHEDULE

    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "schedule", raw)
    schedule_str = ", ".join(SCHEDULES[raw])
    await update.message.reply_text(f"График обновлён: {raw} ({schedule_str})", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


async def edit_daily_fact_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        await update.message.reply_text("Выберите Да или Нет:", reply_markup=YES_NO_KEYBOARD)
        return ASK_EDIT_DAILY_FACT

    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "needs_daily_fact", 1 if raw == "да" else 0)
    await update.message.reply_text(f"Факт дня: {raw}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ➕ Добавление сотрудника
# ══════════════════════════════════════════════════════════════════════════════

async def add_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END

    context.user_data.clear()
    pending_user = context.application.bot_data.pop("pending_unregistered_user", None)
    if pending_user:
        context.user_data["new_worker_id"] = pending_user["telegram_id"]
        context.user_data["pending_auto_user"] = pending_user
        await update.message.reply_text(
            "Telegram ID заполнен автоматически:\n"
            f"{pending_user['telegram_id']} ({pending_user['name']}, {pending_user['username']})\n\n"
            "Введите фамилию:",
            reply_markup=CANCEL_KEYBOARD,
        )
        return ASK_LASTNAME

    await update.message.reply_text("Введите Telegram ID сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_WORKER_ID


async def add_worker_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Введите числовой Telegram ID:", reply_markup=CANCEL_KEYBOARD)
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
    await update.message.reply_text(
        f"Введите ID группы Telegram (0 = по умолчанию {DEFAULT_GROUP_ID}):",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ASK_GROUP


async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        group_id = int(raw)
    except ValueError:
        await update.message.reply_text("Введите числовой ID или 0:", reply_markup=CANCEL_KEYBOARD)
        return ASK_GROUP
    context.user_data["group_id"] = DEFAULT_GROUP_ID if group_id == 0 else group_id
    await update.message.reply_text(
        "Выберите график отчетов:\nA: 10:00, 12:00, 15:00, 17:00\nB: 11:00, 13:00, 16:00, 18:00",
        reply_markup=SCHEDULE_KEYBOARD,
    )
    return ASK_SCHEDULE


async def add_worker_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        await update.message.reply_text("Выберите A или B:", reply_markup=SCHEDULE_KEYBOARD)
        return ASK_SCHEDULE
    context.user_data["schedule"] = raw
    await update.message.reply_text(
        "Нужно ли сотруднику присылать ежедневный факт дня?",
        reply_markup=YES_NO_KEYBOARD,
    )
    return ASK_NEEDS_DAILY_FACT


async def add_worker_needs_daily_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        await update.message.reply_text("Выберите Да или Нет:", reply_markup=YES_NO_KEYBOARD)
        return ASK_NEEDS_DAILY_FACT

    worker_id = context.user_data["new_worker_id"]
    last_name = context.user_data["last_name"]
    first_name = context.user_data["first_name"]
    position = context.user_data["position"]
    group_id = context.user_data["group_id"]
    schedule = context.user_data["schedule"]
    needs_daily_fact = raw == "да"
    pending_auto_user = context.user_data.get("pending_auto_user")

    # sort_order = кол-во сотрудников в этом отделе + 1
    existing = get_workers_by_position(position)
    sort_order = len(existing)

    upsert_worker(worker_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order)

    await update.message.reply_text(
        f"Готово! Сотрудник добавлен:\n{last_name} {first_name} ({position})\n"
        f"Группа: {group_id} | График: {', '.join(SCHEDULES[schedule])} | Факт дня: {'да' if needs_daily_fact else 'нет'}",
        reply_markup=MAIN_MENU,
    )

    if pending_auto_user and pending_auto_user.get("telegram_id") == worker_id:
        employee_chat_id = pending_auto_user.get("chat_id")
        video_file_id = pending_auto_user.get("video_file_id")
        if employee_chat_id:
            try:
                await context.bot.send_message(
                    chat_id=employee_chat_id,
                    text="Вы добавлены в базу. Ваш предыдущий видеоотчет сейчас обрабатывается.",
                    reply_markup=ReplyKeyboardRemove(),
                )
            except Exception as e:
                await update.message.reply_text(f"Сотрудник добавлен, но уведомление не отправилось: {e}")
        if video_file_id:
            worker = get_worker(worker_id)
            try:
                await process_video_report(
                    context=context,
                    worker=worker,
                    user_id=worker_id,
                    video_file_id=video_file_id,
                    employee_chat_id=employee_chat_id,
                )
                await update.message.reply_text("Предыдущий видеоотчет обработан и отправлен в группу.", reply_markup=MAIN_MENU)
            except Exception as e:
                await update.message.reply_text(f"Сотрудник добавлен, но старое видео не удалось обработать: {e}", reply_markup=MAIN_MENU)

    context.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ➖ Удаление сотрудника
# ══════════════════════════════════════════════════════════════════════════════

async def remove_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END
    rows = get_all_workers()
    positions = sorted({row["position"] for row in rows})
    if not positions:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    keyboard = [[p] for p in positions] + [["❌ Отмена"]]
    await update.message.reply_text(
        "Выберите отдел:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return ASK_REMOVE_DEPARTMENT


async def remove_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text(f"Отдел «{position}» не найден.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    context.user_data["remove_position"] = position
    context.user_data["remove_rows"] = [dict(r) for r in rows]
    await update.message.reply_text(
        f"Выберите сотрудника для удаления из отдела «{position}»:",
        reply_markup=numbered_workers_keyboard(rows),
    )
    return ASK_REMOVE_WORKER


async def remove_worker_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    rows = context.user_data.get("remove_rows", [])
    num_str = raw.split(".")[0].strip()
    if not num_str.isdigit():
        await update.message.reply_text("Выберите сотрудника кнопкой из списка.", reply_markup=CANCEL_KEYBOARD)
        return ASK_REMOVE_WORKER
    idx = int(num_str) - 1
    if idx < 0 or idx >= len(rows):
        await update.message.reply_text("Номер не найден.", reply_markup=CANCEL_KEYBOARD)
        return ASK_REMOVE_WORKER
    worker = rows[idx]
    name = f"{worker['last_name']} {worker['first_name']}"
    delete_worker(worker["telegram_id"])
    context.user_data.clear()
    await update.message.reply_text(f"Сотрудник удалён: {name}", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# 🏢 Сотрудники отдела
# ══════════════════════════════════════════════════════════════════════════════

async def department_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END
    rows = get_all_workers()
    positions = sorted({row["position"] for row in rows})
    if not positions:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    keyboard = [[p] for p in positions[:20]] + [["❌ Отмена"]]
    await update.message.reply_text(
        "Выберите отдел:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return ASK_DEPARTMENT


async def department_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text(f"Отдел «{position}» не найден.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    lines = [f"Сотрудники отдела «{position}»:"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['last_name']} {row['first_name']}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# ⏰ Время сводки
# ══════════════════════════════════════════════════════════════════════════════

async def set_report_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Введите время ежедневной сводки в формате HH:MM:", reply_markup=CANCEL_KEYBOARD)
    return ASK_REPORT_TIME


async def set_report_time_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        hour, minute = map(int, raw.split(":"))
        report_time = dtime(hour=hour, minute=minute, tzinfo=LOCAL_TZ)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Введите время как 19:00:", reply_markup=CANCEL_KEYBOARD)
        return ASK_REPORT_TIME

    job_queue = context.application.job_queue
    for job in job_queue.get_jobs_by_name("daily_summary"):
        job.schedule_removal()

    job_queue.run_daily(
        send_daily_summary,
        time=report_time,
        chat_id=update.effective_chat.id,
        name="daily_summary",
    )
    context.application.bot_data["summary_job_chat_id"] = update.effective_chat.id
    context.application.bot_data["summary_job_time"] = raw

    await update.message.reply_text(
        f"Ежедневная сводка будет приходить в {raw} (по Кишинёву) в этот чат.",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Отмена диалога
# ══════════════════════════════════════════════════════════════════════════════

async def cancel_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_auto_user = context.user_data.get("pending_auto_user")
    if pending_auto_user:
        context.application.bot_data["pending_unregistered_user"] = pending_auto_user
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# 📊 Сводка
# ══════════════════════════════════════════════════════════════════════════════

async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    now = now_local()
    today = now.strftime("%Y-%m-%d")

    all_workers = get_all_workers()
    reports_today = get_reports_for_date(today)

    reports_by_worker = {}
    for report in reports_today:
        reports_by_worker.setdefault(report["telegram_id"], []).append(report)

    total_ok = sum(1 for r in reports_today if r["is_ok"])
    total_remarks = sum(1 for r in reports_today if not r["is_ok"])
    total_late = sum(1 for r in reports_today if r["is_late"])

    not_sent_workers = [
        f"{w['last_name']} {w['first_name']} ({w['position']})"
        for w in all_workers
        if w["telegram_id"] not in reports_by_worker
    ]

    lines = [
        f"Сводный отчет за {now.strftime('%d.%m.%Y')}",
        "",
        f"Всего сотрудников: {len(all_workers)}",
        f"Отчетов без замечаний: {total_ok}",
        f"Отчетов с замечаниями: {total_remarks}",
        f"Опозданий: {total_late}",
        f"Не прислали отчет: {len(not_sent_workers)}",
    ]
    if not_sent_workers:
        lines += ["", "Не прислали отчет:"] + [f"- {n}" for n in not_sent_workers]

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def summary_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    class FakeJob:
        chat_id = update.effective_chat.id

    class FakeContext:
        job = FakeJob()
        bot = context.bot

    await send_daily_summary(FakeContext())


# ══════════════════════════════════════════════════════════════════════════════
# Обработка видео
# ══════════════════════════════════════════════════════════════════════════════

async def process_video_report(
    context: ContextTypes.DEFAULT_TYPE,
    worker,
    user_id: int,
    video_file_id: str,
    employee_chat_id: int | None = None,
):
    now = now_local()  # ← локальное время UTC+2/+3
    full_name = f"{worker['last_name']} {worker['first_name']}".strip()
    position = worker["position"]
    group_id = worker["group_id"]
    schedule = SCHEDULES.get(worker["schedule"], SCHEDULE_A)

    try:
        video_file = await context.bot.get_file(video_file_id)
        os.makedirs("tmp", exist_ok=True)
        file_path = os.path.join("tmp", f"{user_id}_{now.strftime('%Y%m%d_%H%M%S')}.mp4")
        await video_file.download_to_drive(file_path)
        speech_text = transcribe_audio(file_path)
        result = check_status(speech_text)
    except Exception as e:
        speech_text = ""
        result = normalize_ai_result(
            {
                "report_type": "status",
                "is_ok": False,
                "issue": f"Не удалось обработать видео: {e}",
                "required_action": "Проверить отчет вручную",
                "employee_message": "Отчет получен, но его нужно проверить вручную.",
            },
            speech_text,
        )

    report_date = now.strftime("%Y-%m-%d")
    report_type = result["report_type"]

    if report_type == "daily_fact":
        header = f"<b>{full_name} ({position})</b> — Ф̲А̲К̲Т̲ за день ({now.strftime('%d.%m')})"
        slot_time = None
        is_late = False
    else:
        slot_time, is_late = find_nearest_slot(schedule, now)
        header = f"<b>{full_name} ({position})</b> — статус {now.strftime('%d.%m')} за {slot_time}"

    text = (
        f"{header}\n"
        f"Формат отчёта: {result['format_comment']}\n"
        f"Действия: {result['required_action']}"
    )

    save_report(
        telegram_id=user_id,
        report_date=report_date,
        report_type=report_type,
        slot_time=slot_time,
        received_at=now.strftime("%H:%M"),
        is_ok=result["is_ok"],
        is_late=is_late,
        format_comment=result["format_comment"],
        required_action=result["required_action"],
    )

    await context.bot.send_video(chat_id=group_id, video=video_file_id)
    await context.bot.send_message(chat_id=group_id, text=text, parse_mode="HTML")

    if employee_chat_id:
        if not result["is_ok"] and result["employee_message"]:
            await context.bot.send_message(
                chat_id=employee_chat_id,
                text=result["employee_message"],
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await context.bot.send_message(
                chat_id=employee_chat_id,
                text="Отчет отправлен и сохранён.",
                reply_markup=ReplyKeyboardRemove(),
            )


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.id)

    if worker is None:
        user_name = " ".join(p for p in [user.first_name, user.last_name] if p).strip() or "Без имени"
        username = f"@{user.username}" if user.username else "username не указан"
        context.application.bot_data["pending_unregistered_user"] = {
            "telegram_id": user.id,
            "name": user_name,
            "username": username,
            "chat_id": update.effective_chat.id,
            "video_file_id": update.message.video.file_id,
        }
        employee_warning = "Вы не зарегистрированы как сотрудник. Обратитесь к администратору."
        await update.message.reply_text(employee_warning, reply_markup=ReplyKeyboardRemove())
        if ADMIN_ID:
            admin_text = (
                "Новый незарегистрированный сотрудник прислал видео.\n\n"
                f"Имя: {user_name}\nUsername: {username}\n"
                f"Telegram ID: {user.id}\nChat ID: {update.effective_chat.id}\n\n"
                "Добавьте через кнопку ➕ Добавить сотрудника — ID подставится автоматически."
            )
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text)
            except Exception as e:
                print(f"Не удалось уведомить админа: {e}")
        return

    try:
        await process_video_report(
            context=context,
            worker=worker,
            user_id=user.id,
            video_file_id=update.message.video.file_id,
            employee_chat_id=update.effective_chat.id,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}", reply_markup=ReplyKeyboardRemove())


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(
            "Отправьте видеоотчет. Меню доступно только администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not TOKEN:
        raise ValueError("Не задана переменная TELEGRAM_TOKEN")
    if not GROQ_API_KEY:
        print("Предупреждение: GROQ_API_KEY не задан.")
    if not ADMIN_ID:
        print("Предупреждение: ADMIN_ID не задан.")

    init_db()
    app = Application.builder().token(TOKEN).build()

    # ── Диалог: просмотр и редактирование сотрудников ─────────────────────
    list_workers_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📋 Сотрудники$"), list_workers),
            CommandHandler("workers", list_workers),
        ],
        states={
            ASK_LIST_DEPARTMENT: [MessageHandler(DIALOG_TEXT, list_workers_department)],
            ASK_LIST_WORKER:     [MessageHandler(DIALOG_TEXT, list_workers_select)],
            ASK_EDIT_FIELD:      [MessageHandler(DIALOG_TEXT, list_workers_action)],
            ASK_EDIT_VALUE:      [MessageHandler(DIALOG_TEXT, edit_value_finish)],
            ASK_EDIT_GROUP_VALUE:[MessageHandler(DIALOG_TEXT, edit_group_finish)],
            ASK_EDIT_SCHEDULE:   [MessageHandler(DIALOG_TEXT, edit_schedule_finish)],
            ASK_EDIT_DAILY_FACT: [MessageHandler(DIALOG_TEXT, edit_daily_fact_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    # ── Диалог: добавление сотрудника ─────────────────────────────────────
    add_worker_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить сотрудника$"), add_worker_start),
            CommandHandler("add_worker", add_worker_start),
        ],
        states={
            ASK_WORKER_ID:       [MessageHandler(DIALOG_TEXT, add_worker_id)],
            ASK_LASTNAME:        [MessageHandler(DIALOG_TEXT, add_worker_lastname)],
            ASK_FIRSTNAME:       [MessageHandler(DIALOG_TEXT, add_worker_firstname)],
            ASK_POSITION:        [MessageHandler(DIALOG_TEXT, add_worker_position)],
            ASK_GROUP:           [MessageHandler(DIALOG_TEXT, add_worker_group)],
            ASK_SCHEDULE:        [MessageHandler(DIALOG_TEXT, add_worker_schedule)],
            ASK_NEEDS_DAILY_FACT:[MessageHandler(DIALOG_TEXT, add_worker_needs_daily_fact)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    # ── Диалог: удаление сотрудника ───────────────────────────────────────
    remove_worker_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➖ Удалить сотрудника$"), remove_worker_start),
            CommandHandler("remove_worker", remove_worker_start),
        ],
        states={
            ASK_REMOVE_DEPARTMENT:[MessageHandler(DIALOG_TEXT, remove_worker_department)],
            ASK_REMOVE_WORKER:    [MessageHandler(DIALOG_TEXT, remove_worker_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    # ── Диалог: сотрудники отдела ─────────────────────────────────────────
    department_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🏢 Сотрудники отдела$"), department_start),
            CommandHandler("department", department_start),
        ],
        states={
            ASK_DEPARTMENT:[MessageHandler(DIALOG_TEXT, department_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    # ── Диалог: время сводки ──────────────────────────────────────────────
    report_time_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^⏰ Время сводки$"), set_report_time_start),
            CommandHandler("set_report_time", set_report_time_start),
        ],
        states={
            ASK_REPORT_TIME:[MessageHandler(DIALOG_TEXT, set_report_time_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))
    app.add_handler(CommandHandler("summary_now", summary_now))

    app.add_handler(list_workers_conv)
    app.add_handler(add_worker_conv)
    app.add_handler(remove_worker_conv)
    app.add_handler(department_conv)
    app.add_handler(report_time_conv)

    app.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), summary_now))
    app.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))

    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    print("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
