import os
import re
import sqlite3
import json
import hashlib
import itertools
from datetime import datetime
import datetime as dt_module
from zoneinfo import ZoneInfo
from openpyxl import load_workbook

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None

DB_PATH = os.environ.get("DB_PATH", "workers.db")
DEFAULT_GROUP_ID = int(os.environ.get("GROUP_ID", "-1003804380536"))
LOCAL_TZ = ZoneInfo("Europe/Chisinau")
LATE_THRESHOLD_MIN = 15

# Read ADMIN_IDS from environment variables
ADMIN_IDS_RAW = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = []
if ADMIN_IDS_RAW:
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.replace("-", "").isdigit():
            ADMIN_IDS.append(int(x))

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

SCHEDULE_A = ["10:00", "12:00", "15:00", "17:00"]
SCHEDULE_B = ["11:00", "13:00", "16:00", "18:00"]
SCHEDULES = {"A": SCHEDULE_A, "B": SCHEDULE_B}

def clean_position(position: str | None) -> str:
    """Strip a trailing '(...)' suffix some positions were saved with, e.g.
    'Сварщик (Industriala_Welders_Reports)' -> 'Сварщик' — the department
    is already shown separately, it shouldn't be duplicated inside position."""
    text = (position or "Не указано").strip()
    return re.sub(r"\s*\([^()]*\)\s*$", "", text).strip() or "Не указано"

ENCRYPTED_SETTING_KEYS = {"google_service_account"}
_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.environ.get("SETTINGS_ENCRYPTION_KEY")
    if not key:
        _fernet = False
        return _fernet
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode())
    except Exception:
        _fernet = False
    return _fernet

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return conn

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

async def run_db(func, *args, **kwargs):
    import asyncio
    return await asyncio.to_thread(func, *args, **kwargs)

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
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            object_id TEXT NOT NULL DEFAULT 'Основной'
        )
        """
    )
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    for col, definition in [
        ("schedule", "TEXT NOT NULL DEFAULT 'A'"),
        ("needs_daily_fact", "INTEGER NOT NULL DEFAULT 1"),
        ("sort_order", "INTEGER NOT NULL DEFAULT 0"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("object_id", "TEXT NOT NULL DEFAULT 'Основной'"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE workers ADD COLUMN {col} {definition}")

    conn.execute("CREATE TABLE IF NOT EXISTS objects (object_id TEXT PRIMARY KEY, group_id INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS groups (group_id INTEGER PRIMARY KEY, group_name TEXT NOT NULL)")
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
            raw_text TEXT NOT NULL DEFAULT '',
            group_chat_id INTEGER,
            group_message_id INTEGER
        )
        """
    )
    cols_reports = {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}
    if "raw_text" not in cols_reports:
        conn.execute("ALTER TABLE reports ADD COLUMN raw_text TEXT NOT NULL DEFAULT ''")
    if "group_chat_id" not in cols_reports:
        conn.execute("ALTER TABLE reports ADD COLUMN group_chat_id INTEGER")
    if "group_message_id" not in cols_reports:
        conn.execute("ALTER TABLE reports ADD COLUMN group_message_id INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            group_message_id INTEGER,
            position INTEGER NOT NULL,
            added_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_report_media_report ON report_media(report_id)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_reminders (
            telegram_id INTEGER,
            report_date TEXT,
            slot_time TEXT,
            PRIMARY KEY (telegram_id, report_date, slot_time)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_pre_reminders (
            telegram_id INTEGER,
            report_date TEXT,
            slot_time TEXT,
            PRIMARY KEY (telegram_id, report_date, slot_time)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_unregistered_users (
            telegram_id INTEGER PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            username TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            text_content TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_worker_date ON reports(telegram_id, report_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_workers_pos ON workers(position)")

    conn.commit()
    conn.close()

def get_worker(telegram_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row

def get_all_workers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM workers ORDER BY object_id, sort_order, last_name, first_name").fetchall()
    conn.close()
    return rows

def get_workers_by_position(position: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers WHERE lower(position) = lower(?) ORDER BY object_id, sort_order, last_name, first_name",
        (position,),
    ).fetchall()
    conn.close()
    return rows

def get_workers_by_object_id(object_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers WHERE lower(object_id) = lower(?) ORDER BY sort_order, last_name, first_name",
        (object_id,),
    ).fetchall()
    conn.close()
    return rows

def find_unregistered_workers_by_lastname(last_name: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM workers WHERE telegram_id < 0 AND LOWER(last_name) = LOWER(?)",
        (last_name.strip(),)
    ).fetchall()
    conn.close()
    return rows

def bind_worker_id(old_id: int, new_id: int):
    conn = get_db()
    try:
        conn.execute("UPDATE workers SET telegram_id = ? WHERE telegram_id = ?", (new_id, old_id))
        conn.execute("UPDATE reports SET telegram_id = ? WHERE telegram_id = ?", (new_id, old_id))
        conn.execute("UPDATE sent_reminders SET telegram_id = ? WHERE telegram_id = ?", (new_id, old_id))
        conn.execute("UPDATE sent_pre_reminders SET telegram_id = ? WHERE telegram_id = ?", (new_id, old_id))
        conn.execute("UPDATE pending_unregistered_users SET telegram_id = ? WHERE telegram_id = ?", (new_id, old_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def save_pending_unregistered_user(telegram_id: int, first_name: str, last_name: str, username: str, timestamp: str, text_content: str):
    conn = get_db()
    conn.execute(
        """
        INSERT OR REPLACE INTO pending_unregistered_users (telegram_id, first_name, last_name, username, timestamp, text_content)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, first_name, last_name, username, timestamp, text_content)
    )
    conn.commit()
    conn.close()

def get_pending_unregistered_user(telegram_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM pending_unregistered_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row

def delete_pending_unregistered_user(telegram_id: int):
    conn = get_db()
    conn.execute("DELETE FROM pending_unregistered_users WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def upsert_worker(telegram_id: int, last_name: str, first_name: str, position: str, group_id: int, schedule: str, needs_daily_fact: bool, sort_order: int = 0, is_active: int = 1, object_id: str = 'Основной'):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO workers (telegram_id, last_name, first_name, position, group_id, schedule, needs_daily_fact, sort_order, is_active, object_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            last_name=excluded.last_name,
            first_name=excluded.first_name,
            position=excluded.position,
            group_id=excluded.group_id,
            schedule=excluded.schedule,
            needs_daily_fact=excluded.needs_daily_fact,
            sort_order=excluded.sort_order,
            is_active=excluded.is_active,
            object_id=excluded.object_id
        """,
        (telegram_id, last_name, first_name, position, group_id, schedule, int(needs_daily_fact), sort_order, is_active, object_id),
    )
    conn.commit()
    conn.close()

def get_object_group(object_id: str) -> int:
    conn = get_db()
    row = conn.execute("SELECT group_id FROM objects WHERE object_id = ?", (object_id,)).fetchone()
    conn.close()
    return row["group_id"] if row else 0

def save_object_group(object_id: str, group_id: int):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO objects (object_id, group_id) VALUES (?, ?)
        ON CONFLICT(object_id) DO UPDATE SET group_id=excluded.group_id
        """,
        (object_id, group_id)
    )
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = None) -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if not row:
        return default
    value = row["value"]
    if key in ENCRYPTED_SETTING_KEYS:
        fernet = _get_fernet()
        if fernet:
            try:
                return fernet.decrypt(value.encode()).decode()
            except Exception:
                return value
    return value

def set_setting(key: str, value: str):
    stored_value = value
    if key in ENCRYPTED_SETTING_KEYS:
        fernet = _get_fernet()
        if fernet:
            stored_value = fernet.encrypt(value.encode()).decode()
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, stored_value)
    )
    conn.commit()
    conn.close()

def delete_worker(telegram_id: int) -> bool:
    conn = get_db()
    try:
        conn.execute("DELETE FROM reports WHERE telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM sent_reminders WHERE telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM sent_pre_reminders WHERE telegram_id = ?", (telegram_id,))
        conn.execute("DELETE FROM pending_unregistered_users WHERE telegram_id = ?", (telegram_id,))
        cur = conn.execute("DELETE FROM workers WHERE telegram_id = ?", (telegram_id,))
        conn.commit()
        deleted = cur.rowcount > 0
    except Exception:
        conn.rollback()
        deleted = False
    finally:
        conn.close()
    return deleted

def save_report(telegram_id: int, report_date: str, report_type: str, slot_time: str | None, received_at: str, is_ok: bool, is_late: bool, format_comment: str, required_action: str, raw_text: str = "") -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reports (telegram_id, report_date, report_type, slot_time, received_at, is_ok, is_late, format_comment, required_action, raw_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, report_date, report_type, slot_time, received_at, int(is_ok), int(is_late), format_comment, required_action, raw_text),
    )
    conn.commit()
    inserted_id = cur.lastrowid
    conn.close()
    return inserted_id

def update_report_text_and_ai(report_id: int, is_ok: bool, format_comment: str, required_action: str, raw_text: str, received_at: str):
    conn = get_db()
    conn.execute(
        """
        UPDATE reports
        SET is_ok = ?, format_comment = ?, required_action = ?, raw_text = ?, received_at = ?
        WHERE id = ?
        """,
        (int(is_ok), format_comment, required_action, raw_text, received_at, report_id)
    )
    conn.commit()
    conn.close()

def set_report_group_message(report_id: int, chat_id: int, message_id: int):
    conn = get_db()
    conn.execute(
        "UPDATE reports SET group_chat_id = ?, group_message_id = ? WHERE id = ?",
        (chat_id, message_id, report_id)
    )
    conn.commit()
    conn.close()

def get_report_group_message(report_id: int):
    conn = get_db()
    row = conn.execute("SELECT group_chat_id, group_message_id FROM reports WHERE id = ?", (report_id,)).fetchone()
    conn.close()
    return row

def get_submitted_status_slots(telegram_id: int, report_date: str) -> set:
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT slot_time FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'status'",
        (telegram_id, report_date)
    ).fetchall()
    conn.close()
    return {r["slot_time"] for r in rows}

def has_pre_reminder_sent(telegram_id: int, report_date: str, slot_time: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM sent_pre_reminders WHERE telegram_id = ? AND report_date = ? AND slot_time = ?",
        (telegram_id, report_date, slot_time)
    ).fetchone()
    conn.close()
    return row is not None

def mark_pre_reminder_sent(telegram_id: int, report_date: str, slot_time: str):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO sent_pre_reminders (telegram_id, report_date, slot_time) VALUES (?, ?, ?)",
        (telegram_id, report_date, slot_time)
    )
    conn.commit()
    conn.close()

def get_existing_report_row(telegram_id: int, report_date: str, report_type: str, slot_time: str | None = None):
    conn = get_db()
    if report_type == "status":
        row = conn.execute(
            "SELECT * FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'status' AND slot_time = ?",
            (telegram_id, report_date, slot_time)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'daily_fact'",
            (telegram_id, report_date)
        ).fetchone()
    conn.close()
    return row

def add_report_media(report_id: int, source_chat_id: int, source_message_id: int, group_message_id: int | None, position: int, added_at: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO report_media (report_id, source_chat_id, source_message_id, group_message_id, position, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (report_id, source_chat_id, source_message_id, group_message_id, position, added_at)
    )
    conn.commit()
    conn.close()

def get_report_media(report_id: int):
    conn = get_db()
    rows = conn.execute("SELECT * FROM report_media WHERE report_id = ? ORDER BY position", (report_id,)).fetchall()
    conn.close()
    return rows

def delete_report_media_rows(report_id: int):
    conn = get_db()
    conn.execute("DELETE FROM report_media WHERE report_id = ?", (report_id,))
    conn.commit()
    conn.close()

def cancel_not_working(telegram_id: int, report_date: str):
    conn = get_db()
    conn.execute("DELETE FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'not_working'", (telegram_id, report_date))
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

def calculate_worker_stats(worker, start_date_str: str, end_date_str: str, conn) -> dict:
    join_row = conn.execute("SELECT MIN(report_date) as start_date FROM reports WHERE telegram_id = ?", (worker["telegram_id"],)).fetchone()
    adjusted_start_date_str = start_date_str
    if join_row and join_row["start_date"] and join_row["start_date"] > start_date_str:
        adjusted_start_date_str = join_row["start_date"]

    reports = conn.execute(
        "SELECT * FROM reports WHERE telegram_id = ? AND report_date >= ? AND report_date <= ?",
        (worker["telegram_id"], adjusted_start_date_str, end_date_str)
    ).fetchall()

    not_working_dates = set()
    submitted_statuses = set()
    submitted_facts = set()
    total_lates = 0
    total_remarks = 0

    for r in reports:
        rep_date = r["report_date"]
        rep_type = r["report_type"]
        if rep_type == "not_working":
            not_working_dates.add(rep_date)
        elif rep_type == "status":
            submitted_statuses.add((rep_date, r["slot_time"]))
            if r["is_late"]: total_lates += 1
            if not r["is_ok"]: total_remarks += 1
        elif rep_type == "daily_fact":
            submitted_facts.add(rep_date)
            if r["is_late"]: total_lates += 1
            if not r["is_ok"]: total_remarks += 1

    start_dt = datetime.strptime(adjusted_start_date_str, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    expected_status_count = 0
    expected_fact_count = 0
    current_date_it = start_dt
    now = now_local()
    now_date_str = now.strftime("%Y-%m-%d")
    current_mins = now.hour * 60 + now.minute

    while current_date_it <= end_dt:
        date_it_str = current_date_it.strftime("%Y-%m-%d")
        if date_it_str not in not_working_dates:
            slots = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
            if date_it_str == now_date_str:
                for slot in slots:
                    hour, minute = map(int, slot.split(":"))
                    if current_mins > hour * 60 + minute + LATE_THRESHOLD_MIN or (date_it_str, slot) in submitted_statuses:
                        expected_status_count += 1
                if worker["needs_daily_fact"]:
                    if date_it_str in submitted_facts:
                        expected_fact_count += 1
                    else:
                        last_slot = slots[-1]
                        hour, minute = map(int, last_slot.split(":"))
                        if current_mins > hour * 60 + minute + 60:
                            expected_fact_count += 1
            else:
                expected_status_count += len(slots)
                if worker["needs_daily_fact"]:
                    expected_fact_count += 1
        current_date_it += dt_module.timedelta(days=1)

    total_expected = expected_status_count + expected_fact_count
    total_submitted = len(submitted_statuses) + len(submitted_facts)
    missed_count = max(0, total_expected - total_submitted)
    percent_submitted = (total_submitted / total_expected * 100.0) if total_expected > 0 else 100.0

    return {
        "expected": total_expected,
        "submitted": total_submitted,
        "lates": total_lates,
        "remarks": total_remarks,
        "percent": percent_submitted,
        "missed": missed_count,
    }

def is_quiet_mode_enabled() -> bool:
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key = 'quiet_mode_enabled'").fetchone()
        conn.close()
        if row:
            return row["value"] == "1"
    except Exception:
        pass
    return False

def set_quiet_mode(enabled: bool):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('quiet_mode_enabled', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("1" if enabled else "0",)
    )
    conn.commit()
    conn.close()

def get_scheduled_times() -> list[str]:
    try:
        val = get_setting("scheduled_summary_times")
        if val:
            return json.loads(val)
    except Exception:
        pass
    return ["19:00"]

def save_scheduled_times(times: list[str]):
    set_setting("scheduled_summary_times", json.dumps(times))

def get_worker_target_group(worker) -> int:
    try:
        w_dict = dict(worker)
    except (TypeError, ValueError):
        return DEFAULT_GROUP_ID
    if "object_id" in w_dict and w_dict["object_id"]:
        obj_group = get_object_group(w_dict["object_id"])
        if obj_group and obj_group != 0:
            return obj_group
    return w_dict.get("group_id") or DEFAULT_GROUP_ID

def get_group_name(group_id: int) -> str:
    conn = get_db()
    row = conn.execute("SELECT group_name FROM groups WHERE group_id = ?", (group_id,)).fetchone()
    conn.close()
    return row["group_name"] if row else str(group_id)

async def get_group_name_async(bot, group_id: int) -> str:
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

async def fetch_and_save_group_name(bot, group_id: int) -> str:
    try:
        chat = await bot.get_chat(group_id)
        name = chat.title or str(group_id)
    except Exception:
        name = str(group_id)
    save_group_name(group_id, name)
    return name

def get_all_group_names() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT group_id, group_name FROM groups").fetchall()
    conn.close()
    res = {row["group_id"]: row["group_name"] for row in rows}
    if DEFAULT_GROUP_ID not in res:
        res[DEFAULT_GROUP_ID] = str(DEFAULT_GROUP_ID)
    return res

def get_next_sort_order(position: str) -> int:
    conn = get_db()
    row = conn.execute("SELECT MAX(sort_order) as max_order FROM workers WHERE lower(position) = lower(?)", (position,)).fetchone()
    conn.close()
    if row and row["max_order"] is not None:
        return row["max_order"] + 1
    return 0

def update_worker_field(telegram_id: int, field_name: str, value):
    conn = get_db()
    allowed_fields = {"last_name", "first_name", "position", "group_id", "schedule", "needs_daily_fact", "sort_order", "is_active", "object_id"}
    if field_name in allowed_fields:
        conn.execute(f"UPDATE workers SET {field_name} = ? WHERE telegram_id = ?", (value, telegram_id))
        conn.commit()
    conn.close()

def get_violators_threshold() -> int:
    try:
        val = get_setting("violators_threshold")
        if val is not None:
            return int(val)
    except Exception:
        pass
    return 3

def save_violators_threshold(val: int):
    set_setting("violators_threshold", str(val))

def generate_and_send_excel(*args, **kwargs):
    pass

def generate_and_send_gsheets(*args, **kwargs):
    pass

def fetch_export_data():
    return get_all_workers()

def export_reports_to_excel() -> bytes:
    import openpyxl
    from openpyxl import Workbook
    import io
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Reports"
    
    headers = [
        "ID отчета", "ID сотрудника", "Фамилия", "Имя", "Должность (Отдел)",
        "Дата отчета", "Тип отчета", "Время слота", "Время получения",
        "Оценка (ОК)", "Опоздание", "Замечания", "Действия", "Оригинальный текст"
    ]
    ws.append(headers)
    
    conn = get_db()
    reports = conn.execute("SELECT * FROM reports ORDER BY report_date DESC, id DESC").fetchall()
    
    # Cache worker profiles
    workers_cache = {}
    
    for r in reports:
        t_id = r["telegram_id"]
        if t_id not in workers_cache:
            w_row = conn.execute("SELECT last_name, first_name, position FROM workers WHERE telegram_id = ?", (t_id,)).fetchone()
            if w_row:
                workers_cache[t_id] = (w_row["last_name"], w_row["first_name"], w_row["position"])
            else:
                workers_cache[t_id] = ("Неизвестно", "Неизвестно", "Неизвестно")
                
        last_name, first_name, position = workers_cache[t_id]
        
        ws.append([
            r["id"],
            r["telegram_id"],
            last_name,
            first_name,
            position,
            r["report_date"],
            r["report_type"],
            r["slot_time"] or "",
            r["received_at"],
            "Да" if r["is_ok"] else "Нет",
            "Да" if r["is_late"] else "Нет",
            r["format_comment"] or "",
            r["required_action"] or "",
            r["raw_text"] or ""
        ])
    
    conn.close()
    
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

def export_workers_to_excel() -> bytes:
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    import io

    wb = Workbook()
    ws = wb.active
    ws.title = "Сотрудники"

    headers = [
        "Telegram ID", "Фамилия", "Имя", "Должность",
        "ID чата (уведомления)", "График (А или Б)", "Итоговый отчет за день",
        "Статус", "Номер в списке", "Объект"
    ]
    hints = [
        "не менять", "фамилия", "имя", "должность",
        "чат id", "А или Б", "Да/Нет",
        "Работает/Отпуск/Больничный", "число", "объект"
    ]
    ws.append(headers)
    ws.append(hints)

    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    hint_fill = PatternFill(start_color="DCE6F1", end_color="DCE6F1", fill_type="solid")
    hint_font = Font(italic=True, color="555555")
    warn_fill = PatternFill(start_color="F8CBAD", end_color="F8CBAD", fill_type="solid")

    for col in range(1, len(headers) + 1):
        c1 = ws.cell(row=1, column=col)
        c1.fill = header_fill
        c1.font = header_font
        c1.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c2 = ws.cell(row=2, column=col)
        c2.fill = hint_fill
        c2.font = hint_font
        c2.alignment = Alignment(horizontal="center")
    ws.cell(row=2, column=1).fill = warn_fill

    ws.freeze_panes = "A3"
    for i, width in enumerate([14, 16, 14, 22, 18, 12, 16, 20, 10, 26], 1):
        ws.column_dimensions[get_column_letter(i)].width = width

    workers = get_all_workers()
    for w in workers:
        ws.append([
            w["telegram_id"],
            w["last_name"],
            w["first_name"],
            w["position"],
            w["group_id"],
            w["schedule"],
            "Да" if w["needs_daily_fact"] else "Нет",
            "Работает" if w["is_active"] else "Отпуск",
            w["sort_order"],
            w["object_id"],
        ])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

EXCEL_HEADER_ALIASES = {
    "telegram id": "telegram_id",
    "фамилия": "last_name",
    "имя": "first_name",
    "должность": "position",
    "должность / отдел": "position",
    "id чата (уведомления)": "group_id",
    "id чата": "group_id",
    "график (а или б)": "schedule",
    "график": "schedule",
    "итоговый отчет за день": "needs_daily_fact",
    "итоговый отчёт за день": "needs_daily_fact",
    "статус": "is_active",
    "номер в списке": "sort_order",
    "объект": "object_id",
}

def read_excel(file_path: str) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    raw_headers = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    headers = [EXCEL_HEADER_ALIASES.get(h.lower(), h) for h in raw_headers]

    data_rows = rows[1:]
    if data_rows and data_rows[0]:
        first_cell = data_rows[0][0]
        if isinstance(first_cell, str) and not first_cell.strip().lstrip("-").isdigit():
            data_rows = data_rows[1:]  # skip the "не менять / фамилия / имя / ..." hint row

    workers = []
    for row in data_rows:
        if not any(row):
            continue
        row_dict = {}
        for i, h in enumerate(headers):
            if i < len(row) and h:
                row_dict[h] = row[i]
        
        if "telegram_id" in row_dict and row_dict["telegram_id"] is not None:
            try:
                row_dict["telegram_id"] = int(row_dict["telegram_id"])
            except ValueError:
                continue
            
            row_dict["last_name"] = str(row_dict.get("last_name", "") or "").strip()
            row_dict["first_name"] = str(row_dict.get("first_name", "") or "").strip()
            row_dict["position"] = str(row_dict.get("position", "Не указано") or "Не указано").strip()
            
            try:
                row_dict["group_id"] = int(row_dict.get("group_id", DEFAULT_GROUP_ID) or DEFAULT_GROUP_ID)
            except ValueError:
                row_dict["group_id"] = DEFAULT_GROUP_ID
                
            row_dict["schedule"] = str(row_dict.get("schedule", "A") or "A").strip().upper()
            if row_dict["schedule"] not in SCHEDULES:
                row_dict["schedule"] = "A"
                
            ndf = row_dict.get("needs_daily_fact")
            if ndf in (True, 1, "1", "Да", "да", "yes", "YES", "True", "true"):
                row_dict["needs_daily_fact"] = True
            else:
                row_dict["needs_daily_fact"] = False
                
            row_dict["object_id"] = str(row_dict.get("object_id", "Основной") or "Основной").strip()
            
            is_act = row_dict.get("is_active")
            if isinstance(is_act, str):
                not_working_labels = {"отпуск", "больничный", "нет", "no", "false", "0", "не работает"}
                row_dict["is_active"] = is_act.strip().lower() not in not_working_labels
            elif is_act in (False, 0):
                row_dict["is_active"] = False
            else:
                row_dict["is_active"] = True
                
            try:
                row_dict["sort_order"] = int(row_dict.get("sort_order", 0) or 0)
            except ValueError:
                row_dict["sort_order"] = 0
                
            workers.append(row_dict)
            
    return workers

def sync_gsheets_task() -> tuple[bool, str | None]:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        return False, f"Отсутствуют необходимые библиотеки Python: {e}"

    spreadsheet_id = get_setting("google_spreadsheet_id")
    creds_str = get_setting("google_service_account")
    if not spreadsheet_id or not creds_str:
        return False, "Настройки Google Таблицы или сервисного аккаунта не заданы."

    try:
        creds_dict = json.loads(creds_str)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        try:
            sheet = client.open_by_key(spreadsheet_id)
        except Exception as ex:
            email = creds_dict.get("client_email", "неизвестный email")
            return False, f"Не удалось открыть таблицу по ID. Убедитесь, что ID правильный и таблица открыта для редактирования (Поделиться) сервисному аккаунту: {email}. Ошибка: {ex}"
        
        # Helper to update worksheet compatibly with older/newer gspread versions
        def safe_update(ws, data):
            ws.clear()
            try:
                ws.update("A1", data)
            except Exception:
                try:
                    ws.update(data, "A1")
                except Exception:
                    ws.update(range_name="A1", values=data)

        # 1. Sync Workers to "Сотрудники"
        workers = get_all_workers()
        headers_workers = [
            "Отдел", "ФИО сотрудника", "Должность", "Telegram ID",
            "ID Группы", "График", "Факт дня", "Активен", "Порядок сортировки"
        ]
        
        rows_workers = [headers_workers]
        for w in workers:
            rows_workers.append([
                str(w["object_id"] or "Основной"),
                f"{w['last_name']} {w['first_name']}",
                clean_position(w["position"]),
                str(w["telegram_id"]),
                str(w["group_id"]),
                str(w["schedule"]),
                "Да" if w["needs_daily_fact"] else "Нет",
                "Да" if w["is_active"] else "Нет",
                str(w["sort_order"])
            ])
            
        try:
            ws_workers = sheet.worksheet("Сотрудники")
        except gspread.exceptions.WorksheetNotFound:
            ws_workers = sheet.add_worksheet(title="Сотрудники", rows="1000", cols="20")
            
        safe_update(ws_workers, rows_workers)
        
        # 2. Sync Reports to "Отчеты"
        conn = get_db()
        reports = conn.execute("""
            SELECT r.*, w.last_name, w.first_name, w.position, w.object_id
            FROM reports r
            LEFT JOIN workers w ON r.telegram_id = w.telegram_id
            ORDER BY r.id DESC
        """).fetchall()
        
        headers_reports = [
            "ID отчета", "Telegram ID", "Отдел", "ФИО сотрудника", "Должность",
            "Дата отчета", "Тип отчета", "Время слота", "Время получения",
            "Оценка (ОК)", "Опоздание", "Замечания", "Действия", "Оригинальный текст"
        ]
        
        rows_reports = [headers_reports]
        for r in reports:
            # Check if unregistered user has pending info
            fio = ""
            if r["last_name"] and r["first_name"]:
                fio = f"{r['last_name']} {r['first_name']}"
            else:
                pending = conn.execute("SELECT first_name, last_name FROM pending_unregistered_users WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
                if pending:
                    fio = f"{pending['last_name']} {pending['first_name']} (Временный)"
                else:
                    fio = "Неизвестный сотрудник"

            rows_reports.append([
                str(r["id"]),
                str(r["telegram_id"]),
                str(r["object_id"] or "Основной"),
                fio,
                clean_position(r["position"]),
                str(r["report_date"]),
                str(r["report_type"]),
                str(r["slot_time"] or ""),
                str(r["received_at"]),
                "Да" if r["is_ok"] else "Нет",
                "Да" if r["is_late"] else "Нет",
                str(r["format_comment"] or ""),
                str(r["required_action"] or ""),
                str(r["raw_text"] or "")
            ])
            
        try:
            ws_reports = sheet.worksheet("Отчеты")
        except gspread.exceptions.WorksheetNotFound:
            ws_reports = sheet.add_worksheet(title="Отчеты", rows="5000", cols="20")
            
        safe_update(ws_reports, rows_reports)

        # 3. Sync Analytics to "Аналитика"
        try:
            ws_analytics = sheet.worksheet("Аналитика")
        except gspread.exceptions.WorksheetNotFound:
            ws_analytics = sheet.add_worksheet(title="Аналитика", rows="1000", cols="20")

        now = now_local()
        end_date_str = now.strftime("%Y-%m-%d")
        
        # Earliest report date fallback
        earliest_row = conn.execute("SELECT MIN(report_date) as min_date FROM reports").fetchone()
        earliest_date_str = (earliest_row["min_date"] if earliest_row and earliest_row["min_date"] else end_date_str) or end_date_str
        
        # Start date for last 30 days
        start_30_days = (now - dt_module.timedelta(days=30)).strftime("%Y-%m-%d")
        if start_30_days < earliest_date_str:
            start_30_days = earliest_date_str
            
        headers_analytics = [
            "Отдел", "ФИО сотрудника", "Должность", 
            "Всего отчетов (за 30 дн)", "Сдано (за 30 дн)", "Опозданий (за 30 дн)", "Замечаний (за 30 дн)", "Пропущено (за 30 дн)", "% Сдачи (за 30 дн)",
            "Всего отчетов (всё время)", "Сдано (всё время)", "Опозданий (всё время)", "Замечаний (всё время)", "Пропущено (всё время)", "% Сдачи (всё время)"
        ]
        
        rows_analytics = [headers_analytics]
        for w in workers:
            if not w["is_active"]:
                continue
            
            stats_30 = calculate_worker_stats(w, start_30_days, end_date_str, conn)
            stats_all = calculate_worker_stats(w, earliest_date_str, end_date_str, conn)
            
            rows_analytics.append([
                str(w["object_id"] or "Основной"),
                f"{w['last_name']} {w['first_name']}",
                clean_position(w["position"]),
                str(stats_30["expected"]),
                str(stats_30["submitted"]),
                str(stats_30["lates"]),
                str(stats_30["remarks"]),
                str(stats_30["missed"]),
                f"{stats_30['percent']:.1f}%",
                str(stats_all["expected"]),
                str(stats_all["submitted"]),
                str(stats_all["lates"]),
                str(stats_all["remarks"]),
                str(stats_all["missed"]),
                f"{stats_all['percent']:.1f}%"
            ])
            
        conn.close()
        safe_update(ws_analytics, rows_analytics)

        # 4. Sync Summary checkbox grid to "Сводка"
        try:
            ws_summary = sheet.worksheet("Сводка")
        except gspread.exceptions.WorksheetNotFound:
            ws_summary = sheet.add_worksheet(title="Сводка", rows="2000", cols="60")

        GREY = {"red": 0.85, "green": 0.85, "blue": 0.85}
        YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.6}
        PINK = {"red": 0.96, "green": 0.78, "blue": 0.78}
        DEPT_BG = {"red": 0.85, "green": 0.82, "blue": 0.93}
        LEGEND = [(PINK, "Не сдал"), (YELLOW, "Сдал, но есть замечание"), (GREY, "Выходной, отпуск")]

        FROZEN_ROWS = len(LEGEND) + 1  # legend rows + the "Сотрудник/Время/даты" header row
        FROZEN_COLS = 2

        # Read the currently published grid (values + notes) before wiping it, so manual edits
        # survive the rewrite: a checkbox someone ticked by hand is never flipped back to unchecked,
        # and a manually-added note is carried forward if we don't have a fresh one to replace it.
        old_map = {}
        try:
            meta = sheet.fetch_sheet_metadata({"ranges": ws_summary.title, "includeGridData": "true"})
            old_grid = []
            for s in meta.get("sheets", []):
                if s.get("properties", {}).get("sheetId") == ws_summary.id:
                    data = s.get("data", [])
                    if data:
                        old_grid = data[0].get("rowData", [])
                    break

            if len(old_grid) > FROZEN_ROWS - 1:
                header_vals = old_grid[FROZEN_ROWS - 1].get("values", [])
                old_date_cols = {
                    idx: cell.get("formattedValue")
                    for idx, cell in enumerate(header_vals)
                    if idx >= 2 and cell.get("formattedValue")
                }
                current_name = None
                for row in old_grid[FROZEN_ROWS:]:
                    vals = row.get("values", [])
                    if not vals:
                        continue
                    name_cell_text = vals[0].get("formattedValue") if len(vals) > 0 else None
                    slot_cell_text = vals[1].get("formattedValue") if len(vals) > 1 else None
                    if name_cell_text:
                        current_name = name_cell_text
                    if not slot_cell_text or current_name is None:
                        continue
                    for idx, date_label in old_date_cols.items():
                        if idx >= len(vals):
                            continue
                        cell = vals[idx]
                        old_map[(current_name, slot_cell_text, date_label)] = {
                            "bool": cell.get("effectiveValue", {}).get("boolValue"),
                            "note": cell.get("note")
                        }
        except Exception:
            old_map = {}

        # Clear merges/formatting/checkboxes left over from a previous sync before rebuilding the grid,
        # and pin the freeze pane to a known state (mergeCells rejects ranges that straddle the
        # frozen/non-frozen column boundary, so every merge below must respect FROZEN_COLS).
        sheet.batch_update({"requests": [
            {"unmergeCells": {"range": {"sheetId": ws_summary.id}}},
            {"repeatCell": {"range": {"sheetId": ws_summary.id}, "cell": {"userEnteredFormat": {}}, "fields": "userEnteredFormat"}},
            {"setDataValidation": {"range": {"sheetId": ws_summary.id}}},
            {"updateSheetProperties": {
                "properties": {"sheetId": ws_summary.id, "gridProperties": {"frozenRowCount": FROZEN_ROWS, "frozenColumnCount": FROZEN_COLS}},
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"
            }}
        ]})

        conn_sum = get_db()
        today_date = now.date()
        month_start = today_date.replace(day=1)
        date_list = []
        d_iter = month_start
        while d_iter <= today_date:
            if d_iter.weekday() < 5:
                date_list.append(d_iter)
            d_iter += dt_module.timedelta(days=1)

        headers_summary = ["Сотрудник", "Время"] + [d.strftime("%d.%m") for d in date_list]

        merge_requests = []
        format_requests = []
        note_requests = []
        checkbox_ranges = []

        # Legend rows, matching the reference sheet: a colored swatch in column A, label in column B
        rows_summary = []
        for color, label in LEGEND:
            legend_row_idx = len(rows_summary)
            rows_summary.append(["", label] + [""] * (len(headers_summary) - 2))
            format_requests.append({
                "repeatCell": {
                    "range": {"sheetId": ws_summary.id, "startRowIndex": legend_row_idx, "endRowIndex": legend_row_idx + 1,
                              "startColumnIndex": 0, "endColumnIndex": 1},
                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                    "fields": "userEnteredFormat(backgroundColor)"
                }
            })
        rows_summary.append(headers_summary)
        header_row_idx = len(rows_summary) - 1
        format_requests.append({
            "repeatCell": {
                "range": {"sheetId": ws_summary.id, "startRowIndex": header_row_idx, "endRowIndex": header_row_idx + 1,
                          "startColumnIndex": 0, "endColumnIndex": len(headers_summary)},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "foregroundColor": {"red": 0, "green": 0, "blue": 0}}}},
                "fields": "userEnteredFormat.textFormat(bold,foregroundColor)"
            }
        })

        for dept, dept_workers in itertools.groupby(
            (w for w in workers if w["is_active"]),
            key=lambda w: str(w["object_id"] or "Основной")
        ):
            dept_row_idx = len(rows_summary)
            rows_summary.append([dept] + [""] * (len(headers_summary) - 1))
            # Two separate merges (frozen columns 0-1, then the rest) — Sheets rejects a merge
            # that spans both frozen and non-frozen columns.
            merge_requests.append({
                "mergeCells": {
                    "range": {"sheetId": ws_summary.id, "startRowIndex": dept_row_idx, "endRowIndex": dept_row_idx + 1,
                              "startColumnIndex": 0, "endColumnIndex": FROZEN_COLS},
                    "mergeType": "MERGE_ALL"
                }
            })
            merge_requests.append({
                "mergeCells": {
                    "range": {"sheetId": ws_summary.id, "startRowIndex": dept_row_idx, "endRowIndex": dept_row_idx + 1,
                              "startColumnIndex": FROZEN_COLS, "endColumnIndex": len(headers_summary)},
                    "mergeType": "MERGE_ALL"
                }
            })
            format_requests.append({
                "repeatCell": {
                    "range": {"sheetId": ws_summary.id, "startRowIndex": dept_row_idx, "endRowIndex": dept_row_idx + 1,
                              "startColumnIndex": 0, "endColumnIndex": len(headers_summary)},
                    "cell": {"userEnteredFormat": {"backgroundColor": DEPT_BG, "textFormat": {"bold": True, "foregroundColor": {"red": 0, "green": 0, "blue": 0}}}},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)"
                }
            })

            for w in dept_workers:
                slots = SCHEDULES.get(w["schedule"], SCHEDULE_A)
                join_row = conn_sum.execute(
                    "SELECT MIN(report_date) as d FROM reports WHERE telegram_id = ?", (w["telegram_id"],)
                ).fetchone()
                hire_date = today_date
                if join_row and join_row["d"]:
                    try:
                        hire_date = datetime.strptime(join_row["d"], "%Y-%m-%d").date()
                    except ValueError:
                        pass

                start_str = date_list[0].strftime("%Y-%m-%d")
                end_str = date_list[-1].strftime("%Y-%m-%d")
                status_rows = conn_sum.execute(
                    "SELECT report_date, slot_time, received_at, is_ok, format_comment, required_action FROM reports "
                    "WHERE telegram_id = ? AND report_type = 'status' AND report_date >= ? AND report_date <= ?",
                    (w["telegram_id"], start_str, end_str)
                ).fetchall()
                status_map = {(r["report_date"], r["slot_time"]): r for r in status_rows}

                not_working_dates = {
                    r["report_date"] for r in conn_sum.execute(
                        "SELECT report_date FROM reports WHERE telegram_id = ? AND report_type = 'not_working' "
                        "AND report_date >= ? AND report_date <= ?",
                        (w["telegram_id"], start_str, end_str)
                    ).fetchall()
                }

                name_text = f"{w['last_name']} {w['first_name']} ({clean_position(w['position'])})"
                worker_start_row = len(rows_summary)
                first_tracked_col = None

                for slot in slots:
                    row_cells = ["", slot]
                    hour, minute = map(int, slot.split(":"))
                    for col_idx, d in enumerate(date_list):
                        abs_col = 2 + col_idx
                        d_str = d.strftime("%Y-%m-%d")
                        date_label = headers_summary[abs_col]
                        old_entry = old_map.get((name_text, slot, date_label))
                        fresh_note = None

                        if d < hire_date:
                            row_cells.append("-")
                            continue

                        if first_tracked_col is None or abs_col < first_tracked_col:
                            first_tracked_col = abs_col

                        if d_str in not_working_dates:
                            cell_value = False
                            fresh_note = "Выходной/отгул"
                        elif d == today_date and (now.hour * 60 + now.minute) <= hour * 60 + minute + LATE_THRESHOLD_MIN:
                            cell_value = ""
                        else:
                            rep = status_map.get((d_str, slot))
                            if rep is not None:
                                cell_value = True
                                note_parts = []
                                received_at = rep["received_at"] or ""
                                try:
                                    rh, rm = int(received_at[0:2]), int(received_at[3:5])
                                    diff_mins = (rh * 60 + rm) - (hour * 60 + minute)
                                    if diff_mins < -30:
                                        note_parts.append("Прислал рано")
                                    elif diff_mins > 60:
                                        note_parts.append("Прислал поздно")
                                except (ValueError, IndexError):
                                    pass
                                if not rep["is_ok"]:
                                    remark = (rep["format_comment"] or "").strip()
                                    action = (rep["required_action"] or "").strip()
                                    note_parts.append(f"Замечание: {remark}" if remark else "Есть замечание")
                                    if action:
                                        note_parts.append(f"Требуется: {action}")
                                if note_parts:
                                    fresh_note = "\n".join(note_parts)
                            else:
                                cell_value = False
                                if old_entry and old_entry.get("bool") is True:
                                    cell_value = True  # a manual tick in the sheet always wins over "missed"

                        row_cells.append(cell_value)

                        note_to_set = fresh_note or (old_entry.get("note") if old_entry else None)
                        if note_to_set:
                            note_requests.append({
                                "updateCells": {
                                    "range": {"sheetId": ws_summary.id, "startRowIndex": len(rows_summary),
                                              "endRowIndex": len(rows_summary) + 1,
                                              "startColumnIndex": abs_col, "endColumnIndex": abs_col + 1},
                                    "rows": [{"values": [{"note": note_to_set}]}],
                                    "fields": "note"
                                }
                            })

                    rows_summary.append(row_cells)

                rows_summary[worker_start_row][0] = name_text
                merge_requests.append({
                    "mergeCells": {
                        "range": {"sheetId": ws_summary.id, "startRowIndex": worker_start_row,
                                  "endRowIndex": worker_start_row + len(slots),
                                  "startColumnIndex": 0, "endColumnIndex": 1},
                        "mergeType": "MERGE_ALL"
                    }
                })

                if first_tracked_col is not None:
                    checkbox_ranges.append(
                        (worker_start_row, worker_start_row + len(slots), first_tracked_col, len(headers_summary))
                    )

        conn_sum.close()

        grid_last_row = len(rows_summary)
        BLACK = {"red": 0, "green": 0, "blue": 0}
        border_requests = []
        if grid_last_row > header_row_idx:
            # Thin line between every day column
            border_requests.append({
                "updateBorders": {
                    "range": {"sheetId": ws_summary.id, "startRowIndex": header_row_idx, "endRowIndex": grid_last_row,
                              "startColumnIndex": 2, "endColumnIndex": len(headers_summary)},
                    "innerVertical": {"style": "SOLID", "width": 1, "color": BLACK}
                }
            })
            # Thick line at the end of each work week (after every Friday column)
            for col_idx, d in enumerate(date_list):
                abs_col = 2 + col_idx
                if d.weekday() == 4 and abs_col + 1 < len(headers_summary):
                    border_requests.append({
                        "repeatCell": {
                            "range": {"sheetId": ws_summary.id, "startRowIndex": header_row_idx, "endRowIndex": grid_last_row,
                                      "startColumnIndex": abs_col, "endColumnIndex": abs_col + 1},
                            "cell": {"userEnteredFormat": {"borders": {"right": {"style": "SOLID_THICK", "width": 2, "color": BLACK}}}},
                            "fields": "userEnteredFormat.borders.right"
                        }
                    })

        safe_update(ws_summary, rows_summary)

        checkbox_requests = [
            {
                "setDataValidation": {
                    "range": {"sheetId": ws_summary.id, "startRowIndex": r0, "endRowIndex": r1,
                              "startColumnIndex": c0, "endColumnIndex": c1},
                    "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}
                }
            }
            for (r0, r1, c0, c1) in checkbox_ranges
        ]

        all_requests = merge_requests + format_requests + border_requests + checkbox_requests + note_requests
        if all_requests:
            for i in range(0, len(all_requests), 400):
                sheet.batch_update({"requests": all_requests[i:i + 400]})

        return True, None
        
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[GSheets Sync Error] {e}")
        return False, str(e)

def async_sync_gsheets_background():
    import threading
    t = threading.Thread(target=sync_gsheets_task, daemon=True)
    t.start()
