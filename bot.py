import json
import os
import sqlite3
from datetime import datetime
import datetime as dt_module
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

    # Таблица для настроек (для сохранения расписания сводок)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

def save_report(telegram_id: int, report_date: str, report_type: str, slot_time: str | None, received_at: str, is_ok: bool, is_late: bool, format_comment: str, required_action: str) -> int:
    """Слегка изменена для возврата ID новой записи (для исправления оценок ИИ)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reports (telegram_id, report_date, report_type, slot_time, received_at, is_ok, is_late, format_comment, required_action)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, report_date, report_type, slot_time, received_at, int(is_ok), int(is_late), format_comment, required_action),
    )
    conn.commit()
    inserted_id = cur.lastrowid
    conn.close()
    return inserted_id

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

    summary_lines = [f"📊 Сводка отчетов за {report_date}\n"]
    
    workers_by_dept = {}
    for w in workers:
        dept = w["position"]
        if dept not in workers_by_dept:
            workers_by_dept[dept] = []
        workers_by_dept[dept].append(w)

    for dept, dept_workers in workers_by_dept.items():
        summary_lines.append(f"🏢 Отдел: {dept}")
        for w in dept_workers:
            tid = w["telegram_id"]
            name = f"{w['last_name']} {w['first_name']}"
            w_reports = reports_by_worker.get(tid, {"status": {}, "daily_fact": []})
            
            # 1. Почасовые статусы
            schedule_slots = SCHEDULES.get(w["schedule"], SCHEDULE_A)
            status_segments = []
            for slot in schedule_slots:
                rep = w_reports["status"].get(slot)
                if rep:
                    status_icon = "✅" if rep["is_ok"] else "⚠️"
                    late_icon = "⏰" if rep["is_late"] else ""
                    comment = f" ({rep['format_comment']})" if not rep["is_ok"] else ""
                    status_segments.append(f"{slot}:{status_icon}{late_icon}{comment}")
                else:
                    status_segments.append(f"{slot}:❌")
            
            status_str = " | ".join(status_segments)
            summary_lines.append(f"  • {name}")
            summary_lines.append(f"    📢 Статусы: {status_str}")

            # 2. Факт дня (daily_fact)
            if w["needs_daily_fact"]:
                fact_reps = w_reports["daily_fact"]
                if fact_reps:
                    f_rep = fact_reps[-1]
                    fact_icon = "✅" if f_rep["is_ok"] else "⚠️"
                    comment = f" ({f_rep['format_comment']})" if not f_rep["is_ok"] else ""
                    summary_lines.append(f"    📝 Итог дня (Факт): {fact_icon}{comment}")
                else:
                    summary_lines.append(f"    📝 Итог дня (Факт): ❌ Не отправлен")
            else:
                summary_lines.append(f"    📝 Итог дня (Факт): Не требуется")
        summary_lines.append("")

    return "\n".join(summary_lines)


# ══════════════════════════════════════════════════════════════════════════════
# Решение проблемы 7 (Обработчик Callback-кнопки переключения результатов)
# ══════════════════════════════════════════════════════════════════════════════

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
        new_text = (
            f"🔧 Оценка отчета изменена вручную администратором @{query.from_user.username or user_id}:\n"
            f"Сотрудник: {worker_name}\n"
            f"Дата отчета: {report['report_date']}\n"
            f"Слот/Тип: {report['slot_time'] or report['report_type']}\n"
            f"Новый статус: {status_emoji} ({new_comment})"
        )
        
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Изменить оценку повторно", callback_data=f"fix_toggle_{report_id}")]
        ])
        
        try:
            await query.edit_message_text(text=new_text, reply_markup=kbd)
        except Exception as e:
            print(f"Ошибка обновления интерактивной кнопки: {e}")


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
    info = f"👤 {worker['last_name']} {worker['first_name']}\nОтдел: {worker['position']}\nГрафик: {worker['schedule']} ({schedule_str})\nГруппа: {gname}\nФакт дня: {fact}\n\nЧто хотите сделать?"

    # Добавление кнопки Истории еженедельных оценок (проблема 6)
    kbd = ReplyKeyboardMarkup(
        [
            ["📅 История за неделю"],
            ["✏️ Изменить фамилию", "✏️ Изменить имя"],
            ["✏️ Изменить отдел", "✏️ Изменить график"],
            ["✏️ Изменить группу", "✏️ Факт дня"],
            ["🔼 Вверх в списке", "🔽 Вниз в списке"],
            ["❌ Отмена"]
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
    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    summary_text = generate_daily_summary_text(date_str)
    
    await update.message.reply_text(summary_text, reply_markup=MAIN_MENU)
    
    if SUMMARY_CHAT_ID and SUMMARY_CHAT_ID != update.effective_chat.id:
        try:
            await context.bot.send_message(chat_id=SUMMARY_CHAT_ID, text=summary_text)
        except Exception as e:
            print(f"Не удалось отправить сводку в чат {SUMMARY_CHAT_ID}: {e}")


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
                text_content = transcribe_audio(tmp_path)

        if not text_content:
            await update.message.reply_text("Ошибка: Не удалось распознать аудио или медиа отчета.")
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

        # Анализ промптом Llama
        ai_res = check_status(text_content)
        now = now_local()
        date_str = now.strftime("%Y-%m-%d")
        sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
        nearest_slot, is_late = find_nearest_slot(sched_list, now)

        # Решение проблемы 5: Проверка дубликатов для одной даты и слота/типа
        if check_duplicate_report(user_id, date_str, ai_res["report_type"], nearest_slot):
            if ai_res["report_type"] == "status":
                await update.message.reply_text(f"⚠️ Вы уже отправляли отчет по статусу за сегодня (слот {nearest_slot}).")
            else:
                await update.message.reply_text("⚠️ Вы уже отправляли итоговый факт дня на сегодня.")
            return

        report_id = save_report(
            telegram_id=user_id,
            report_date=date_str,
            report_type=ai_res["report_type"],
            slot_time=nearest_slot if ai_res["report_type"] == "status" else None,
            received_at=now.strftime("%H:%M:%S"),
            is_ok=ai_res["is_ok"],
            is_late=is_late if ai_res["report_type"] == "status" else 0,
            format_comment=ai_res["format_comment"],
            required_action=ai_res["required_action"]
        )

        w_name = f"{worker['last_name']} {worker['first_name']}"
        
        # Информирование сотрудника
        if ai_res["is_ok"]:
            await update.message.reply_text("✅ Отчёт успешно проверен ИИ и принят без замечаний! Спасибо.")
        else:
            await update.message.reply_text(f"⚠️ Оценка отчета: {ai_res['employee_message']}")

        # Решение проблемы 7: Кнопка «Исправить оценку» во всех отчетах для администраторов или в группе
        is_ok_emoji = "✅" if ai_res["is_ok"] else "⚠️"
        dest_chat = worker["group_id"] or DEFAULT_GROUP_ID
        
        # Получаем красивое название группы
        gname = await get_group_name_async(context.bot, dest_chat)
        
        notify_text = (
            f"📊 {is_ok_emoji} Новый отчет: {w_name}\n"
            f"Тип/Слот: {nearest_slot if ai_res['report_type'] == 'status' else 'Факт дня (Итог)'}\n"
            f"Оценка ИИ: {'ОК' if ai_res['is_ok'] else 'НЕ ОК'}\n"
            f"Комментарий ИИ: {ai_res['format_comment']}\n"
            f"Группа: {gname}\n"
            f"Текст:\n\"{text_content}\""
        )
        
        inline_kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Изменить оценку (ОК / НЕ ОК)", callback_data=f"fix_toggle_{report_id}")]
        ])
        
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

def main():
    init_db()
    if not TOKEN:
        print("Критическая ошибка: не задан TELEGRAM_TOKEN")
        return

    # Запуск бота с подключением к функции post_init для восстановления кеша и сводок
    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Точечные команды и кнопки меню (срабатывают моментально)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^🆔 ID чата$"), get_chat_id))
    application.add_handler(MessageHandler(filters.Regex("^📊 Сводка сейчас$"), send_summary_now))
    
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

    # Регистрация диалогов
    application.add_handler(list_handler)
    application.add_handler(add_handler)
    application.add_handler(delete_handler)
    application.add_handler(view_dept_handler)
    application.add_handler(summary_scheduler_handler)

    # Хэндлер для приема аудио/видео/текстовых отчетов сотрудников (регистрируется в самом конце)
    application.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.TEXT & ~filters.COMMAND, 
        handle_report
    ))

    print("Бот успешно инициализирован и запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()
