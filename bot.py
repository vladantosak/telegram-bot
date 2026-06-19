import json
import os
import sqlite3
from datetime import datetime, time as dtime

from groq import Groq
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ==========================
# НАСТРОЙКИ
# ==========================

TOKEN = os.environ.get("TELEGRAM_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
# Чат руководителей, куда уходит ежедневный сводный отчёт.
# Если не задан — используется ADMIN_ID (личка админу).
SUMMARY_CHAT_ID = int(os.environ.get("SUMMARY_CHAT_ID", "0")) or ADMIN_ID

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Допуск на опоздание в минутах: статус считается "вовремя", если пришёл
# не позже чем через LATE_THRESHOLD_MIN минут после ожидаемого времени слота.
LATE_THRESHOLD_MIN = 15

# Два варианта расписания статусов в течение дня (время "ХЧ:ММ")
SCHEDULE_A = ["10:00", "12:00", "15:00", "17:00"]
SCHEDULE_B = ["11:00", "13:00", "16:00", "18:00"]
SCHEDULES = {"A": SCHEDULE_A, "B": SCHEDULE_B}

# Состояния диалога добавления сотрудника
(
    ASK_LASTNAME,
    ASK_FIRSTNAME,
    ASK_POSITION,
    ASK_GROUP,
    ASK_SCHEDULE,
    ASK_NEEDS_DAILY_FACT,
) = range(6)


# ==========================
# БАЗА ДАННЫХ (SQLite)
# ==========================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    existing_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()
    }

    if existing_cols and "last_name" not in existing_cols:
        print("Обнаружена старая структура таблицы workers — выполняю миграцию")
        conn.execute("ALTER TABLE workers RENAME TO workers_old")
        conn.execute(
            """
            CREATE TABLE workers (
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
        if "name" in existing_cols:
            old_rows = conn.execute("SELECT * FROM workers_old").fetchall()
            for row in old_rows:
                parts = (row["name"] or "").split(" ", 1)
                last_name = parts[0] if parts else "Без фамилии"
                first_name = parts[1] if len(parts) > 1 else ""
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workers
                    (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact)
                    VALUES (?, ?, ?, ?, ?, 'A', 1)
                    """,
                    (
                        row["telegram_id"],
                        last_name,
                        first_name,
                        row["position"] if "position" in row.keys() else "Не указано",
                        row["group_id"],
                    ),
                )
        conn.execute("DROP TABLE workers_old")
        conn.commit()
        print("Миграция завершена")
    else:
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
        # На случай если таблица уже была в "промежуточной" версии без schedule
        cols_now = {
            row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()
        }
        if "schedule" not in cols_now:
            conn.execute("ALTER TABLE workers ADD COLUMN schedule TEXT NOT NULL DEFAULT 'A'")
        if "needs_daily_fact" not in cols_now:
            conn.execute(
                "ALTER TABLE workers ADD COLUMN needs_daily_fact INTEGER NOT NULL DEFAULT 1"
            )
        conn.commit()

    # Таблица отчётов — для статистики и сводного отчёта
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
    print(f"База данных готова: {DB_PATH}")


def get_worker(telegram_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    conn.close()
    return row


def get_workers_by_position(position: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers WHERE position = ? ORDER BY last_name", (position,)
    ).fetchall()
    conn.close()
    return rows


def get_all_workers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM workers ORDER BY position, last_name").fetchall()
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
        (telegram_id, last_name, first_name, position, group_id, schedule, int(needs_daily_fact)),
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


def count_workers() -> int:
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
    conn.close()
    return n


def save_report(
    telegram_id: int,
    report_date: str,
    report_type: str,
    slot_time: str,
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
    rows = conn.execute(
        "SELECT * FROM reports WHERE report_date = ?", (report_date,)
    ).fetchall()
    conn.close()
    return rows


init_db()
print(f"Загружено сотрудников: {count_workers()}")


# ==========================
# ТРАНСКРИПЦИЯ ЧЕРЕЗ GROQ WHISPER
# ==========================

def transcribe_audio(file_path: str) -> str:
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
        return f"Ошибка распознавания: {e}"


# ==========================
# ПРОВЕРКА ОТЧЁТА ЧЕРЕЗ GROQ LLM
# ==========================

CHECK_PROMPT_TEMPLATE = """Ты — строгий бригадир на стройке, который проверяет голосовые видеоотчёты рабочих.

Рабочие — простые строители разных специальностей (маляры, охранники, экскаваторщики, водители и т.д.). Они говорят разговорным языком, путают слова, строят фразы криво, иногда повторяются или перескакивают с одного на другое. Текст может содержать ошибки распознавания речи (отдельные слова искажены или пропущены). Твоя задача — игнорировать форму и понять суть: что именно человек делал.

ГЛАВНОЕ ПРАВИЛО: ты склонен пропускать слабые отчёты как "ОК" — это твоя главная ошибка, с которой нужно бороться. Если есть малейшее сомнение, достаточно ли конкретен отчёт — выбирай "ЗАМЕЧАНИЕ", а не "ОК". Лучше лишний раз уточнить у сотрудника, чем пропустить пустой отчёт.

ШАГ 0 — ПРОВЕРЬ, ЕСТЬ ЛИ ВООБЩЕ РЕЧЬ:
Если текст пустой, состоит только из пробелов, бессмысленного набора символов, или содержит лишь служебные фразы без какой-либо информации о работе — это автоматически is_ok=false с замечанием "в видео нет голосового отчёта". Это проверяй ПЕРВЫМ, до всего остального.

ШАГ 1 — ОПРЕДЕЛИ ТИП ОТЧЁТА:
- Если сотрудник говорит про последние 1-2-3 часа, недавний период, "сейчас", "только что" — это СТАТУС (короткий отчёт за рабочий отрезок).
- Если сотрудник говорит "за весь день", "сегодня в течение дня", "с утра до вечера", "за смену", подводит итог дня целиком — это ФАКТ ДНЯ.
- Если из текста непонятно — считай это СТАТУСОМ по умолчанию.

ШАГ 2 — ПОЙМИ СУТЬ РАБОТЫ:
Восстанови, что сотрудник реально делал, даже если фраза построена странно. Учитывай, что работа может быть НЕ связана с производством конкретных физических единиц — например:
- перевозка/транспортировка грузов или людей
- работа на технике (дробилка, экскаватор, кран и т.п.) — здесь объёмом может быть время работы техники, количество циклов/рейсов, объём переработанного материала
- охрана объекта — здесь не может быть "штук" или "метров", достаточно того, что смена/пост подтверждены, есть ли замечания по объекту
- любая другая вспомогательная или нестандартная работа

Не требуй один и тот же тип измерения для всех профессий. Подходи по смыслу: для маляра — квадратные метры или количество объектов, для водителя — рейсы или объём перевезённого, для охранника — факт несения смены и любые замечания по объекту, для оператора техники — время работы или объём переработанного.

ШАГ 3 — РЕШИ, ЕСТЬ ЛИ ЗАМЕЧАНИЯ (применяй СТРОГО):

is_ok = false (замечание), если выполняется ХОТЯ БЫ ОДНО:
- отчёт не даёт понять, что конкретно делал сотрудник (расплывчато: "работал", "занимался делами", "всё нормально", "делал что было нужно")
- для работы, где естественно ожидать измеримый результат (покраска, кладка, монтаж, заливка, сварка и т.п.), результат не назван вообще ни в каком виде (ни в штуках, ни в долях, ни в метрах, ни во времени)
- названы только действия-глаголы без итога ("красил, убирал, таскал") — без того, СКОЛЬКО или ЧТО именно в результате
- объём заявлен абстрактно без числа и без ясной доли: "много", "прилично", "почти всё", "значительную часть" — это НЕ считается конкретным объёмом, даже если звучит как описание объёма
- текст короче одного полноценного предложения по смыслу, или содержит только общие слова

is_ok = true (всё ОК), только если:
- объём назван в конкретной форме: число + единица (штуки, метры, кг, литры, мешки), ИЛИ чётко названная доля от конкретного объекта ("половина забора", "треть стены" — именно с указанием ОТ ЧЕГО доля), ИЛИ время непрерывной работы на технике/посту, ИЛИ количество рейсов/циклов
- ИЛИ характер работы по своей природе не предполагает измеримых единиц (охрана, дежурство, ожидание) И сотрудник внятно доложил по существу (что охранял/дежурил, без происшествий или с конкретным замечанием)

ПРИМЕРЫ (ориентируйся на них):

Текст: "Ну я короче два часа работал, всё в принципе нормально, без проблем"
→ is_ok: false (нет ни одного факта о том, ЧТО делал — только общие слова "работал", "нормально")

Текст: "Покрасил почти всё, что было, ну в принципе много сделал"
→ is_ok: false ("почти всё" и "много" — это не число и не чёткая доля от названного объекта)

Текст: "Сегодня два часа красил забор, ну где-то половину забора сделал"
→ is_ok: true (чётко названа доля "половина забора" — понятно от чего доля)

Текст: "Возил щебень с карьера, сделал четыре рейса"
→ is_ok: true (конкретное число — рейсы)

Текст: "Стоял на посту, всё спокойно, происшествий не было"
→ is_ok: true (для охраны это и есть полноценный по существу доклад, измеримых единиц тут не требуется)

Текст: "Дежурил, ну как обычно"
→ is_ok: false ("как обычно" — это не доклад по существу, неясно дежурил ли он вообще весь период, были ли обходы)

Текст: "Работал на дробилке весь день, перемолол где-то 10 кубов щебня"
→ is_ok: true (конкретный объём в кубах)

ФОРМАТ ОТВЕТА (строго JSON, без markdown, без пояснений вокруг):
{{
  "report_type": "статус" или "факт_дня",
  "is_ok": true или false,
  "format_comment": "если is_ok=true: всё ОК. Если is_ok=false: краткая суть проблемы одним предложением",
  "required_action": "если is_ok=true: ничего не предпринимать. Если is_ok=false: что конкретно нужно донести/уточнить у сотрудника, одним предложением"
}}

Текст отчёта сотрудника:
{text}
"""


def check_status(text: str) -> dict:
    prompt = CHECK_PROMPT_TEMPLATE.format(text=text)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        return {
            "report_type": data.get("report_type", "статус"),
            "is_ok": bool(data.get("is_ok", False)),
            "format_comment": data.get("format_comment", "всё ОК"),
            "required_action": data.get("required_action", "ничего не предпринимать"),
        }
    except Exception as e:
        return {
            "report_type": "статус",
            "is_ok": False,
            "format_comment": f"Ошибка проверки ИИ: {e}",
            "required_action": "проверить вручную",
        }


# ==========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def find_nearest_slot(schedule: list[str], now: datetime):
    """Находит ближайший ожидаемый слот времени (строка 'HH:MM') не позже текущего момента + допуска.
    Возвращает (slot_str, is_late: bool) либо (None, False), если ни один слот не подходит."""
    now_minutes = now.hour * 60 + now.minute
    best_slot = None
    best_diff = None

    for slot in schedule:
        h, m = map(int, slot.split(":"))
        slot_minutes = h * 60 + m
        diff = now_minutes - slot_minutes
        # Слот считается релевантным, если текущее время не раньше слота
        # и в пределах разумного окна (до следующего слота или конца дня)
        if diff >= 0:
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_slot = slot

    if best_slot is None:
        return None, False

    is_late = best_diff > LATE_THRESHOLD_MIN
    return best_slot, is_late


# ==========================
# ОБРАБОТКА ВИДЕО
# ==========================

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.video:
        return

    user = update.effective_user
    print(f"Получено видео от {user.full_name} ({user.id})")

    worker = get_worker(user.id)

    if worker is None:
        await update.message.reply_text(
            "Вы пока не зарегистрированы в системе. "
            "Администратор получил уведомление и скоро добавит вас."
        )

        if ADMIN_ID:
            notify_text = (
                f"⚠️ Новый сотрудник прислал видео, но не найден в базе.\n\n"
                f"Имя в Telegram: {user.full_name}\n"
                f"Username: @{user.username if user.username else '—'}\n"
                f"ID: {user.id}\n\n"
                f"Чтобы добавить — ответьте (Reply) на это сообщение командой /add_worker"
            )
            try:
                await context.bot.send_video(
                    chat_id=ADMIN_ID, video=update.message.video.file_id
                )
                await context.bot.send_message(chat_id=ADMIN_ID, text=notify_text)
                context.bot_data.setdefault("pending_workers", {})[ADMIN_ID] = user.id
            except Exception as e:
                print(f"Не удалось уведомить админа: {e}")
        return

    last_name = worker["last_name"]
    first_name = worker["first_name"]
    position = worker["position"]
    group_id = int(worker["group_id"])
    schedule = SCHEDULES.get(worker["schedule"], SCHEDULE_A)

    now = datetime.now()
    await update.message.reply_text("Видео получено. Выполняю распознавание речи...")

    file = await update.message.video.get_file()
    video_path = f"/tmp/video_{user.id}_{int(now.timestamp())}.mp4"
    await file.download_to_drive(video_path)

    speech_text = transcribe_audio(video_path)

    if os.path.exists(video_path):
        os.remove(video_path)

    if speech_text and "Ошибка" not in speech_text:
        result = check_status(speech_text)
    else:
        result = {
            "report_type": "статус",
            "is_ok": False,
            "format_comment": "текст отчёта не распознан",
            "required_action": "попросить сотрудника переснять видео",
        }

    full_name = f"{last_name} {first_name}".strip()
    report_date = now.strftime("%Y-%m-%d")

    if result["report_type"] == "факт_дня":
        header = f"<b>{full_name} ({position})</b> - Ф̲А̲К̲Т̲ за день ({now.strftime('%d.%m')})"
        slot_time = None
        is_late = False
    else:
        header = (
            f"<b>{full_name} ({position})</b> - статус "
            f"{now.strftime('%d.%m')} за {now.strftime('%H:%M')}"
        )
        slot_time, is_late = find_nearest_slot(schedule, now)

    text = (
        f"{header}\n"
        f"Формат отчёта: {result['format_comment']},\n"
        f"Требуемые действия: {result['required_action']}"
    )

    save_report(
        telegram_id=user.id,
        report_date=report_date,
        report_type=result["report_type"],
        slot_time=slot_time,
        received_at=now.strftime("%H:%M"),
        is_ok=result["is_ok"],
        is_late=is_late,
        format_comment=result["format_comment"],
        required_action=result["required_action"],
    )

    try:
        await context.bot.send_video(
            chat_id=group_id, video=update.message.video.file_id
        )
        await context.bot.send_message(chat_id=group_id, text=text, parse_mode="HTML")
        await update.message.reply_text("Статус успешно отправлен в группу.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при отправке в группу: {e}")


# ==========================
# ДОБАВЛЕНИЕ СОТРУДНИКА (диалог, только для админа)
# ==========================

async def add_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return ConversationHandler.END

    target_id = None
    if update.message.reply_to_message:
        pending = context.bot_data.get("pending_workers", {})
        target_id = pending.get(update.effective_user.id)

    if target_id is None:
        await update.message.reply_text(
            "Не нашёл, кого добавлять. Ответьте (Reply) этой командой на "
            "сообщение-уведомление о новом сотруднике, либо используйте "
            "/add_worker_id <telegram_id>"
        )
        return ConversationHandler.END

    context.user_data["new_worker_id"] = target_id
    await update.message.reply_text(
        f"Добавляю сотрудника (ID: {target_id}).\nВведите фамилию:"
    )
    return ASK_LASTNAME


async def add_worker_by_id_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return ConversationHandler.END

    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Использование: /add_worker_id <telegram_id>\n"
            "Например: /add_worker_id 123456789"
        )
        return ConversationHandler.END

    target_id = int(args[0])
    context.user_data["new_worker_id"] = target_id
    await update.message.reply_text(
        f"Добавляю сотрудника (ID: {target_id}).\nВведите фамилию:"
    )
    return ASK_LASTNAME


async def add_worker_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["last_name"] = update.message.text.strip()
    await update.message.reply_text("Введите имя:")
    return ASK_FIRSTNAME


async def add_worker_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["first_name"] = update.message.text.strip()
    await update.message.reply_text("Введите должность/отдел (например: Строитель, Охранник):")
    return ASK_POSITION


async def add_worker_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text.strip()
    await update.message.reply_text(
        "Введите ID группы Telegram, куда слать отчёты этого сотрудника.\n"
        f"Если не знаете — отправьте 0, будет использована группа по умолчанию ({DEFAULT_GROUP_ID})."
    )
    return ASK_GROUP


async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    try:
        group_id = int(raw)
    except ValueError:
        await update.message.reply_text(
            "Нужно число. Введите ID группы ещё раз, либо 0 для группы по умолчанию:"
        )
        return ASK_GROUP

    if group_id == 0:
        group_id = DEFAULT_GROUP_ID

    context.user_data["group_id"] = group_id

    await update.message.reply_text(
        "Выберите расписание статусов сотрудника:\n"
        "Отправьте A — статусы в 10:00, 12:00, 15:00, 17:00\n"
        "Отправьте B — статусы в 11:00, 13:00, 16:00, 18:00"
    )
    return ASK_SCHEDULE


async def add_worker_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        await update.message.reply_text("Введите A или B:")
        return ASK_SCHEDULE

    context.user_data["schedule"] = raw

    await update.message.reply_text(
        "Нужно ли этому сотруднику присылать ФАКТ за день (итог в конце дня)?\n"
        "Отправьте: да / нет"
    )
    return ASK_NEEDS_DAILY_FACT


async def add_worker_needs_daily_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        await update.message.reply_text("Ответьте словом 'да' или 'нет':")
        return ASK_NEEDS_DAILY_FACT

    needs_daily_fact = raw == "да"

    worker_id = context.user_data["new_worker_id"]
    last_name = context.user_data["last_name"]
    first_name = context.user_data["first_name"]
    position = context.user_data["position"]
    group_id = context.user_data["group_id"]
    schedule = context.user_data["schedule"]

    upsert_worker(worker_id, last_name, first_name, position, group_id, schedule, needs_daily_fact)

    schedule_str = ", ".join(SCHEDULES[schedule])
    await update.message.reply_text(
        f"Готово! Сотрудник добавлен:\n"
        f"{last_name} {first_name} ({position})\n"
        f"ID: {worker_id}\n"
        f"Группа: {group_id}\n"
        f"Расписание: {schedule_str}\n"
        f"Факт за день: {'да' if needs_daily_fact else 'нет'}"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def add_worker_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Добавление сотрудника отменено.")
    return ConversationHandler.END


# ==========================
# УДАЛЕНИЕ СОТРУДНИКА
# ==========================

async def remove_worker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    args = context.args
    if not args or not args[0].lstrip("-").isdigit():
        await update.message.reply_text(
            "Использование: /remove_worker <telegram_id>\n"
            "ID можно посмотреть в списке /workers"
        )
        return

    target_id = int(args[0])
    worker = get_worker(target_id)

    if worker is None:
        await update.message.reply_text(f"Сотрудник с ID {target_id} не найден.")
        return

    name = f"{worker['last_name']} {worker['first_name']}"
    delete_worker(target_id)
    await update.message.reply_text(f"Сотрудник удалён: {name} (ID {target_id})")


# ==========================
# КОМАНДЫ БОТА
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправьте мне видеоотчёт, и я его обработаю.")


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID чата: {update.effective_chat.id}")


async def list_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    rows = get_all_workers()

    if not rows:
        await update.message.reply_text("База сотрудников пуста.")
        return

    lines = ["Список сотрудников:"]
    for row in rows:
        lines.append(
            f"• {row['last_name']} {row['first_name']} ({row['position']}) "
            f"— ID {row['telegram_id']}, группа {row['group_id']}, "
            f"график {row['schedule']}"
        )
    await update.message.reply_text("\n".join(lines))


async def department_workers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    args = context.args
    if not args:
        # Показываем список доступных отделов
        rows = get_all_workers()
        positions = sorted({row["position"] for row in rows})
        if not positions:
            await update.message.reply_text("База сотрудников пуста.")
            return
        positions_str = "\n".join(f"• {p}" for p in positions)
        await update.message.reply_text(
            f"Использование: /department <название отдела>\n\nДоступные отделы:\n{positions_str}"
        )
        return

    position = " ".join(args)
    rows = get_workers_by_position(position)

    if not rows:
        await update.message.reply_text(f"В отделе «{position}» сотрудников не найдено.")
        return

    lines = [f"Сотрудники отдела «{position}»:"]
    for row in rows:
        lines.append(f"• {row['last_name']} {row['first_name']} — ID {row['telegram_id']}")
    await update.message.reply_text("\n".join(lines))


async def set_report_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Использование: /set_report_time ЧЧ:ММ\nНапример: /set_report_time 19:00"
        )
        return

    raw = args[0]
    try:
        h, m = map(int, raw.split(":"))
        report_time = dtime(hour=h, minute=m)
    except (ValueError, IndexError):
        await update.message.reply_text("Неверный формат. Используйте ЧЧ:ММ, например 19:00")
        return

    chat_id = update.effective_chat.id

    # Удаляем старое задание, если было
    current_jobs = context.application.bot_data.get("summary_job_chat_id")
    job_queue = context.application.job_queue
    for job in job_queue.get_jobs_by_name("daily_summary"):
        job.schedule_removal()

    job_queue.run_daily(
        send_daily_summary,
        time=report_time,
        chat_id=chat_id,
        name="daily_summary",
    )
    context.application.bot_data["summary_job_chat_id"] = chat_id
    context.application.bot_data["summary_job_time"] = raw

    await update.message.reply_text(
        f"Ежедневный сводный отчёт будет приходить в {raw} в этот чат."
    )


# ==========================
# ЕЖЕДНЕВНЫЙ СВОДНЫЙ ОТЧЁТ
# ==========================

async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    today = datetime.now().strftime("%Y-%m-%d")

    all_workers = get_all_workers()
    reports_today = get_reports_for_date(today)

    reports_by_worker = {}
    for r in reports_today:
        reports_by_worker.setdefault(r["telegram_id"], []).append(r)

    total_ok = sum(1 for r in reports_today if r["is_ok"])
    total_remarks = sum(1 for r in reports_today if not r["is_ok"])
    total_late = sum(1 for r in reports_today if r["is_late"])

    not_sent_workers = []
    for w in all_workers:
        worker_reports = reports_by_worker.get(w["telegram_id"], [])
        if not worker_reports:
            not_sent_workers.append(f"{w['last_name']} {w['first_name']} ({w['position']})")

    lines = [
        f"📊 Сводный отчёт за {datetime.now().strftime('%d.%m.%Y')}",
        "",
        f"Всего сотрудников: {len(all_workers)}",
        f"✅ Отчётов без замечаний: {total_ok}",
        f"⚠️ Отчётов с замечаниями: {total_remarks}",
        f"🕐 Отчётов с опозданием: {total_late}",
        f"❌ Не прислали ни одного отчёта: {len(not_sent_workers)}",
    ]

    if not_sent_workers:
        lines.append("")
        lines.append("Не прислали отчёт:")
        for name in not_sent_workers:
            lines.append(f"• {name}")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


async def summary_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной вызов сводного отчёта прямо сейчас, для теста."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    class FakeJob:
        chat_id = update.effective_chat.id

    class FakeContext:
        job = FakeJob()
        bot = context.bot

    await send_daily_summary(FakeContext())


# ==========================
# ЗАПУСК БОТА
# ==========================

def main():
    if not TOKEN:
        raise ValueError("Не задана переменная окружения TELEGRAM_TOKEN")
    if not os.environ.get("GROQ_API_KEY"):
        raise ValueError("Не задана переменная окружения GROQ_API_KEY")
    if not ADMIN_ID:
        print("ПРЕДУПРЕЖДЕНИЕ: не задан ADMIN_ID, команды администратора будут недоступны")

    app = Application.builder().token(TOKEN).build()

    add_worker_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add_worker", add_worker_start),
            CommandHandler("add_worker_id", add_worker_by_id_start),
        ],
        states={
            ASK_LASTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_lastname)],
            ASK_FIRSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_firstname)],
            ASK_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_position)],
            ASK_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_group)],
            ASK_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_schedule)],
            ASK_NEEDS_DAILY_FACT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_needs_daily_fact)
            ],
        },
        fallbacks=[CommandHandler("cancel", add_worker_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))
    app.add_handler(CommandHandler("workers", list_workers))
    app.add_handler(CommandHandler("remove_worker", remove_worker_cmd))
    app.add_handler(CommandHandler("department", department_workers_cmd))
    app.add_handler(CommandHandler("set_report_time", set_report_time_cmd))
    app.add_handler(CommandHandler("summary_now", summary_now_cmd))
    app.add_handler(add_worker_conv)
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))

    print("Бот успешно запущен и готов к работе...")
    app.run_polling()


if __name__ == "__main__":
    main()
    
