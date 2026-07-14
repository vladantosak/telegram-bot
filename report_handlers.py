import os
import html
import re
import asyncio
import logging
import uuid
from datetime import datetime
from telegram import ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

def extract_report_summaries(format_comment: str) -> list[str]:
    """Turns a report's format_comment - either multi-video ("Видео 1: ОК - ...; Видео 2:
    не ОК - ...") or single ("ОК - ...") - into a plain list of just the content summaries,
    one per video, each with its own ОК/не ОК prefix stripped. Used for the compact group
    message, which states the overall verdict once up front instead of repeating it per
    video."""
    items = parse_video_comments(format_comment)
    summaries = []
    for item in items:
        comment = re.sub(r"^(не\s*ОК|ОК)\s*-\s*", "", item["comment"].strip(), flags=re.IGNORECASE).strip()
        if comment:
            summaries.append(comment)
    return summaries

from db import (
    get_worker, get_submitted_status_slots, get_existing_report_row,
    save_report, update_report_text_and_ai, set_report_group_message,
    get_report_group_message, add_report_media, get_report_media,
    delete_report_media_rows, get_group_name_async, cancel_not_working,
    get_pending_unregistered_user, save_pending_unregistered_user,
    delete_pending_unregistered_user, bind_worker_id, async_sync_gsheets_background,
    get_db, run_db, is_admin, ADMIN_IDS, DEFAULT_GROUP_ID, SCHEDULES, SCHEDULE_A,
    STATUS_LATE_TOLERANCE_MIN, clean_position,
    now_local, is_quiet_mode_enabled, get_worker_target_group,
    get_group_name, get_pending_reason_requests, resolve_pending_reason_requests,
    get_missed_status_reason, check_and_update_remark_alert_threshold, get_recent_remarks,
    is_message_already_processed, is_missed_reason_request_enabled,
    resolve_unrecognized_report, count_consecutive_unrecognized_reports,
    save_speech_review_message, get_speech_review_messages, set_report_media_group_message,
    count_effective_remarks, is_ai_error_simulation_enabled,
    count_unresolved_batch_siblings, get_resolved_batch_reports_by_type, merge_resolved_batch_reports
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

from ai_gemini import (
    transcribe_audio, clean_report, check_status, classify_report_type,
    assess_transcription_quality, AITechnicalError
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

def _short_issue_text(ai_results) -> str:
    """Turns one or more AI result dicts into a short, plain-language pointer at what was
    wrong (e.g. "На видео не слышно голоса.") for the employee's personal message - never
    the full technical remark list, which stays in the group/Sheets notification."""
    issues = []
    for res in ai_results:
        if res.get("is_ok"):
            continue
        issue = (res.get("issue") or "").strip()
        if not issue:
            continue
        issue_cap = issue[0].upper() + issue[1:]
        if issue_cap not in issues:
            issues.append(issue_cap)
    if not issues:
        return "Обнаружены недочёты."
    return "; ".join(issues).rstrip(".") + "."

def format_status_or_fact_line(report_type: str, slot_time: str | None, report_date: str) -> str:
    formatted_date = format_show_date(report_date)
    if report_type == "daily_fact":
        return f"Факт за {formatted_date}"
    else:
        slot_str = slot_time or "Неизвестно"
        return f"Статус за {formatted_date} в {slot_str}"

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

def _resolve_report_type(report_id: int, report_type: str | None) -> str:
    if report_type is not None:
        return report_type
    try:
        conn = get_db()
        row = conn.execute("SELECT report_type FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.close()
        return row["report_type"] if row else "status"
    except Exception:
        return "status"

def make_report_keyboard(report_id: int, report_type: str | None = None) -> InlineKeyboardMarkup:
    """Collapsed state (default): a single bare "⚙️" button, no label - kept as small and
    unobtrusive as Telegram allows. Telegram can't hide inline buttons from specific chat
    members, so this button and the message itself are visible to everyone in the group -
    the real access control is the is_admin() check at the top of handle_callback_query,
    which gates actions_expand_ itself and every action behind it before anything runs.
    report_type isn't needed for the collapsed button itself, but stays in the signature so
    every existing caller (which already resolves/passes it) needs no change."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("⚙️", callback_data=f"actions_expand_{report_id}")]])

def make_report_actions_expanded_keyboard(report_id: int, report_type: str | None = None) -> InlineKeyboardMarkup:
    """Expanded state, shown after an admin taps "⚙️": Оценка (fix_toggle_, content quality
    only) / Тип (toggle_type_) / Опоздание (toggle_late_, status only - a SEPARATE flag from
    the content оценка, see render_report_message_from_row's effective_not_ok) / Время
    (edit_time_, status only) / Действия (edit_action_, a manual override for "Требуемые
    действия") - "📝 Изменить комментарий" is no longer on this quick menu (Формат отчета no
    longer shows a freeform summary to edit - see render_report_message_from_row), but
    edit_comment_/ed_overall_/ed_vid_ still exist unchanged in case that's needed again."""
    report_type = _resolve_report_type(report_id, report_type)
    rows = [
        [
            InlineKeyboardButton("✏️ Оценка", callback_data=f"fix_toggle_{report_id}"),
            InlineKeyboardButton("🔄 Тип", callback_data=f"toggle_type_{report_id}"),
        ],
    ]
    second_row = []
    if report_type == "status":
        # Lets an admin correct a report misattributed to the wrong schedule slot (e.g. the
        # "nearest slot" pick landed on the wrong one) without having to delete and redo it.
        second_row.append(InlineKeyboardButton("🕒 Время", callback_data=f"edit_time_{report_id}"))
        # Content quality (✏️ Оценка) and lateness are independent attributes - toggling one
        # must never be the only way to affect the other (see effective_not_ok), so lateness
        # gets its own dedicated button rather than being folded into "Оценка".
        second_row.append(InlineKeyboardButton("⏰ Опоздание", callback_data=f"toggle_late_{report_id}"))
    second_row.append(InlineKeyboardButton("📋 Действия", callback_data=f"edit_action_{report_id}"))
    rows.append(second_row)
    rows.append([InlineKeyboardButton("◀️ Свернуть", callback_data=f"actions_collapse_{report_id}")])
    return InlineKeyboardMarkup(rows)

# Fixed vocabulary for a manually-assigned "не ОК" reason during manual review (AI technical
# failure or unrecognized speech) - the SAME 3 content-quality reasons _derive_not_ok_reason
# already recognizes by keyword in format_comment ("молчал"/"неразборч", else the default), so
# a manually-resolved не-ОК report renders identically to an automatically-scored one with no
# changes needed there. "прислал поздно" is deliberately NOT one of these - it's an is_late
# attribute with its own dedicated "⏰ Опоздание" toggle (see make_report_actions_expanded_
# keyboard), never a content-quality verdict an admin picks here.
MANUAL_REVIEW_REASONS = [
    ("silent", "🔇 Молчал в видео", "на видео молчал"),
    ("unclear", "🗣 Говорил неразборчиво", "неразборчивый текст"),
    ("novolume", "📦 Не указал объём работы", "не указал объём работы"),
]
MANUAL_REVIEW_REASON_TEXT = {code: text for code, _, text in MANUAL_REVIEW_REASONS}

def make_manual_review_keyboard(report_id: int) -> InlineKeyboardMarkup:
    """Initial keyboard for a report awaiting manual review (AI technical failure OR
    unrecognized speech): the admin picks TYPE and VERDICT in a single tap - "ОК" resolves
    immediately, "НЕ ОК" swaps this same message's keyboard (edit_message_reply_markup - only
    THIS admin's own copy of the message, every other admin's copy is untouched) to a reason
    sub-menu via make_manual_review_reason_keyboard, nothing is written to the database until
    a final verdict (ОК, or a specific не-ОК reason) is actually chosen."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Статус ОК", callback_data=f"mr_status_ok_{report_id}"),
            InlineKeyboardButton("⚠️ Статус НЕ ОК", callback_data=f"mr_status_bad_{report_id}"),
        ],
        [
            InlineKeyboardButton("✅ Факт ОК", callback_data=f"mr_fact_ok_{report_id}"),
            InlineKeyboardButton("⚠️ Факт НЕ ОК", callback_data=f"mr_fact_bad_{report_id}"),
        ],
    ])

def make_manual_review_reason_keyboard(report_id: int, rtype: str) -> InlineKeyboardMarkup:
    """rtype is "status" or "fact" (kept as a distinct short token in callback_data, alongside
    report_type's real "daily_fact" spelling elsewhere) - shown after "⚠️ ... НЕ ОК" is
    tapped, replacing this admin's own copy of the initial 4-button keyboard."""
    rows = [[InlineKeyboardButton(label, callback_data=f"mr_{rtype}_{code}_{report_id}")] for code, label, _ in MANUAL_REVIEW_REASONS]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"mr_back_{report_id}")])
    return InlineKeyboardMarkup(rows)

def make_cancel_edit_keyboard(report_id: int) -> InlineKeyboardMarkup:
    """Real inline "❌ Отмена" button shown on a text-input prompt (edit comment/slot time),
    replacing the old "(или введите «Отмена»)" text hint. A message can't carry both a
    ForceReply and an inline keyboard, so these prompts are now sent as plain messages -
    typing still works exactly as before (any next text from this admin is still picked up
    by the editing_comment_*/editing_slot_time_* check in handle_report), this button is
    just a faster, unambiguous way to back out."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_edit_{report_id}")]])

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

def _derive_not_ok_reason(format_comment: str, content_ok: bool, is_late: bool) -> str:
    """Maps whatever the content-quality check produced (format_comment's own free-text
    issue, e.g. "не ОК - на видео молчал") plus the separate is_late flag onto ONE of the 4
    fixed reason phrases the group message is allowed to show - never the LLM's raw text
    verbatim. The content-based reasons are only ever considered when content_ok is False -
    if an admin has manually flipped is_ok to True (fix_toggle_), format_comment may still
    contain old wording like "не ОК - на видео молчал" verbatim, and that stale text must
    NOT resurface as the reason once content has been marked fine; the only way this report
    is still не ОК for THIS message at that point is lateness."""
    if not content_ok:
        lower = (format_comment or "").lower()
        if "молчал" in lower:
            return "молчал в видео"
        if "неразборч" in lower:
            return "говорил неразборчиво"
        return "не указал объём работы"
    return "прислал поздно"

async def render_report_message_from_row(report: dict, worker_name: str, position: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Builds the group message: name+position (bold) - ✅/⚠️ статус/факт (underlined) date
    за time / Формат отчета / Требуемые действия / Со слов сотрудника - sent with
    parse_mode="HTML", so every dynamic piece (name, position, cleaned report text) MUST be
    html.escape()'d before going in, or a stray "<"/">"/"&" in someone's spoken text would
    break the tags for the whole message. No per-video breakdown or raw transcript here -
    full detail still goes to Google Sheets unchanged (sync_gsheets_task reads the same
    `reports` row directly).

    "Формат отчета"/"Требуемые действия"/the ✅⚠️ icon are all computed HERE from is_ok +
    is_late (+ the cumulative remark count), never read as stored free text - this is what
    lets the existing fix_toggle_/toggle_type_ buttons "just work": flipping is_ok or
    report_type and re-rendering via this same function automatically recomputes every
    dependent field, no separate update logic needed. required_action is the one exception:
    if an admin has set a manual override (the "📋 Действия" button), that verbatim text is
    shown instead of the auto-computed wording, until they change it again."""
    report_id = report["id"]
    report_type = report["report_type"]
    slot_time = report["slot_time"]
    report_date = report["report_date"]
    received_at = report["received_at"] or ""
    content_ok = bool(report["is_ok"])
    is_late = bool(report["is_late"])
    format_comment = report["format_comment"] or ""
    raw_text = report["raw_text"] or ""

    # Per product decision: lateness alone makes a report не ОК for THIS message and its
    # remark counter, even though the stored is_ok/Сводка computation is untouched (a
    # late-but-content-fine report stays is_ok=1 in the database).
    effective_not_ok = (not content_ok) or is_late
    verdict_icon = "⚠️" if effective_not_ok else "✅"

    formatted_date = html.escape(format_show_date(report_date))
    if report_type == "daily_fact":
        time_str = html.escape(received_at[:5] if received_at else "??:??")
        type_html = f"{verdict_icon} <u>факт</u> {formatted_date} за {time_str}"
    elif report_type == "status":
        time_str = html.escape(slot_time or "??:??")
        type_html = f"{verdict_icon} <u>статус</u> {formatted_date} за {time_str}"
    else:
        type_label = {"unrecognized_speech": "не удалось распознать речь"}.get(report_type, report_type)
        type_html = f"{html.escape(type_label)} {formatted_date}"

    if effective_not_ok:
        reason = _derive_not_ok_reason(format_comment, content_ok, is_late)
        format_line = f"⚠️ не ОК - {html.escape(reason)}"
    else:
        format_line = "✅ всё ОК"

    if report["action_override"]:
        action_line_html = html.escape(report["required_action"] or "")
    elif not effective_not_ok:
        action_line_html = "ничего не предпринимать"
    else:
        remark_count = await run_db(count_effective_remarks, report["telegram_id"])
        if remark_count >= 3:
            action_line_html = "сделано замечание, нарушение повторяется — делегировано отделу контроля"
        else:
            action_line_html = "сделано замечание сотруднику"

    cleaned_text = await clean_report_async(raw_text)

    header = f"<b>{html.escape(worker_name)} ({html.escape(position or '?')})</b> - {type_html}"
    notify_text = (
        f"{header}\n"
        f"<b>Формат отчета:</b> {format_line}\n"
        f"<b>Требуемые действия:</b> {action_line_html}\n\n"
        f"<b>Со слов сотрудника:</b> {html.escape(cleaned_text)}"
    )
    inline_kbd = make_report_keyboard(report_id, report_type)

    return notify_text, inline_kbd

# The 3 admin text-input edit flows (комментарий / время сдачи / требуемые действия) each
# store their own pending state as a handful of flat context.user_data keys.
# context.user_data is a SINGLE dict per admin, shared across every chat and report card
# they touch - it is NOT scoped to one report or one button press. "all_keys" lists every
# key that flow ever sets (cleared in full when superseded); report/chat/message/prompt
# single out the 4 keys every flow has in common, needed to restore the old card.
_EDIT_FLOWS = {
    "comment": {
        "report": "editing_comment_report_id", "chat": "editing_comment_chat_id",
        "message": "editing_comment_message_id", "prompt": "editing_comment_prompt_message_id",
        "all_keys": ["editing_comment_report_id", "editing_comment_video_num", "editing_comment_chat_id",
                     "editing_comment_message_id", "editing_comment_original_text", "editing_comment_prompt_message_id"],
    },
    "slot_time": {
        "report": "editing_slot_time_report_id", "chat": "editing_slot_time_chat_id",
        "message": "editing_slot_time_message_id", "prompt": "editing_slot_time_prompt_message_id",
        "all_keys": ["editing_slot_time_report_id", "editing_slot_time_chat_id", "editing_slot_time_message_id",
                     "editing_slot_time_prompt_message_id"],
    },
    "action": {
        "report": "editing_action_report_id", "chat": "editing_action_chat_id",
        "message": "editing_action_message_id", "prompt": "editing_action_prompt_message_id",
        "all_keys": ["editing_action_report_id", "editing_action_chat_id", "editing_action_message_id",
                     "editing_action_prompt_message_id"],
    },
}

async def _cancel_any_pending_edit_flow(context: ContextTypes.DEFAULT_TYPE):
    """Called at the START of every one of the 5 entry points that start a text-input edit
    flow (edit_comment_/ed_overall_/ed_vid_ for комментарий, edit_time_, edit_action_),
    BEFORE it sets its own context.user_data keys. At most ONE such flow can ever be
    legitimately pending per admin at a time.

    ROOT CAUSE this fixes: without this guard, an admin who starts editing report A's time
    (or comment, or action) and then, before replying, taps a DIFFERENT edit button on
    report B's card, ends up with a SECOND flow's keys also set in the same shared
    context.user_data dict. handle_report checks the 3 flows in a fixed order and the FIRST
    one that has a report_id set wins - so a reply meant for report B's flow could silently
    get consumed by report A's still-pending (and higher-priority) flow instead: applied to
    the WRONG report, or simply swallowed - while report B's card, and possibly report A's
    too, stayed with disabled buttons forever, since nothing else ever re-enables them. This
    is exactly what made "Изменить время сдачи" look like it silently did nothing: the typed
    time went to whichever OTHER edit flow was still pending from an earlier, forgotten tap,
    not to the time-edit flow the admin thought they were answering.

    This restores the previously-pending flow's card to its normal keyboard and deletes its
    now-orphaned prompt message, exactly as if the admin had pressed "❌ Отмена" on it
    themselves, before the new flow is allowed to start."""
    for flow in _EDIT_FLOWS.values():
        pending_report_id = context.user_data.get(flow["report"])
        if not pending_report_id:
            continue

        pending_chat_id = context.user_data.get(flow["chat"])
        pending_msg_id = context.user_data.get(flow["message"])
        pending_prompt_id = context.user_data.get(flow["prompt"])
        for key in flow["all_keys"]:
            context.user_data.pop(key, None)

        if pending_prompt_id and pending_chat_id:
            try:
                await context.bot.delete_message(chat_id=pending_chat_id, message_id=pending_prompt_id)
            except Exception:
                pass

        if pending_chat_id and pending_msg_id:
            def _fetch(rid=pending_report_id):
                conn = get_db()
                r = conn.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
                w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone() if r else None
                conn.close()
                return r, w

            report, worker = await run_db(_fetch)
            if report:
                worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
                position = clean_position(worker["position"]) if worker else "?"
                text, kbd = await render_report_message_from_row(report, worker_name, position)
                try:
                    await context.bot.edit_message_text(
                        chat_id=pending_chat_id, message_id=pending_msg_id,
                        text=text, reply_markup=kbd, parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Ошибка при восстановлении карточки отчёта {pending_report_id} при старте нового редактирования: {e}")
        return  # at most one flow is ever legitimately pending

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
    # QA toggle (/simulate_ai_error) - lets an admin force the whole "technical failure ->
    # manual review" pipeline on demand instead of waiting for a real rate limit.
    if await run_db(is_ai_error_simulation_enabled):
        raise AITechnicalError("Симулированная ошибка API (тестовый режим включён администратором через /simulate_ai_error)")
    return await asyncio.to_thread(transcribe_audio, file_path)

async def clean_report_async(text: str) -> str:
    return await asyncio.to_thread(clean_report, text)

async def check_status_async(text: str, report_type_override: str | None = None) -> dict:
    if await run_db(is_ai_error_simulation_enabled):
        raise AITechnicalError("Симулированная ошибка API (тестовый режим включён администратором через /simulate_ai_error)")
    return await asyncio.to_thread(check_status, text, report_type_override)

async def classify_report_type_async(text: str) -> dict:
    return await asyncio.to_thread(classify_report_type, text)

async def notify_admins_ai_check_failed(context: ContextTypes.DEFAULT_TYPE, report_id: int, worker_name: str,
                                         submitted_at_str: str, report_date_str: str, upd: Update | None,
                                         error_message: str):
    """Same review mechanism as notify_admins_unrecognized_speech (mr_ buttons, self-locking
    across every admin via speech_review_messages) but for a report whose CONTENT-ANALYSIS
    call failed technically (rate limit/timeout/etc.) - not one whose speech genuinely
    couldn't be understood. The Telegram message only ever gets a short, fixed phrase about
    the failure - the real error text (needed to check the provider's billing/rate-limit
    dashboard) is logged server-side only, never sent here."""
    logger.error(f"[ai_check_failed] Техническая ошибка ИИ при анализе отчёта {report_id} сотрудника {worker_name}: {error_message}")
    is_media = bool(upd and (upd.message.voice or upd.message.video or upd.message.video_note))
    alert_text = (
        "⚠️ Не удалось автоматически проверить отчёт (техническая причина).\n"
        f"Сотрудник: {html.escape(worker_name)}\n"
        f"Время получения: {html.escape(submitted_at_str)} ({html.escape(format_show_date(report_date_str))})\n"
        + ("Пожалуйста, оцените отчёт вручную по видео выше.\n" if is_media else "Пожалуйста, оцените отчёт вручную по тексту ниже.\n")
        + "Причина: ошибка LLM-провайдера (лимит/недоступность)."
    )
    if not is_media and upd is not None:
        alert_text += f"\n\n🗣 Текст отчёта:\n\"{html.escape(upd.message.text or '')}\""
    kbd = make_manual_review_keyboard(report_id)
    for admin_id in ADMIN_IDS:
        try:
            if is_media:
                copied = await context.bot.copy_message(
                    chat_id=admin_id, from_chat_id=upd.effective_chat.id, message_id=upd.message.message_id
                )
                sent = await context.bot.send_message(
                    chat_id=admin_id, text=alert_text, reply_markup=kbd, reply_to_message_id=copied.message_id
                )
            else:
                sent = await context.bot.send_message(chat_id=admin_id, text=alert_text, reply_markup=kbd)
            await run_db(save_speech_review_message, report_id, admin_id, sent.message_id)
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id} уведомление о технической ошибке ИИ (отчёт {report_id}): {e}")

async def _save_ai_check_failure_report(user_id: int, date_str: str, now: datetime, text_content: str,
                                         upd: Update, batch_id: str | None = None) -> tuple[int, bool]:
    """Saves the report_type='unrecognized_speech' row for a content-analysis technical
    failure (and its media, if any) - split out of handle_ai_check_failure so a multi-video
    submission session can save EVERY manual-review row first and only notify admins once all
    of them exist (see process_media_batch), instead of notifying one at a time as each item
    is evaluated. Returns (report_id, is_media)."""
    is_media = bool(upd.message.voice or upd.message.video or upd.message.video_note)
    submitted_at_str = now.strftime("%H:%M:%S")
    report_id = await run_db(
        save_report,
        telegram_id=user_id,
        report_date=date_str,
        report_type="unrecognized_speech",
        slot_time=None,
        received_at=submitted_at_str,
        is_ok=False,
        is_late=0,
        format_comment="техническая ошибка при анализе отчёта (LLM API)",
        required_action="Требуется ручная проверка администратором",
        raw_text=text_content,
        batch_id=batch_id
    )
    if is_media:
        await run_db(add_report_media, report_id, upd.effective_chat.id, upd.message.message_id, None, 1, submitted_at_str)
    return report_id, is_media

async def handle_ai_check_failure(context: ContextTypes.DEFAULT_TYPE, user_id: int, worker_name: str,
                                   date_str: str, now: datetime, text_content: str, upd: Update,
                                   error: AITechnicalError, batch_id: str | None = None):
    """Called the moment content-analysis (check_status) fails technically, for either a
    video/voice item or a typed text report - no retries, straight to the SAME manual-review
    mechanism as "не удалось распознать речь" (report_type='unrecognized_speech', the
    mr_ buttons, resolve_unrecognized_speech_report on click).
    Never treated as не ОК, never counted toward the remark counter (no report row exists
    with a real verdict until an admin resolves it), never shown any error text at all."""
    report_id, is_media = await _save_ai_check_failure_report(user_id, date_str, now, text_content, upd, batch_id)
    await notify_admins_ai_check_failed(context, report_id, worker_name, now.strftime("%H:%M:%S"), date_str, upd, error.message)
    try:
        confirm_text = "✅ Видео получено, ожидайте оценки." if is_media else "✅ Отчёт получен, ожидайте оценки."
        await upd.message.reply_text(confirm_text)
    except Exception as e:
        logger.warning(f"Не удалось отправить сотруднику {user_id} подтверждение получения при технической ошибке ИИ: {e}")

async def assess_transcription_quality_async(text: str, duration_seconds: float | None) -> dict:
    return await asyncio.to_thread(assess_transcription_quality, text, duration_seconds)

UNRECOGNIZED_SPEECH_STREAK_THRESHOLD = 3

async def _save_unrecognized_speech_report(user_id: int, date_str: str, now: datetime, text_content: str,
                                            upd: Update, batch_id: str | None = None) -> int:
    """Saves the report_type='unrecognized_speech' row (and its media) for a video that
    failed the speech-recognition quality gate - split out so a multi-video submission
    session can save every manual-review row first and notify admins only once all of them
    exist (see process_media_batch)."""
    report_id = await run_db(
        save_report,
        telegram_id=user_id,
        report_date=date_str,
        report_type="unrecognized_speech",
        slot_time=None,
        received_at=now.strftime("%H:%M:%S"),
        is_ok=False,
        is_late=0,
        format_comment="не удалось распознать речь",
        required_action="Требуется ручная проверка администратором",
        raw_text=text_content,
        batch_id=batch_id
    )
    await run_db(add_report_media, report_id, upd.effective_chat.id, upd.message.message_id, None, 1, now.strftime("%H:%M:%S"))
    return report_id

async def notify_admins_unrecognized_speech(context: ContextTypes.DEFAULT_TYPE, report_id: int, worker_name: str, text_content: str, upd: Update):
    """Sends the original video + a fixed review prompt to EVERY admin's private chat (not
    the work group) with buttons to manually pick type AND verdict in one tap - per product
    decision, ALL admins get notified and the buttons self-lock on first click (see the mr_
    callback dispatch in handle_callback_query), rather than picking one "on duty" admin."""
    transcript_shown = html.escape(text_content.strip()) if text_content.strip() else "Пустой результат"
    review_text = (
        f"⚠️ Не удалось распознать речь в отчёте.\n"
        f"Сотрудник: {html.escape(worker_name)}\n"
        f"Результат транскрибации: \"{transcript_shown}\""
    )
    kbd = make_manual_review_keyboard(report_id)
    for admin_id in ADMIN_IDS:
        try:
            copied = await context.bot.copy_message(
                chat_id=admin_id, from_chat_id=upd.effective_chat.id, message_id=upd.message.message_id
            )
            sent = await context.bot.send_message(
                chat_id=admin_id, text=review_text, reply_markup=kbd,
                reply_to_message_id=copied.message_id
            )
            await run_db(save_speech_review_message, report_id, admin_id, sent.message_id)
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id} видео с нераспознанной речью (отчёт {report_id}): {e}")

async def notify_admins_unrecognized_speech_streak(context: ContextTypes.DEFAULT_TYPE, telegram_id: int, worker_name: str):
    streak = await run_db(count_consecutive_unrecognized_reports, telegram_id)
    if streak == 0 or streak % UNRECOGNIZED_SPEECH_STREAK_THRESHOLD != 0:
        return
    alert_text = (
        "⚠️ Обратите внимание.\n"
        f"У сотрудника {html.escape(worker_name)} уже несколько подряд отчётов с нераспознанной речью.\n"
        "Рекомендуется проверить качество записи видео или работу микрофона."
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=alert_text)
        except Exception as e:
            logger.error(f"Не удалось отправить админу {admin_id} предупреждение о повторных сбоях распознавания у {telegram_id}: {e}")

async def _post_resolved_batch_type_group_message(batch_id: str, telegram_id: int, report_type: str,
                                                    worker: dict, dest_chat: int, w_name: str,
                                                    context: ContextTypes.DEFAULT_TYPE):
    """Once every video from one manually-reviewed submission session has been labeled,
    combines every already-resolved sibling of ONE type (status or daily_fact) into a single
    report row via merge_resolved_batch_reports (a no-op if there's only one), forwards all
    of its videos to the group in order, and posts one group card - mirrors exactly how
    process_media_batch already combines multiple same-type videos from an auto-classified
    session, so a manually-resolved session ends up looking identical. Does nothing if this
    session had no video of this type."""
    rows = await run_db(get_resolved_batch_reports_by_type, batch_id, telegram_id, report_type)
    if not rows:
        return

    error_count = sum(1 for r in rows if not r["is_ok"])
    required_action = REMARK_REQUIRED_ACTION_TEXT if error_count > 0 else "Ничего не предпринимать, всё в порядке"
    ids = [r["id"] for r in rows]
    primary_id = await run_db(merge_resolved_batch_reports, ids, required_action)

    media_rows = await run_db(get_report_media, primary_id)
    copied_msg_id = None
    for m in media_rows:
        try:
            copied = await context.bot.copy_message(chat_id=dest_chat, from_chat_id=m["source_chat_id"], message_id=m["source_message_id"])
            await run_db(set_report_media_group_message, m["id"], copied.message_id)
            if copied_msg_id is None:
                copied_msg_id = copied.message_id
        except Exception as e:
            logger.error(f"Ошибка пересылки видео объединённого вручную обработанного отчёта {primary_id} в группу: {e}")

    def _fetch():
        conn = get_db()
        r = conn.execute("SELECT * FROM reports WHERE id = ?", (primary_id,)).fetchone()
        conn.close()
        return r

    report_row = await run_db(_fetch)
    notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name, clean_position(worker["position"]))
    try:
        sent = await context.bot.send_message(
            chat_id=dest_chat, text=notify_text, reply_markup=inline_kbd,
            reply_to_message_id=copied_msg_id, parse_mode="HTML"
        )
        await run_db(set_report_group_message, primary_id, dest_chat, sent.message_id)
    except Exception as e:
        logger.error(f"Ошибка отправки карточки объединённого вручную обработанного отчёта {primary_id} в группу: {e}")

async def resolve_unrecognized_speech_report(report_id: int, chosen_type: str, is_ok: bool,
                                              reason_code: str | None,
                                              context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str | None, int]:
    """Admin manually resolves a report_type='unrecognized_speech' row into Status or Fact,
    with the FULL verdict (is_ok, and - if НЕ ОК - which of MANUAL_REVIEW_REASONS) given
    directly by the admin's button tap, never re-derived by asking the AI again: the whole
    point of this button existing is that the AI is either down or the speech genuinely
    couldn't be transcribed, so there is nothing left for it to weigh in on here - the admin
    has already watched the video and made the call. Runs the same slot attribution a normal
    status report would get (using the ORIGINAL submission time, not the time of the admin's
    click). The employee gets their personal verdict message immediately, per video, exactly
    as before - but the video is only forwarded to the WORK GROUP once every OTHER video from
    the SAME submission session (batch_id) has also been resolved, so a session an admin
    labels one video at a time - in any order, possibly hours apart - still ends up posted as
    ONE combined message per type actually used (matching how an automatically-classified
    session's videos get grouped), instead of separate messages trickling into the group as
    each button is clicked. A lone report with no batch_id (e.g. a pre-migration row) posts
    immediately, exactly as this function always worked before session tracking existed.

    Returns (status, worker_name, remaining):
    - "conflict": already resolved by another admin (or a stale second click) - worker_name is None.
    - "waiting": resolved, but other videos from the same session are still unresolved -
      nothing was posted to the group yet; `remaining` counts how many are still pending.
    - "posted": resolved AND every sibling in its session is now resolved (or there was no
      session to wait for) - the combined group message(s) were just sent.
    """

    def _load():
        conn = get_db()
        r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone() if r else None
        media = conn.execute("SELECT * FROM report_media WHERE report_id = ? ORDER BY position", (report_id,)).fetchall()
        conn.close()
        return r, w, media

    report, worker, media_rows = await run_db(_load)
    if not report or report["report_type"] != "unrecognized_speech" or not worker:
        return "conflict", None, 0

    telegram_id = report["telegram_id"]
    report_date = report["report_date"]
    raw_text = report["raw_text"] or ""
    batch_id = report["batch_id"]
    w_name = f"{worker['last_name']} {worker['first_name']}"
    sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)

    try:
        h, m, s = map(int, report["received_at"].split(":"))
        y, mo, d = map(int, report_date.split("-"))
        submitted_at = datetime(y, mo, d, h, m, s)
    except Exception:
        submitted_at = now_local()

    # format_comment/required_action/issue follow the EXACT same "ОК - .../не ОК - ..."
    # convention ai.py's normalize_ai_result already produces for an automatic verdict, so
    # every downstream consumer (_derive_not_ok_reason's "молчал"/"неразборч" keyword match,
    # parse_video_comments, count_effective_remarks, Google Sheets) treats a manually-given
    # verdict identically to an automatic one with no separate-casing needed anywhere else.
    if is_ok:
        ai_res = {
            "report_type": chosen_type,
            "is_ok": True,
            "format_comment": "ОК - оценено вручную администратором",
            "required_action": "ничего не предпринимать",
            "employee_message": "",
            "issue": "",
        }
    else:
        reason_text = MANUAL_REVIEW_REASON_TEXT.get(reason_code, "не указал объём работы")
        ai_res = {
            "report_type": chosen_type,
            "is_ok": False,
            "format_comment": f"не ОК - {reason_text}",
            "required_action": f"сделал замечание сотруднику: {reason_text}",
            "employee_message": f"В отчете есть замечание: {reason_text}. В следующем отчете исправьте это.",
            "issue": reason_text,
        }

    if chosen_type == "status":
        submitted_slots = await run_db(get_submitted_status_slots, telegram_id, report_date)
        slot_time, is_late = pick_target_status_slot(sched_list, submitted_at, submitted_slots, w_name)
    else:
        slot_time, is_late = None, False

    await run_db(
        resolve_unrecognized_report,
        report_id=report_id,
        report_type=chosen_type,
        slot_time=slot_time,
        is_ok=ai_res["is_ok"],
        is_late=is_late,
        format_comment=ai_res["format_comment"],
        required_action=ai_res["required_action"],
    )
    async_sync_gsheets_background()

    # The employee gets the exact same personal message a normal auto-processed report
    # would produce, immediately, one per video, regardless of whether its session's group
    # post is still waiting on siblings - it's this video's own confirmation that IT was
    # judged, not a group-session concept. Nothing here reveals that an admin resolved this
    # manually rather than the AI doing it automatically, per product decision.
    label = "Статус" if chosen_type == "status" else "Факт"
    try:
        if ai_res["is_ok"]:
            if chosen_type == "status":
                personal_text = f"✅ Статус за {slot_time} принят без замечаний."
                if is_late:
                    personal_text += " Статус получен позже установленного времени."
            else:
                personal_text = "✅ Факт получен и принят без замечаний."
        else:
            personal_text = (
                f"❌ {label} НЕ ОК. {_short_issue_text([ai_res])}\n"
                "Сотрудник контроля свяжется с вами и скажет замечания.\n"
                "Напоминаем, что при частом допущении ошибок в сдаче отчетов, "
                "проблема будет делегироваться руководству."
            )
        await context.bot.send_message(chat_id=telegram_id, text=personal_text)
    except Exception as e:
        logger.warning(f"Не удалось отправить личный фидбек по вручную обработанному отчёту пользователю {telegram_id}: {e}")

    if batch_id:
        remaining = await run_db(count_unresolved_batch_siblings, batch_id, telegram_id)
        if remaining > 0:
            return "waiting", w_name, remaining

    dest_chat = await run_db(get_worker_target_group, worker)

    if batch_id:
        # This was the last unresolved sibling in its session - combine and post ONE message
        # per type actually used in the session (status and/or daily_fact).
        for rt in ("status", "daily_fact"):
            await _post_resolved_batch_type_group_message(batch_id, telegram_id, rt, worker, dest_chat, w_name, context)
    else:
        # No session to wait for (a lone report, or a pre-migration row with no batch_id) -
        # post immediately, exactly as this function always worked before session tracking.
        copied_msg_id = None
        for m in media_rows:
            try:
                copied = await context.bot.copy_message(chat_id=dest_chat, from_chat_id=m["source_chat_id"], message_id=m["source_message_id"])
                await run_db(set_report_media_group_message, m["id"], copied.message_id)
                if copied_msg_id is None:
                    copied_msg_id = copied.message_id
            except Exception as e:
                logger.error(f"Ошибка пересылки видео вручную обработанного отчёта {report_id} в группу: {e}")

        def _fetch_report_row():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r

        report_row = await run_db(_fetch_report_row)
        notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name, clean_position(worker["position"]))
        try:
            sent = await context.bot.send_message(
                chat_id=dest_chat, text=notify_text, reply_markup=inline_kbd,
                reply_to_message_id=copied_msg_id, parse_mode="HTML"
            )
            await run_db(set_report_group_message, report_id, dest_chat, sent.message_id)
        except Exception as e:
            logger.error(f"Ошибка отправки карточки вручную обработанного отчёта {report_id} в группу: {e}")

    return "posted", w_name, 0

async def enqueue_media_report_item(user_id: int, context: ContextTypes.DEFAULT_TYPE, update: Update, text_content: str, now: datetime, duration_seconds: float | None = None):
    buf = MEDIA_BATCH_BUFFERS.setdefault(user_id, {"items": [], "task": None})
    buf["items"].append({"update": update, "text_content": text_content, "now": now, "duration": duration_seconds})

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
        # One id per debounce-window burst - shared by every report this batch's videos end
        # up creating, so a technical-failure report knows which OTHER reports came from the
        # same submission session (see batch_id on the `reports` table).
        batch_id = uuid.uuid4().hex[:12]
        logger.info(f"[media_batch] Начало обработки {len(buf['items'])} видео от пользователя {user_id} (batch_id={batch_id})")
        await process_media_batch(user_id, buf["items"], context, batch_id)
        logger.info(f"[media_batch] Обработка завершена для пользователя {user_id}")
    except Exception as exc:
        # Last-resort catch-all for a genuinely unexpected bug in the pipeline (every
        # EXPECTED failure mode - AITechnicalError from ASR/content-analysis, a bad
        # transcription - is already caught deeper inside process_media_batch and routed to
        # manual review without ever reaching here). The employee must never see the raw
        # exception text (it could be anything - a stack-trace-adjacent message, a raw
        # provider error), so only a fixed, generic phrase goes to them; the real detail is
        # logged server-side and also sent to admins so an unexpected crash doesn't go
        # unnoticed the way a silent background-sync failure would.
        logger.exception(f"Ошибка обработки пачки видео-отчетов пользователя {user_id}")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="❌ Не удалось обработать видео из-за технической ошибки.\n\nПопробуйте отправить ещё раз."
            )
        except Exception:
            pass
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "⚠️ Необработанная ошибка при обработке пачки видео-отчётов.\n"
                        f"Сотрудник (telegram_id): {user_id}\n"
                        "Подробности — в серверных логах."
                    )
                )
            except Exception:
                pass
    finally:
        try:
            user_lock.release()
        except Exception:
            pass

def is_late_by_tolerance(signed_diff_minutes: int) -> bool:
    """Single shared definition of "прислал поздно": strictly later than
    STATUS_LATE_TOLERANCE_MIN minutes past the slot's clock time (early is never late, no
    matter how early - exactly-at-tolerance still counts as on time, matching the historical
    behavior this replaces). Used by BOTH pick_target_status_slot (initial attribution) and
    the edit_time_ admin correction flow, which used to reimplement this same comparison
    independently - kept as one function so the two can never quietly drift apart if the
    tolerance or the comparison itself ever changes."""
    return signed_diff_minutes > STATUS_LATE_TOLERANCE_MIN

def pick_target_status_slot(schedule: list[str], now: datetime, submitted_slots: set, worker_name: str = "?"):
    """Attributes a status video to the schedule slot closest to it in clock time — always
    the globally nearest slot, submitted or not. Pure nearest-by-distance, no "must already
    be due" gate. The previous version only ever considered a slot once its clock time had
    numerically passed, so a video sent a few minutes BEFORE the next slot (e.g. 11:58 for
    a 12:00 slot) fell through to the older, already-passed 10:00 slot instead of the
    obviously-intended 12:00 one. Distance-based matching fixes that at the root, for every
    slot pair, instead of special-casing the boundary.

    BUG FIX: this used to exclude already-submitted slots from consideration entirely
    (`[s for s in schedule if s not in submitted_slots]`). That meant a second video sent
    for a slot that was already submitted just minutes earlier - e.g. one more clip at
    12:00 when 12:00 was already recorded - got excluded and reattributed to the nearest
    UNSUBMITTED slot instead, which can be hours away (observed: a video at 12:00 with
    12:00 already submitted got attributed to 10:00, 120 minutes off, purely because 12:00
    was filtered out of the candidate list). There was never a real need for this filter:
    once the nearest slot is chosen (submitted or not), the caller's existing-report lookup
    (get_existing_report_row) is exactly what decides whether this is a fresh report or a
    merge/addendum to the one already there - that's the correct place to react to "already
    submitted", not here. submitted_slots is kept as a parameter purely for the diagnostic
    log line below.

    Once a slot is picked, "вовремя" means received any time up to and including
    STATUS_LATE_TOLERANCE_MIN (grace period) minutes after the slot's clock time — an early
    submission is never late, no matter how early; only later than the grace period counts
    as "прислал поздно" (опоздание)."""
    current_mins = now.hour * 60 + now.minute

    best_slot, best_diff = None, None
    for slot in schedule:
        h, m = map(int, slot.split(":"))
        diff = abs(current_mins - (h * 60 + m))
        if best_diff is None or diff < best_diff:
            best_slot, best_diff = slot, diff

    h, m = map(int, best_slot.split(":"))
    slot_mins = h * 60 + m
    signed_diff = current_mins - slot_mins
    # BUG FIX: "опоздание" must only mean sent LATER than the acceptance window - an early
    # submission (signed_diff negative) is never late, no matter how early. The previous
    # version also flagged anything sent too far EARLY as "late", which is exactly backwards
    # (e.g. a video sent at 08:10 for a 10:00 slot was being marked "прислал поздно").
    is_late = is_late_by_tolerance(signed_diff)

    logger.info(
        "[Определение времени] Получено видео. "
        f"Сотрудник: {worker_name}. "
        f"Время Telegram (локальное, Europe/Chisinau): {now.strftime('%H:%M:%S')} ({now.strftime('%d.%m.%Y')}). "
        f"Расписание: {schedule}. Уже сдано сегодня: {sorted(submitted_slots) or 'ничего'}. "
        f"Допустимое опоздание: до +{STATUS_LATE_TOLERANCE_MIN} мин от времени слота (ранняя отправка опозданием не считается). "
        f"Определено окно сдачи: {best_slot} (расстояние {best_diff} мин). "
        f"Результат: видео успешно привязано к статусу за {best_slot}"
        + (f", прислал поздно (получено в {now.strftime('%H:%M')})." if is_late else ", вовремя.")
    )
    return best_slot, is_late

async def process_media_batch(user_id: int, items: list[dict], context: ContextTypes.DEFAULT_TYPE, batch_id: str | None = None):
    worker = await run_db(get_worker, user_id)
    if not worker:
        return

    sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
    w_name = f"{worker['last_name']} {worker['first_name']}"
    # LOGIC FIX: route by the worker's department group when one is configured (via
    # /set_object_group), falling back to the worker's own group_id - previously this
    # always used the worker's own group_id directly, so /set_object_group had no effect
    # on where reports actually got sent.
    dest_chat = await run_db(get_worker_target_group, worker)

    # Pipeline (strictly sequential per item): 1) speech-recognition quality gate -
    # assess_transcription_quality decides ONLY whether there is usable real speech at all,
    # completely independent of what type the report is. 2) type classification
    # (classify_report_type) - runs ONLY on text that already passed the quality gate, and
    # always resolves to status/daily_fact/mixed (never "can't tell"). This separation is the
    # whole point of this pass: a silent/garbled/empty video is a SPEECH problem
    # (manual_review_items, routed to admins), never a "couldn't determine type" problem.
    evaluated_items = []
    # Unified list for BOTH reasons a video ends up needing a human: failed the speech-quality
    # gate ("unrecognized") or hit an AITechnicalError during content analysis ("ai_error") -
    # both are saved as report_type='unrecognized_speech' and resolved via the same mr_
    # buttons, so batch-sibling tracking treats them identically regardless of which one
    # triggered it.
    manual_review_items = []
    for idx, item in enumerate(items, start=1):
        text_content = item["text_content"]
        now = item["now"]
        upd = item["update"]
        duration = item.get("duration")
        date_str = now.strftime("%Y-%m-%d")

        logger.info(f"[process_media_batch] Видео {idx}/{len(items)} пользователя {user_id}: текст='{text_content[:80]}'")

        quality = await assess_transcription_quality_async(text_content, duration)
        if not quality["ok"]:
            logger.info(f"[Проверка качества транскрибации] Видео {idx}/{len(items)} пользователя {user_id}: не распознано ({quality['reason']})")
            manual_review_items.append({"kind": "unrecognized", "text_content": text_content, "now": now, "upd": upd, "date_str": date_str, "error": None})
            continue

        classification = await classify_report_type_async(text_content)
        kind = classification["classification"]
        logger.info(f"[Классификация статус/факт] Видео {idx}/{len(items)} пользователя {user_id}: {kind}")

        if kind == "mixed":
            status_text = classification["status_part"] or text_content
            fact_text = classification["fact_part"] or text_content
            # Both halves must succeed to keep the status/fact split - if either hits a
            # technical error, there's no clean way to send just one half to manual review
            # (the admin judges the whole video), so the whole item goes to manual review
            # using the original, unsplit text_content.
            try:
                status_ai = await check_status_async(status_text, report_type_override="status")
                fact_ai = await check_status_async(fact_text, report_type_override="daily_fact")
                evaluated_items.append({
                    "item": item, "ai_res": status_ai, "report_type": "status",
                    "text_content": status_text, "now": now, "upd": upd, "date_str": date_str
                })
                evaluated_items.append({
                    "item": item, "ai_res": fact_ai, "report_type": "daily_fact",
                    "text_content": fact_text, "now": now, "upd": upd, "date_str": date_str
                })
            except AITechnicalError as e:
                manual_review_items.append({"kind": "ai_error", "text_content": text_content, "now": now, "upd": upd, "date_str": date_str, "error": e})
        else:
            try:
                ai_res_pre = await check_status_async(text_content, report_type_override=kind)
                evaluated_items.append({
                    "item": item, "ai_res": ai_res_pre, "report_type": kind,
                    "text_content": text_content, "now": now, "upd": upd, "date_str": date_str
                })
            except AITechnicalError as e:
                manual_review_items.append({"kind": "ai_error", "text_content": text_content, "now": now, "upd": upd, "date_str": date_str, "error": e})

    # Reports that failed the speech-recognition quality gate or hit a technical AI error
    # never reach automatic scoring - saved as their own type, forwarded to every admin's
    # PRIVATE chat (not the work group) with buttons to manually resolve the type. Saved in
    # TWO passes: first every row in this batch is written to the DB (so
    # count_unresolved_batch_siblings sees the true final count of this session from the very
    # first notification onward - notifying before every sibling row exists could let a fast
    # admin resolve undercount how many are still pending), THEN admins/employees are
    # notified once all of them exist.
    for m_item in manual_review_items:
        if m_item["kind"] == "unrecognized":
            report_id = await _save_unrecognized_speech_report(user_id, m_item["date_str"], m_item["now"], m_item["text_content"], m_item["upd"], batch_id)
        else:
            report_id, _ = await _save_ai_check_failure_report(user_id, m_item["date_str"], m_item["now"], m_item["text_content"], m_item["upd"], batch_id)
        m_item["report_id"] = report_id

    for m_item in manual_review_items:
        upd = m_item["upd"]
        now = m_item["now"]
        date_str = m_item["date_str"]
        text_content = m_item["text_content"]
        report_id = m_item["report_id"]
        if m_item["kind"] == "unrecognized":
            await notify_admins_unrecognized_speech(context, report_id, w_name, text_content, upd)
            try:
                await upd.message.reply_text(
                    "Не удалось разобрать текст в вашем видео.\n"
                    "Пожалуйста, в следующий раз чётче формулируйте, что именно вы делали."
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить личный фидбек по нераспознанной речи пользователю {user_id}: {e}")
            await notify_admins_unrecognized_speech_streak(context, user_id, w_name)
        else:
            error = m_item["error"]
            is_media = bool(upd.message.voice or upd.message.video or upd.message.video_note)
            await notify_admins_ai_check_failed(context, report_id, w_name, now.strftime("%H:%M:%S"), date_str, upd, error.message)
            try:
                confirm_text = "✅ Видео получено, ожидайте оценки." if is_media else "✅ Отчёт получен, ожидайте оценки."
                await upd.message.reply_text(confirm_text)
            except Exception as e:
                logger.warning(f"Не удалось отправить сотруднику {user_id} подтверждение получения при технической ошибке ИИ: {e}")
        await notify_admins_unrecognized_speech_streak(context, user_id, w_name)

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

        notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name, clean_position(worker["position"]))

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
                await upd.message.reply_text("✅ Факт получен и принят без замечаний.")
            else:
                await upd.message.reply_text(
                    f"❌ Факт НЕ ОК. {_short_issue_text([ai_res])}\n"
                    "Сотрудник контроля свяжется с вами и скажет замечания.\n"
                    "Напоминаем, что при частом допущении ошибок в сдаче отчетов, "
                    "проблема будет делегироваться руководству."
                )
        except Exception as e:
            logger.warning(f"Не удалось отправить личный фидбек по факту пользователю {user_id}: {e}")

    # 2. Process all status items TOGETHER as a single status report
    if status_items:
        status_now = status_items[0]["now"]
        date_str = status_items[0]["date_str"]

        submitted_slots = await run_db(get_submitted_status_slots, user_id, date_str)
        slot_time, is_late = pick_target_status_slot(sched_list, status_now, submitted_slots, w_name)

        existing = await run_db(get_existing_report_row, user_id, date_str, "status", slot_time)

        do_full_merge = False
        old_media_rows = []
        existing_media_count = 0
        if existing:
            try:
                prev_h, prev_m, prev_s = map(int, existing["received_at"].split(":"))
                elapsed_minutes = (status_now.hour * 60 + status_now.minute) - (prev_h * 60 + prev_m)
                do_full_merge = 0 <= elapsed_minutes <= MEDIA_MERGE_WINDOW_MINUTES
            except Exception:
                do_full_merge = False
            # BUG FIX: the video count used to continue numbering ("Видео 3", "Видео 4", ...)
            # was only ever fetched when do_full_merge was True (needed there to re-forward
            # the old videos). A later addendum outside the merge window skipped this
            # entirely, so existing_media_count silently stayed 0 and its new video(s) were
            # numbered starting back at "Видео 1" - colliding with the numbering the
            # original submission already used (both showing up as "Видео 1" in the
            # Комментарий/Оригинальный отчёт fields). Fetch the real count unconditionally.
            all_existing_media = await run_db(get_report_media, existing["id"])
            existing_media_count = len(all_existing_media)
            if do_full_merge:
                old_media_rows = all_existing_media

        if existing:
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
            report_id = existing["id"]

            error_count = overall_format_comment.lower().count("не ок")
            overall_required_action = REMARK_REQUIRED_ACTION_TEXT if error_count > 0 else "Ничего не предпринимать, всё в порядке"

            # LOGIC FIX: only bump received_at to "now" for a genuine full merge (a new video
            # arriving within MEDIA_MERGE_WINDOW_MINUTES of the first - still the same
            # submission in progress). A later addendum outside that window kept the original
            # received_at overwritten with the addendum's time, so a report submitted exactly
            # on time could retroactively become "прислал поздно" in Сводка purely because a
            # follow-up video arrived after the acceptance window - even though the original,
            # on-time submission is what should count for timeliness.
            received_at_to_store = status_now.strftime("%H:%M:%S") if do_full_merge else existing["received_at"]

            await run_db(
                update_report_text_and_ai,
                report_id=report_id,
                is_ok=overall_is_ok,
                format_comment=overall_format_comment,
                required_action=overall_required_action,
                raw_text=combined_raw_text,
                received_at=received_at_to_store
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
            # Forward only new videos. existing_media_count was already computed above from
            # the real report_media count (not just old_media_rows, which is only populated
            # for a full merge) - same fix as the numbering bug, applied here so a later
            # addendum's report_media rows get positions that continue after the existing
            # ones instead of restarting at 1 and colliding with them.
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

        notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name, clean_position(worker["position"]))

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

        # Reply directly to the employee in their DM for each video.
        # Short, non-technical wording only - the detailed per-video remarks stay in the
        # group/Sheets notification, not in the employee's personal feedback.
        if overall_is_ok:
            personal_text = f"✅ Статус за {slot_time} принят без замечаний."
            if is_late:
                personal_text += " Статус получен позже установленного времени."
        else:
            personal_text = (
                f"❌ Статус НЕ ОК. {_short_issue_text(res['ai_res'] for res in status_items)}\n"
                "Сотрудник контроля свяжется с вами и скажет замечания.\n"
                "Напоминаем, что при частом допущении ошибок в сдаче отчетов, "
                "проблема будет делегироваться руководству."
            )

        for s_item in status_items:
            upd = s_item["upd"]
            try:
                await upd.message.reply_text(personal_text)
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
                new_action = f"Комментарий видео {video_num} изменен отделом контроля складовки вручную: {new_comment}"
                conn.execute(
                    "UPDATE reports SET format_comment = ?, is_ok = ?, required_action = ? WHERE id = ?",
                    (new_format_comment, 1 if new_overall_ok else 0, new_action, report_id)
                )
            else:
                # We are editing the overall / general comment!
                new_action = f"Комментарий изменен отделом контроля складовки вручную: {new_comment}"
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
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)

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

        confirm_text = f"✅ Комментарий для Видео {video_num} обновлён." if video_num is not None else "✅ Комментарий обновлён."
        try:
            await context.bot.send_message(chat_id=original_chat_id or user_id, text=confirm_text)
        except Exception as e:
            logger.warning(f"Не удалось отправить подтверждение изменения комментария: {e}")
        return

    if is_admin(user_id) and context.user_data.get("editing_slot_time_report_id"):
        report_id = context.user_data["editing_slot_time_report_id"]
        original_chat_id = context.user_data.get("editing_slot_time_chat_id")
        original_msg_id = context.user_data.get("editing_slot_time_message_id")

        context.user_data.pop("editing_slot_time_report_id", None)
        context.user_data.pop("editing_slot_time_chat_id", None)
        context.user_data.pop("editing_slot_time_message_id", None)
        prompt_message_id = context.user_data.pop("editing_slot_time_prompt_message_id", None)

        if prompt_message_id and original_chat_id:
            try:
                await context.bot.delete_message(chat_id=original_chat_id, message_id=prompt_message_id)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass

        new_time = update.message.text.strip() if update.message.text else ""
        if not new_time or new_time.lower() in ("отмена", "❌ отмена"):
            return

        def _apply_manual_slot_time():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None, None, "not_found"
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            schedule = SCHEDULES.get(w["schedule"], SCHEDULE_A) if w else SCHEDULE_A

            if new_time not in schedule:
                conn.close()
                return r, w, schedule, "invalid_time"

            if new_time != r["slot_time"]:
                # Guard against creating a second report for a slot that already has one -
                # exactly the kind of duplicate-slot mess this button exists to clean up
                # after, not create more of.
                conflict = conn.execute(
                    "SELECT id FROM reports WHERE telegram_id = ? AND report_date = ? "
                    "AND report_type = 'status' AND slot_time = ? AND id != ?",
                    (r["telegram_id"], r["report_date"], new_time, report_id)
                ).fetchone()
                if conflict:
                    conn.close()
                    return r, w, schedule, "conflict"

            new_is_late = bool(r["is_late"])
            try:
                rh, rm = int(r["received_at"][0:2]), int(r["received_at"][3:5])
                h, m = map(int, new_time.split(":"))
                diff = (rh * 60 + rm) - (h * 60 + m)
                new_is_late = is_late_by_tolerance(diff)
            except Exception:
                pass

            conn.execute("UPDATE reports SET slot_time = ?, is_late = ? WHERE id = ?", (new_time, int(new_is_late), report_id))
            conn.commit()
            r2 = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r2, w, schedule, "ok"

        report, worker, schedule, status = await run_db(_apply_manual_slot_time)
        if not report:
            return

        if status == "invalid_time":
            try:
                await context.bot.send_message(
                    chat_id=original_chat_id or user_id,
                    text=f"❌ Неверное время. Введите одно из значений расписания сотрудника: {', '.join(schedule)}."
                )
            except Exception:
                pass
            return

        if status == "conflict":
            try:
                await context.bot.send_message(
                    chat_id=original_chat_id or user_id,
                    text=f"❌ У сотрудника уже есть отдельный отчёт за {new_time} в этот день. Слияние вручную не производится."
                )
            except Exception:
                pass
            return

        async_sync_gsheets_background()

        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)

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
                logger.error(f"Ошибка при обновлении сообщения после изменения времени сдачи: {e}")

        try:
            await context.bot.send_message(chat_id=original_chat_id or user_id, text=f"✅ Время сдачи изменено на {new_time}.")
        except Exception as e:
            logger.warning(f"Не удалось отправить подтверждение изменения времени сдачи: {e}")
        return

    if is_admin(user_id) and context.user_data.get("editing_action_report_id"):
        report_id = context.user_data["editing_action_report_id"]
        original_chat_id = context.user_data.get("editing_action_chat_id")
        original_msg_id = context.user_data.get("editing_action_message_id")

        context.user_data.pop("editing_action_report_id", None)
        context.user_data.pop("editing_action_chat_id", None)
        context.user_data.pop("editing_action_message_id", None)
        prompt_message_id = context.user_data.pop("editing_action_prompt_message_id", None)

        if prompt_message_id and original_chat_id:
            try:
                await context.bot.delete_message(chat_id=original_chat_id, message_id=prompt_message_id)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass

        new_action_text = update.message.text.strip() if update.message.text else ""
        if not new_action_text or new_action_text.lower() in ("отмена", "❌ отмена"):
            return

        def _apply_action_override():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None
            conn.execute(
                "UPDATE reports SET required_action = ?, action_override = 1 WHERE id = ?",
                (new_action_text, report_id)
            )
            conn.commit()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            conn.close()
            return r, w

        report, worker = await run_db(_apply_action_override)
        if not report:
            return

        async_sync_gsheets_background()

        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)

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
                logger.error(f"Ошибка при обновлении сообщения после изменения требуемых действий: {e}")

        try:
            await context.bot.send_message(chat_id=original_chat_id or user_id, text="✅ Требуемые действия обновлены.")
        except Exception as e:
            logger.warning(f"Не удалось отправить подтверждение изменения требуемых действий: {e}")
        return

    if is_admin(user_id):
        return

    # BUG FIX: only process a report sent as a private DM to the bot. A worker who sends
    # their video/voice/text report into a group chat by mistake (the working group, the
    # control department group, etc.) must not have it analyzed, saved to the DB, or
    # announced anywhere - the bot stays completely silent, as if it never saw the message.
    # Placed here (after the admin edit-comment/edit-time interception above, and after the
    # "is_admin -> return" gate) rather than at the very top of the function, because those
    # two admin flows legitimately happen IN the group chat - the "✏️ Изменить комментарий"/
    # "🕐 Изменить время сдачи" text-input prompt is posted as a reply to the report card,
    # which lives in the group, so the admin's reply also arrives there. By this point the
    # sender is confirmed to be a non-admin worker, so this check only ever affects them.
    if update.effective_chat.type != "private":
        return

    from bot import get_user_lock
    lock = get_user_lock(user_id)
    await lock.acquire()

    text_content = ""
    tmp_path = None

    try:
        worker = await run_db(get_worker, user_id)

        if worker and await run_db(is_missed_reason_request_enabled):
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
                        resolved_slots = await run_db(resolve_pending_reason_requests, user_id, reason_text)
                        async_sync_gsheets_background()
                        await update.message.reply_text(
                            "✅ Спасибо, причина зафиксирована. Теперь можете отправить видео-отчёт.",
                            reply_markup=menu_for_user(user_id, update.effective_chat.type)
                        )
                        # Forward the reason to the control department group, tagged to the
                        # exact missed slot(s) it was given for (already stored per-slot in
                        # missed_status_reasons, not against "now").
                        w_name = f"{worker['last_name']} {worker['first_name']}"
                        dest_chat = await run_db(get_worker_target_group, worker)
                        slots_str = ", ".join(
                            f"{p['slot_time']} ({format_show_date(p['report_date'])})" for p in resolved_slots
                        )
                        try:
                            await context.bot.send_message(
                                chat_id=dest_chat,
                                text=(
                                    f"📋 Причина несдачи статуса — {w_name}\n"
                                    f"Пропущено: {slots_str}\n"
                                    f"Причина: {reason_text}"
                                )
                            )
                        except Exception as e:
                            logger.error(f"Не удалось переслать причину несдачи статуса в группу: {e}")
                        return

        is_media = bool(update.message.voice or update.message.video or update.message.video_note)
        media_duration = None

        if update.message.text:
            text_content = update.message.text.strip()
        else:
            file_obj = None
            if update.message.voice: file_obj = update.message.voice
            elif update.message.video: file_obj = update.message.video
            elif update.message.video_note: file_obj = update.message.video_note

            if file_obj:
                already_processed = await run_db(
                    is_message_already_processed, update.effective_chat.id, update.message.message_id
                )
                if already_processed:
                    logger.warning(
                        f"[dedup] Пропускаю повторно доставленное сообщение message_id={update.message.message_id} "
                        f"от user_id={user_id} — это видео уже было обработано ранее."
                    )
                    return

                media_duration = getattr(file_obj, "duration", None)
                await update.message.reply_text("📹 Видео получено, ожидайте оценки:")
                tg_file = await context.bot.get_file(file_obj.file_id)
                ext = "mp4" if update.message.video or update.message.video_note else "ogg"

                os.makedirs("tmp", exist_ok=True)
                tmp_path = f"tmp/file_{user_id}_{int(datetime.now().timestamp())}.{ext}"

                await tg_file.download_to_drive(tmp_path)
                logger.info(f"[handler] Начало транскрипции файла {tmp_path} для пользователя {user_id}")
                try:
                    text_content = await transcribe_audio_async(tmp_path)
                except AITechnicalError as e:
                    # An ASR-level infra failure (rate limit/timeout) is NOT "the worker
                    # spoke unclearly" - straight to the same manual-review mechanism as any
                    # other technical AI failure, no retries, no automatic blame.
                    asr_now = now_local()
                    w_name_for_asr = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {user_id}"
                    await handle_ai_check_failure(
                        context, user_id, w_name_for_asr, asr_now.strftime("%Y-%m-%d"), asr_now, "", update, e
                    )
                    return
                logger.info(f"[handler] Транскрипция завершена: '{text_content[:80]}'")

        # Media (voice/video/video_note) always proceeds from here, even on an empty or
        # technically-failed transcription - assess_transcription_quality() (run per item
        # inside process_media_batch) is the single, dedicated place that recognizes "no
        # usable speech" and routes it to admin review, instead of a separate dead-end error
        # message here that never saved or notified anyone about the report at all. Only a
        # genuinely empty TEXT message (not media - Telegram text is never "unrecognized
        # speech") is rejected this early.
        if not is_media and not text_content:
            await update.message.reply_text("Ошибка: не удалось прочитать текстовый отчёт.")
            return

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
                "Ваш отчет отправлен в отдел контроля складовки как временный.\n\n"
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
                dest_chat = await run_db(get_worker_target_group, worker)
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
            await enqueue_media_report_item(user_id, context, update, text_content, now, media_duration)
            return

        # Handle purely text status report (worker typewriting)
        date_str = now.strftime("%Y-%m-%d")
        sched_list = SCHEDULES.get(worker["schedule"], SCHEDULE_A)
        w_name = f"{worker['last_name']} {worker['first_name']}"

        async def _process_one_text_report(rt: str, part_text: str):
            """Processes a single classified report (status or daily_fact) from a text
            message - the same save-or-merge-then-notify logic used regardless of whether
            classify_report_type found exactly one type or split a "mixed" message into two.
            """
            if rt == "status":
                submitted_slots = await run_db(get_submitted_status_slots, user_id, date_str)
                nearest_slot, is_late = pick_target_status_slot(sched_list, now, submitted_slots, w_name)
            else:
                nearest_slot, is_late = None, False

            existing_report = await run_db(get_existing_report_row, user_id, date_str, rt, nearest_slot)

            try:
                ai_res = await check_status_async(part_text, report_type_override=rt)
            except AITechnicalError as e:
                # Infra failure, not a verdict on the report - straight to the same manual-
                # review mechanism as "не удалось распознать речь" (see handle_ai_check_
                # failure), no retries.
                await handle_ai_check_failure(context, user_id, w_name, date_str, now, part_text, update, e)
                return

            is_addon = False
            if existing_report:
                is_addon = True
                report_id = existing_report["id"]
                action_text = REMARK_REQUIRED_ACTION_TEXT if (ai_res["report_type"] == "status" and not ai_res["is_ok"]) else ai_res["required_action"]

                await run_db(
                    update_report_text_and_ai,
                    report_id=report_id,
                    is_ok=ai_res["is_ok"],
                    format_comment=ai_res["format_comment"],
                    required_action=action_text,
                    raw_text=part_text,
                    received_at=now.strftime("%H:%M:%S")
                )
            else:
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
                    raw_text=part_text
                )
            async_sync_gsheets_background()

            if ai_res["report_type"] == "status" and not ai_res["is_ok"]:
                await notify_admins_if_remark_threshold_crossed(context, user_id, w_name)
            is_status = ai_res["report_type"] == "status" and nearest_slot
            label = "Статус" if is_status else "Факт"

            if ai_res["is_ok"]:
                if is_status:
                    personal_text = f"✅ Статус за {nearest_slot} принят без замечаний."
                    if is_late:
                        personal_text += " Статус получен позже установленного времени."
                else:
                    personal_text = "✅ Факт получен и принят без замечаний."
                await update.message.reply_text(personal_text)
            else:
                await update.message.reply_text(
                    f"❌ {label} НЕ ОК. {_short_issue_text([ai_res])}\n"
                    "Сотрудник контроля свяжется с вами и скажет замечания.\n"
                    "Напоминаем, что при частом допущении ошибок в сдаче отчетов, "
                    "проблема будет делегироваться руководству."
                )

            dest_chat = await run_db(get_worker_target_group, worker)

            def _fetch_report_row():
                conn = get_db()
                r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
                conn.close()
                return r

            report_row = await run_db(_fetch_report_row)
            notify_text, inline_kbd = await render_report_message_from_row(report_row, w_name, clean_position(worker["position"]))

            if is_addon and existing_report and existing_report["group_chat_id"] and existing_report["group_message_id"]:
                try:
                    await context.bot.delete_message(chat_id=existing_report["group_chat_id"], message_id=existing_report["group_message_id"])
                except Exception:
                    pass

            try:
                sent_notify_msg = await context.bot.send_message(
                    chat_id=dest_chat, text=notify_text, reply_markup=inline_kbd, parse_mode="HTML"
                )
                await run_db(set_report_group_message, report_id, dest_chat, sent_notify_msg.message_id)
            except Exception as e:
                logger.error(f"Ошибка отправки оценки в чат {dest_chat}: {e}")

        # Type is decided purely by meaning (classify_report_type), same classification step
        # as the video path above. A typed text message is never a "speech recognition"
        # problem (there's no ASR involved), so it always resolves to status/daily_fact,
        # optionally split into two via "mixed" - never "can't tell".
        classification = await classify_report_type_async(text_content)
        kind = classification["classification"]
        logger.info(f"[Классификация статус/факт] Текстовый отчёт пользователя {user_id}: {kind}")

        if kind == "mixed":
            await _process_one_text_report("status", classification["status_part"] or text_content)
            await _process_one_text_report("daily_fact", classification["fact_part"] or text_content)
        else:
            await _process_one_text_report(kind, text_content)
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
        await query.answer("⛔ Недостаточно прав. Действие доступно только отделу контроля.", show_alert=True)
        return
        
    data = query.data
    logger.info(f"Получен callback_query: {data} от администратора {user_id}")

    # Workers-database sync confirmation buttons (see admin_handlers.handle_workers_file_
    # upload) - deferred import to avoid a circular import (admin_handlers already imports
    # from report_handlers at module load time).
    if data.startswith("wsync_"):
        from admin_handlers import handle_workers_sync_callback
        await handle_workers_sync_callback(query, context, data)
        return

    # 0a. Manual review menu navigation - "◀️ Назад" from the reason sub-menu back to the
    # initial 4-button menu. Pure UI state, encoded entirely in which keyboard THIS admin's
    # own copy of the message currently shows - nothing is read from or written to the
    # database here, so there's no shared state to race on between admins or clicks.
    if data.startswith("mr_back_"):
        report_id = int(data.split("_")[-1])
        try:
            await query.edit_message_reply_markup(reply_markup=make_manual_review_keyboard(report_id))
        except Exception as e:
            logger.error(f"Ошибка при возврате к меню ручной оценки отчёта {report_id}: {e}")
        return

    # 0b. "⚠️ Статус/Факт НЕ ОК" - not a verdict yet, just swaps this admin's own copy of the
    # keyboard to the reason sub-menu. Checked before the general "mr_" dispatch below since
    # both share the "mr_status_"/"mr_fact_" prefix.
    if data.startswith("mr_status_bad_") or data.startswith("mr_fact_bad_"):
        rtype = "status" if data.startswith("mr_status_bad_") else "fact"
        report_id = int(data.split("_")[-1])
        try:
            await query.edit_message_reply_markup(reply_markup=make_manual_review_reason_keyboard(report_id, rtype))
        except Exception as e:
            logger.error(f"Ошибка при показе меню причин ручной оценки отчёта {report_id}: {e}")
        return

    # 0c. Final verdict for a report stuck at report_type='unrecognized_speech' (speech
    # recognition quality gate failed, or an AI technical failure) - type AND verdict
    # (ОК, or a specific не-ОК reason) are both given directly by this one tap, no further AI
    # call. Self-locking across every admin that got a copy of this notification, so only the
    # first admin to reach a final verdict does anything - a second click (by the same admin
    # re-tapping, or a different admin who was mid-navigation on their own copy) is a no-op
    # "conflict".
    if data.startswith("mr_status_") or data.startswith("mr_fact_"):
        parts = data.split("_")
        chosen_type = "status" if parts[1] == "status" else "daily_fact"
        verdict_token = parts[2]
        report_id = int(parts[-1])
        is_ok = verdict_token == "ok"
        reason_code = None if is_ok else verdict_token

        status, w_name, remaining = await resolve_unrecognized_speech_report(
            report_id, chosen_type, is_ok, reason_code, context
        )
        if status == "conflict":
            await query.answer("Этот отчёт уже был обработан другим администратором.", show_alert=True)
            return

        admin_name = update.effective_user.first_name or str(user_id)
        type_label = "Статус" if chosen_type == "status" else "Факт"
        verdict_label = "ОК" if is_ok else f"НЕ ОК ({MANUAL_REVIEW_REASON_TEXT.get(reason_code, 'не указал объём работы')})"
        if status == "waiting":
            # Other videos from this same submission session are still unlabeled - the group
            # post is deliberately held back until every one of them is resolved, so the
            # admin needs to see clearly that their click registered but nothing was posted
            # yet (otherwise it can look like the button silently did nothing).
            plural_suffix = "о" if remaining == 1 else ""
            resolved_note = (
                f"⚠️ Не удалось автоматически обработать отчёт.\n"
                f"Сотрудник: {html.escape(w_name)}\n\n"
                f"✅ Обработано администратором {html.escape(admin_name)} — {type_label}, {verdict_label}\n"
                f"⏳ Ожидание остальных видео из этой отправки (ещё не размечен{plural_suffix} {remaining}) — "
                "сообщение в группу будет отправлено, когда все видео этой отправки будут размечены."
            )
        else:
            resolved_note = (
                f"⚠️ Не удалось автоматически обработать отчёт.\n"
                f"Сотрудник: {html.escape(w_name)}\n\n"
                f"✅ Обработано администратором {html.escape(admin_name)} — {type_label}, {verdict_label}"
            )
        review_msgs = await run_db(get_speech_review_messages, report_id)
        for rm in review_msgs:
            try:
                await context.bot.edit_message_text(
                    chat_id=rm["admin_chat_id"], message_id=rm["message_id"],
                    text=resolved_note, reply_markup=None
                )
            except Exception as e:
                logger.warning(f"Не удалось обновить сообщение админу {rm['admin_chat_id']} после обработки отчёта {report_id}: {e}")
        return

    # 0b. Expand/collapse the "⚙️ Действия" button into the 4 action buttons and back.
    # is_admin(user_id) was already checked above, at the top of this function, before this
    # dispatch is even reached - so a non-admin tapping "⚙️ Действия" never gets here at all,
    # matching "проверка прав И на разворачивание, И на каждое действие отдельно".
    if data.startswith("actions_expand_"):
        report_id = int(data.split("_")[-1])
        try:
            await query.edit_message_reply_markup(reply_markup=make_report_actions_expanded_keyboard(report_id))
        except Exception as e:
            logger.error(f"Ошибка при разворачивании меню действий отчёта {report_id}: {e}")
        return

    if data.startswith("actions_collapse_"):
        report_id = int(data.split("_")[-1])
        try:
            await query.edit_message_reply_markup(reply_markup=make_report_keyboard(report_id))
        except Exception as e:
            logger.error(f"Ошибка при сворачивании меню действий отчёта {report_id}: {e}")
        return

    # 0c. Cancel a pending text-input edit (comment - overall or a specific video - or slot
    # time). Works no matter which of those was in flight (only one can be at a time per
    # admin), pops every possible editing_* key defensively, deletes the prompt message this
    # button lives on, and restores the report card to its normal collapsed state. No changes
    # are saved.
    if data.startswith("cancel_edit_"):
        report_id = int(data.split("_")[-1])
        original_chat_id = (
            context.user_data.get("editing_comment_chat_id")
            or context.user_data.get("editing_slot_time_chat_id")
            or context.user_data.get("editing_action_chat_id")
        )
        original_msg_id = (
            context.user_data.get("editing_comment_message_id")
            or context.user_data.get("editing_slot_time_message_id")
            or context.user_data.get("editing_action_message_id")
        )
        for key in (
            "editing_comment_report_id", "editing_comment_video_num", "editing_comment_chat_id",
            "editing_comment_message_id", "editing_comment_original_text", "editing_comment_prompt_message_id",
            "editing_slot_time_report_id", "editing_slot_time_chat_id", "editing_slot_time_message_id",
            "editing_slot_time_prompt_message_id",
            "editing_action_report_id", "editing_action_chat_id", "editing_action_message_id",
            "editing_action_prompt_message_id",
        ):
            context.user_data.pop(key, None)

        try:
            await query.message.delete()
        except Exception:
            pass

        def _fetch_report_and_worker_for_cancel():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone() if r else None
            conn.close()
            return r, w

        report, worker = await run_db(_fetch_report_and_worker_for_cancel)
        if report and original_chat_id and original_msg_id:
            worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
            position = clean_position(worker["position"]) if worker else "?"
            text, kbd = await render_report_message_from_row(report, worker_name, position)
            try:
                await context.bot.edit_message_text(
                    chat_id=original_chat_id, message_id=original_msg_id,
                    text=text, reply_markup=kbd, parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка при восстановлении карточки отчёта {report_id} после отмены редактирования: {e}")
        return

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
            text, kbd = await render_report_message_from_row(report, worker_name, clean_position(worker["position"]))
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
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)
        try:
            await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка при изменении типа отчета: {e}")
        return

    # 2a-bis. Toggle the "опоздание" flag - independent of content-quality оценка (fix_toggle_).
    # Root cause this button fixes: the group message's ✅/⚠️ verdict is effective_not_ok =
    # (not is_ok) OR is_late, but fix_toggle_ only ever flips is_ok - so on a late report,
    # toggling "Оценка" could never make the verdict show ✅, no matter how many times it was
    # clicked (is_late stayed stuck at 1 forever). This button flips is_late directly.
    if data.startswith("toggle_late_"):
        report_id = int(data.split("_")[-1])

        def _toggle_late():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not r:
                conn.close()
                return None, None

            new_is_late = 0 if r["is_late"] == 1 else 1
            conn.execute("UPDATE reports SET is_late = ? WHERE id = ?", (new_is_late, report_id))
            conn.commit()

            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone()
            conn.close()
            return r, w

        report, worker = await run_db(_toggle_late)
        if not report:
            return

        async_sync_gsheets_background()

        worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)
        try:
            await query.edit_message_text(text=text, reply_markup=kbd, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка при изменении пометки опоздания: {e}")
        return

    # 2b. Edit slot time -> ask for the correct time from this worker's own schedule
    if data.startswith("edit_time_"):
        report_id = int(data.split("_")[-1])

        def _fetch_report_and_worker_for_time():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r["telegram_id"],)).fetchone() if r else None
            conn.close()
            return r, w

        report, worker = await run_db(_fetch_report_and_worker_for_time)
        if not report or report["report_type"] != "status":
            return

        await _cancel_any_pending_edit_flow(context)

        schedule = SCHEDULES.get(worker["schedule"], SCHEDULE_A) if worker else SCHEDULE_A
        context.user_data["editing_slot_time_report_id"] = report_id
        context.user_data["editing_slot_time_chat_id"] = query.message.chat.id
        context.user_data["editing_slot_time_message_id"] = query.message.message_id

        # Disable the card's own buttons while a text reply is pending - otherwise the
        # expanded action menu stays live and clickable, and a second tap on it (e.g.
        # "🕒 Изменить время" again, or another action) would silently overwrite this
        # editing session instead of being blocked.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        prompt_msg = await query.message.reply_text(
            f"🕐 Введите правильное время сдачи из расписания сотрудника ({', '.join(schedule)}):",
            reply_markup=make_cancel_edit_keyboard(report_id)
        )
        context.user_data["editing_slot_time_prompt_message_id"] = prompt_msg.message_id
        return

    # Manual override for "Требуемые действия" - lets an admin write their own text instead
    # of the auto-computed "ничего не предпринимать"/"сделано замечание.../делегировано..."
    if data.startswith("edit_action_"):
        report_id = int(data.split("_")[-1])

        def _fetch_report_for_action():
            conn = get_db()
            r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            conn.close()
            return r

        report = await run_db(_fetch_report_for_action)
        if not report:
            return

        await _cancel_any_pending_edit_flow(context)

        context.user_data["editing_action_report_id"] = report_id
        context.user_data["editing_action_chat_id"] = query.message.chat.id
        context.user_data["editing_action_message_id"] = query.message.message_id

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        prompt_msg = await query.message.reply_text(
            "📋 Введите текст для «Требуемые действия»:",
            reply_markup=make_cancel_edit_keyboard(report_id)
        )
        context.user_data["editing_action_prompt_message_id"] = prompt_msg.message_id
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
            # Re-reads the CURRENT row and computes+writes the flip in ONE uninterrupted
            # thread call, with no `await` between reading is_ok/format_comment and writing
            # the toggled result. Previously the read happened in _fetch_report above and the
            # decision (new_is_ok/new_format_comment) was computed here in the async function
            # body, from data that could already be stale by the time _apply_fix_toggle
            # actually wrote it - a fast double-tap on this same button could let both clicks
            # read the same pre-toggle state and both write the same "flipped once" result,
            # silently losing one of the two intended flips.
            def _apply_fix_toggle():
                conn = get_db()
                r = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
                if not r:
                    conn.close()
                    return None, None
                new_is_ok = 0 if r["is_ok"] == 1 else 1
                new_format_comment = r["format_comment"] or ""
                items = parse_video_comments(r["format_comment"])
                if items:
                    items[0]["is_ok"] = (new_is_ok == 1)
                    new_format_comment = rebuild_format_comment(items)
                conn.execute("UPDATE reports SET is_ok = ?, format_comment = ? WHERE id = ?", (new_is_ok, new_format_comment, report_id))
                conn.commit()
                r2 = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
                w = conn.execute("SELECT * FROM workers WHERE telegram_id = ?", (r2["telegram_id"],)).fetchone()
                conn.close()
                return r2, w

            report, worker = await run_db(_apply_fix_toggle)
            if not report:
                return

            async_sync_gsheets_background()

            worker_name = f"{worker['last_name']} {worker['first_name']}" if worker else f"ID {report['telegram_id']}"
            position = clean_position(worker["position"]) if worker else "?"
            text, kbd = await render_report_message_from_row(report, worker_name, position)
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
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)
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
        position = clean_position(worker["position"]) if worker else "?"
        text, kbd = await render_report_message_from_row(report, worker_name, position)
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
            await _cancel_any_pending_edit_flow(context)

            context.user_data["editing_comment_report_id"] = report_id
            context.user_data["editing_comment_video_num"] = None
            context.user_data["editing_comment_chat_id"] = query.message.chat.id
            context.user_data["editing_comment_message_id"] = query.message.message_id
            context.user_data["editing_comment_original_text"] = query.message.text

            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            prompt_msg = await query.message.reply_text(
                "✏️ Введите новый комментарий к отчету:",
                reply_markup=make_cancel_edit_keyboard(report_id)
            )
            context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        return

    # 7. Edit Overall Comment
    if data.startswith("ed_overall_"):
        report_id = int(data.split("_")[-1])
        await _cancel_any_pending_edit_flow(context)
        context.user_data["editing_comment_report_id"] = report_id
        context.user_data["editing_comment_video_num"] = None
        context.user_data["editing_comment_chat_id"] = query.message.chat.id
        context.user_data["editing_comment_message_id"] = query.message.message_id
        context.user_data["editing_comment_original_text"] = query.message.text

        # Disable the still-live "Видео 1/2/3" selection menu the moment ONE option is
        # picked - this is what prevents a second, overlapping tap (e.g. on "Видео 2" while
        # this prompt is pending) from silently hijacking the pending text capture.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        prompt_msg = await query.message.reply_text(
            "✏️ Введите новый общий комментарий к отчету:",
            reply_markup=make_cancel_edit_keyboard(report_id)
        )
        context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        return

    # 8. Edit Specific Video Comment
    if data.startswith("ed_vid_"):
        parts = data.split("_")
        report_id = int(parts[2])
        video_num = int(parts[3])

        await _cancel_any_pending_edit_flow(context)
        context.user_data["editing_comment_report_id"] = report_id
        context.user_data["editing_comment_video_num"] = video_num
        context.user_data["editing_comment_chat_id"] = query.message.chat.id
        context.user_data["editing_comment_message_id"] = query.message.message_id
        context.user_data["editing_comment_original_text"] = query.message.text

        # Same as ed_overall_ above - disable the video-selection menu immediately so a
        # second "Видео N" tap can't land while this one's answer is still pending.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        
        prompt_msg = await query.message.reply_text(
            f"✏️ Введите новый комментарий для Видео {video_num}:",
            reply_markup=make_cancel_edit_keyboard(report_id)
        )
        context.user_data["editing_comment_prompt_message_id"] = prompt_msg.message_id
        return
