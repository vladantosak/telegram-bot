import os
import logging
import asyncio
import json
from datetime import datetime
import datetime as dt_module
from logging.handlers import RotatingFileHandler
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)

from db import (
    init_db, get_worker, get_all_workers, get_workers_by_position, get_db, run_db,
    DEFAULT_GROUP_ID, LATE_THRESHOLD_MIN, LOCAL_TZ, SCHEDULES, SCHEDULE_A,
    is_admin, ADMIN_IDS, save_scheduled_times, get_scheduled_times,
    get_group_name_async, now_local
)

from report_handlers import (
    handle_report, process_media_batch, menu_for_user
)

from admin_handlers import (
    MAIN_MENU, CANCEL_KEYBOARD, CANCEL_TEXT, add_worker_start, add_worker_id,
    add_worker_lastname, add_worker_firstname, add_worker_position,
    add_worker_group, add_worker_schedule, add_worker_needs_daily_fact,
    delete_worker_start, delete_worker_department, delete_worker_finish,
    delete_worker_confirm, department_workers_start, department_workers_show,
    import_workers_start, import_workers_action, import_workers_file,
    register_start, register_lastname_received, register_firstname_received,
    settings_start, settings_action, cancel,
    ASK_WORKER_ID, ASK_LASTNAME, ASK_FIRSTNAME, ASK_POSITION, ASK_GROUP,
    ASK_SCHEDULE, ASK_NEEDS_DAILY_FACT, ASK_REMOVE_DEPARTMENT, ASK_REMOVE_WORKER,
    ASK_DEPARTMENT, ASK_REG_LAST_NAME, ASK_REG_FIRST_NAME, ASK_SETTINGS_ACTION,
    ASK_CONFIRM_DELETE, ASK_IMPORT_ACTION, ASK_IMPORT_FILE, ASK_GSHEETS_URL,
    ASK_GSHEETS_CREDS
)

# Set up logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# Lock per user to prevent race condition in media batching / reports
user_locks = {}

def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    return user_locks[user_id]

def find_nearest_slot(schedule: list[str], now: datetime):
    current_minutes = now.hour * 60 + now.minute
    nearest_slot = None
    nearest_diff = None

    for slot in schedule:
        hour, minute = map(int, slot.split(":"))
        slot_minutes = hour * 60 + minute
        diff = abs(current_minutes - slot_minutes)
        if nearest_diff is None or diff < nearest_diff:
            nearest_diff = diff
            nearest_slot = slot

    is_late = nearest_diff is not None and nearest_diff > LATE_THRESHOLD_MIN
    return nearest_slot, is_late

# Summary Schedules
def reschedule_summary_jobs(application: Application):
    job_queue = application.job_queue
    if not job_queue:
        logger.warning("JobQueue is not available.")
        return

    for job in job_queue.get_jobs_by_name("daily_summary"):
        job.schedule_removal()
    for job in job_queue.get_jobs_by_name("weekly_monthly_summary"):
        job.schedule_removal()

    times = get_scheduled_times() or ["19:00"]
    for t_str in times:
        try:
            hour, minute = map(int, t_str.split(":"))
            time_obj = dt_module.time(hour=hour, minute=minute, tzinfo=LOCAL_TZ)
            job_queue.run_daily(
                scheduled_summary_callback,
                time=time_obj,
                days=(0, 1, 2, 3, 4, 5, 6),
                name="daily_summary"
            )
            logger.info(f"Запланирована сводка на {t_str}")
        except Exception as e:
            logger.error(f"Ошибка при планировании сводки на {t_str}: {e}")

async def scheduled_summary_callback(context: ContextTypes.DEFAULT_TYPE):
    from admin_handlers import MAIN_MENU # local import
    now = now_local()
    date_str = now.strftime("%Y-%m-%d")
    
    # Generate daily summary text
    from db import calculate_worker_stats
    conn = get_db()
    workers = conn.execute("SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name").fetchall()
    conn.close()
    
    summary_text = f"⏰ Автоматическая запланированная сводка за {date_str}:\n\n"
    for w in workers:
        if not w["is_active"]:
            continue
        summary_text += f"• {w['last_name']} {w['first_name']} ({w['position']})\n"

    # Send summary to group chat and admins
    from db import DEFAULT_GROUP_ID
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=summary_text)
        except Exception:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id
    if is_admin(user_id) and chat_type == "private":
        await update.message.reply_text("Привет! Выберите действие кнопкой ниже.", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Привет! Отправьте видеоотчет, когда он будет готов.", reply_markup=menu_for_user(user_id, chat_type))

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) and get_worker(user_id) is None:
        await update.message.reply_text("Эта команда доступна только сотрудникам и администраторам.")
        return
    await update.message.reply_text("🏆 Рейтинг лучших сотрудников формируется в Google Таблице во вкладке 'Аналитика'.")

async def set_object_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Эта команда доступна только администраторам.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Пример использования: `/set_object_group ИмяОбъекта IDГруппы`", parse_mode="Markdown")
        return
    from db import save_object_group
    obj_name = args[0]
    group_id = int(args[1]) if len(args) > 1 else update.effective_chat.id
    save_object_group(obj_name, group_id)
    await update.message.reply_text(f"✅ Объект *{obj_name}* успешно привязан к группе `{group_id}`!", parse_mode="Markdown")

async def cmd_quiet_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    from db import set_quiet_mode, is_quiet_mode_enabled
    curr = is_quiet_mode_enabled()
    set_quiet_mode(not curr)
    status_label = "ВКЛЮЧЕН" if not curr else "ВЫКЛЮЧЕН"
    await update.message.reply_text(f"🔇 Тихий режим {status_label}.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("ℹ️ Полная аналитика по отчетам выгружается в Google Таблицу на лист 'Аналитика'.")

async def post_init(application: Application):
    reschedule_summary_jobs(application)

def main():
    init_db()
    TOKEN = os.environ.get("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN is missing!")
        return

    application = Application.builder().token(TOKEN).post_init(post_init).build()

    # Generic handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("top", top_command))
    application.add_handler(CommandHandler("set_object_group", set_object_group_command))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("quiet_mode", cmd_quiet_mode))

    # Add worker ConversationHandler
    add_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить сотрудника$"), add_worker_start)],
        states={
            ASK_WORKER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_id)],
            ASK_LASTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_lastname)],
            ASK_FIRSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_firstname)],
            ASK_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_position)],
            ASK_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_group)],
            ASK_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_schedule)],
            ASK_NEEDS_DAILY_FACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_worker_needs_daily_fact)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )
    application.add_module_handler(add_handler)

    # Delete worker ConversationHandler
    delete_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить сотрудника$"), delete_worker_start)],
        states={
            ASK_REMOVE_DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_worker_department)],
            ASK_REMOVE_WORKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_worker_finish)],
            ASK_CONFIRM_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_worker_confirm)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )
    application.add_module_handler(delete_handler)

    # View Department workers
    view_dept_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🏢 Сотрудники отдела$"), department_workers_start)],
        states={
            ASK_DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, department_workers_show)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )
    application.add_module_handler(view_dept_handler)

    # Import workers
    import_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📥 Импорт сотрудников$"), import_workers_start)],
        states={
            ASK_IMPORT_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, import_workers_action)],
            ASK_IMPORT_FILE: [MessageHandler(filters.Document.ALL, import_workers_file)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )
    application.add_module_handler(import_handler)

    # Settings panel
    settings_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚙️ Настройки бота$"), settings_start)],
        states={
            ASK_SETTINGS_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, settings_action)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )
    application.add_module_handler(settings_handler)

    # Registration Handler
    registration_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔑 Начать регистрацию$"), register_start)],
        states={
            ASK_REG_LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_lastname_received)],
            ASK_REG_FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_firstname_received)],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{CANCEL_TEXT}$"), cancel)],
    )
    application.add_module_handler(registration_handler)

    # Global message report catcher
    application.add_handler(MessageHandler(
        filters.VOICE | filters.VIDEO | filters.VIDEO_NOTE | filters.TEXT & ~filters.COMMAND,
        handle_report
    ))

    logger.info("Bot main listener initialized successfully...")
    application.run_polling()

if __name__ == "__main__":
    main()
