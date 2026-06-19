import os
import sqlite3
import traceback
from datetime import datetime
from groq import Groq
from telegram import Update
from telegram.ext import (
    Application,
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

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# Состояния диалога добавления сотрудника
ASK_LASTNAME, ASK_FIRSTNAME, ASK_POSITION, ASK_GROUP = range(4)


# ==========================
# БАЗА ДАННЫХ (SQLite)
# ==========================

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
            group_id INTEGER NOT NULL
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


def upsert_worker(telegram_id: int, last_name: str, first_name: str, position: str, group_id: int):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers (telegram_id, last_name, first_name, position, group_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            last_name=excluded.last_name,
            first_name=excluded.first_name,
            position=excluded.position,
            group_id=excluded.group_id
        """,
        (telegram_id, last_name, first_name, position, group_id),
    )
    conn.commit()
    conn.close()


def count_workers() -> int:
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
    conn.close()
    return n


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

CHECK_PROMPT_TEMPLATE = """Ты — бригадир на стройке, который проверяет голосовые видеоотчёты рабочих.

Рабочие — простые строители разных специальностей (маляры, охранники, экскаваторщики, водители и т.д.). Они говорят разговорным языком, путают слова, строят фразы криво, иногда повторяются или перескакивают с одного на другое. Текст может содержать ошибки распознавания речи (отдельные слова искажены или пропущены). Твоя задача — игнорировать форму и понять суть: что именно человек делал.

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

ШАГ 3 — РЕШИ, ЕСТЬ ЛИ ЗАМЕЧАНИЯ:
Замечание нужно, если:
- отчёт не даёт понять, что конкретно делал сотрудник (расплывчато: "работал", "занимался делами", "всё нормально")
- для работы, где естественно ожидать измеримый результат (покраска, кладка, монтаж, заливка и т.п.), результат не назван вообще ни в каком виде (ни в штуках, ни в долях, ни в метрах)
- сотрудник явно недоговаривает или текст подозрительно пустой

Замечание НЕ нужно, если:
- объём назван в любой разумной форме (штуки, метры, доли, время работы, количество рейсов и т.п.)
- характер работы такой, что конкретные "объёмы" по своей природе не применимы (например, охрана, ожидание, дежурство), и сотрудник просто подтвердил факт выполнения и доложил по существу

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
    import json

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
# ПРОВЕРКА ПРАВ АДМИНА
# ==========================

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


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
        # Неизвестный сотрудник — уведомляем админа, видео не отправляем в группу
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
                sent = await context.bot.send_video(
                    chat_id=ADMIN_ID, video=update.message.video.file_id
                )
                await context.bot.send_message(chat_id=ADMIN_ID, text=notify_text)
                # Сохраняем pending telegram_id для последующей привязки через /add_worker
                context.bot_data.setdefault("pending_workers", {})[ADMIN_ID] = user.id
            except Exception as e:
                print(f"Не удалось уведомить админа: {e}")
        return

    last_name = worker["last_name"]
    first_name = worker["first_name"]
    position = worker["position"]
    group_id = int(worker["group_id"])

    now = datetime.now()
    await update.message.reply_text("Видео получено. Выполняю распознавание речи...")

    # Скачиваем видео
    file = await update.message.video.get_file()
    video_path = f"/tmp/video_{user.id}_{int(now.timestamp())}.mp4"
    await file.download_to_drive(video_path)

    # ТРАНСКРИПЦИЯ
    speech_text = transcribe_audio(video_path)

    if os.path.exists(video_path):
        os.remove(video_path)

    # ПРОВЕРКА НЕЙРОСЕТЬЮ
    if speech_text and "Ошибка" not in speech_text:
        result = check_status(speech_text)
    else:
        result = {
            "report_type": "статус",
            "is_ok": False,
            "format_comment": "текст отчёта не распознан",
            "required_action": "попросить сотрудника переснять видео",
        }

    # ЗАГОЛОВОК В ЗАВИСИМОСТИ ОТ ТИПА ОТЧЁТА
    full_name = f"{last_name} {first_name}".strip()
    if result["report_type"] == "факт_дня":
        header = f"<b>{full_name} ({position})</b> - Ф̲А̲К̲Т̲ за день ({now.strftime('%d.%m')})"
    else:
        header = (
            f"<b>{full_name} ({position})</b> - статус "
            f"{now.strftime('%d.%m')} за {now.strftime('%H:%M')}"
        )

    text = (
        f"{header}\n"
        f"Формат отчёта: {result['format_comment']},\n"
        f"Требуемые действия: {result['required_action']}"
    )

    # ОТПРАВКА В ГРУППУ
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
    # Если команда вызвана как Reply на уведомление о неизвестном сотруднике
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
        await update.message.reply_text("Нужно число. Введите ID группы ещё раз, либо 0 для группы по умолчанию:")
        return ASK_GROUP

    if group_id == 0:
        group_id = DEFAULT_GROUP_ID

    worker_id = context.user_data["new_worker_id"]
    last_name = context.user_data["last_name"]
    first_name = context.user_data["first_name"]
    position = context.user_data["position"]

    upsert_worker(worker_id, last_name, first_name, position, group_id)

    await update.message.reply_text(
        f"Готово! Сотрудник добавлен:\n"
        f"{last_name} {first_name} ({position})\n"
        f"ID: {worker_id}\n"
        f"Группа: {group_id}"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def add_worker_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Добавление сотрудника отменено.")
    return ConversationHandler.END


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

    conn = get_db()
    rows = conn.execute("SELECT * FROM workers ORDER BY last_name").fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("База сотрудников пуста.")
        return

    lines = ["Список сотрудников:"]
    for row in rows:
        lines.append(
            f"• {row['last_name']} {row['first_name']} ({row['position']}) "
            f"— ID {row['telegram_id']}, группа {row['group_id']}"
        )
    await update.message.reply_text("\n".join(lines))


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
        },
        fallbacks=[CommandHandler("cancel", add_worker_cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))
    app.add_handler(CommandHandler("workers", list_workers))
    app.add_handler(add_worker_conv)
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))

    print("Бот успешно запущен и готов к работе...")
    app.run_polling()


if __name__ == "__main__":
    main()
