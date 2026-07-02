import os
import html
import re
import asyncio
import logging
from datetime import datetime
import datetime as dt_module
from telegram import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, Update
from telegram.ext import ContextTypes, ConversationHandler

def parse_video_comments(format_comment_str: str) -> list[dict]:
    items = []
    if not format_comment_str:
        return items
    parts = format_comment_str.split("; ")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^Видео\s+(\d+):\s*(ОК|не\s+ОК|НЕ\s+ОК)(?:\s*-\s*|\s+)?(.*)$", part, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            status_str = m.group(2).strip().lower()
            is_ok = "не" not in status_str
            comment = m.group(3).strip()
            items.append({
                "num": num,
                "is_ok": is_ok,
                "comment": comment,
                "full": part
            })
        else:
            items.append({
                "num": len(items) + 1,
                "is_ok": "не ок" not in part.lower(),
                "comment": part,
                "full": part
            })
    return items

def rebuild_format_comment(video_items: list[dict]) -> str:
    parts = []
    for item in video_items:
        status_label = "ОК" if item["is_ok"] else "не ОК"
        comment_part = f" - {item['comment']}" if item["comment"] else ""
        parts.append(f"Видео {item['num']}: {status_label}{comment_part}")
    return "; ".join(parts)

from db import (
    get_worker, get_submitted_status_slots, get_existing_report_row,
    save_report, update_report_text_and_ai, set_report_group_message,
    get_report_group_message, add_report_media, get_report_media,
    delete_report_media_rows, get_group_name_async, cancel_not_working,
    get_pending_unregistered_user, save_pending_unregistered_user,
    delete_pending_unregistered_user, bind_worker_id, async_sync_gsheets_background,
    get_db, run_db, is_admin, ADMIN_IDS, DEFAULT_GROUP_ID, SCHEDULES, SCHEDULE_A,
    LATE_THRESHOLD_MIN, now_local, is_quiet_mode_enabled, get_worker_target_group,
    get_group_name, get_pending_reason_requests, resolve_pending_reason_requests,
    get_missed_status_reason, check_and_update_remark_alert_threshold, get_recent_remarks
)

REMARK_REQUIRED_ACTION_TEXT = (
    "Сотруднику сделано замечание по выявленным нарушениям. "
    "Необходимо проконтролировать исправление ошибок при следующих сдачах статусов."
)

async def notify_admins_if_remark_threshold_crossed(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, worker_name: str):
    new_total = await run_db(check_and_update_remark_alert_threshold, telegram_id)
    if new_total is None:
        return

    recent = await run_db(get_recent_remarks, telegram_id, 5)
    lines = [
        "⚠️ <b>Требуется внимание.</b>\n",
        f"У сотрудника <b>{html.escape(worker_name)}</b> накопилось {new_total} замечаний при сдаче видео-статусов.\n",
        "Рекомендуется обратить внимание на качество сдачи отчётов и при необходимости провести личную беседу с сотрудником.\n",
        f"Всего замечаний: {new_total}",
    ]
    if recent:
        lines.append("\nПоследние нарушения:")
        for r in recent:
            date_label = format_show_date(r["report_date"])
            slot_label = f" ({r['slot_time']})" if r["slot_time"] else ""
            comment = (r["format_comment"] or "").strip() or "без комментария"
            lines.append(f"• {date_label}{slot_label} — {html.escape(comment)}")

    alert_text = "\n".join(lines)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id} накопительное предупреждение о сотруднике {telegram_id}: {e}")

from ai import (
    transcribe_audio, clean_report, check_status
)

logger = logging.getLogger(__name__)

MEDIA_BATCH_BUFFERS = {}
MEDIA_BATCH_DEBOUNCE_SECONDS = 8
MEDIA_MERGE_WINDOW_MINUTES = 20

def format_show_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d.%m")
    except Exception:
        return date_str

def format_status_or_fact_line(report_type: str, slot_time: str | None, report_date: str) -> str:
    formatted_date = format_show_date(report_date)
    if report_type == "daily_fact":
        return f"Факт за {formatted_date}"
    else:
        slot_str = slot_time or "Неизвестно"
        return f"Статус за {slot_str} за {formatted_date}"

def update_message_metadata(original_text: str, is_ok: bool | None = None, comment: str | None = None, status_val: str | None = None, is_manual: bool = False) -> str:
    lines = original_text.split("\n")
    for i, line in enumerate(lines):
        if (line.startswith("Статус:") or line.startswith("Статус за") or line.startswith("Факт за")) and status_val is not None:
            lines[i] = status_val
        elif (line.startswith("Оценка ИИ:") or line.startswith("Оценка:")) and is_ok is not None:
            label = "Оценка" if (is_manual or line.startswith("Оценка:")) else "Оценка ИИ"
            lines[i] = f"{label}: {'ОК' if is_ok else 'НЕ ОК'}"
        elif (line.startswith("Комментарий ИИ:") or line.startswith("Комментарий:")) and comment is not None:
            label = "Комментарий" if (is_manual or line.startswith("Комментарий:")) else "Комментарий ИИ"
            lines[i] = f"{label}: {comment}"
    return "\n".join(lines)

def update_message_text_fields(original_text: str, is_ok: bool, new_comment: str) -> str:
    return update_message_metadata(original_text, is_ok=is_ok, comment=new_comment, is_manual=True)

def make_report_keyboard(report_id: int, report_type: str | None = None) -> InlineKeyboardMarkup:
    if report_type is None:
        try:
            conn = get_db()
            row = conn.execute("SELECT report_type FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            report_type = row["report_type"] if row else "status"
        except Exception:
            report_type = "status"
            
    type_btn_text = "📋 Сделать Итогом дня" if report_type == "status" else "⏱ Сделать Статусом"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Изменить оценку (ОК / НЕ ОК)", callback_data=f"fix_toggle_{report_id}"),
            InlineKeyboardButton("✏️ Изменить комментарий", callback_data=f"edit_comment_{report_id}")
        ],
        [
            InlineKeyboardButton(type_btn_text, callback_data=f"toggle_type_{report_id}")
        ]
    ])

def make_video_selection_keyboard(report_id: int, action_type: str, video_items: list[dict]) -> InlineKeyboardMarkup:
    prefix = "tg" if action_type == "toggle" else "ed"
    buttons = []
    
    if action_type == "toggle":
        buttons.append([InlineKeyboardButton("🔄 Общая оценка", callback_data=f"{prefix}_overall_{report_id}")])
    else:
        buttons.append([InlineKeyboardButton("✏️ Общий комментарий", callback_data=f"{prefix}_overall_{report_id}")])
        
    for item in video_items:
        num = item["num"]
        if action_type == "toggle":
            status_emoji = "✅ ОК" if item["is_ok"] else "⚠️ НЕ ОК"
            btn_text = f"📹 Видео {num}: {status_emoji}"
        else:
            btn_text = f"📹 Видео {num}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"{prefix}_vid_{report_id}_{num}")])
        
    buttons.append([InlineKeyboardButton("❌ Назад", callback_data=f"back_to_main_{report_id}")])
    return InlineKeyboardMarkup(buttons)

async def render_report_message_from_row(report: dict, worker_name: str) -> tuple[str, InlineKeyboardMarkup]:
    report_id = report["id"]
    report_type = report["report_type"]
    slot_time = report["slot_time"]
    report_date = report["report_date"]
    is_ok = bool(report["is_ok"])
    format_comment = report["format_comment"] or ""
    required_action = report["required_action"] or ("Ничего не предпринимать, всё в порядке" if is_ok else "")
    raw_text = report["raw_text"] or ""
    
    cleaned_text = await clean_report_async(raw_text)

    status_line = format_status_or_fact_line(report_type, slot_time, report_date)
    status_emoji = "✅" if is_ok else "⚠️"
    
    notify_lines = [
        f"👤 <b>{html.escape(worker_name)}</b>",
        f"📍 {html.escape(status_line)}",
        f"Оценка: {status_emoji} {'ОК' if is_ok else 'НЕ ОК'}",
    ]
    
    is_status_with_videos = (report_type == "status" and ("Видео" in format_comment or ";" in format_comment))

    if is_status_with_videos:
        video_count = len([part for part in format_comment.split("; ") if part.strip()])
        notify_lines.append("")
        for i in range(1, video_count + 1):
            notify_lines.append(f"📹 Видео {i}")
        notify_lines.append(f"Комментарий: {html.escape(format_comment)}")
    else:
        notify_lines.append(f"Комментарий: {html.escape(format_comment)}")

    notify_lines.append(f"⚡ Требуемое действие: {html.escape(required_action)}")

    notify_lines.append("")
    notify_lines.append("📝 <b>Официальный отчет:</b>")
    notify_lines.append(f"\"{html.escape(cleaned_text)}\"")
    notify_lines.append("")
    notify_lines.append("🗣 <b>Оригинальный текст:</b>")
    notify_lines.append(f"\"{html.escape(raw_text)}\"")
    
    notify_text = "\n".join(notify_lines)
    inline_kbd = make_report_keyboard(report_id, report_type)
    
    return notify_text, inline_kbd

def menu_for_user(user_id: int, chat_type: str = "private"):
    if is_admin(user_id) and chat_type == "private":
        # Note: MAIN_MENU is imported dynamically from bot main entry
        from bot import MAIN_MENU
        return MAIN_MENU
    if chat_type != "private":
        return ReplyKeyboardMarkup([], remove_keyboard=True)
    if get_worker(user_id) is not None:
        return ReplyKeyboardMarkup([["📤 Сдать статус"]], resize_keyboard=True)
    return ReplyKeyboardMarkup([["🔑 Начать регистрацию"]], resize_keyboard=True)

async def transcribe_audio_async(file_path: str) -> str:
    return await asyncio.to_thread(transcribe_audio, file_path)

async def clean_report_async(text: str) -> str:
    return await asyncio.to_thread(clean_report, text)

async def check_status_async(text: str, report_type_override: str | None = None) -> dict:
    return await asyncio.to_thread(check_status, text, report_type_override)

async def enqueue_media_report_item(user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Update, text_content: str, now: datetime):
    buf = MEDIA_BATCH_BUFFERS.setdefault(user_id, {"items": [], "task": None})
    buf["items"].append({"update": update, "text_content": text_content, "now": now})

    old_task = buf.get("task")
    if old_task and not old_task.done():
        old_task.cancel()
    buf["task"] = asyncio.create_task(_flush_media_batch(user_id, context))

async def _flush_media_batch(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.sleep(MEDIA_BATCH_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    buf = MEDIA_BATCH_BUFFERS.pop(user_id, None)
    if not buf or not buf["items"]:
        return

    lock = asyncio.Lock() # Lock per flush or use user_locks dynamically
    from bot import get_user_lock
    user_lock = get_user_lock(user_id)
    await user_lock.acquire()
    try:
        logger.info(f"[media_batch] Начало обработки {len(buf['items'])} видео от пользователя {user_id}")
        await process_media_batch(user_id, buf["items"], context)
        logger.info(f"[media_batch] Обработка завершена для пользователя {user_id}")
    except Exception as exc:
        logger.exception(f"Ошибка обработки пачки видео-отчетов пользователя {user_id}")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Ошибка при обработке видео: {exc}\n\nПопробуйте отправить ещё раз."
            )
        except Exception:
            pass
    finally:
        try:
            user_lock.release()
        except Exception:
            pass

def pick_target_status_slot(schedule: list[str], now: datetime, submitted_slots: set):
    current_mins = now.hour * 60 + now.minute
    missing_passed = []
    for slot in schedule:
        if slot in submitted_slots:
            continue
        h, m = map(int, slot.split(":"))
        if h * 60 + m <= current_mins:
            missing_passed.append(slot)
    if missing_passed:
        # Attribute the report to the CLOSEST due-but-unsubmitted slot, not the oldest one —
        # a video sent at 12:55 belongs to the 12:00 slot even if 10:00 was also never submitted.
        # The 10:00 slot is left genuinely missing, which is correct: it wasn't reported on.
        missing_passed.sort(key=lambda s: tuple(map(int, s.split(":"))))
        target_slot = missing_passed[-1]
        h, m = map(int, target_slot.split(":"))
        is_late = current_mins > h * 60 + m + LATE_THRESHOLD_MIN
        return target_slot, is_late

    # Otherwise find nearest
    from bot import find_nearest_slot
    return find_nearest_slot(schedule, now)

async def process_media_batch(user_id: int, items: list[dict], context: ContextTypes.DEFAULT_TYPE):
    worker = await run_db(get_worker, user_id)
    if not worker:
        return

    sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
    w_name = f"{worker['last_name']} {worker['first_name']}"
    dest_chat = worker["group_id"] or DEFAULT_GROUP_ID

    last_slot_time_str = sched_list[-1]
    last_hour, last_minute = map(int, last_slot_time_str.split(":"))

    evaluated_items = []
    for idx, item in enumerate(items, start=1):
        text_content = item["text_content"]
        now = item["now"]
        upd = item["update"]
        date_str = now.strftime("%Y-%m-%d")

        logger.info(f"[process_media_batch] Видео {idx}/{len(items)} пользователя {user_id}: текст='{text_content[:80]}'")

        last_slot_time = now.replace(hour=last_hour, minute=last_minute, second=0, microsecond=0)
        last_slot_limit = last_slot_time + dt_module.timedelta(minutes=LATE_THRESHOLD_MIN)
        forced_type = "status" if now <= last_slot_limit else None

        ai_res_pre = await check_status_async(text_content, report_type_override=forced_type)
        report_type = ai_res_pre["report_type"]

        evaluated_items.append({
            "item": item,
            "ai_res": ai_res_pre,
            "report_type": report_type,
            "text_content": text_content,
            "now": now,
            "upd": upd,
            "date_str": date_str
        })

    # Separate status items and daily facts
    status_items = [x for x in evaluated_items if x["report_type"] == "status"]
    fact_items = [x for x in evaluated_items if x["report_type"] == "daily_fact"]

    # 1. Process daily_fact items individually
    for f_item in fact_items:
        text_content = f_item["text_content"]
        now = f_item["now"]
        upd = f_item["upd"]
        date_str = f_item["date_str"]
        ai_res = f_item["ai_res"]

        cleaned_text = await clean_report_async(text_content)
        report_id = await run_db(
            save_report,
            telegram_id=user_id,
            report_date=date_str,
            report_type="daily_fact",
            slot_time=None,
            received_at=now.strftime("%H:%M:%S"),
            is_ok=ai_res["is_ok"],
            is_late=0,
            format_comment=ai_res["format_comment"],
            required_action=ai_res["required_action"],
            raw_text=text_content
        )
        async_sync_gsheets_background()

        copied_msg_id = None
        try:
            copied_msg = await context.bot.copy_message(
                chat_id=dest_chat,
                from_chat_id=upd.effective_chat.id,
                message_id=upd.message.message_id
            )
            copied_msg_id = copied_msg.message_id
            await run_db(add_report_media, report_id, upd.effective_chat.id, upd.message.message_id, copied_msg_id, 1, now.strftime("%H:%M:%S"))
        except Exception as e:
            logger.error(f"Ошибка копирования видео факта дня в чат {dest_chat}: {e}")

        # Fact reports CAN be notified to admins (only status notifications are restricted to group chat)
        def _fetch_report_row():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r

        report_row = await run_db(_fetch_report_row)

        notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name)

        try:
            if copied_msg_id:
                sent_notify_msg = await context.bot.send_message(
                    chat_id=dest_chat, text=notify_text, reply_markup=inline_kbd,
                    reply_to_message_id=copied_msg_id, parse_mode="HTML"
                )
            else:
                sent_notify_msg = await context.bot.send_message(
                    chat_id=dest_chat, text=notify_text, reply_markup=inline_kbd, parse_mode="HTML"
                )
            await run_db(set_report_group_message, report_id, dest_chat, sent_notify_msg.message_id)
        except Exception as e:
            logger.error(f"Ошибка отправки оценки факта в чат {dest_chat}: {e}")

        try:
            if ai_res["is_ok"]:
                await upd.message.reply_text(f"✅ Факт получен и принят без замечаний!", parse_mode="Markdown")
            else:
                await upd.message.reply_text(f"⚠️ Факт получен.\n{ai_res['employee_message']}", parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Не удалось отправить личный фидбек по факту пользователю {user_id}: {e}")

    # 2. Process all status items TOGETHER as a single status report
    if status_items:
        status_now = status_items[0]["now"]
        date_str = status_items[0]["date_str"]

        submitted_slots = await run_db(get_submitted_status_slots, user_id, date_str)
        slot_time, is_late = pick_target_status_slot(sched_list, status_now, submitted_slots)

        existing = await run_db(get_existing_report_row, user_id, date_str, "status", slot_time)

        do_full_merge = False
        old_media_rows = []
        if existing:
            try:
                prev_h, prev_m, prev_s = map(int, existing["received_at"].split(":"))
                elapsed_minutes = (status_now.hour * 60 + status_now.minute) - (prev_h * 60 + prev_m)
                do_full_merge = 0 <= elapsed_minutes <= MEDIA_MERGE_WINDOW_MINUTES
            except Exception:
                do_full_merge = False
            if do_full_merge:
                old_media_rows = await run_db(get_report_media, existing["id"])

        if existing:
            existing_media_count = len(old_media_rows) if old_media_rows else 0
            raw_text_parts = []
            for idx, s_item in enumerate(status_items, start=1 + existing_media_count):
                raw_text_parts.append(f"[Видео {idx}]: {s_item['text_content']}")
            combined_raw_text = existing["raw_text"].strip() + "\n" + "\n".join(raw_text_parts)

            new_comments = []
            for idx, s_item in enumerate(status_items, start=1 + existing_media_count):
                res = s_item["ai_res"]
                new_comments.append(f"Видео {idx}: {res['format_comment']}")

            if existing["format_comment"] and existing["format_comment"] != "всё ОК":
                overall_format_comment = existing["format_comment"] + "; " + "; ".join(new_comments)
            else:
                overall_format_comment = "; ".join(new_comments)

            overall_is_ok = bool(existing["is_ok"]) or any(x["ai_res"]["is_ok"] for x in status_items)
            cleaned_text = await clean_report_async(combined_raw_text)
            report_id = existing["id"]

            error_count = overall_format_comment.lower().count("не ок")
            overall_required_action = REMARK_REQUIRED_ACTION_TEXT if error_count > 0 else "Ничего не предпринимать, всё в порядке"

            await run_db(
                update_report_text_and_ai,
                report_id=report_id,
                is_ok=overall_is_ok,
                format_comment=overall_format_comment,
                required_action=overall_required_action,
                raw_text=combined_raw_text,
                received_at=status_now.strftime("%H:%M:%S")
            )
        else:
            raw_text_parts = []
            for idx, s_item in enumerate(status_items, start=1):
                raw_text_parts.append(f"[Видео {idx}]: {s_item['text_content']}")
            combined_raw_text = "\n".join(raw_text_parts)

            new_comments = []
            for idx, s_item in enumerate(status_items, start=1):
                res = s_item["ai_res"]
                new_comments.append(f"Видео {idx}: {res['format_comment']}")
            overall_format_comment = "; ".join(new_comments)

            overall_is_ok = any(x["ai_res"]["is_ok"] for x in status_items)
            cleaned_text = await clean_report_async(combined_raw_text)

            error_count = overall_format_comment.lower().count("не ок")
            overall_required_action = REMARK_REQUIRED_ACTION_TEXT if error_count > 0 else "Ничего не предпринимать, всё в порядке"

            report_id = await run_db(
                save_report,
                telegram_id=user_id,
                report_date=date_str,
                report_type="status",
                slot_time=slot_time,
                received_at=status_now.strftime("%H:%M:%S"),
                is_ok=overall_is_ok,
                is_late=is_late,
                format_comment=overall_format_comment,
                required_action=overall_required_action,
                raw_text=combined_raw_text
            )
        async_sync_gsheets_background()

        if error_count > 0:
            await notify_admins_if_remark_threshold_crossed(context, user_id, w_name)

        copied_msg_ids = []
        if do_full_merge and old_media_rows:
            # Delete old forwarded video messages from group
            for m in old_media_rows:
                if m["group_message_id"]:
                    try:
                        await context.bot.delete_message(chat_id=dest_chat, message_id=m["group_message_id"])
                    except Exception as e:
                        logger.warning(f"Не удалось удалить старое видео {m['group_message_id']}: {e}")
            await run_db(delete_report_media_rows, report_id)

            # Re-forward all media files
            media_sources = [(m["source_chat_id"], m["source_message_id"]) for m in old_media_rows]
            for s_item in status_items:
                media_sources.append((s_item["upd"].effective_chat.id, s_item["upd"].message.message_id))

            for pos, (src_chat, src_msg) in enumerate(media_sources, start=1):
                try:
                    copied_msg = await context.bot.copy_message(chat_id=dest_chat, from_chat_id=src_chat, message_id=src_msg)
                    await run_db(add_report_media, report_id, src_chat, src_msg, copied_msg.message_id, pos, status_now.strftime("%H:%M:%S"))
                    copied_msg_ids.append(copied_msg.message_id)
                except Exception as e:
                    logger.error(f"Ошибка повторной пересылки видео {pos}: {e}")
        else:
            # Forward only new videos
            existing_media_count = len(old_media_rows) if old_media_rows else 0
            for idx, s_item in enumerate(status_items, start=1):
                upd = s_item["upd"]
                try:
                    copied_msg = await context.bot.copy_message(
                        chat_id=dest_chat,
                        from_chat_id=upd.effective_chat.id,
                        message_id=upd.message.message_id
                    )
                    copied_msg_ids.append(copied_msg.message_id)
                    next_pos = existing_media_count + idx
                    await run_db(add_report_media, report_id, upd.effective_chat.id, upd.message.message_id, copied_msg.message_id, next_pos, status_now.strftime("%H:%M:%S"))
                except Exception as e:
                    logger.error(f"Ошибка копирования видео в чат {dest_chat}: {e}")

        # Delete old evaluation comment if it exists
        old_eval = await run_db(get_report_group_message, report_id)
        if old_eval and old_eval["group_chat_id"] and old_eval["group_message_id"]:
            try:
                await context.bot.delete_message(chat_id=old_eval["group_chat_id"], message_id=old_eval["group_message_id"])
            except Exception as e:
                logger.warning(f"Не удалось удалить старую оценку {old_eval['group_message_id']}: {e}")

        # Construct beautiful general comment for the group!
        def _fetch_report_row2():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r

        report_row = await run_db(_fetch_report_row2)

        notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name)

        # Send replying to the last forwarded video
        last_copied_msg_id = copied_msg_ids[-1] if copied_msg_ids else None
        try:
            if last_copied_msg_id:
                sent_notify_msg = await context.bot.send_message(
                    chat_id=dest_chat,
                    text=notify_text,
                    reply_markup=inline_kbd,
                    reply_to_message_id=last_copied_msg_id,
                    parse_mode="HTML"
                )
            else:
                sent_notify_msg = await context.bot.send_message(
                    chat_id=dest_chat,
                    text=notify_text,
                    reply_markup=inline_kbd,
                    parse_mode="HTML"
                )
            await run_db(set_report_group_message, report_id, dest_chat, sent_notify_msg.message_id)
        except Exception as e:
            logger.error(f"Ошибка отправки оценки в чат {dest_chat}: {e}")

        # CRITICAL REQ: Why does the status comment go to the admin's chat? It must ONLY go to the group chat!
        # So we DO NOT forward the evaluation text of "status" reports to admins in their personal chats!
        # Status reports won't spam admins anymore.

        # Reply directly to the employee in their DM for each video
        employee_messages = []
        for idx, s_item in enumerate(status_items, start=1):
            res = s_item["ai_res"]
            if not res["is_ok"]:
                employee_messages.append(f"Видео {idx}: {res['employee_message']}")

        suffix_tail = f" (видео 1-{len(status_items)})" if len(status_items) > 1 else ""
        info_suffix = f" за статус *{slot_time}*{suffix_tail}"

        for s_item in status_items:
            upd = s_item["upd"]
            try:
                if overall_is_ok:
                    await upd.message.reply_text(f"✅ Отчёт{info_suffix} принят без замечаний!", parse_mode="Markdown")
                else:
                    msg = "Есть замечания по видео-статусам:\n" + "\n".join(employee_messages)
                    await upd.message.reply_text(f"⚠️ Отчёт{info_suffix}.\n{msg}", parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"Не удалось отправить личный фидбек по статусу пользователю {user_id}: {e}")


async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if is_admin(user_id) and context.user_data.get("editing_comment_report_id"):
        report_id = context.user_data["editing_comment_report_id"]
        video_num = context.user_data.get("editing_comment_video_num")
        original_chat_id = context.user_data.get("editing_comment_chat_id")
        original_msg_id = context.user_data.get("editing_comment_message_id")
        original_text = context.user_data.get("editing_comment_original_text", "")
        
        context.user_data.pop("editing_comment_report_id", None)
        context.user_data.pop("editing_comment_video_num", None)
        context.user_data.pop("editing_comment_chat_id", None)
        context.user_data.pop("editing_comment_message_id", None)
        context.user_data.pop("editing_comment_original_text", None)
        prompt_message_id = context.user_data.pop("editing_comment_prompt_message_id", None)
        
        if prompt_message_id and original_chat_id:
            try:
                await context.bot.delete_message(chat_id=original_chat_id, message_id=prompt_message_id)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass
            
        new_comment = update.message.text.strip() if update.message.text else ""
        if not new_comment or new_comment.lower() in ("отмена", "❌ отмена"):
            return
            
        def _apply_manual_comment():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None

            if video_num is not None:
                # We are editing a specific video's comment!
                video_items = parse_video_comments(r["format_comment"])
                for item in video_items:
                    if item["num"] == video_num:
                        cleaned_comment = new_comment.strip()
                        if cleaned_comment.lower().startswith("ок"):
                            item["is_ok"] = True
                            item["comment"] = cleaned_comment[2:].strip().lstrip("-").strip()
                        elif cleaned_comment.lower().startswith("не ок"):
                            item["is_ok"] = False
                            item["comment"] = cleaned_comment[5:].strip().lstrip("-").strip()
                        else:
                            item["comment"] = cleaned_comment

                new_format_comment = rebuild_format_comment(video_items)
                new_overall_ok = all(item["is_ok"] for item in video_items)
                new_action = f"Комментарий видео {video_num} изменен администратором вручную: {new_comment}"
                conn.execute(
                    "UPDATE reports SET format_comment = ?, is_ok = ?, required_action = ? WHERE id = ?",
                    (new_format_comment, 1 if new_overall_ok else 0, new_action, report_id)
                )
            else:
                # We are editing the overall / general comment!
                new_action = f"Комментарий изменен администратором вручную: {new_comment}"
                conn.execute(
                    "UPDATE reports SET format_comment = ?, required_action = ? WHERE id = ?",
                    (new_comment, new_action, report_id)
                )

            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            conn.commit()
            conn.close()
            return r, w

        report, worker = await run_db(_apply_manual_comment)
        if not report:
            return

        async_sync_gsheets_background()
        
        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        text, kbd = await render_report_message_from_row(report, worker_name)
        
        if original_chat_id and original_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=original_chat_id,
                    message_id=original_msg_id,
                    text=text,
                    reply_markup=kbd,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка при обновлении сообщения после редактирования комментария: {e}")
        return

    if is_admin(user_id):
        return

    from bot import get_user_lock
    lock = get_user_lock(user_id)
    await lock.acquire()

    text_content = ""
    tmp_path = None

    try:
        worker = await run_db(get_worker, user_id)

        if worker:
            pending_reasons = await run_db(get_pending_reason_requests, user_id)
            if pending_reasons:
                is_media_msg = bool(update.message.voice or update.message.video or update.message.video_note)
                if is_media_msg:
                    slots_str = ", ".join(f"{p['slot_time']} ({p['report_date']})" for p in pending_reasons)
                    await update.message.reply_text(
                        f"⚠️ Сначала укажите причину, почему не был сдан статус за: {slots_str}.\n"
                        f"Напишите текстом объяснение — после этого сможете отправить видео-отчёт."
                    )
                    return
                if update.message.text:
                    reason_text = update.message.text.strip()
                    if reason_text and reason_text != "❌ Отмена":
                        await run_db(resolve_pending_reason_requests, user_id, reason_text)
                        async_sync_gsheets_background()
                        await update.message.reply_text(
                            "✅ Спасибо, причина зафиксирована. Теперь можете отправить видео-отчёт.",
                            reply_markup=menu_for_user(user_id, update.effective_chat.type)
                        )
                        return

        if update.message.text:
            text_content = update.message.text.strip()
        else:
            file_obj = None
            if update.message.voice: file_obj = update.message.voice
            elif update.message.video: file_obj = update.message.video
            elif update.message.video_note: file_obj = update.message.video_note

            if file_obj:
                await update.message.reply_text("📹 Видео получено, ожидайте оценки:")
                tg_file = await context.bot.get_file(file_obj.file_id)
                ext = "mp4" if update.message.video or update.message.video_note else "ogg"
                
                os.makedirs("tmp", exist_ok=True)
                tmp_path = f"tmp/file_{user_id}_{int(datetime.now().timestamp())}.{ext}"
                
                await tg_file.download_to_drive(tmp_path)
                logger.info(f"[handler] Начало транскрипции файла {tmp_path} для пользователя {user_id}")
                text_content = await transcribe_audio_async(tmp_path)
                logger.info(f"[handler] Транскрипция завершена: '{text_content[:80]}'")

        if not text_content:
            await update.message.reply_text("Ошибка: Не удалось распознать аудио или медиа отчета.")
            return

        if text_content.startswith("Ошибка распознавания аудио"):
            await update.message.reply_text("❌ При распознавании аудио произошла ошибка. Пожалуйста, отправьте текстовый отчет или попробуйте перезаписать.")
            return

        is_media = bool(update.message.voice or update.message.video or update.message.video_note)

        if not worker:
            user_info = {
                "first_name": update.effective_user.first_name or "",
                "last_name": update.effective_user.last_name or "",
                "username": update.effective_user.username or "",
                "timestamp": datetime.now().isoformat(),
                "text": text_content
            }
            await run_db(
                save_pending_unregistered_user,
                telegram_id=user_id,
                first_name=user_info["first_name"],
                last_name=user_info["last_name"],
                username=user_info["username"],
                timestamp=user_info["timestamp"],
                text_content=user_info["text"]
            )
            
            admin_msg = (
                f"👤 Обнаружен отчет от незарегистрированного сотрудника!\n"
                f"TG ID: {user_id}\n"
                f"Имя в Telegram: {user_info['first_name']} {user_info['last_name']} (@{user_info['username']})\n"
                f"Текст:\n\"{text_content[:300]}\"\n\n"
                f"Вы можете добавить его в базу через меню, указав ID."
            )
            for admin_id in ADMIN_IDS:
                try:
                    admin_copied_msg_id = None
                    if is_media:
                        try:
                            admin_copied = await context.bot.copy_message(
                                chat_id=admin_id,
                                from_chat_id=update.effective_chat.id,
                                message_id=update.message.message_id
                            )
                            admin_copied_msg_id = admin_copied.message_id
                        except Exception as copy_err:
                            logger.error(f"Ошибка копирования медиа незарегистрированного пользователя администратору {admin_id}: {copy_err}")
                    
                    if admin_copied_msg_id:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=admin_msg,
                            reply_to_message_id=admin_copied_msg_id
                        )
                    else:
                        await context.bot.send_message(chat_id=admin_id, text=admin_msg)
                except Exception:
                    pass

            await update.message.reply_text(
                "Ошибка: Вы не зарегистрированы в системе авторизации бота.\n"
                "Ваш отчет отправлен администраторам как временный.\n\n"
                "Нажмите кнопку ниже, чтобы зарегистрироваться:",
                reply_markup=ReplyKeyboardMarkup([["🔑 Начать регистрацию"]], resize_keyboard=True)
            )
            return

        if context.user_data.get("awaiting_not_working_reason"):
            reason = text_content
            if reason.lower() in ("отмена", "❌ отмена"):
                context.user_data.pop("awaiting_not_working_reason", None)
                await update.message.reply_text("Отменено.", reply_markup=menu_for_user(user_id, update.effective_chat.type))
                return
            
            context.user_data["not_working_reason"] = reason
            context.user_data.pop("awaiting_not_working_reason", None)
            context.user_data["awaiting_not_working_confirm"] = True
            
            kbd = ReplyKeyboardMarkup([["Да, я уверен", "❌ Отмена"]], resize_keyboard=True)
            await update.message.reply_text(
                f"Уже сданные сегодня отчёты (если были) сохранятся — статус «Не работаю» просто добавится к ним.\n\n"
                f"Подтвердите: причина — {reason}",
                reply_markup=kbd
            )
            return

        if context.user_data.get("awaiting_not_working_confirm"):
            confirm = text_content
            if confirm == "Да, я уверен":
                reason = context.user_data.pop("not_working_reason", "Без причины")
                context.user_data.pop("awaiting_not_working_confirm", None)
                
                now = now_local()
                date_str = now.strftime("%Y-%m-%d")
                
                await run_db(
                    save_report,
                    telegram_id=user_id,
                    report_date=date_str,
                    report_type="not_working",
                    slot_time=None,
                    received_at=now.strftime("%H:%M:%S"),
                    is_ok=True,
                    is_late=False,
                    format_comment=reason,
                    required_action="Не работает",
                    raw_text=f"Не работает сегодня. Причина: {reason}"
                )
                async_sync_gsheets_background()
                
                await update.message.reply_text(
                    f"✅ Статус 'Не работаю' успешно сохранен.\nПричина: {reason}",
                    reply_markup=menu_for_user(user_id, update.effective_chat.type)
                )
                
                w_name = f"{worker['last_name']} {worker['first_name']}"
                dest_chat = worker["group_id"] or DEFAULT_GROUP_ID
                notify_text = f"🛌 {w_name} сегодня не работает.\nПричина: {reason}"
                try:
                    await context.bot.send_message(chat_id=dest_chat, text=notify_text)
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления в группу: {e}")
                return
            else:
                context.user_data.pop("not_working_reason", None)
                context.user_data.pop("awaiting_not_working_confirm", None)
                await update.message.reply_text("Действие отменено.", reply_markup=menu_for_user(user_id, update.effective_chat.type))
                return

        if text_content.strip() == "📤 Сдать статус":
            await update.message.reply_text(
                "📹 Нажмите на значок 📎 (скрепка) рядом с полем ввода и запишите или выберите видео с рабочего места.\n"
                "После отправки видео дождитесь оценки — она придёт автоматически.",
                reply_markup=menu_for_user(user_id, update.effective_chat.type)
            )
            return

        if text_content in ("🛌 Не работаю сегодня", "Не работаю сегодня") or text_content.lower() == "не работаю сегодня":
            now = now_local()
            date_str = now.strftime("%Y-%m-%d")

            def _fetch_not_working():
                conn = get_db()
                r = conn.execute(
                    "SELECT * FROM reports WHERE telegram_id = ? AND report_date = ? AND report_type = 'not_working'",
                    (user_id, date_str)
                ).fetchone()
                conn.close()
                return r

            existing_not_working = await run_db(_fetch_not_working)

            if existing_not_working:
                await update.message.reply_text("У вас уже установлен статус «Не работаю сегодня» на сегодня.", reply_markup=menu_for_user(user_id, update.effective_chat.type))
                return
                
            context.user_data["awaiting_not_working_reason"] = True
            await update.message.reply_text(
                "Укажите, пожалуйста, причину, почему вы сегодня не работаете (например: заболел, отпуск, отпросился у прораба):",
                reply_markup=ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)
            )
            return

        if not is_media and text_content.strip() in ("❌ Отмена", "Да, я уверен"):
            await update.message.reply_text(
                "Это была кнопка диалога, который уже завершился — отчётом она не считается.\n"
                "Если хотите сдать отчёт, нажмите «📤 Сдать статус» или просто запишите видео.",
                reply_markup=menu_for_user(user_id, update.effective_chat.type)
            )
            return

        now = now_local()

        if is_media:
            await enqueue_media_report_item(user_id, context, update, text_content, now)
            return

        # Handle purely text status report (worker typewriting)
        date_str = now.strftime("%Y-%m-%d")
        sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
        
        last_slot_time_str = sched_list[-1]
        last_hour, last_minute = map(int, last_slot_time_str.split(":"))
        last_slot_time = now.replace(hour=last_hour, minute=last_minute, second=0, microsecond=0)
        last_slot_limit = last_slot_time + dt_module.timedelta(minutes=LATE_THRESHOLD_MIN)

        report_type_override = "status" if now <= last_slot_limit else None

        ai_res_pre = await check_status_async(text_content, report_type_override=report_type_override)
        report_type = ai_res_pre["report_type"]

        if report_type == "status":
            submitted_slots = await run_db(get_submitted_status_slots, user_id, date_str)
            nearest_slot, is_late = pick_target_status_slot(sched_list, now, submitted_slots)
        else:
            nearest_slot, is_late = None, False

        existing_report = await run_db(get_existing_report_row, user_id, date_str, report_type, nearest_slot)

        is_addon = False
        if existing_report:
            is_addon = True
            ai_res = await check_status_async(text_content, report_type_override=report_type)
            cleaned_text = await clean_report_async(text_content)
            report_id = existing_report["id"]
            action_text = REMARK_REQUIRED_ACTION_TEXT if (ai_res["report_type"] == "status" and not ai_res["is_ok"]) else ai_res["required_action"]

            await run_db(
                update_report_text_and_ai,
                report_id=report_id,
                is_ok=ai_res["is_ok"],
                format_comment=ai_res["format_comment"],
                required_action=action_text,
                raw_text=text_content,
                received_at=now.strftime("%H:%M:%S")
            )
        else:
            ai_res = ai_res_pre
            cleaned_text = await clean_report_async(text_content)
            action_text = REMARK_REQUIRED_ACTION_TEXT if (ai_res["report_type"] == "status" and not ai_res["is_ok"]) else ai_res["required_action"]
            report_id = await run_db(
                save_report,
                telegram_id=user_id,
                report_date=date_str,
                report_type=ai_res["report_type"],
                slot_time=nearest_slot if ai_res["report_type"] == "status" else None,
                received_at=now.strftime("%H:%M:%S"),
                is_ok=ai_res["is_ok"],
                is_late=is_late if ai_res["report_type"] == "status" else 0,
                format_comment=ai_res["format_comment"],
                required_action=action_text,
                raw_text=text_content
            )
        async_sync_gsheets_background()

        w_name = f"{worker['last_name']} {worker['first_name']}"
        if ai_res["report_type"] == "status" and not ai_res["is_ok"]:
            await notify_admins_if_remark_threshold_crossed(context, user_id, w_name)
        time_str = now.strftime("%H:%M")
        if ai_res["report_type"] == "status" and nearest_slot:
            sh, sm = map(int, nearest_slot.split(":"))
            diff_mins = (now.hour * 60 + now.minute) - (sh * 60 + sm)
            late_str = f" (опоздание {diff_mins} мин)" if diff_mins > LATE_THRESHOLD_MIN else ""
            info_suffix = f" за статус *{nearest_slot}* принят в *{time_str}*{late_str}"
        else:
            info_suffix = f" — факт получен в *{time_str}*"

        if ai_res["is_ok"]:
            await update.message.reply_text(f"✅ Отчёт{info_suffix} успешно проверен ИИ и принят без замечаний! Спасибо.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ Отчёт{info_suffix}.\nОценка отчета: {ai_res['employee_message']}", parse_mode="Markdown")

        dest_chat = worker["group_id"] or DEFAULT_GROUP_ID
        title_text = f"Отчет обновлен: {w_name}" if is_addon else w_name
        notify_text = (
            f"{title_text}\n"
            f"{format_status_or_fact_line(ai_res['report_type'], nearest_slot if ai_res['report_type'] == 'status' else None, date_str)}\n"
            f"Оценка ИИ: {'ОК' if ai_res['is_ok'] else 'НЕ ОК'}\n"
            f"Комментарий ИИ: {ai_res['format_comment']}\n\n"
            f"📝 Официальный отчет:\n\"{cleaned_text}\"\n\n"
            f"🗣 Оригинальный текст:\n\"{text_content}\""
        )
        
        inline_kbd = make_report_keyboard(report_id, ai_res["report_type"])

        if is_addon and existing_report and existing_report["group_chat_id"] and existing_report["group_message_id"]:
            try:
                await context.bot.delete_message(chat_id=existing_report["group_chat_id"], message_id=existing_report["group_message_id"])
            except Exception:
                pass

        try:
            sent_notify_msg = await context.bot.send_message(
                chat_id=dest_chat, text=notify_text, reply_markup=inline_kbd, parse_mode="Markdown"
            )
            await run_db(set_report_group_message, report_id, dest_chat, sent_notify_msg.message_id)
        except Exception as e:
            logger.error(f"Ошибка отправки оценки в чат {dest_chat}: {e}")
    finally:
        try:
            lock.release()
        except Exception:
            pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await query.answer("У вас нет прав администратора.", show_alert=True)
        return
        
    data = query.data
    logger.info(f"Получен callback_query: {data} от администратора {user_id}")
    
    # 1. Back to main menu
    if data.startswith("back_to_main_"):
        report_id = int(data.split("_")[-1])

        def _fetch_report_and_worker():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone() if r else None
            conn.close()
            return r, w

        report, worker = await run_db(_fetch_report_and_worker)

        if report and worker:
            worker_name = f"{worker['last_name']} {worker['first_name']}"
            text, kbd = await render_report_message_from_row(report, worker_name)
            try:
                await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Ошибка при возврате к основному меню: {e}")
        return

    # 2. Toggle type (status <-> daily_fact)
    if data.startswith("toggle_type_"):
        report_id = int(data.split("_")[-1])

        def _toggle_type():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None

            new_type = "daily_fact" if r["report_type"] == "status" else "status"
            new_slot_time = r["slot_time"]
            if new_type == "status" and not new_slot_time:
                new_slot_time = "10:00"

            conn.execute("UPDATE reports SET report_type = ?, slot_time = ? WHERE id = ?", (new_type, new_slot_time, report_id))
            conn.commit()

            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            conn.close()
            return r, w

        report, worker = await run_db(_toggle_type)
        if not report:
            return

        async_sync_gsheets_background()
        
        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        text, kbd = await render_report_message_from_row(report, worker_name)
        try:
            await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка при изменении типа отчета: {e}")
        return

    # 3. Main Toggle Button -> Choose overall or video
    if data.startswith("fix_toggle_"):
        report_id = int(data.split("_")[-1])

        def _fetch_report():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r

        report = await run_db(_fetch_report)

        if not report:
            return

        video_items = parse_video_comments(report["format_comment"])
        if len(video_items) > 1:
            kbd = make_video_selection_keyboard(report_id, "toggle", video_items)
            try:
                await query.edit_message_reply_markup(reply_markup=kbd)
            except Exception as e:
                logger.error(f"Ошибка при показе меню выбора видео для toggling: {e}")
        else:
            new_is_ok = 0 if report["is_ok"] == 1 else 1
            new_format_comment = report["format_comment"] or ""
            if video_items:
                video_items[0]["is_ok"] = (new_is_ok == 1)
                new_format_comment = rebuild_format_comment(video_items)

            def _apply_fix_toggle():
                conn = get_db()
                conn.execute("UPDATE reports SET is_ok = ?, format_comment = ? WHERE id = ?", (new_is_ok, new_format_comment, report_id))
                conn.commit()
                r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
                w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
                conn.close()
                return r, w

            report, worker = await run_db(_apply_fix_toggle)

            async_sync_gsheets_background()
            
            worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
            text, kbd = await render_report_message_from_row(report, worker_name)
            try:
                await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Ошибка при непосредственном изменении оценки: {e}")
        return

    # 4. Toggle Overall
    if data.startswith("tg_overall_"):
        report_id = int(data.split("_")[-1])

        def _toggle_overall():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None

            new_is_ok = 0 if r["is_ok"] == 1 else 1
            conn.execute("UPDATE reports SET is_ok = ? WHERE id = ?", (new_is_ok, report_id))
            conn.commit()

            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            conn.close()
            return r, w

        report, worker = await run_db(_toggle_overall)
        if not report:
            return

        async_sync_gsheets_background()
        
        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        text, kbd = await render_report_message_from_row(report, worker_name)
        try:
            await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка при изменении общей оценки: {e}")
        return

    # 5. Toggle Individual Video Assessment
    if data.startswith("tg_vid_"):
        parts = data.split("_")
        report_id = int(parts[2])
        video_num = int(parts[3])
        
        def _toggle_video():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None

            video_items = parse_video_comments(r["format_comment"])
            for item in video_items:
                if item["num"] == video_num:
                    item["is_ok"] = not item["is_ok"]

            new_overall_ok = all(item["is_ok"] for item in video_items)
            new_format_comment = rebuild_format_comment(video_items)

            conn.execute(
                "UPDATE reports SET is_ok = ?, format_comment = ? WHERE id = ?",
                (1 if new_overall_ok else 0, new_format_comment, report_id)
            )
            conn.commit()

            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            conn.close()
            return r, w

        report, worker = await run_db(_toggle_video)
        if not report:
            return

        async_sync_gsheets_background()
        
        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        text, kbd = await render_report_message_from_row(report, worker_name)
        try:
            await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка при изменении оценки видео: {e}")
        return

    # 6. Main Edit Comment Button -> Choose overall or video
    if data.startswith("edit_comment_"):
        report_id = int(data.split("_")[-1])

        def _fetch_report():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r

        report = await run_db(_fetch_report)

        if not report:
            return
            
        video_items = parse_video_comments(report["format_comment"])
        if len(video_items) > 1:
            kbd = make_video_selection_keyboard(report_id, "edit", video_items)
            try:
                await query.edit_message_reply_markup(reply_markup=kbd)
            except Exception as e:
                logger.error(f"Ошибка при показе меню выбора видео для editing: {e}")
        else:
            context.user_data["editing_comment_report_id"] = report_id
            context.user_data["editing_comment_video_num"] = None
            context.user_data["editing_comment_chat_id"] = query.message.chat.id
            context.user_data["editing_comment_message_id"] = query.message.message_id
            context.user_data["editing_comment_original_text"] = query.message.text
            
            prompt_msg = await query.message.reply_text(
                "✏️ Введите новый комментарий к отчету:\n(или введите «Отмена»)",
                reply_markup=ForceReply(selective=True)
            )
            context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        return

    # 7. Edit Overall Comment
    if data.startswith("ed_overall_"):
        report_id = int(data.split("_")[-1])
        context.user_data["editing_comment_report_id"] = report_id
        context.user_data["editing_comment_video_num"] = None
        context.user_data["editing_comment_chat_id"] = query.message.chat.id
        context.user_data["editing_comment_message_id"] = query.message.message_id
        context.user_data["editing_comment_original_text"] = query.message.text
        
        prompt_msg = await query.message.reply_text(
            "✏️ Введите новый общий комментарий к отчету:\n(или введите «Отмена»)",
            reply_markup=ForceReply(selective=True)
        )
        context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        return

    # 8. Edit Specific Video Comment
    if data.startswith("ed_vid_"):
        parts = data.split("_")
        report_id = int(parts[2])
        video_num = int(parts[3])
        
        context.user_data["editing_comment_report_id"] = report_id
        context.user_data["editing_comment_video_num"] = video_num
        context.user_data["editing_comment_chat_id"] = query.message.chat.id
        context.user_data["editing_comment_message_id"] = query.message.message_id
        context.user_data["editing_comment_original_text"] = query.message.text
        
        prompt_msg = await query.message.reply_text(
            f"✏️ Введите новый комментарий для Видео {video_num}:\n(или введите «Отмена»)",
            reply_markup=ForceReply(selective=True)
        )
        context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        return
