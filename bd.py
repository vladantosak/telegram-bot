import os
import sqlite3
import json
import hashlib
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
    rows = conn.execute("SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name").fetchall()
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
