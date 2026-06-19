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
    ASK_REMOVE_ID,
    ASK_DEPARTMENT,
    ASK_REPORT_TIME,
) = range(10)


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
