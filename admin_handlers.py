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
    get_db, run_db, get_worker, get_all_workers, get_workers_by_position,
    find_unregistered_workers_by_lastname, bind_worker_id, upsert_worker,
    delete_worker, update_worker_field, get_object_group, save_object_group,
    get_group_name, get_group_name_async, fetch_and_save_group_name, get_all_group_names,
    get_setting, set_setting, calculate_worker_stats, SCHEDULES, SCHEDULE_A, DEFAULT_GROUP_ID,
    is_admin, ADMIN_IDS, get_pending_unregistered_user, delete_pending_unregistered_user,
    export_workers_to_excel, read_excel, get_next_sort_order, fetch_export_data,
    generate_and_send_excel, generate_and_send_gsheets, get_violators_threshold,
    save_violators_threshold, now_local, set_quiet_mode, is_quiet_mode_enabled,
    save_scheduled_times, get_scheduled_times
)

logger = logging.getLogger(__name__)

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📋 Сотрудники", "➕ Добавить сотрудника", "➖ Удалить сотрудника"],
        ["🏢 Сотрудники отдела", "⏰ Время оповещений о статусах", "📣 Напомнить всем"],
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
) = range(33)

def schedule_description_text() -> str:
    lines = []
    for key in sorted(SCHEDULES.keys()):
        times_str = ", ".join(SCHEDULES[key])
        lines.append(f"{key} — {times_str}")
    return "\n".join(lines)

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
        [["📊 Настроить Google Таблицу"], ["🗑 Очистить базу от удалённых сотрудников"], ["❌ Назад"]],
        resize_keyboard=True
    )
    await update.message.reply_text("⚙️ Настройки бота. Выберите действие:", reply_markup=kbd)
    return ASK_SETTINGS_ACTION

async def settings_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
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
    context.user_data["worker_to_delete"] = worker
    kbd = ReplyKeyboardMarkup([["Да, удалить", "Нет, отмена"]], resize_keyboard=True)
    await update.message.reply_text(
        f"⚠️ Вы уверены, что хотите удалить сотрудника {worker['last_name']} {worker['first_name']} (ID: {worker['telegram_id']})?",
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
            await update.message.reply_text(f"✅ Сотрудник {worker['last_name']} успешно удален.", reply_markup=MAIN_MENU)
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
    position = update.message.text.strip()
    rows = get_workers_by_position(position)
    if not rows:
        await update.message.reply_text("Сотрудники не найдены.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    lines = [f"📋 Отдел: {position}"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['last_name']} {r['first_name']}")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)
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
        await update.message.reply_text(f"❌ Сотрудник с фамилией *{text}* не найден среди незарегистрированных.", parse_mode="Markdown", reply_markup=menu_for_user(user_id))
        return ConversationHandler.END
    elif len(workers) == 1:
        candidate = workers[0]
        bind_worker_id(candidate["telegram_id"], user_id)
        w_fio = f"{candidate['last_name']} {candidate['first_name']}"
        await update.message.reply_text(f"🎉 *Регистрация успешна!*\nВы привязаны к профилю: *{w_fio}*.", parse_mode="Markdown", reply_markup=menu_for_user(user_id))
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
    await update.message.reply_text(f"🎉 *Регистрация успешна!*\nВы привязаны к профилю: *{matched_candidate['last_name']} {matched_candidate['first_name']}*.", parse_mode="Markdown", reply_markup=menu_for_user(user_id))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=menu_for_user(update.effective_user.id, update.effective_chat.type))
    return ConversationHandler.END
