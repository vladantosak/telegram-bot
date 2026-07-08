import os
import json
import logging
import re
import html
import io
from datetime import datetime
import datetime as dt_module
from openpyxl import load_workbook
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from db import (
    get_db, run_db, get_worker, get_all_workers, get_workers_by_position, get_workers_by_object_id,
    find_unregistered_workers_by_lastname, find_registered_workers_by_lastname, bind_worker_id, upsert_worker,
    delete_worker, update_worker_field, get_object_group, save_object_group, clean_position,
    get_worker_target_group,
    get_group_name, get_group_name_async, fetch_and_save_group_name, get_all_group_names,
    get_setting, set_setting, calculate_worker_stats, SCHEDULES, SCHEDULE_A, DEFAULT_GROUP_ID,
    is_admin, ADMIN_IDS, get_pending_unregistered_user, delete_pending_unregistered_user,
    export_workers_to_excel, read_excel, get_next_sort_order, fetch_export_data,
    generate_and_send_excel, generate_and_send_gsheets, get_violators_threshold,
    save_violators_threshold, now_local, set_quiet_mode, is_quiet_mode_enabled,
    save_scheduled_times, get_scheduled_times, sync_gsheets_task, async_sync_gsheets_background,
    save_report, get_all_departments, add_department, delete_department, count_workers_in_department,
    get_departments_ordered, save_departments_order,
    is_missed_reason_request_enabled, set_missed_reason_request_enabled,
    parse_workers_excel_bytes, compute_workers_diff, apply_workers_sync,
    backup_database_to_file, restore_database_from_file
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
    ASK_NOT_WORKING_DAYS,
    ASK_NOT_WORKING_REASON,
    ASK_REG_CONFIRM,
    ASK_REG_CONTACT,
    ASK_WORKER_DEPARTMENT,
    ASK_DEPT_ACTION,
    ASK_DEPT_ADD_NAME,
    ASK_DEPT_DELETE_SELECT,
    ASK_SUMMARY_DATE,
    ASK_DEPT_ORDER_ACTION,
) = range(44)

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
        await update.message.reply_text("Эта функция доступна только отделу контроля складовки.", reply_markup=ReplyKeyboardRemove())
        return False
    if update.effective_chat.type != "private":
        try:
            await update.message.reply_text("⚠️ Эта функция доступна только в личных сообщениях с ботом.", reply_markup=ReplyKeyboardRemove())
        except Exception:
            pass
        return False
    return True

SETTINGS_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📊 Настроить Google Таблицу", "🔗 Получить ссылку на таблицу"],
        ["🏢 Редактировать отделы", "📊 Порядок отделов в таблице"],
        ["📋 Запрос причины несдачи статуса"],
        ["🗑 Очистить базу от удалённых сотрудников"],
        ["❌ Назад"]
    ],
    resize_keyboard=True
)

def _cleanup_orphaned_records():
    conn = get_db()
    conn.execute(
        "DELETE FROM report_media WHERE report_id IN "
        "(SELECT id FROM reports WHERE telegram_id NOT IN (SELECT telegram_id FROM workers))"
    )
    deleted_reports = conn.execute(
        "DELETE FROM reports WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)"
    ).rowcount
    deleted_reminders = conn.execute(
        "DELETE FROM sent_reminders WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)"
    ).rowcount
    deleted_pre_reminders = conn.execute(
        "DELETE FROM sent_pre_reminders WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)"
    ).rowcount
    conn.execute("DELETE FROM pending_reason_requests WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)")
    conn.execute("DELETE FROM missed_status_reasons WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)")
    # pending_unregistered_users holds "temporary report" data for senders who aren't (yet) a
    # worker. Rows for IDs that used to be a worker (now deleted) are pure ghosts — they were
    # never swept here before, so re-adding that same ID kept resurfacing the old temp report.
    deleted_pending = conn.execute(
        "DELETE FROM pending_unregistered_users WHERE telegram_id NOT IN (SELECT telegram_id FROM workers)"
    ).rowcount
    conn.commit()
    conn.close()
    return deleted_reports, deleted_reminders + deleted_pre_reminders, deleted_pending

async def settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    await update.message.reply_text("⚙️ Настройки бота. Выберите действие:", reply_markup=SETTINGS_KEYBOARD)
    return ASK_SETTINGS_ACTION

async def settings_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    kbd = SETTINGS_KEYBOARD
    if choice == "❌ Назад":
        await update.message.reply_text("Главное меню.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "🏢 Редактировать отделы":
        return await department_manage_start(update, context)

    if choice == "📊 Порядок отделов в таблице":
        return await department_order_start(update, context)

    if choice == "📋 Запрос причины несдачи статуса":
        curr = await run_db(is_missed_reason_request_enabled)
        await run_db(set_missed_reason_request_enabled, not curr)
        status_label = "ВКЛЮЧЁН" if not curr else "ВЫКЛЮЧЕН"
        await update.message.reply_text(
            f"📋 Запрос причины несдачи статуса {status_label}.\n"
            + (
                "Теперь при пропуске статуса сотрудник должен будет написать причину, прежде чем сможет отправить новое видео."
                if not curr else
                "Теперь пропуск статуса не требует объяснения и не блокирует отправку видео."
            ),
            reply_markup=kbd
        )
        return ASK_SETTINGS_ACTION

    if choice == "🗑 Очистить базу от удалённых сотрудников":
        deleted_reports, deleted_reminders, deleted_pending = await run_db(_cleanup_orphaned_records)
        async_sync_gsheets_background()
        await update.message.reply_text(
            f"✅ База очищена:\n"
            f"• Отчётов удалено: {deleted_reports}\n"
            f"• Напоминаний удалено: {deleted_reminders}\n"
            f"• Временных заявок удалено: {deleted_pending}",
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END

    if choice == "🔗 Получить ссылку на таблицу":
        spreadsheet_id = await run_db(get_setting, "google_spreadsheet_id")
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
        spreadsheet_id = await run_db(get_setting, "google_spreadsheet_id", "Не задан")
        email = "Не задан"
        service_account_str = await run_db(get_setting, "google_service_account")
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

DEPT_MANAGE_KEYBOARD = ReplyKeyboardMarkup(
    [["➕ Добавить отдел", "➖ Удалить отдел"], ["❌ Назад"]],
    resize_keyboard=True
)

async def department_manage_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    departments = await run_db(get_all_departments)
    listing = "\n".join(f"  • {d}" for d in departments)
    await update.message.reply_text(
        f"🏢 <b>Текущие отделы:</b>\n{listing}\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=DEPT_MANAGE_KEYBOARD
    )
    return ASK_DEPT_ACTION

async def department_manage_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "❌ Назад":
        await update.message.reply_text("⚙️ Настройки бота. Выберите действие:", reply_markup=SETTINGS_KEYBOARD)
        return ASK_SETTINGS_ACTION

    if choice == "➕ Добавить отдел":
        await update.message.reply_text("Введите название нового отдела:", reply_markup=CANCEL_KEYBOARD)
        return ASK_DEPT_ADD_NAME

    if choice == "➖ Удалить отдел":
        departments = await run_db(get_all_departments)
        if not departments:
            await update.message.reply_text("Список отделов пуст.", reply_markup=DEPT_MANAGE_KEYBOARD)
            return ASK_DEPT_ACTION
        buttons = [[d] for d in departments] + [["❌ Назад"]]
        await update.message.reply_text(
            "Выберите отдел для удаления:",
            reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        )
        return ASK_DEPT_DELETE_SELECT

    await update.message.reply_text("Выберите действие кнопкой ниже.", reply_markup=DEPT_MANAGE_KEYBOARD)
    return ASK_DEPT_ACTION

async def department_add_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if name == CANCEL_TEXT:
        return await department_manage_start(update, context)
    if not name or len(name) > 100:
        await update.message.reply_text("Введите корректное название отдела (до 100 символов):")
        return ASK_DEPT_ADD_NAME

    await run_db(add_department, name)
    logger.info(f"[DEPT] Администратор {update.effective_user.id} добавил отдел '{name}'")
    await update.message.reply_text(f"✅ Отдел «{name}» добавлен.", reply_markup=DEPT_MANAGE_KEYBOARD)
    return ASK_DEPT_ACTION

async def department_delete_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if name == CANCEL_TEXT or name == "❌ Назад":
        return await department_manage_start(update, context)

    count = await run_db(count_workers_in_department, name)
    if count > 0:
        await update.message.reply_text(
            f"❌ Нельзя удалить отдел «{name}» — в нём {count} сотрудник(ов).\n"
            f"Сначала переназначьте их в другой отдел (через редактирование сотрудника).",
            reply_markup=DEPT_MANAGE_KEYBOARD
        )
        return ASK_DEPT_ACTION

    deleted = await run_db(delete_department, name)
    if deleted:
        logger.info(f"[DEPT] Администратор {update.effective_user.id} удалил отдел '{name}'")
        await update.message.reply_text(f"✅ Отдел «{name}» удалён.", reply_markup=DEPT_MANAGE_KEYBOARD)
    else:
        await update.message.reply_text(f"❌ Не удалось удалить отдел «{name}».", reply_markup=DEPT_MANAGE_KEYBOARD)
    return ASK_DEPT_ACTION

def _build_department_order_view(ordered: list[str], new_names: list[str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Renders the "📊 Порядок отделов в таблице" screen text + inline keyboard for the
    current working order. Called both when the screen first opens and after every
    up/down button press, so the numbered list and the keyboard always match."""
    lines = []
    if new_names:
        names_str = ", ".join(new_names)
        lines.append(f"⚠️ Обнаружен новый отдел, добавлен в конец списка: {names_str}. Вы можете изменить его позицию ниже.\n")
    lines.append("Текущий порядок отделов:")
    for i, name in enumerate(ordered, 1):
        lines.append(f"{i}. {name}")
    lines.append("\nИзмените порядок кнопками ниже, затем нажмите «✅ Сохранить».")
    text = "\n".join(lines)

    rows = []
    for i, name in enumerate(ordered):
        label = name if len(name) <= 30 else name[:29] + "…"
        btns = []
        if i > 0:
            btns.append(InlineKeyboardButton(f"⬆️ {label}", callback_data=f"deptord_up_{i}"))
        if i < len(ordered) - 1:
            btns.append(InlineKeyboardButton(f"⬇️ {label}", callback_data=f"deptord_down_{i}"))
        rows.append(btns)
    rows.append([
        InlineKeyboardButton("✅ Сохранить", callback_data="deptord_save"),
        InlineKeyboardButton("❌ Отмена", callback_data="deptord_cancel"),
    ])
    return text, InlineKeyboardMarkup(rows)

async def department_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    ordered, new_names = await run_db(get_departments_ordered)
    context.user_data["dept_order_working"] = ordered
    text, kbd = _build_department_order_view(ordered, new_names)
    await update.message.reply_text(text, reply_markup=kbd)
    return ASK_DEPT_ORDER_ACTION

async def department_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.answer("Это действие доступно только отделу контроля складовки.", show_alert=True)
        return ASK_DEPT_ORDER_ACTION

    data = query.data
    ordered = context.user_data.get("dept_order_working")
    if ordered is None:
        return ASK_DEPT_ORDER_ACTION

    if data == "deptord_cancel":
        context.user_data.pop("dept_order_working", None)
        await query.edit_message_text("Изменение порядка отделов отменено — сохранённый порядок не тронут.")
        await query.message.reply_text("⚙️ Настройки бота. Выберите действие:", reply_markup=SETTINGS_KEYBOARD)
        return ASK_SETTINGS_ACTION

    if data == "deptord_save":
        await run_db(save_departments_order, ordered)
        async_sync_gsheets_background()
        context.user_data.pop("dept_order_working", None)
        lines = ["✅ Порядок отделов обновлён.", ""]
        lines += [f"{i}. {name}" for i, name in enumerate(ordered, 1)]
        await query.edit_message_text("\n".join(lines))
        await query.message.reply_text("⚙️ Настройки бота. Выберите действие:", reply_markup=SETTINGS_KEYBOARD)
        return ASK_SETTINGS_ACTION

    if data.startswith("deptord_up_") or data.startswith("deptord_down_"):
        try:
            idx = int(data.rsplit("_", 1)[-1])
        except ValueError:
            return ASK_DEPT_ORDER_ACTION
        swap_with = idx - 1 if data.startswith("deptord_up_") else idx + 1
        if 0 <= idx < len(ordered) and 0 <= swap_with < len(ordered):
            ordered[idx], ordered[swap_with] = ordered[swap_with], ordered[idx]
            context.user_data["dept_order_working"] = ordered
            text, kbd = _build_department_order_view(ordered)
            try:
                await query.edit_message_text(text, reply_markup=kbd)
            except Exception as e:
                logger.error(f"Ошибка при обновлении экрана порядка отделов: {e}")
        return ASK_DEPT_ORDER_ACTION

    return ASK_DEPT_ORDER_ACTION

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
    
    pending = await run_db(get_pending_unregistered_user, worker_id)
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
    position_text = update.message.text.strip()
    if not position_text:
        await update.message.reply_text("Должность не может быть пустой. Введите должность сотрудника:", reply_markup=CANCEL_KEYBOARD)
        return ASK_POSITION
    context.user_data["position"] = clean_position(position_text)
    departments = await run_db(get_all_departments)
    buttons = [[d] for d in departments]
    buttons.append(["➕ Другой (ввести вручную)"])
    buttons.append(["❌ Отмена"])
    await update.message.reply_text(
        "Выберите отдел сотрудника (или введите новый вручную):",
        reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )
    return ASK_WORKER_DEPARTMENT

async def add_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "➕ Другой (ввести вручную)":
        await update.message.reply_text("Введите название нового отдела:", reply_markup=CANCEL_KEYBOARD)
        context.user_data["awaiting_new_department_name"] = True
        return ASK_WORKER_DEPARTMENT

    if context.user_data.pop("awaiting_new_department_name", False):
        if not text or len(text) > 100:
            await update.message.reply_text("Введите корректное название отдела:")
            context.user_data["awaiting_new_department_name"] = True
            return ASK_WORKER_DEPARTMENT
        await run_db(add_department, text)

    context.user_data["object_id"] = text
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

    next_order = await run_db(get_next_sort_order, context.user_data["position"])
    await run_db(
        upsert_worker,
        telegram_id=context.user_data["new_worker_id"],
        last_name=context.user_data["last_name"],
        first_name=context.user_data["first_name"],
        position=context.user_data["position"],
        group_id=context.user_data["group_id"],
        schedule=context.user_data["schedule"],
        needs_daily_fact=(raw == "да"),
        sort_order=next_order,
        object_id=context.user_data.get("object_id", "Основной"),
    )
    await run_db(delete_pending_unregistered_user, context.user_data["new_worker_id"])
    await fetch_and_save_group_name(context.bot, context.user_data["group_id"])

    # LOGIC FIX: prefer the department's configured group (/set_object_group) over the
    # group ID typed in during this flow, same resolution used everywhere else now.
    new_worker_row = await run_db(get_worker, context.user_data["new_worker_id"])
    target_group_id = await run_db(get_worker_target_group, new_worker_row)
    try:
        notify_msg = (
            f"✅ В чат успешно добавлен новый сотрудник.\n"
            f"{context.user_data['last_name']} {context.user_data['first_name']}"
        )
        await context.bot.send_message(chat_id=target_group_id, text=notify_msg)
    except Exception:
        pass

    new_worker_id = context.user_data["new_worker_id"]
    if new_worker_id > 0:
        try:
            await context.bot.send_message(
                chat_id=new_worker_id,
                text="✅ Вы успешно добавлены в базу!\nТеперь вы можете отправлять видео-отчёты контролю."
            )
        except Exception as e:
            logger.warning(f"[ADD] Не удалось уведомить сотрудника {new_worker_id} о добавлении: {e}")

    async_sync_gsheets_background()
    await update.message.reply_text("Сотрудник успешно добавлен в базу!", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def delete_worker_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    rows = await run_db(get_all_workers)
    if not rows:
        await update.message.reply_text("В базе нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел сотрудника для удаления:", reply_markup=kbd)
    return ASK_REMOVE_DEPARTMENT

async def delete_worker_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dept = update.message.text.strip()
    rows = await run_db(get_workers_by_object_id, dept)
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
            await run_db(delete_worker, worker["telegram_id"])
            async_sync_gsheets_background()
            await update.message.reply_text(f"✅ Сотрудник {worker['last_name']} {worker['first_name']} успешно удален.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Удаление сотрудника отменено.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def department_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    rows = await run_db(get_all_workers)
    if not rows:
        await update.message.reply_text("В базе нет сотрудников.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    kbd, _ = positions_keyboard(rows)
    await update.message.reply_text("Выберите отдел для просмотра:", reply_markup=kbd)
    return ASK_DEPARTMENT

async def department_workers_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dept = update.message.text.strip()
    rows = await run_db(get_workers_by_object_id, dept)
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
        cur_val = clean_position(worker.get(field)) if field == "position" else worker.get(field)
        await update.message.reply_text(f"Сейчас: {cur_val}\n{prompt}", reply_markup=CANCEL_KEYBOARD)
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
    if field == "position":
        if not raw:
            await update.message.reply_text("Должность не может быть пустой. Введите новую должность:")
            return ASK_EDIT_VALUE
        value = clean_position(raw)
    elif field == "group_id":
        if not raw.lstrip("-").isdigit():
            await update.message.reply_text("Введите число.")
            return ASK_EDIT_VALUE
        value = DEFAULT_GROUP_ID if int(raw) == 0 else int(raw)
    elif field == "sort_order":
        if not raw.isdigit():
            await update.message.reply_text("Введите число.")
            return ASK_EDIT_VALUE
        value = int(raw)

    await run_db(update_worker_field, worker["telegram_id"], field, value)
    async_sync_gsheets_background()
    await update.message.reply_text("✅ Изменения сохранены.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_worker_schedule_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().upper()
    if raw not in SCHEDULES:
        return ASK_EDIT_SCHEDULE
    worker = context.user_data.get("edit_worker")
    await run_db(update_worker_field, worker["telegram_id"], "schedule", raw)
    async_sync_gsheets_background()
    await update.message.reply_text("✅ График обновлён.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_worker_daily_fact_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        return ASK_EDIT_DAILY_FACT
    worker = context.user_data.get("edit_worker")
    await run_db(update_worker_field, worker["telegram_id"], "needs_daily_fact", raw == "да")
    async_sync_gsheets_background()
    await update.message.reply_text("✅ Обновлено.", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return ConversationHandler.END

async def edit_worker_status_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip().lower()
    if raw not in ("да", "нет"):
        return ASK_EDIT_STATUS_WORK
    if raw == "да":
        worker = context.user_data.get("edit_worker")
        await run_db(update_worker_field, worker["telegram_id"], "is_active", True)
        async_sync_gsheets_background()
        await update.message.reply_text("✅ Сотрудник отмечен как работающий.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text(
        "На сколько дней сотрудник не работает (считая сегодня)? Введите число:",
        reply_markup=CANCEL_KEYBOARD
    )
    return ASK_NOT_WORKING_DAYS

async def edit_worker_not_working_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if not raw.isdigit() or int(raw) <= 0:
        await update.message.reply_text("Введите положительное число дней.")
        return ASK_NOT_WORKING_DAYS
    context.user_data["not_working_days"] = int(raw)
    await update.message.reply_text("Укажите причину (например: отпуск, больничный):", reply_markup=CANCEL_KEYBOARD)
    return ASK_NOT_WORKING_REASON

async def edit_worker_not_working_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    worker = context.user_data.get("edit_worker")
    days = context.user_data.get("not_working_days")
    if not worker or not days:
        await update.message.reply_text("Сессия истекла, начните заново.", reply_markup=MAIN_MENU)
        context.user_data.clear()
        return ConversationHandler.END

    now = now_local()
    for i in range(days):
        d = now + dt_module.timedelta(days=i)
        await run_db(
            save_report,
            telegram_id=worker["telegram_id"],
            report_date=d.strftime("%Y-%m-%d"),
            report_type="not_working",
            slot_time=None,
            received_at=now.strftime("%H:%M:%S"),
            is_ok=True,
            is_late=False,
            format_comment=reason,
            required_action="Не работает",
            raw_text=f"Отмечено отделом контроля складовки: не работает. Причина: {reason}"
        )
    async_sync_gsheets_background()
    await update.message.reply_text(
        f"✅ Сотрудник отмечен как не работающий на {days} дн. (с сегодняшнего дня). Причина: {reason}",
        reply_markup=MAIN_MENU
    )
    context.user_data.clear()
    return ConversationHandler.END

async def import_workers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    kbd = ReplyKeyboardMarkup(
        [["📤 Скачать текущий список сотрудников"], ["📥 Загрузить обновления из файла"], ["❌ Отмена"]],
        resize_keyboard=True
    )
    await update.message.reply_text(
        "📁 <b>Импорт/экспорт сотрудников через Excel</b>\n\n"
        "1️⃣ Сначала скачайте файл со ВСЕМИ текущими сотрудниками — так вы не потеряете уже внесённые данные.\n"
        "2️⃣ Отредактируйте его в Excel/Google Таблицах (можно менять данные, добавлять новые строки).\n"
        "3️⃣ Отправьте изменённый файл боту — он обновит базу.\n\n"
        "Выберите действие:",
        reply_markup=kbd,
        parse_mode="HTML"
    )
    return ASK_IMPORT_ACTION

async def import_workers_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if choice == "📤 Скачать текущий список сотрудников":
        await update.message.reply_text("⏳ Формирую файл...", reply_markup=CANCEL_KEYBOARD)
        try:
            data = await run_db(export_workers_to_excel)
            bio = io.BytesIO(data)
            bio.name = "workers.xlsx"
            await update.message.reply_document(
                document=bio,
                filename="workers.xlsx",
                caption=(
                    "📋 <b>Текущий список сотрудников</b>\n\n"
                    "Как редактировать:\n"
                    "• 1-я строка (тёмно-синяя) — названия колонок, не трогайте.\n"
                    "• 2-я строка (голубая, курсив) — подсказка, что писать в колонке ниже, тоже не трогайте.\n"
                    "• Столбец <b>Telegram ID</b> отмечен розовым «не менять» — если меняете существующего "
                    "сотрудника, ID должен остаться прежним, иначе появится дубликат.\n"
                    "• Чтобы добавить нового сотрудника — впишите новую строку снизу.\n"
                    "• Столбец «Статус» — просто впишите словом: Работает / Отпуск / Больничный.\n\n"
                    "Когда закончите — пришлите этот же файл боту через «📥 Загрузить обновления из файла»."
                ),
                reply_markup=MAIN_MENU,
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка при формировании файла: {e}", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "📥 Загрузить обновления из файла":
        await update.message.reply_text(
            "📎 Просто отправьте файл Excel (.xlsx) со списком сотрудников сюда, в личные сообщения боту — "
            "он сам распознает файл, сравнит с текущей базой и покажет, что именно изменится, ДО того как "
            "что-либо применится.\n\n"
            "Если вы ещё не скачивали текущий список — сначала выберите «📤 Скачать текущий список сотрудников», "
            "чтобы не потерять данные.",
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END
    return ConversationHandler.END

def _format_worker_line(w: dict) -> str:
    return f"- {w['last_name']} {w['first_name']} ({w['position']}, объект {w['object_id']})"

def _build_workers_sync_summary(diff: dict) -> str:
    lines = ["📋 Обнаружены изменения в базе сотрудников:\n"]
    if diff["new"]:
        lines.append(f"🆕 Новые сотрудники ({len(diff['new'])}):")
        for w in diff["new"]:
            lines.append(_format_worker_line(w))
        lines.append("")
    if diff["changed"]:
        lines.append(f"✏️ Изменённые поля ({len(diff['changed'])}):")
        for item in diff["changed"]:
            w = item["new"]
            name = f"{w['last_name']} {w['first_name']}"
            for label, old_val, new_val in item["diffs"]:
                lines.append(f"- {name}: {label} '{old_val}' → '{new_val}'")
        lines.append("")
    if diff["missing"]:
        lines.append(f"⚠️ Отсутствуют в новом файле, но есть в текущей базе ({len(diff['missing'])}):")
        for w in diff["missing"]:
            lines.append(f"- {w['last_name']} {w['first_name']} (Telegram ID: {w['telegram_id']})")
        lines.append("")
    lines.append("Применить эти изменения?")
    return "\n".join(lines)

async def handle_workers_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Global entry point for the workers-database sync feature - an admin can just send an
    updated workers.xlsx directly in a private chat with the bot at any time, without
    navigating a menu first (the "📥 Импорт сотрудников" menu now only explains this and
    points here). Silently ignores anything that isn't an admin+private+.xlsx combination,
    per the "не мешать другому функционалу бота" requirement - a non-admin's document, a
    group chat document, or a file that isn't .xlsx, is simply none of this feature's
    concern and is left for whatever else might handle it."""
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith(".xlsx"):
        return

    await update.message.reply_text("⏳ Читаю файл и сравниваю с текущей базой...")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"❌ Не удалось скачать файл: {e}")
        return

    workers, errors = await run_db(parse_workers_excel_bytes, file_bytes)
    if errors:
        await update.message.reply_text("❌ Файл не принят.\n" + "\n".join(errors) + "\n\nБаза не изменена.")
        return
    if not workers:
        await update.message.reply_text(
            "⚠️ В файле не найдено ни одной записи с заполненным Telegram ID.\n"
            "Проверьте, что данные начинаются с 3-й строки (после заголовков и подсказки). База не изменена."
        )
        return

    diff = await run_db(compute_workers_diff, workers)
    if not diff["new"] and not diff["changed"] and not diff["missing"]:
        await update.message.reply_text("✅ Изменений не обнаружено — файл полностью совпадает с текущей базой.")
        return

    # Kept per-admin in context.user_data (same convention as other short-lived "pending
    # admin action" state elsewhere in this project) rather than a new DB table - this is a
    # single admin's own confirm-or-cancel decision, not data other users/admins need to see.
    context.user_data["pending_workers_sync"] = diff

    summary = _build_workers_sync_summary(diff)
    kbd = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Применить все изменения", callback_data="wsync_apply_all")],
        [InlineKeyboardButton("✏️ Применить только новых и изменённых", callback_data="wsync_apply_no_delete")],
        [InlineKeyboardButton("❌ Отменить", callback_data="wsync_cancel")],
    ])
    await update.message.reply_text(summary, reply_markup=kbd)

async def handle_workers_sync_callback(query, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Handles the 3 buttons on the summary message built by handle_workers_file_upload.
    Admin rights are already checked by the caller (handle_callback_query's global is_admin
    gate) before this is ever reached."""
    pending = context.user_data.get("pending_workers_sync")
    if not pending:
        await query.answer("Сессия синхронизации устарела или уже обработана. Отправьте файл заново.", show_alert=True)
        return

    if data == "wsync_cancel":
        context.user_data.pop("pending_workers_sync", None)
        await query.edit_message_text("❌ Синхронизация отменена. База не изменена.")
        return

    include_deletions = (data == "wsync_apply_all")
    missing_ids = [w["telegram_id"] for w in pending["missing"]] if include_deletions else []
    context.user_data.pop("pending_workers_sync", None)

    ts = now_local().strftime("%Y%m%d_%H%M%S")
    backup_path = f"pre_workers_sync_backup_{ts}.db"
    try:
        await run_db(backup_database_to_file, backup_path)
    except Exception as e:
        logger.error(f"[workers_sync] Не удалось создать резервную копию перед применением изменений: {e}")
        await query.edit_message_text("❌ Не удалось сделать резервную копию перед применением изменений — изменения НЕ применены.")
        return

    try:
        result = await run_db(apply_workers_sync, pending["new"], pending["changed"], missing_ids)
    except Exception as e:
        logger.error(f"[workers_sync] Ошибка при применении изменений, откат к резервной копии {backup_path}: {e}")
        try:
            await run_db(restore_database_from_file, backup_path)
            await query.edit_message_text(
                "❌ Ошибка при обновлении базы. Изменения отменены, база возвращена к предыдущему состоянию.\n"
                "Детали: техническая ошибка при записи в базу данных."
            )
        except Exception as restore_err:
            logger.error(f"[workers_sync] КРИТИЧНО: не удалось откатить базу после сбоя применения: {restore_err}")
            await query.edit_message_text(
                "❌ Критическая ошибка при обновлении базы, и автоматический откат тоже не удался.\n"
                f"Резервная копия сохранена на сервере в файле {backup_path} — обратитесь к разработчику."
            )
        return

    async_sync_gsheets_background()

    try:
        with open(backup_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.from_user.id, document=f, filename=backup_path,
                caption="🗄 Резервная копия базы сотрудников перед этой синхронизацией (на случай, если понадобится откатиться вручную)."
            )
    except Exception as e:
        logger.error(f"[workers_sync] Не удалось отправить резервную копию админу: {e}")
    finally:
        if os.path.exists(backup_path):
            os.remove(backup_path)

    await query.edit_message_text(
        f"✅ База сотрудников обновлена.\n"
        f"Добавлено: {result['added']}, изменено: {result['changed']}, удалено: {result['deleted']}."
    )

async def _notify_admins_registration_issue(context, update, reason, entered_last_name, matches=None, phone=None):
    user = update.effective_user
    now_str = now_local().strftime("%d.%m.%Y %H:%M:%S")

    lines = ["🆕 <b>Обращение по регистрации</b>"]
    if reason == "not_found":
        lines.append("Сотрудник не найден в базе данных.")
    elif reason == "already_registered":
        lines.append("⚠️ Попытка регистрации на фамилию, уже привязанную к другому Telegram-аккаунту.")
    elif reason == "contact_shared":
        lines.append("📱 Пользователь поделился номером телефона по предыдущему обращению.")

    lines.append(f"Введённая фамилия: <b>{html.escape(entered_last_name)}</b>")
    lines.append(f"Telegram ID: <code>{user.id}</code>")
    lines.append(f"Username: {('@' + user.username) if user.username else 'нет'}")
    tg_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "не указано"
    lines.append(f"Имя в Telegram: {html.escape(tg_name)}")
    if phone:
        lines.append(f"Телефон: {html.escape(phone)}")
    lines.append(f"Дата обращения: {now_str}")
    if user.username:
        lines.append(f"Профиль: https://t.me/{user.username}")
    else:
        lines.append(f"Профиль: tg://user?id={user.id} (откроется, если клиент поддерживает)")
    if matches:
        lines.append("\nУже зарегистрированы под этой фамилией:")
        for m in matches:
            lines.append(f"  • {m['last_name']} {m['first_name']} (ID: {m['telegram_id']})")
    lines.append("\nЧтобы добавить сотрудника: «➕ Добавить сотрудника» → укажите Telegram ID выше.")

    text = "\n".join(lines)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"[REG] Не удалось уведомить администратора {admin_id}: {e}")

async def _show_registration_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, candidate: dict):
    context.user_data["reg_candidate"] = candidate
    fio = f"{candidate['last_name']} {candidate['first_name']}"
    kbd = ReplyKeyboardMarkup([["✅ Да, это я", "❌ Нет, не я"]], resize_keyboard=True)
    await update.message.reply_text(
        f"✅ Отлично! Мы нашли Вашу учётную запись.\n\n"
        f"👤 <b>{html.escape(fio)}</b>\n"
        f"💼 Должность: {html.escape(clean_position(candidate['position']))}\n"
        f"🏢 Отдел: {html.escape(str(candidate['object_id'] or 'Основной'))}\n\n"
        f"<b>Шаг 2 из 3</b>\n\n"
        f"Сейчас осталось подтвердить, что это Вы, и регистрация завершится.\n"
        f"Внизу экрана появятся две кнопки. Нажмите «✅ Да, это я», если данные верны.",
        parse_mode="HTML",
        reply_markup=kbd
    )
    return ASK_REG_CONFIRM

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type != "private": return ConversationHandler.END
    user_id = update.effective_user.id
    logger.info(f"[REG] Начало регистрации: user_id={user_id}, username=@{update.effective_user.username}")

    if is_admin(user_id):
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    worker = await run_db(get_worker, user_id)
    if worker:
        logger.info(f"[REG] user_id={user_id} уже зарегистрирован как {worker['last_name']} {worker['first_name']} — повторная регистрация не требуется")
        await update.message.reply_text(
            f"Вы уже зарегистрированы как <b>{html.escape(worker['last_name'])} {html.escape(worker['first_name'])}</b>.\n"
            f"Отправьте видеоотчёт, когда он будет готов.",
            parse_mode="HTML",
            reply_markup=menu_for_user(user_id, chat_type)
        )
        return ConversationHandler.END

    context.user_data.clear()
    await update.message.reply_text(
        "Пожалуйста, введите Вашу фамилию латинскими буквами.\n\n"
        "Например: <b>Ivanov</b>\n\n"
        "Затем введите Вашу фамилию в поле сообщения и нажмите кнопку «Отправить».",
        parse_mode="HTML", reply_markup=CANCEL_KEYBOARD
    )
    return ASK_REG_LAST_NAME

async def register_lastname_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if text == CANCEL_TEXT:
        logger.info(f"[REG] user_id={user_id} отменил регистрацию на этапе ввода фамилии")
        await update.message.reply_text("Регистрация отменена.", reply_markup=menu_for_user(user_id))
        context.user_data.clear()
        return ConversationHandler.END

    if len(text) < 2 or len(text) > 50 or text.isdigit():
        await update.message.reply_text(
            "❌ Пожалуйста, введите фамилию буквами (не цифрами), не менее 2 букв.\n\n"
            "Например: Popescu\n\n"
            "Попробуйте ещё раз — введите фамилию в поле сообщения."
        )
        return ASK_REG_LAST_NAME

    logger.info(f"[REG] user_id={user_id} ищет фамилию '{text}'")
    try:
        unregistered = await run_db(find_unregistered_workers_by_lastname, text)
    except Exception as e:
        logger.error(f"[REG] Ошибка поиска по фамилии '{text}' для user_id={user_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка поиска. Попробуйте ещё раз позже.", reply_markup=menu_for_user(user_id))
        context.user_data.clear()
        return ConversationHandler.END

    if unregistered:
        context.user_data["reg_last_name_query"] = text
        if len(unregistered) == 1:
            return await _show_registration_confirm(update, context, dict(unregistered[0]))
        context.user_data["candidate_workers"] = [dict(w) for w in unregistered]
        buttons = [[f"{w['last_name']} {w['first_name']} ({clean_position(w['position'])})"] for w in unregistered]
        buttons.append([CANCEL_TEXT])
        await update.message.reply_text(
            "🔍 Мы нашли несколько сотрудников с такой фамилией.\n\n"
            "Найдите себя в списке ниже и нажмите на кнопку со своим именем.",
            reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        )
        return ASK_REG_FIRST_NAME

    # Not among the unregistered placeholders — check whether it's already claimed by someone else
    already_registered = await run_db(find_registered_workers_by_lastname, text)
    if already_registered:
        logger.warning(
            f"[REG] user_id={user_id} (@{update.effective_user.username}) попытался зарегистрироваться на "
            f"фамилию '{text}', уже привязанную к другому Telegram ID — возможная ошибка или дублирование"
        )
        await _notify_admins_registration_issue(context, update, "already_registered", text, matches=already_registered)
        await update.message.reply_text(
            "⚠️ Сотрудник с такой фамилией уже зарегистрирован в системе.\n\n"
            "Если это ошибка — не переживайте, отдел контроля складовки уже получил уведомление и свяжется с Вами.",
            reply_markup=menu_for_user(user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Not found anywhere in the database
    logger.warning(f"[REG] user_id={user_id} (@{update.effective_user.username}) не найден в базе по фамилии '{text}'")
    context.user_data["reg_last_name_query"] = text
    await _notify_admins_registration_issue(context, update, "not_found", text)
    await update.message.reply_text(
        f"❌ Мы не смогли найти сотрудника с фамилией <b>{html.escape(text)}</b>.\n\n"
        f"Проверьте правильность написания фамилии — используйте латинские буквы.\n"
        f"Например: Popescu\n\n"
        f"Отдел контроля складовки уже получил уведомление о Вашем обращении и добавит Вас в систему.\n\n"
        f"Если хотите, поделитесь номером телефона — это поможет отделу контроля складовки связаться с Вами быстрее. "
        f"Нажмите кнопку «📱 Поделиться контактом» внизу экрана, либо «Пропустить», если не хотите:",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Поделиться контактом", request_contact=True)], ["Пропустить"]],
            resize_keyboard=True
        )
    )
    return ASK_REG_CONTACT

async def register_contact_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    contact = update.message.contact
    if contact and contact.user_id == user_id:
        logger.info(f"[REG] user_id={user_id} поделился номером телефона по обращению без совпадения в базе")
        await _notify_admins_registration_issue(
            context, update, "contact_shared",
            context.user_data.get("reg_last_name_query", "?"),
            phone=contact.phone_number
        )
        await update.message.reply_text(
            "✅ Спасибо! Ваш номер телефона передан в отдел контроля складовки.\n\n"
            "Отдел контроля складовки свяжется с Вами и добавит Вас в систему.",
            reply_markup=menu_for_user(user_id)
        )
    else:
        await update.message.reply_text(
            "Хорошо. Отдел контроля складовки свяжется с Вами и добавит Вас в систему.",
            reply_markup=menu_for_user(user_id)
        )
    context.user_data.clear()
    return ConversationHandler.END

async def register_firstname_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    if text == CANCEL_TEXT:
        logger.info(f"[REG] user_id={user_id} отменил выбор из списка кандидатов")
        await update.message.reply_text("Регистрация отменена.", reply_markup=menu_for_user(user_id))
        context.user_data.clear()
        return ConversationHandler.END

    candidates = context.user_data.get("candidate_workers", [])
    matched_candidate = None
    for c in candidates:
        label = f"{c['last_name']} {c['first_name']} ({clean_position(c['position'])})"
        if text.strip().lower() == label.lower():
            matched_candidate = c
            break
    if not matched_candidate:
        await update.message.reply_text(
            "❌ Пожалуйста, нажмите на одну из кнопок со списком имён внизу экрана — не вводите текст вручную."
        )
        return ASK_REG_FIRST_NAME

    return await _show_registration_confirm(update, context, matched_candidate)

async def register_confirm_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    candidate = context.user_data.get("reg_candidate")

    if text == "✅ Да, это я" and candidate:
        try:
            now_str = now_local().strftime("%Y-%m-%d %H:%M:%S")
            await run_db(bind_worker_id, candidate["telegram_id"], user_id, now_str)
        except Exception as e:
            logger.error(f"[REG] Ошибка привязки user_id={user_id} к профилю ID={candidate.get('telegram_id')}: {e}")
            await update.message.reply_text(
                "❌ Произошла ошибка при регистрации.\n\n"
                "Пожалуйста, нажмите кнопку «🔑 Начать регистрацию» внизу экрана, чтобы попробовать ещё раз, "
                "или обратитесь в отдел контроля складовки.",
                reply_markup=ReplyKeyboardMarkup([["🔑 Начать регистрацию"]], resize_keyboard=True)
            )
            context.user_data.clear()
            return ConversationHandler.END

        w_fio = f"{candidate['last_name']} {candidate['first_name']}"
        logger.info(f"[REG] Успешная регистрация: user_id={user_id} привязан к профилю '{w_fio}' (был временный ID {candidate['telegram_id']})")
        await update.message.reply_text(
            f"<b>Шаг 3 из 3</b>\n\n"
            f"✅ Регистрация успешно завершена!\n"
            f"Вы привязаны к профилю: <b>{html.escape(w_fio)}</b>.\n\n"
            f"Теперь Вы можете пользоваться ботом.\n\n"
            f"Перед каждым временем сдачи отчёта Вы будете автоматически получать напоминание.\n"
            f"После получения напоминания просто отправьте видео в этот чат — дождитесь результата проверки.",
            parse_mode="HTML",
            reply_markup=menu_for_user(user_id)
        )
        context.user_data.clear()
        return ConversationHandler.END

    logger.info(f"[REG] user_id={user_id} не подтвердил найденного кандидата ({candidate.get('last_name') if candidate else '?'})")
    context.user_data.pop("reg_candidate", None)
    await update.message.reply_text(
        "Хорошо, попробуем ещё раз.\n\n"
        "Введите Вашу фамилию латинскими буквами в поле сообщения внизу экрана, либо обратитесь в отдел контроля складовки:",
        reply_markup=CANCEL_KEYBOARD
    )
    return ASK_REG_LAST_NAME

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip() if update.message.text else ""
    context.user_data.clear()
    
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    
    if text in (CANCEL_TEXT, "❌ Отмена", "❌ Назад"):
        await update.message.reply_text("Действие отменено.", reply_markup=menu_for_user(user_id, chat_type))
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
        ["📋 Сводка сейчас", "📅 Сводка за дату"],
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
            data = await run_db(export_reports_to_excel)
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
        spreadsheet_id = await run_db(get_setting, "google_spreadsheet_id")
        creds_str = await run_db(get_setting, "google_service_account")
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

    if choice == "📋 Сводка сейчас":
        from bot import build_missing_status_summary_text
        now = now_local()
        date_str = now.strftime("%Y-%m-%d")
        summary_text = await build_missing_status_summary_text(date_str, now)
        await update.message.reply_text(summary_text, parse_mode="HTML", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    if choice == "📅 Сводка за дату":
        await update.message.reply_text(
            "Введите дату в формате ДД.ММ.ГГГГ (например, 01.07.2026):",
            reply_markup=CANCEL_KEYBOARD
        )
        return ASK_SUMMARY_DATE

    return ASK_EXPORT_TYPE

async def export_summary_date_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == CANCEL_TEXT:
        await update.message.reply_text("Действие отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    try:
        target_date = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        await update.message.reply_text("Некорректный формат. Введите дату как ДД.ММ.ГГГГ (например, 01.07.2026):")
        return ASK_SUMMARY_DATE

    today = now_local().date()
    if target_date > today:
        await update.message.reply_text("Эта дата ещё не наступила. Введите сегодняшнюю дату или более раннюю:")
        return ASK_SUMMARY_DATE

    from bot import build_missing_status_summary_text
    date_str = target_date.strftime("%Y-%m-%d")
    now_for_calc = now_local() if target_date == today else None
    summary_text = await build_missing_status_summary_text(date_str, now_for_calc)
    await update.message.reply_text(summary_text, parse_mode="HTML", reply_markup=MAIN_MENU)
    return ConversationHandler.END

async def alert_time_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_check(update): return ConversationHandler.END
    times = await run_db(get_scheduled_times) or ["19:00"]
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
        
    await run_db(save_scheduled_times, valid_times)
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
        
    workers = await run_db(get_all_workers)
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
        
    await run_db(set_setting, "google_spreadsheet_id", sheet_id)
    
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
            
        await run_db(set_setting, "google_service_account", content)

        email = creds_dict.get("client_email")
        spreadsheet_id = await run_db(get_setting, "google_spreadsheet_id")
        
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
