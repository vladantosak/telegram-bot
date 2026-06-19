import json
import os
import sqlite3
from datetime import datetime, time as dtime

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
) = range(11)


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
            needs_daily_fact INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    if "schedule" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN schedule TEXT NOT NULL DEFAULT 'A'")
    if "needs_daily_fact" not in cols:
        conn.execute("ALTER TABLE workers ADD COLUMN needs_daily_fact INTEGER NOT NULL DEFAULT 1")

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
    rows = conn.execute("SELECT * FROM workers ORDER BY position, last_name, first_name").fetchall()
    conn.close()
    return rows


def get_workers_by_position(position: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers WHERE lower(position) = lower(?) ORDER BY last_name, first_name",
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
):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers
            (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            last_name=excluded.last_name,
            first_name=excluded.first_name,
            position=excluded.position,
            group_id=excluded.group_id,
            schedule=excluded.schedule,
            needs_daily_fact=excluded.needs_daily_fact
        """,
        (
            telegram_id,
            last_name,
            first_name,
            position,
            group_id,
            schedule,
            int(needs_daily_fact),
        ),
    )
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
            telegram_id,
            report_date,
            report_type,
            slot_time,
            received_at,
            int(is_ok),
            int(is_late),
            format_comment,
            required_action,
        ),
    )
    conn.commit()
    conn.close()


def get_reports_for_date(report_date: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM reports WHERE report_date = ?", (report_date,)).fetchall()
    conn.close()
    return rows


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


def normalize_ai_result(data: dict, source_text: str) -> dict:
    text_lower = source_text.lower()
    report_type = str(data.get("report_type", "status")).strip().lower()

    fact_words = (
        "факт",
        "факт дня",
        "за день",
        "итог дня",
        "итоги дня",
        "сегодня за день",
        "дневной отчет",
    )
    status_words = (
        "статус",
        "сейчас",
        "на данный момент",
        "за 10",
        "за 11",
        "за 12",
        "за 13",
        "за 15",
        "за 16",
        "за 17",
        "за 18",
    )

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
        format_comment = f"не ОК: {issue}"
        if not required_action or required_action.lower() == "ничего не предпринимать":
            required_action = "уточнить отчет у сотрудника"
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
Ты проверяешь расшифровку видеоотчета сотрудника.

Нужно строго отличать два типа отчета:

1. "status" - текущий статус за конкретное время. Обычно сотрудник говорит:
   "статус", "на данный момент", "сейчас", "за 10:00", "за 12:00", "за 15:00".
   В статусе должно быть понятно, что сделано/проверено сейчас, есть ли проблемы,
   и по возможности объем работы. Если сотрудник просто говорит "все нормально"
   без сути, объема или результата, это замечание.

2. "daily_fact" - факт за день, итог за весь день. Обычно сотрудник говорит:
   "факт", "факт за день", "итог дня", "за день", "сегодня за день".
   В факте нужно оценить дневной итог, а не текущий слот.

Оценка:
- Если отчет понятный, есть смысл/результат, нет проблем и не нужны действия,
  поставь is_ok=true.
- Если не указан объем работ, нет конкретики, есть проблема, неясно что сделано,
  или нужна реакция руководителя, поставь is_ok=false.
- Если is_ok=true, required_action обязательно: "ничего не предпринимать".
- Если is_ok=false, issue должен коротко объяснить замечание.
- Если is_ok=false, required_action должен говорить, что нужно сделать руководителю.
- Если is_ok=false, employee_message должен быть понятным сообщением сотруднику.
  Например: "Вы не указали объем работ. В следующем отчете не забудьте указать, что именно сделано."

Верни только JSON без Markdown:
{{
  "report_type": "status" или "daily_fact",
  "is_ok": true или false,
  "issue": "короткое замечание или пустая строка",
  "required_action": "ничего не предпринимать или конкретное действие",
  "employee_message": "сообщение сотруднику при замечании или пустая строка"
}}

Расшифровка отчета:
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
                {
                    "role": "system",
                    "content": "Отвечай только валидным JSON без Markdown.",
                },
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


async def list_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return

    lines = ["Список сотрудников:"]
    for row in rows:
        schedule_str = ", ".join(SCHEDULES.get(row["schedule"], SCHEDULE_A))
        daily_fact = "да" if row["needs_daily_fact"] else "нет"
        lines.append(
            f"- {row['last_name']} {row['first_name']} ({row['position']})\n"
            f"  ID: {row['telegram_id']}, группа: {row['group_id']}, график: {schedule_str}, факт дня: {daily_fact}"
        )

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)


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
    await update.message.reply_text(
        "Введите должность или отдел сотрудника:",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ASK_POSITION


async def add_worker_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите ID группы Telegram, куда отправлять отчеты сотрудника.\n"
        f"Если нужно использовать группу по умолчанию ({DEFAULT_GROUP_ID}), введите 0.",
        reply_markup=CANCEL_KEYBOARD,
    )
    return ASK_GROUP


async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        group_id = int(raw)
    except ValueError:
        await update.message.reply_text("Введите числовой ID группы или 0:", reply_markup=CANCEL_KEYBOARD)
        return ASK_GROUP

    context.user_data["group_id"] = DEFAULT_GROUP_ID if group_id == 0 else group_id
    await update.message.reply_text(
        "Выберите график отчетов:\n"
        "A: 10:00, 12:00, 15:00, 17:00\n"
        "B: 11:00, 13:00, 16:00, 18:00",
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

    upsert_worker(worker_id, last_name, first_name, position, group_id, schedule, needs_daily_fact)

    await update.message.reply_text(
        "Готово! Сотрудник добавлен:\n"
        f"{last_name} {first_name} ({position})\n"
        f"ID: {worker_id}\n"
        f"Группа: {group_id}\n"
        f"График: {', '.join(SCHEDULES[schedule])}\n"
        f"Факт дня: {'да' if needs_daily_fact else 'нет'}",
        reply_markup=MAIN_MENU,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def remove_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END

    rows = get_all_workers()
    positions = sorted({row["position"] for row in rows})
    if not positions:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    keyboard = [[position] for position in positions]
    keyboard.append(["❌ Отмена"])
    await update.message.reply_text(
        "Выберите отдел, из которого нужно удалить сотрудника:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return ASK_REMOVE_DEPARTMENT


async def remove_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text(
            f"В отделе '{position}' сотрудники не найдены. Выберите отдел еще раз.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    context.user_data["remove_position"] = position
    keyboard = []
    for row in rows:
        keyboard.append([f"{row['last_name']} {row['first_name']} - ID {row['telegram_id']}"])
    keyboard.append(["❌ Отмена"])

    await update.message.reply_text(
        f"Выберите сотрудника для удаления из отдела '{position}':",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return ASK_REMOVE_WORKER


async def remove_worker_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    marker = "ID "
    if marker not in raw:
        await update.message.reply_text("Выберите сотрудника кнопкой из списка.", reply_markup=CANCEL_KEYBOARD)
        return ASK_REMOVE_WORKER

    target_id_raw = raw.rsplit(marker, 1)[1].strip()
    if not target_id_raw.lstrip("-").isdigit():
        await update.message.reply_text("Не удалось прочитать ID. Выберите сотрудника кнопкой из списка.", reply_markup=CANCEL_KEYBOARD)
        return ASK_REMOVE_WORKER

    target_id = int(target_id_raw)
    worker = get_worker(target_id)
    if worker is None:
        await update.message.reply_text(f"Сотрудник с ID {target_id} не найден.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    name = f"{worker['last_name']} {worker['first_name']}"
    delete_worker(target_id)
    context.user_data.pop("remove_position", None)
    await update.message.reply_text(f"Сотрудник удален: {name} (ID {target_id})", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def department_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END

    rows = get_all_workers()
    positions = sorted({row["position"] for row in rows})
    if not positions:
        await update.message.reply_text("В базе пока нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    keyboard = [[position] for position in positions[:20]]
    keyboard.append(["❌ Отмена"])
    await update.message.reply_text(
        "Выберите отдел или введите название вручную:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return ASK_DEPARTMENT


async def department_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = update.message.text.strip()
    rows = get_workers_by_position(position)

    if not rows:
        await update.message.reply_text(f"В отделе '{position}' сотрудники не найдены.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    lines = [f"Сотрудники отдела '{position}':"]
    for row in rows:
        lines.append(f"- {row['last_name']} {row['first_name']} - ID {row['telegram_id']}")

    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def set_report_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END

    await update.message.reply_text("Введите время ежедневной сводки в формате HH:MM:", reply_markup=CANCEL_KEYBOARD)
    return ASK_REPORT_TIME


async def set_report_time_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        hour, minute = map(int, raw.split(":"))
        report_time = dtime(hour=hour, minute=minute)
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
        f"Ежедневная сводка будет приходить в {raw} в этот чат.",
        reply_markup=MAIN_MENU,
    )
    return ConversationHandler.END


async def cancel_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_auto_user = context.user_data.get("pending_auto_user")
    if pending_auto_user:
        context.application.bot_data["pending_unregistered_user"] = pending_auto_user
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    today = datetime.now().strftime("%Y-%m-%d")

    all_workers = get_all_workers()
    reports_today = get_reports_for_date(today)

    reports_by_worker = {}
    for report in reports_today:
        reports_by_worker.setdefault(report["telegram_id"], []).append(report)

    total_ok = sum(1 for report in reports_today if report["is_ok"])
    total_remarks = sum(1 for report in reports_today if not report["is_ok"])
    total_late = sum(1 for report in reports_today if report["is_late"])

    not_sent_workers = []
    for worker in all_workers:
        if worker["telegram_id"] not in reports_by_worker:
            not_sent_workers.append(f"{worker['last_name']} {worker['first_name']} ({worker['position']})")

    lines = [
        f"Сводный отчет за {datetime.now().strftime('%d.%m.%Y')}",
        "",
        f"Всего сотрудников: {len(all_workers)}",
        f"Отчетов без замечаний: {total_ok}",
        f"Отчетов с замечаниями: {total_remarks}",
        f"Опозданий: {total_late}",
        f"Не прислали отчет: {len(not_sent_workers)}",
    ]

    if not_sent_workers:
        lines.append("")
        lines.append("Не прислали отчет:")
        for name in not_sent_workers:
            lines.append(f"- {name}")

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


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    worker = get_worker(user.id)

    if worker is None:
        user_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "Без имени"
        username = f"@{user.username}" if user.username else "username не указан"
        context.application.bot_data["pending_unregistered_user"] = {
            "telegram_id": user.id,
            "name": user_name,
            "username": username,
            "chat_id": update.effective_chat.id,
        }

        employee_warning = "Вы не зарегистрированы как сотрудник. Обратитесь к администратору."
        await update.message.reply_text(
            employee_warning,
            reply_markup=ReplyKeyboardRemove(),
        )

        admin_text = (
            "Сотруднику показано сообщение, что его нет в базе и нужно обратиться к администратору.\n\n"
            "Данные пользователя:\n"
            f"Имя: {user_name}\n"
            f"Username: {username}\n"
            f"Telegram ID: {user.id}\n"
            f"Chat ID: {update.effective_chat.id}\n\n"
            f"Текст для сотрудника: {employee_warning}\n\n"
            "Добавьте сотрудника через кнопку:\n"
            "➕ Добавить сотрудника\n\n"
            "Telegram ID подставится автоматически."
        )
        if ADMIN_ID:
            try:
                await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text)
            except Exception as e:
                print(f"Не удалось отправить уведомление админу: {e}")

        return

    now = datetime.now()
    full_name = f"{worker['last_name']} {worker['first_name']}".strip()
    position = worker["position"]
    group_id = worker["group_id"]
    schedule = SCHEDULES.get(worker["schedule"], SCHEDULE_A)

    try:
        video_file = await update.message.video.get_file()
        os.makedirs("tmp", exist_ok=True)
        file_path = os.path.join("tmp", f"{user.id}_{now.strftime('%Y%m%d_%H%M%S')}.mp4")
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
        header = f"<b>{full_name} ({position})</b> - Ф̲А̲К̲Т̲  за день ({now.strftime('%d.%m')})"
        slot_time = None
        is_late = False
    else:
        slot_time, is_late = find_nearest_slot(schedule, now)
        header = f"<b>{full_name} ({position})</b> - статус {now.strftime('%d.%m')} за {slot_time}"

    text = (
        f"{header}\n"
        f"Формат отчета: {result['format_comment']} ,\n"
        f"Требуемые действия: {result['required_action']}"
    )

    save_report(
        telegram_id=user.id,
        report_date=report_date,
        report_type=report_type,
        slot_time=slot_time,
        received_at=now.strftime("%H:%M"),
        is_ok=result["is_ok"],
        is_late=is_late,
        format_comment=result["format_comment"],
        required_action=result["required_action"],
    )

    try:
        await context.bot.send_video(chat_id=group_id, video=update.message.video.file_id)
        await context.bot.send_message(chat_id=group_id, text=text, parse_mode="HTML")
        if not result["is_ok"] and result["employee_message"]:
            await update.message.reply_text(result["employee_message"], reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text("Отчет отправлен и сохранен.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        await update.message.reply_text(f"Ошибка отправки в группу: {e}", reply_markup=ReplyKeyboardRemove())


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(
            "Отправьте видеоотчет. Меню с кнопками доступно только администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )


def main():
    if not TOKEN:
        raise ValueError("Не задана переменная окружения TELEGRAM_TOKEN")
    if not GROQ_API_KEY:
        print("Предупреждение: GROQ_API_KEY не задан, распознавание и проверка ИИ будут недоступны.")
    if not ADMIN_ID:
        print("Предупреждение: ADMIN_ID не задан, админские кнопки работать не будут.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    add_worker_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Добавить сотрудника$"), add_worker_start),
            CommandHandler("add_worker", add_worker_start),
        ],
        states={
            ASK_WORKER_ID: [MessageHandler(DIALOG_TEXT, add_worker_id)],
            ASK_LASTNAME: [MessageHandler(DIALOG_TEXT, add_worker_lastname)],
            ASK_FIRSTNAME: [MessageHandler(DIALOG_TEXT, add_worker_firstname)],
            ASK_POSITION: [MessageHandler(DIALOG_TEXT, add_worker_position)],
            ASK_GROUP: [MessageHandler(DIALOG_TEXT, add_worker_group)],
            ASK_SCHEDULE: [MessageHandler(DIALOG_TEXT, add_worker_schedule)],
            ASK_NEEDS_DAILY_FACT: [
                MessageHandler(DIALOG_TEXT, add_worker_needs_daily_fact)
            ],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    remove_worker_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➖ Удалить сотрудника$"), remove_worker_start),
            CommandHandler("remove_worker", remove_worker_start),
        ],
        states={
            ASK_REMOVE_DEPARTMENT: [MessageHandler(DIALOG_TEXT, remove_worker_department)],
            ASK_REMOVE_WORKER: [MessageHandler(DIALOG_TEXT, remove_worker_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    department_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🏢 Сотрудники отдела$"), department_start),
            CommandHandler("department", department_start),
        ],
        states={
            ASK_DEPARTMENT: [MessageHandler(DIALOG_TEXT, department_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    report_time_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^⏰ Время сводки$"), set_report_time_start),
            CommandHandler("set_report_time", set_report_time_start),
        ],
        states={
            ASK_REPORT_TIME: [MessageHandler(DIALOG_TEXT, set_report_time_finish)],
        },
        fallbacks=[
            MessageHandler(filters.Regex("^❌ Отмена$"), cancel_dialog),
            CommandHandler("cancel", cancel_dialog),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))
    app.add_handler(CommandHandler("workers", list_workers))
    app.add_handler(CommandHandler("summary_now", summary_now))

    app.add_handler(add_worker_conv)
    app.add_handler(remove_worker_conv)
    app.add_handler(department_conv)
    app.add_handler(report_time_conv)

    app.add_handler(MessageHandler(filters.Regex("^📋 Сотрудники$"), list_workers))
    app.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), summary_now))
    app.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))

    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    print("Бот запущен. Управление доступно через кнопки.")
    app.run_polling()


if __name__ == "__main__":
    main()
