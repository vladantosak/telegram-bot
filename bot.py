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
    MessageHandler,
    filters,
)

# ==========================
# GROQ КЛИЕНТ
# ==========================

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))


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

CHECK_PROMPT_TEMPLATE = """Ты — бригадир на стройке, который проверяет голосовые отчёты рабочих за последние 2 часа.

Рабочие — простые строители. Они говорят разговорным языком, путают слова, строят фразы криво, иногда повторяются или перескакивают с одного на другое. Текст может содержать ошибки распознавания речи (отдельные слова искажены или пропущены). Твоя задача — игнорировать форму и понять суть: что именно человек делал последние 2 часа.

ЗАДАЧА:
Определи, указал ли сотрудник конкретный измеримый объём выполненной работы.

Конкретный объём — это любое из:
- количество готовых единиц («покрасил 2 бокса», «установил 3 двери», «заварил 5 швов»)
- количество использованного материала («ушло 5 мешков раствора», «постелил 10 метров кабеля»)
- площадь, длина, вес, объём в любых единицах («покрасил 20 квадратов», «залил 2 куба бетона»)
- доля от объекта, если она названа конкретно («покрасил половину забора», «доделал треть стены») — это тоже считается конкретным, если ясно ОТ ЧЕГО доля

НЕ считается конкретным объёмом:
- «много», «почти всё», «прилично», «нормально», «целый день» — без указания, чего именно и сколько
- просто перечисление действий без результата («красил, убирал, таскал») без итога в цифрах или ясных единицах
- общие фразы без сути («работал», «занимался делами», «всё сделал»)

ЧТО ДЕЛАТЬ:
1. Мысленно восстанови, что сотрудник реально делал, даже если фраза построена странно или содержит лишние слова/повторы.
2. Найди в восстановленном смысле измеримый результат по критериям выше.
3. Не придумывай и не дополняй то, чего нет в тексте — если объём не назван, значит его нет, даже если работа сама по себе понятна.

ФОРМАТ ОТВЕТА (строго):
Если объём указан:
ОК: [кратко, в одно предложение, что сделано и какой объём]

Если объём не указан:
ЗАМЕЧАНИЕ: не указан конкретный объём работы. Уточни у сотрудника, сколько именно сделано (в штуках/метрах/кг/процентах и т.п.)

Не добавляй ничего, кроме одной из этих двух строк. Никаких пояснений сверху, никакого "вот мой анализ".

Текст отчёта сотрудника:
{text}
"""


def check_status(text: str) -> str:
    prompt = CHECK_PROMPT_TEMPLATE.format(text=text)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Ошибка проверки ИИ: {e}"


# ==========================
# ТОКЕН БОТА (из переменной окружения)
# ==========================

TOKEN = os.environ.get("TELEGRAM_TOKEN")

# ==========================
# БАЗА ДАННЫХ (SQLite)
# ==========================
# DB_PATH можно переопределить переменной окружения, чтобы хранить файл
# на постоянном Volume (например /data/workers.db на Railway).
DB_PATH = os.environ.get("DB_PATH", "workers.db")


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
            name TEXT NOT NULL,
            position TEXT NOT NULL DEFAULT 'Не указано',
            department TEXT NOT NULL DEFAULT 'Не указано',
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


def add_worker(telegram_id: int, name: str, position: str, department: str, group_id: int):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers (telegram_id, name, position, department, group_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO NOTHING
        """,
        (telegram_id, name, position, department, group_id),
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
# ОБРАБОТКА ВИДЕО
# ==========================

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.video:
        return

    user = update.effective_user
    print(f"Получено видео от {user.full_name}")

    worker = get_worker(user.id)
    default_group_id = int(os.environ.get("GROUP_ID", "-1003804380536"))

    if worker is None:
        name = user.full_name
        position = "Не указано"
        department = "Не указано"
        group_id = default_group_id
        add_worker(user.id, name, position, department, group_id)
        print(f"Добавлен новый сотрудник: {name}")
    else:
        name = worker["name"]
        position = worker["position"]
        department = worker["department"]
        group_id = int(worker["group_id"])

    now = datetime.now()
    await update.message.reply_text("Видео получено. Выполняю распознавание речи...")

    # Скачиваем видео
    file = await update.message.video.get_file()
    video_path = f"/tmp/video_{user.id}_{int(now.timestamp())}.mp4"
    await file.download_to_drive(video_path)

    # ==========================
    # ТРАНСКРИПЦИЯ (Groq Whisper)
    # ==========================
    speech_text = transcribe_audio(video_path)

    # Удаляем временный файл
    if os.path.exists(video_path):
        os.remove(video_path)

    # ==========================
    # ПРОВЕРКА НЕЙРОСЕТЬЮ (Groq LLM)
    # ==========================
    if speech_text and "Ошибка" not in speech_text:
        check = check_status(speech_text)
    else:
        check = "Проверка не выполнена: текст отчёта не распознан"

    # ==========================
    # ТЕКСТ ДЛЯ ГРУППЫ
    # ==========================
    text = (
        f"<b>{name} ({position})</b> - статус "
        f"{now.strftime('%d.%m')} "
        f"за {now.strftime('%H:%M')}\n\n"
        f"<b>Отчёт сотрудника:</b>\n"
        f"{speech_text}\n\n"
        f"<b>Проверка:</b>\n"
        f"{check}\n\n"
        f"<b>Отдел:</b> {department}"
    )

    # ==========================
    # ОТПРАВКА В ГРУППУ
    # ==========================
    try:
        await context.bot.send_video(
            chat_id=group_id, video=update.message.video.file_id
        )
        await context.bot.send_message(chat_id=group_id, text=text, parse_mode="HTML")
        await update.message.reply_text("Статус успешно отправлен в группу.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при отправке в группу: {e}")


# ==========================
# КОМАНДЫ БОТА
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправьте мне видеоотчёт, и я его обработаю.")


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID чата: {update.effective_chat.id}")


# ==========================
# ЗАПУСК БОТА
# ==========================

def main():
    if not TOKEN:
        raise ValueError("Не задана переменная окружения TELEGRAM_TOKEN")
    if not os.environ.get("GROQ_API_KEY"):
        raise ValueError("Не задана переменная окружения GROQ_API_KEY")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_chat_id))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))

    print("Бот успешно запущен и готов к работе...")
    app.run_polling()


if __name__ == "__main__":
    main()
