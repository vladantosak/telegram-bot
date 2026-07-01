import os
import json
import logging
import re
import html
import io
from datetime import datetime
import datetime as dt_module
from openpyxl import load_workbook
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ContextTypes, ConversationHandler

from db import (
    get_db, run_db, get_worker, get_all_workers, get_workers_by_position, get_workers_by_object_id,
    find_unregistered_workers_by_lastname, bind_worker_id, upsert_worker,
    delete_worker, update_worker_field, get_object_group, save_object_group, clean_position,
    get_group_name, get_group_name_async, fetch_and_save_group_name, get_all_group_names,
    get_setting, set_setting, calculate_worker_stats, SCHEDULES, SCHEDULE_A, DEFAULT_GROUP_ID,
    is_admin, ADMIN_IDS, get_pending_unregistered_user, delete_pending_unregistered_user,
    export_workers_to_excel, read_excel, get_next_sort_order, fetch_export_data,
    generate_and_send_excel, generate_and_send_gsheets, get_violators_threshold,
    save_violators_threshold, now_local, set_quiet_mode, is_quiet_mode_enabled,
    save_scheduled_times, get_scheduled_times, sync_gsheets_task, async_sync_gsheets_background
)

from report_handlers import menu_for_user

logger = logging.getLogger(__name__)

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Добавить сотрудника", "➖ Удалить сотрудника", "🏢 Сотрудники отдела"],
        ["⏰ Время оповещений о статусах", "📣 Напомнить всем"],
        ["📥 Выгрузить отчеты", "📥 Импорт сотрудников", "⚙️ Настройки бота"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
SCHEDULE_KEYBOARD = ReplyKeyboardMarkup([["A", "B"], ["❌ Отмена"]], resize_keyboard=True)
YES_NO_KEYBOARD = ReplyKeyboardMarkup([["Да", "Нет"], ["❌ Отмена"]], resize_keyboard=True)
CANCEL_TEXT = "❌ Отмена"

# Dialog States
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
    ASK_EDIT_SORT_ORDER,
    ASK_CONFIRM_DELETE,
    ASK_EDIT_STATUS_WORK,
    ASK_EXPORT_TYPE,
    ASK_EXPORT_DEPARTMENT,
    ASK_EXPORT_FORMAT,
    ASK_GSHEETS_URL,
    ASK_GSHEETS_CREDS,
    ASK_IMPORT_FILE,
    ASK_MOVE_POSITION_ORDER,
    ASK_REG_LAST_NAME,
    ASK_REG_FIRST_NAME,
    ASK_SETTINGS_ACTION,
    ASK_IMPORT_ACTION,
    ASK_CONFIRM_REMIND,
) = range(34)

def schedule_description_text() -> str:
    lines = []
    for key in sorted(SCHEDULES.keys()):
        times_str = ", ".join(SCHEDULES[key])
        lines.append(f"{key} — {times_str}")
    return "\n".join(lines)

def positions_keyboard(rows):
    departments = sorted({row["object_id"] or "Основной" for row in rows})
    keyboard = [[d] for d in departments]
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True), departments

def numbered_workers_keyboard(rows):
    keyboard = []
    for i, row in enumerate(rows, 1):
        keyboard.append([f"{i}. {row['last_name']} {row['first_name']} ({clean_position(row['position'])})"])
    keyboard.append(["❌ Отмена"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def require_admin_check(update: Update) -> bool:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Эта функция доступна только администраторам.", reply_markup=ReplyKeyboardRemove())
        return False
    if update.effective_chat.type != "private":
        try:
            await update.message.reply_text("⚠️ Эта функция доступна только в личных сообщениях с ботом.", reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass
        return False
    return True

async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    kbd = ReplyKeyboardMarkup(
        [["📊 Настроить Google Таблицу", "🔗 Получить ссылку на таблицу"], ["🗑 Очистить базу от удалённых сотрудников"], ["❌ Назад"]],
        resize_keyboard=True
    )
    await update.message.reply_text("⚙️ Настройки бота. Выберите действие:", reply_markup=kbd)
    return ASK_SETTINGS_ACTION

async def settings_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    kbd = ReplyKeyboardMarkup(
        [["📊 Настроить Google Таблицу", "🔗 Получить ссылку на таблицу"], ["🗑 Очистить базу от удалённых сотрудников"], ["❌ Назад"]],
        resize_keyboard=True
    )
    if choice == "❌ Назад":
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "🗑 Очистить базу от удалённых сотрудников":
        # Cleanup orphaned reports
        conn = get_db()
        cur = conn.execute("DELETE FROM reports WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)")
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        async_sync_gsheets_background()
        await update.message.reply_text(f"✅ Удалено {deleted} лишних записей отчетов.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "🔗 Получить ссылку на таблицу":
        spreadsheet_id = get_setting("google_spreadsheet_id")
        if not spreadsheet_id:
            await update.message.reply_text(
                "❌ Google Таблица не настроена. Пожалуйста, настройте её с помощью кнопки «📊 Настроить Google Таблицу».",
                reply_markup=kbd
            )
        else:
            link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
            await update.message.reply_text(
                f"🔗 **Ссылка на вашу Google Таблицу:**\n{link}",
                reply_markup=kbd,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )
        return ASK_SETTINGS_ACTION

    if choice == "📊 Настроить Google Таблицу":
        spreadsheet_id = get_setting("google_spreadsheet_id", "Не задан")
        email = "Не задан"
        service_account_str = get_setting("google_service_account")
        if service_account_str:
            try: email = json.loads(service_account_str).get("client_email", "Не задан")
            except Exception: pass
        await update.message.reply_text(
            f"⚙️ **Текущие настройки Google Таблиц:**\n\n"
            f"🔗 **ID таблицы:** `{spreadsheet_id}`\n"
            f"📧 **Сервисный аккаунт:** `{email}`\n\n"
            f"Пришлите ссылку на Google Таблицу или её ID, чтобы настроить интеграцию.",
            reply_markup=CANCEL_KEYBOARD,
            parse_mode="Markdown"
        )
        return ASK_GSHEETS_URL
    return ASK_SETTINGS_ACTION

async def add_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Введите Telegram ID сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_WORKER_ID

async def add_worker_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit():
        await update.message.reply_text("Введите числовой ID:")
        return ASK_WORKER_ID
    worker_id = int(raw)
    context.user_data["new_worker_id"] = worker_id
    
    pending = get_pending_unregistered_user(worker_id)
    if pending:
        context.user_data["pending_last_name"] = pending["last_name"]
        context.user_data["pending_first_name"] = pending["first_name"]
        await update.message.reply_text(
            f"Найден временный отчет сотрудника!\nФИО: {pending['last_name']} {pending['first_name']}\n\n"
            f"Подтвердите фамилию (нажмите на кнопку ниже) или введите новую:",
            reply_markup=ReplyKeyboardMarkup([[pending["last_name"]], ["❌ Отмена"]], resize_keyboard=True)
        )
    else:
        await update.message.reply_text("Введите фамилию:", reply_markup=CANCEL_KEYBOARD)
    return ASK_LASTNAME

async def add_worker_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["last_name"] = val
    pending_first = context.user_data.get("pending_first_name")
    if pending_first:
        await update.message.reply_text(
            "Подтвердите имя (нажмите на кнопку ниже) или введите новое:",
            reply_markup=ReplyKeyboardMarkup([[pending_first], ["❌ Отмена"]], resize_keyboard=True)
        )
    else:
        await update.message.reply_text("Введите имя:", reply_markup=CANCEL_KEYBOARD)
    return ASK_FIRSTNAME

async def add_worker_firstname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["first_name"] = update.message.text.strip()
    await update.message.reply_text("Введите должность сотрудника:", reply_markup=CANCEL_KEYBOARD)
    return ASK_POSITION

async def add_worker_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["position"] = update.message.text.strip()
    await update.message.reply_text("Введите ID группы Telegram (или 0 для группы по умолчанию):", reply_markup=CANCEL_KEYBOARD)
    return ASK_GROUP

async def add_worker_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.lstrip("-").isdigit(): return ASK_GROUP
    context.user_data["group_id"] = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    await update.message.reply_text(f"Выберите график сдачи статусов:\n{schedule_description_text()}", reply_markup=SCHEDULE_KEYBOARD)
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

    next_order = get_next_sort_order(context.user_data["position"])
    upsert_worker(
        telegram_id=context.user_data["new_worker_id"],
        last_name=context.user_data["last_name"],
        first_name=context.user_data["first_name"],
        position=context.user_data["position"],
        group_id=context.user_data["group_id"],
        schedule=context.user_data["schedule"],
        needs_daily_fact=(raw == "да"),
        sort_order=next_order,
    )
    delete_pending_unregistered_user(context.user_data["new_worker_id"])
    await fetch_and_save_group_name(context.bot, context.user_data["group_id"])
    
    target_group_id = context.user_data["group_id"] or DEFAULT_GROUP_ID
    try:
        notify_msg = f"👤 {context.user_data['last_name']} {context.user_data['first_name']} добавлен в систему, ID: {context.user_data['new_worker_id']}"
        await context.bot.send_message(chat_id=target_group_id, text=notify_msg)
    except Exception:
        pass

    async_sync_gsheets_background()
    await update.message.reply_text("Сотрудник успешно добавлен в базу!", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def delete_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел сотрудника для удаления:", reply_markup=kbd)
    return ASK_REMOVE_DEPARTMENT

async def delete_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dept = update.message.text.strip()
    rows = get_workers_by_object_id(dept)
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
    context.user_data["worker_to_delete"] = worker
    kbd = ReplyKeyboardMarkup([["Да, удалить", "Нет, отмена"]], resize_keyboard=True)
    await update.message.reply_text(
        f"⚠️ Вы уверены, что хотите удалить сотрудника {worker['last_name']} {worker['first_name']} ({clean_position(worker['position'])}) (ID: {worker['telegram_id']})?",
        reply_markup=kbd
    )
    return ASK_CONFIRM_DELETE

async def delete_worker_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "Да, удалить":
        worker = context.user_data.get("worker_to_delete")
        if worker:
            delete_worker(worker["telegram_id"])
            async_sync_gsheets_background()
            await update.message.reply_text(f"✅ Сотрудник {worker['last_name']} {worker['first_name']} успешно удален.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Удаление сотрудника отменено.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def department_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    rows = get_all_workers()
    if not rows:
        await update.message.reply_text("В базе нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел для просмотра:", reply_markup=kbd)
    return ASK_DEPARTMENT

async def department_workers_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dept = update.message.text.strip()
    rows = get_workers_by_object_id(dept)
    if not rows:
        await update.message.reply_text("Сотрудники не найдены.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    context.user_data["edit_dept_workers"] = [dict(r) for r in rows]
    lines = [f"📋 Отдел: {dept}"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['last_name']} {r['first_name']} ({clean_position(r['position'])})")
    lines.append("\nВведите номер сотрудника, чтобы отредактировать его, или нажмите «❌ Отмена».")
    await update.message.reply_text("\n".join(lines), reply_markup=CANCEL_KEYBOARD)
    return ASK_LIST_WORKER

EDIT_FIELD_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["Фамилия", "Имя", "Должность"],
        ["График", "Отдел/Объект", "ID группы"],
        ["Ежедневный факт", "Активен", "Порядок"],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
)

EDIT_FIELD_MAP = {
    "Фамилия": ("last_name", "Введите новую фамилию:"),
    "Имя": ("first_name", "Введите новое имя:"),
    "Должность": ("position", "Введите новую должность:"),
    "Отдел/Объект": ("object_id", "Введите название отдела (объекта):"),
    "ID группы": ("group_id", "Введите ID группы Telegram (0 — группа по умолчанию):"),
    "Порядок": ("sort_order", "Введите номер сортировки (число):"),
}

async def edit_worker_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    num_str = raw.split(".")[0].strip()
    rows = context.user_data.get("edit_dept_workers", [])
    if not num_str.isdigit() or not (1 <= int(num_str) <= len(rows)):
        await update.message.reply_text("Введите корректный номер сотрудника из списка.")
        return ASK_LIST_WORKER
    worker = rows[int(num_str) - 1]
    context.user_data["edit_worker"] = worker
    await update.message.reply_text(
        f"✏️ Редактирование: {worker['last_name']} {worker['first_name']} ({clean_position(worker['position'])})\n"
        f"Что изменить?",
        reply_markup=EDIT_FIELD_KEYBOARD
    )
    return ASK_EDIT_FIELD

async def edit_worker_field_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    if not worker:
        await update.message.reply_text("Сессия редактирования истекла, начните заново.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "График":
        await update.message.reply_text(
            f"Текущий график: {worker['schedule']}\n{schedule_description_text()}", reply_markup=SCHEDULE_KEYBOARD
        )
        return ASK_EDIT_SCHEDULE
    if choice == "Ежедневный факт":
        cur = "Да" if worker["needs_daily_fact"] else "Нет"
        await update.message.reply_text(f"Сейчас: {cur}. Нужно ли присылать ежедневный факт дня?", reply_markup=YES_NO_KEYBOARD)
        return ASK_EDIT_DAILY_FACT
    if choice == "Активен":
        cur = "Да" if worker["is_active"] else "Нет"
        await update.message.reply_text(f"Сейчас: {cur}. Сотрудник активен?", reply_markup=YES_NO_KEYBOARD)
        return ASK_EDIT_STATUS_WORK
    if choice in EDIT_FIELD_MAP:
        field, prompt = EDIT_FIELD_MAP[choice]
        context.user_data["edit_field"] = field
        await update.message.reply_text(f"Сейчас: {worker.get(field)}\n{prompt}", reply_markup=CANCEL_KEYBOARD)
        return ASK_EDIT_VALUE

    await update.message.reply_text("Выберите поле из меню ниже.", reply_markup=EDIT_FIELD_KEYBOARD)
    return ASK_EDIT_FIELD

async def edit_worker_value_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    worker = context.user_data.get("edit_worker")
    field = context.user_data.get("edit_field")
    raw = update.message.text.strip()
    if not worker or not field:
        await update.message.reply_text("Сессия редактирования истекла, начните заново.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    value = raw
    if field == "group_id":
        if not raw.lstrip("-").isdigit():
            await update.message.reply_text("Введите число.")
            return ASK_EDIT_VALUE
        value = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    elif field == "sort_order":
        if not raw.isdigit():
            await update.message.reply_text("Введите число.")
            return ASK_EDIT_VALUE
        value = int(raw)

    update_worker_field(worker["telegram_id"], field, value)
    async_sync_gsheets_background()
    await update.message.reply_text("✅ Изменения сохранены.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_worker_schedule_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        return ASK_EDIT_SCHEDULE
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "schedule", raw)
    async_sync_gsheets_background()
    await update.message.reply_text("✅ График обновлён.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_worker_daily_fact_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        return ASK_EDIT_DAILY_FACT
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "needs_daily_fact", raw == "да")
    async_sync_gsheets_background()
    await update.message.reply_text("✅ Обновлено.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_worker_status_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        return ASK_EDIT_STATUS_WORK
    worker = context.user_data.get("edit_worker")
    update_worker_field(worker["telegram_id"], "is_active", raw == "да")
    async_sync_gsheets_background()
    await update.message.reply_text("✅ Обновлено.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def import_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    kbd = ReplyKeyboardMarkup(
        [["📤 Скачать текущий список сотрудников"], ["📥 Загрузить обновления из файла"], ["❌ Отмена"]],
        resize_keyboard=True
    )
    await update.message.reply_text("Выберите действие:", reply_markup=kbd)
    return ASK_IMPORT_ACTION

async def import_workers_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "📤 Скачать текущий список сотрудников":
        await update.message.reply_text("⏳ Формирую файл...", reply_markup=CANCEL_KEYBOARD)
        try:
            data = export_workers_to_excel()
            bio = io.BytesIO(data)
            bio.name = "workers.xlsx"
            await update.message.reply_document(
                document=bio,
                filename="workers.xlsx",
                caption="📋 Текущий список сотрудников. Внесите изменения и отправьте файл обратно.",
                reply_markup=MAIN_MENU
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при формировании файла: {e}", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "📥 Загрузить обновления из файла":
        await update.message.reply_text("📎 Отправьте файл Excel (.xlsx) со списком сотрудников.", reply_markup=CANCEL_KEYBOARD)
        return ASK_IMPORT_FILE
    return ConversationHandler.END

async def import_workers_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Пожалуйста, отправьте файл типа документ (.xlsx).")
        return ASK_IMPORT_FILE
    
    await update.message.reply_text("⏳ Получение файла и импорт данных...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        local_path = "workers_temp_import.xlsx"
        await tg_file.download_to_drive(local_path)
        workers = read_excel(local_path)
        if not workers:
            await update.message.reply_text("⚠️ Не обнаружено записей в файле.", reply_markup=MAIN_MENU)
            if os.path.exists(local_path): os.remove(local_path)
            return ConversationHandler.END
        
        for w in workers:
            upsert_worker(
                telegram_id=w["telegram_id"], last_name=w["last_name"], first_name=w["first_name"],
                position=w["position"], group_id=w["group_id"], schedule=w["schedule"],
                needs_daily_fact=w["needs_daily_fact"], object_id=w.get("object_id", "Основной"),
                is_active=1 if w.get("is_active", True) else 0, sort_order=w.get("sort_order", 0)
            )
        if os.path.exists(local_path): os.remove(local_path)
        async_sync_gsheets_background()
        await update.message.reply_text(f"✅ Импорт успешно завершен! Загружено/обновлено: {len(workers)}.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка во время импорта: {e}", reply_markup=MAIN_MENU)
        return ConversationHandler.END

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type != "private": return ConversationHandler.END
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    worker = get_worker(user_id)
    if worker:
        await update.message.reply_text("Привет! Отправьте видеоотчет, когда он будет готов.", reply_markup=menu_for_user(user_id, chat_type))
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 *Добро пожаловать в систему сдачи отчетов!*\n\nПожалуйста, введите вашу **Фамилию** для поиска:",
        parse_mode="Markdown", reply_markup=CANCEL_KEYBOARD
    )
    return ASK_REG_LAST_NAME

async def register_lastname_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if text == CANCEL_TEXT:
        await update.message.reply_text("Регистрация отменена.", reply_markup=menu_for_user(user_id))
        return ConversationHandler.END
    
    workers = find_unregistered_workers_by_lastname(text)
    if len(workers) == 0:
        await update.message.reply_text(f"❌ Сотрудник с фамилией <b>{html.escape(text)}</b> не найден среди незарегистрированных.", parse_mode="HTML", reply_markup=menu_for_user(user_id))
        return ConversationHandler.END
    elif len(workers) == 1:
        candidate = workers[0]
        bind_worker_id(candidate["telegram_id"], user_id)
        w_fio = f"{candidate['last_name']} {candidate['first_name']}"
        await update.message.reply_text(f"🎉 <b>Регистрация успешна!</b>\nВы привязаны к профилю: <b>{html.escape(w_fio)}</b>.", parse_mode="HTML", reply_markup=menu_for_user(user_id))
        return ConversationHandler.END
    else:
        context.user_data["candidate_workers"] = [dict(w) for w in workers]
        buttons = [[f"{w['last_name']} {w['first_name']}"] for w in workers]
        buttons.append([CANCEL_TEXT])
        await update.message.reply_text("🔍 Найдено несколько сотрудников. Выберите ваше имя на клавиатуре:", reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True))
        return ASK_REG_FIRST_NAME

async def register_firstname_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if text == CANCEL_TEXT:
        await update.message.reply_text("Регистрация отменена.", reply_markup=menu_for_user(user_id))
        return ConversationHandler.END
    
    candidates = context.user_data.get("candidate_workers", [])
    matched_candidate = None
    for c in candidates:
        if text.lower() == f"{c['last_name']} {c['first_name']}".lower():
            matched_candidate = c
            break
    if not matched_candidate:
        await update.message.reply_text("❌ Пожалуйста, выберите имя из списка.")
        return ASK_REG_FIRST_NAME
    
    bind_worker_id(matched_candidate["telegram_id"], user_id)
    w_fio = f"{matched_candidate['last_name']} {matched_candidate['first_name']}"
    await update.message.reply_text(f"🎉 <b>Регистрация успешна!</b>\nВы привязаны к профилю: <b>{html.escape(w_fio)}</b>.", parse_mode="HTML", reply_markup=menu_for_user(user_id))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    context.user_data.clear()
    
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    
    if text in (CANCEL_TEXT, "❌ Отмена", "❌ Назад"):
        await update.message.reply_text("Действие отменено.", reply_markup=menu_for_user(user_id, chat_type))
        return ConversationHandler.END
        
    if text == "📥 Выгрузить отчеты":
        await export_reports_action(update, context)
        return ConversationHandler.END
        
    await update.message.reply_text(
        f"❌ Предыдущее действие отменено.\nПожалуйста, нажмите кнопку <b>«{html.escape(text)}»</b> ещё раз.",
        reply_markup=menu_for_user(user_id, chat_type),
        parse_mode="HTML"
    )
    return ConversationHandler.END

EXPORT_MENU = ReplyKeyboardMarkup(
    [
        ["📊 Скачать Excel файл", "🔄 Синхронизировать сейчас"],
        ["❌ Назад"]
    ],
    resize_keyboard=True
)

async def export_reports_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    await update.message.reply_text(
        "📥 Выгрузка отчетов и синхронизация.\nВыберите действие:",
        reply_markup=EXPORT_MENU
    )
    return ASK_EXPORT_TYPE

async def export_reports_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "❌ Назад":
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "📊 Скачать Excel файл":
        await update.message.reply_text("⏳ Формирую Excel файл с отчётами...", reply_markup=MAIN_MENU)
        try:
            from db import export_reports_to_excel
            data = export_reports_to_excel()
            bio = io.BytesIO(data)
            bio.name = "reports.xlsx"
            await update.message.reply_document(
                document=bio,
                filename="reports.xlsx",
                caption="📋 Полная выгрузка отчётов из базы данных.",
                reply_markup=MAIN_MENU
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при формировании файла отчётов: {e}", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "🔄 Синхронизировать сейчас":
        from db import get_setting, sync_gsheets_task
        spreadsheet_id = get_setting("google_spreadsheet_id")
        creds_str = get_setting("google_service_account")
        if not spreadsheet_id or not creds_str:
            await update.message.reply_text(
                "❌ Google Таблица не настроена. Пожалуйста, сначала настройте Google Таблицу в «⚙️ Настройки бота».",
                reply_markup=MAIN_MENU
            )
            return ConversationHandler.END
            
        await update.message.reply_text("⏳ Запускаю синхронизацию данных с Google Таблицей...", reply_markup=MAIN_MENU)
        import asyncio
        success, err = await asyncio.to_thread(sync_gsheets_task)
        if success:
            await update.message.reply_text("🎉 **Синхронизация завершена успешно!**\nВсе листы (Сотрудники, Отчеты, Аналитика, Сводка) обновлены.", reply_markup=MAIN_MENU, parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ **Ошибка синхронизации:**\n`{err}`", reply_markup=MAIN_MENU, parse_mode="Markdown")
        return ConversationHandler.END

    return ASK_EXPORT_TYPE

async def alert_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    times = get_scheduled_times() or ["19:00"]
    times_str = ", ".join(times)
    await update.message.reply_text(
        f"⏰ **Текущее время авто-сводок:**\n"
        f"`{times_str}`\n\n"
        f"Пожалуйста, введите новое время (или список времён через запятую в формате `ЧЧ:ММ`, например: `12:00, 15:00, 19:00`):",
        reply_markup=CANCEL_KEYBOARD,
        parse_mode="Markdown"
    )
    return ASK_REPORT_TIME

async def alert_time_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == "отмена" or text == CANCEL_TEXT:
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    parts = [p.strip() for p in text.split(",")]
    valid_times = []
    invalid_parts = []
    
    import re
    for p in parts:
        if re.match(r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$", p):
            valid_times.append(p)
        else:
            invalid_parts.append(p)
            
    if invalid_parts:
        await update.message.reply_text(
            f"❌ Неверный формат времени: {', '.join(invalid_parts)}.\n"
            f"Пожалуйста, введите время в формате `ЧЧ:ММ` (например: `19:00` или `10:00, 15:00`):",
            reply_markup=CANCEL_KEYBOARD
        )
        return ASK_REPORT_TIME
        
    save_scheduled_times(valid_times)
    from bot import reschedule_summary_jobs
    reschedule_summary_jobs(context.application)
    
    times_str = ", ".join(valid_times)
    await update.message.reply_text(
        f"✅ Время автоматических сводок успешно обновлено: `{times_str}`!",
        reply_markup=MAIN_MENU,
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def remind_all_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    await update.message.reply_text(
        "📣 **Рассылка напоминания всем активным сотрудникам**\n\n"
        "Вы можете отправить текст напоминания по умолчанию:\n"
        "_«🔔 Напоминание: пожалуйста, не забудьте вовремя отправить ваш видеоотчёт!»_\n\n"
        "Напишите свой текст напоминания для отправки или введите `Да`, чтобы использовать стандартный:",
        reply_markup=CANCEL_KEYBOARD,
        parse_mode="Markdown"
    )
    return ASK_CONFIRM_REMIND

async def remind_all_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio
    text = update.message.text.strip()
    if text.lower() == "отмена" or text == CANCEL_TEXT:
        await update.message.reply_text("Рассылка отменена.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    reminder_text = "🔔 Напоминание: пожалуйста, не забудьте вовремя отправить ваш видеоотчёт!"
    if text.lower() != "да":
        reminder_text = text
        
    workers = get_all_workers()
    active_workers = [w for w in workers if w["is_active"]]
    
    if not active_workers:
        await update.message.reply_text("Нет активных сотрудников в базе данных.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    await update.message.reply_text(f"⏳ Отправка сообщения {len(active_workers)} сотрудникам...", reply_markup=MAIN_MENU)
    
    success_count = 0
    fail_count = 0
    for w in active_workers:
        try:
            await context.bot.send_message(chat_id=w["telegram_id"], text=reminder_text)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание {w['telegram_id']}: {e}")
            fail_count += 1
            
    await update.message.reply_text(
        f"✅ Рассылка завершена!\n"
        f"Успешно отправлено: {success_count}\n"
        f"Ошибок отправки: {fail_count}",
        reply_markup=MAIN_MENU
    )
    return ConversationHandler.END

def extract_spreadsheet_id(text: str) -> str:
    text = text.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    if match:
        return match.group(1)
    return text

async def save_gsheets_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == "отмена" or text == CANCEL_TEXT:
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
        
    sheet_id = extract_spreadsheet_id(text)
    if not sheet_id:
        await update.message.reply_text("❌ Не удалось распознать ID таблицы. Пожалуйста, отправьте корректную ссылку или ID:")
        return ASK_GSHEETS_URL
        
    set_setting("google_spreadsheet_id", sheet_id)
    
    await update.message.reply_text(
        f"✅ ID Google Таблицы сохранен: `{sheet_id}`\n\n"
        f"Теперь, пожалуйста, **отправьте JSON-файл с ключами сервисного аккаунта** (credentials) как документ.\n"
        f"Этот файл вы можете скачать из Google Cloud Console при создании ключа сервисного аккаунта.",
        reply_markup=CANCEL_KEYBOARD,
        parse_mode="Markdown"
    )
    return ASK_GSHEETS_CREDS

async def save_gsheets_creds_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.lower() == "отмена" or text == CANCEL_TEXT:
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    await update.message.reply_text(
        "❌ Пожалуйста, пришлите JSON-файл ключа сервисного аккаунта Google как документ (или введите «Отмена»):",
        reply_markup=CANCEL_KEYBOARD
    )
    return ASK_GSHEETS_CREDS

async def save_gsheets_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ Пожалуйста, пришлите JSON-файл ключа сервисного аккаунта Google как документ:")
        return ASK_GSHEETS_CREDS
        
    if not doc.file_name.lower().endswith(".json"):
        await update.message.reply_text("❌ Файл должен иметь расширение .json. Пожалуйста, отправьте правильный файл:")
        return ASK_GSHEETS_CREDS
        
    try:
        # Download the file
        file_obj = await context.bot.get_file(doc.file_id)
        file_bytes = await file_obj.download_as_bytearray()
        content = file_bytes.decode("utf-8")
        
        # Verify JSON
        creds_dict = json.loads(content)
        if "client_email" not in creds_dict or "private_key" not in creds_dict:
            await update.message.reply_text(
                "❌ Некорректный формат файла. Убедитесь, что это JSON-файл ключей сервисного аккаунта Google (содержит client_email и private_key):"
            )
            return ASK_GSHEETS_CREDS
            
        set_setting("google_service_account", content)
        
        email = creds_dict.get("client_email")
        spreadsheet_id = get_setting("google_spreadsheet_id")
        
        await update.message.reply_text(
            f"✅ Ключи сервисного аккаунта успешно сохранены!\n"
            f"📧 Email сервисного аккаунта: `{email}`\n\n"
            f"⚠️ **ВАЖНО:** Обязательно откройте доступ к вашей Google Таблице (кнопка 'Поделиться' в правом верхнем углу таблицы) на редактирование для этого email: `{email}`.\n\n"
            f"Запускаю тестовую синхронизацию...",
            reply_markup=MAIN_MENU,
            parse_mode="Markdown"
        )
        
        # Sync synchronously inside thread pool to verify and report success/failure immediately
        import asyncio
        from db import sync_gsheets_task
        success, err = await asyncio.to_thread(sync_gsheets_task)
        if success:
            await update.message.reply_text(
                "🎉 **Синхронизация с Google Таблицей прошла успешно!**\n"
                "Данные о сотрудниках, отчетах и вкладки Аналитика и Сводка обновлены в вашей таблице.",
                reply_markup=MAIN_MENU,
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"⚠️ **Внимание:** Синхронизация завершилась с ошибкой:\n`{err}`\n\n"
                f"Убедитесь, что вы выдали доступ (Поделиться) на редактирование сервисному аккаунту `{email}` в вашей таблице, после чего попробуйте запустить синхронизацию вручную из настроек.",
                reply_markup=MAIN_MENU,
                parse_mode="Markdown"
            )
            
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка при обработке файла credentials: {e}")
        await update.message.reply_text(f"❌ Ошибка при чтении или разборе файла: {e}\nПожалуйста, пришлите корректный файл:")
        return ASK_GSHEETS_CREDS
