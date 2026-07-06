import os
import re
import hashlib
import json
import logging
from groq import Groq

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
groq_client = Groq(api_key=GROQ_API_KEY, timeout=120.0) if GROQ_API_KEY else None

_ai_status_cache = {}
_ai_clean_cache = {}
_ai_classify_cache = {}

# Same silence/hallucination signal words normalize_ai_result() already treats as "no real
# report to analyze" - assess_transcription_quality() checks this FIRST, BEFORE the type
# (status/fact) is ever considered, so a silent/garbled/meaningless video is recognized as a
# speech-recognition problem at its own dedicated pipeline stage instead of being fed into
# type classification at all.
_NO_CONTENT_MARKERS = (
    "продолжение следует", "на видео молчал", "[без звука]", "[тишина]", "[вздох]",
    "без звука", "тишина", "музыка", "молчание", "молчал", "молчит", "шум",
    "неразборчиво", "неразборчивый текст", "шумы", "помехи", "неразборчивая речь",
)

# Prefixes transcribe_audio() itself uses to report a technical failure (API error, missing
# key) rather than an actual transcription - treated as the same "couldn't get usable
# speech" outcome as silence/noise, not as real (if odd) report content.
_TRANSCRIPTION_ERROR_PREFIXES = (
    "Ошибка распознавания аудио",
    "Не задан GROQ_API_KEY",
)

# Tunable thresholds for assess_transcription_quality(). Both are 0 (no minimum) per current
# product decision: rather than reject on an arbitrary duration/word-count cutoff, quality is
# judged by whether the transcription is genuinely empty, is a known silence/noise marker, or
# is a technical transcription error - a real (even if short/terse) transcribed report is
# left for the normal is_ok/issue content check further down the pipeline, not rejected here.
MIN_REPORT_DURATION_SECONDS = 0
MIN_MEANINGFUL_WORDS = 0

def assess_transcription_quality(text: str, duration_seconds: float | None = None) -> dict:
    """Dedicated speech-recognition quality gate, run BEFORE type classification and BEFORE
    the content (is_ok) check. Answers exactly one question: is there usable, real speech to
    analyze at all? This is deliberately separate from "what type is this report" and "is the
    described work good enough" - those are different questions asked by later stages, only
    once this one has already passed.
    Returns {"ok": True} when there is real speech to work with, or {"ok": False, "reason":
    one of "too_short_duration"/"empty"/"no_content_marker"/"transcription_error"/
    "too_few_words"} when speech recognition itself failed and the report needs a human to
    listen to the original video instead of being auto-analyzed."""
    if duration_seconds is not None and duration_seconds <= MIN_REPORT_DURATION_SECONDS:
        return {"ok": False, "reason": "too_short_duration"}

    lower_src = (text or "").lower().strip()
    if not lower_src:
        return {"ok": False, "reason": "empty"}

    if any((text or "").startswith(prefix) for prefix in _TRANSCRIPTION_ERROR_PREFIXES):
        return {"ok": False, "reason": "transcription_error"}

    if any(marker in lower_src for marker in _NO_CONTENT_MARKERS):
        return {"ok": False, "reason": "no_content_marker"}

    word_count = len(re.findall(r"[a-zA-Zа-яА-ЯёЁ]{2,}", text or ""))
    if word_count < MIN_MEANINGFUL_WORDS:
        return {"ok": False, "reason": "too_few_words"}

    return {"ok": True, "reason": ""}

def get_md5(text: str) -> str:
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()

def transcribe_audio(file_path: str) -> str:
    if groq_client is None:
        return "Не задан GROQ_API_KEY, аудио не распознано."
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
        logger.error(f"Ошибка распознавания аудио: {e}")
        return f"Ошибка распознавания аудио: {e}"

CHECK_PROMPT_TEMPLATE = """
Ты — опытный прораб строительного объекта.
Твоя задача — проверить отчёт рабочего.
Рабочие могут писать коротко, с ошибками, простыми словами.
Ты должен понимать смысл, а не искать идеальную формулировку.

Главный вопрос: СДЕЛАЛ ЛИ ЧЕЛОВЕК РАБОТУ ИЛИ НЕТ?

{mode_instruction}

=====================
КАК ОЦЕНИВАТЬ
=====================
Отчёт считается ХОРОШИМ, если понятно:
- какую работу выполнял человек
- с чем он работал
- какой процесс выполнялся
Не требуй обязательно цифры.

Примеры ХОРОШИХ отчётов:
"работал на дробилке" -> хорошо
"стоял на кране, подавал материал" -> хорошо
"делал опалубку" -> хорошо

=====================
ПЛОХИЕ ОТЧЁТЫ
=====================
Отклонять: "работаю", "в процессе", "нормально", "всё сделал", "на объекте", "занимаюсь", если невозможно понять, что именно делал человек. Если в отчете не указан объем или детали работы, причиной (issue) напиши строго "не указал объем работы".

Если текст пустой, или рабочий на видео ничего не говорил, причиной (issue) напиши строго "на видео молчал".
Если в тексте написано "неразборчиво" или речь совсем непонятна, причиной (issue) напиши строго "неразборчивый текст".

=====================
ФОРМАТ
=====================
Ответь только JSON:
{{
{json_type_field}"is_ok": true или false,
"issue": "причина (например: 'не указал объем работы', 'на видео молчал', 'неразборчивый текст' или краткое описание замечания если is_ok=false). Если is_ok=true, то оставь пустым.",
"format_comment": "краткое резюме сделанного дела на русском языке без лишних слов (например: 'сварка перемычек', 'уборка берега', 'сказал что сделал'). Если не ок - укажи причину.",
"required_action": "что написать сотруднику",
"employee_message": "короткое сообщение сотруднику"
}}

Отчёт:
{text}
"""

CLASSIFY_PROMPT_TEMPLATE = """
Ты определяешь тип отчёта строительного рабочего по СМЫСЛУ его речи — СТАТУС, ФАКТ, или оба сразу.

СТАТУС — рабочий рассказывает, что делал за КОРОТКИЙ, недавний период (последние 1-2 часа,
с утра, прямо сейчас).
Слова-сигналы: "за последние 2 часа", "за последний час", "с утра", "только что", "сейчас", "буквально".

ФАКТ — рабочий подводит ИТОГ ВСЕГО рабочего дня целиком: что сделано за весь день.
Слова-сигналы: "за весь день", "на сегодня", "по итогам дня", "весь день", "в целом за день".

Определяй не только по этим словам буквально, но и по смыслу — если рабочий сказал то же
самое другими словами (например, "с самого начала смены" — это тоже про весь день = ФАКТ).

Если рабочий В ОДНОМ сообщении говорит И про короткий период (статус), И про итог всего дня
(факт) — раздели его речь на два отдельных фрагмента и верни classification = "mixed".

Если ни один явный признак ФАКТА не подходит — считай это СТАТУСОМ (это тип по умолчанию).
НЕ возвращай никакое другое значение, кроме "status", "daily_fact" или "mixed" — здесь ты
работаешь только с речью, которая уже точно распознана и содержит реальный смысл.

Ответь только JSON:
{{
"classification": "status" или "daily_fact" или "mixed",
"status_part": "часть текста про короткий период, если classification = status или mixed, иначе пустая строка",
"fact_part": "часть текста про итог дня, если classification = daily_fact или mixed, иначе пустая строка"
}}

Текст рабочего:
{text}
"""

def classify_report_type(text: str) -> dict:
    """Dedicated classification step, run AFTER assess_transcription_quality has already
    confirmed there is real, usable speech to work with, and BEFORE the content (is_ok)
    check. Decides purely by meaning/trigger words whether a report is about a short recent
    period (status) or the whole day (daily_fact) - the submission time no longer forces the
    type. Can also detect a single message that mixes both ("mixed", split into
    status_part/fact_part). Never returns "can't tell" - per product decision, once speech is
    confirmed recognizable it always resolves to status or daily_fact, defaulting to status
    when the LLM is unavailable or genuinely unsure."""
    default = {"classification": "status", "status_part": text, "fact_part": ""}
    if groq_client is None:
        return default
    h = get_md5(f"classify:{text}")
    if h in _ai_classify_cache:
        return _ai_classify_cache[h]
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай только валидным JSON без Markdown."},
                {"role": "user", "content": CLASSIFY_PROMPT_TEMPLATE.format(text=text)},
            ],
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        data = json.loads(raw)
        classification = str(data.get("classification", "")).strip().lower()
        if classification not in ("status", "daily_fact", "mixed"):
            classification = "status"
        result = {
            "classification": classification,
            "status_part": str(data.get("status_part") or "").strip(),
            "fact_part": str(data.get("fact_part") or "").strip(),
        }
        _ai_classify_cache[h] = result
        return result
    except Exception as e:
        logger.error(f"Ошибка классификации типа отчёта (статус/факт): {e}")
        return default

CLEAN_REPORT_PROMPT = """
Ты — технический специалист, который оформляет отчёты строительной бригады.
Тебе дают сообщение рабочего — иногда из нескольких видео подряд, разделённых пометками
вида "[Видео 1]:", "[Видео 2]:".
Твоя задача: превратить это сообщение в короткий понятный официальный отчёт о том, что
было сделано.

Правила:
1. Не придумывай работу, которой не было.
2. Сохраняй только смысл исходного сообщения.
3. Исправляй ошибки и убирай слова-паразиты.
4. Убирай пометки вида "[Видео N]:" — в готовом отчёте их быть не должно.
5. Игнорируй технические артефакты распознавания речи, которые не являются частью
   реальной речи рабочего (например: "Продолжение следует...", "Субтитры создавал...",
   "молчал", "неразборчиво", "[без звука]", "[тишина]") — не включай их в отчёт.
6. Если после этого не осталось содержательной информации, верни ровно фразу:
   "Содержательная информация отсутствует."

Верни только готовый текст отчёта, без кавычек и лишних комментариев.

Сообщение рабочего:
{text}
"""

def clean_report(text: str) -> str:
    if groq_client is None:
        return text
    h = get_md5(text)
    if h in _ai_clean_cache:
        return _ai_clean_cache[h]
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Ты преобразуешь сообщения рабочих в официальные отчеты строительной бригады. Верни только готовый текст отчета без каких-либо комментариев и кавычек."},
                {"role": "user", "content": CLEAN_REPORT_PROMPT.format(text=text)},
            ],
            max_tokens=400,
            temperature=0,
        )
        res = response.choices[0].message.content.strip().strip('"').strip("'")
        _ai_clean_cache[h] = res
        return res
    except Exception as e:
        logger.error(f"Ошибка при очистке отчета: {e}")
        return text

def normalize_ai_result(data: dict, source_text: str, report_type: str | None = "status") -> dict:
    if report_type not in ("status", "daily_fact"):
        report_type = str(data.get("report_type", "status")).strip().lower()
        if report_type not in ("status", "daily_fact"):
            report_type = "status"

    raw_ok = data.get("is_ok", False)
    if isinstance(raw_ok, str):
        is_ok = raw_ok.strip().lower() in ("true", "1", "yes", "да")
    else:
        is_ok = bool(raw_ok)

    issue = str(data.get("issue") or "").strip()
    format_comment = str(data.get("format_comment") or "").strip()
    required_action = str(data.get("required_action") or "").strip()
    employee_message = str(data.get("employee_message") or "").strip()

    # Rule-based overrides for silence/unintelligibility
    lower_src = source_text.lower().strip()
    # Whisper hallucinates stock subtitle-outro phrases like "продолжение следует" when the
    # audio has no real speech (silence/near-silence) - treat that as the same "couldn't
    # make out a report" case rather than showing the hallucinated text as if it were what
    # the worker actually said.
    if "продолжение следует" in lower_src:
        is_ok = False
        issue = "в видео был неразборчивый звук или на видео молчал"
        format_comment = issue
        required_action = f"сделал замечание: {issue}"
        employee_message = "В видео был неразборчивый звук или на видео молчал. Пожалуйста, перезапишите отчёт."
    elif not lower_src or any(x in lower_src for x in ("на видео молчал", "[без звука]", "[тишина]", "[вздох]", "без звука", "тишина", "музыка", "молчание", "молчал", "молчит", "шум")):
        is_ok = False
        issue = "на видео молчал"
        format_comment = "на видео молчал"
        required_action = "сделал замечание: на видео молчал"
        employee_message = "Пожалуйста, сдавайте отчет голосом — вы молчали на видео."
    elif any(x in lower_src for x in ("неразборчиво", "неразборчивый текст", "шумы", "помехи", "неразборчивая речь")):
        is_ok = False
        issue = "неразборчивый текст"
        format_comment = "неразборчивый текст"
        required_action = "сделал замечание: неразборчивый текст"
        employee_message = "Голос на видео неразборчив, пожалуйста, перезапишите отчет."

    if is_ok:
        issue = ""
        # Ensure format_comment describes what they did, but is prefixed by OK
        if not format_comment or format_comment.lower() in ("всё ок", "все ок", "ок", "всё хорошо"):
            format_comment = "сказал что сделал"
        if format_comment.startswith("ОК") or format_comment.startswith("OK"):
            pass
        else:
            format_comment = f"ОК - {format_comment}"
        required_action = "ничего не предпринимать"
        employee_message = ""
    else:
        if not issue or issue.lower() in ("всё ок", "все ок", "ок", "всё хорошо"):
            issue = "не указал объем работы"
        if not format_comment or format_comment.lower() in ("всё ок", "все ок", "ок", "всё хорошо"):
            format_comment = issue
        if format_comment.startswith("не ОК") or format_comment.startswith("не ок") or format_comment.startswith("НЕ ОК"):
            pass
        else:
            format_comment = f"не ОК - {format_comment}"
        required_action = f"сделал замечание сотруднику: {issue}"
        if not employee_message:
            employee_message = f"В отчете есть замечание: {issue}. В следующем отчете исправьте это."

    return {
        "report_type": report_type,
        "is_ok": is_ok,
        "format_comment": format_comment,
        "required_action": required_action,
        "employee_message": employee_message,
        "issue": issue
    }

def check_status(text: str, report_type: str | None = "status") -> dict:
    is_forced = report_type in ("status", "daily_fact")

    if is_forced:
        report_type_label = "СТАТУС (отчёт о текущей работе в течение дня)" if report_type == "status" else "ИТОГ ДНЯ / ФАКТ (финальный отчёт о всей проделанной за день работе)"
        report_type_hint = (
            "Оценивай как промежуточный отчёт о том, чем человек занимается прямо сейчас."
            if report_type == "status"
            else "Оценивай как итоговый отчёт за весь рабочий день — ожидай более полного описания того, что было сделано в течение дня."
        )
        mode_instruction = (
            f"ВАЖНО: тип отчёта уже точно определён системой по времени отправки — это {report_type_label}.\n"
            f"Не пытайся определить тип отчёта сам, просто оцени содержание с учётом этого контекста:\n{report_type_hint}"
        )
        json_type_field = ""
        cache_key_prefix = report_type
    else:
        mode_instruction = (
            "ВАЖНО: определи тип отчёта по смыслу речи, а не по времени отправки.\n\n"
            "СТАТУС (status) — короткое сообщение о том, чем человек занимается ПРЯМО СЕЙЧАС или "
            "занимался в один конкретный момент/период дня (например: 'работаю на дробилке', "
            "'подаю материал', 'делаю опалубку').\n\n"
            "ИТОГ ДНЯ / ФАКТ (daily_fact) — человек подводит итог ВСЕГО рабочего дня целиком: "
            "что он сделал за весь день, какие работы выполнил, каких результатов достиг, что было "
            "завершено. Обычно это перечисление нескольких дел или итоговая фраза о завершении дня "
            "(например: 'за день сделал опалубку, залил фундамент и убрал стройплощадку').\n\n"
            "Ключевой вопрос: человек описывает ОДИН момент работы (статус) или подводит ИТОГ ЗА ВЕСЬ "
            "ДЕНЬ (факт)?\n"
            "Если по смыслу неясно — считай это ФАКТОМ (daily_fact)."
        )
        json_type_field = '"report_type": "status" или "daily_fact",\n\n'
        cache_key_prefix = "auto"

    if groq_client is None:
        return normalize_ai_result({"is_ok": False, "issue": "GROQ_API_KEY не задан"}, text, report_type if is_forced else None)
    h = get_md5(f"{cache_key_prefix}:{text}")
    if h in _ai_status_cache:
        return _ai_status_cache[h]
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "Отвечай только валидным JSON без Markdown."},
                {"role": "user", "content": CHECK_PROMPT_TEMPLATE.format(text=text, mode_instruction=mode_instruction, json_type_field=json_type_field)},
            ],
            max_tokens=400,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        res = normalize_ai_result(json.loads(raw), text, report_type if is_forced else None)
        _ai_status_cache[h] = res
        return res
    except Exception as e:
        return normalize_ai_result({"is_ok": False, "issue": f"Ошибка ИИ: {e}"}, text, report_type if is_forced else None)
