import os
import re
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

def check_status(text: str) -> str:
    prompt = f"""Проверь строительный статус.
Если в тексте отчета нет конкретных объемов работы (цифр, килограммов, литров, штук, метров), то верни замечание.

Текст отчета:
{text}

Верни короткий ответ: ОК или конкретное замечание."""

    try:
        response = groq_client.chat.completions.create(
            model="llama3-8b-8192",
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
    # ПРОВЕРКА ОТЧЁТА (Регулярные выражения)
    # ==========================
    report_lower = speech_text.lower()
    remarks = []

    work_words = [
        "делал", "сделал", "установил", "смонтировал", "проверил",
        "убрал", "залил", "проложил", "варил", "собрал",
        "ремонтировал", "работал", "затирки", "раствор",
    ]

    if not any(word in report_lower for word in work_words):
        remarks.append("Не указана выполненная работа")

    numbers = re.findall(r"\d+", speech_text)
    if not numbers:
        remarks.append("Не указан объём работы")

    if len(remarks) == 0:
        check = "Формат отчёта: всё ОК\nТребуемые действия: ничего не предпринимать"
    else:
        check = "Замечания:\n- " + "\n- ".join(remarks)

    # ==========================
    # ПРОВЕРКА НЕЙРОСЕТЬЮ (Groq LLM)
    # ==========================
    if speech_text and "Ошибка" not in speech_text:
        ai_verdict = check_status(speech_text)
        check += f"\n\nВердикт ИИ: {ai_verdict}"

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
