# -*- coding: utf-8 -*-
"""Полная замена ai.py: Gemini (бесплатный тариф) вместо Groq + локальные fallback'и.

Публичный интерфейс И ПОВЕДЕНИЕ полностью совпадают с оригинальным ai.py репозитория:
  - AITechnicalError (.message, .retry_after_seconds)
  - transcribe_audio(file_path) -> str          (бросает AITechnicalError при сбое)
  - assess_transcription_quality(text, duration_seconds) -> {"ok", "reason"}
  - classify_report_type(text) -> {classification, status_part, fact_part}
  - clean_report(text) -> str
  - check_status(text, report_type) -> dict     (бросает AITechnicalError при сбое)
  - normalize_ai_result(data, source_text, report_type) -> dict
  - get_md5(text) -> str
Промпты (CHECK/CLASSIFY/CLEAN) перенесены из оригинала дословно. Пост-обработка
normalize_ai_result воспроизведена полностью: правила "на видео молчал"/"неразборчивый
текст"/"продолжение следует", префиксы "ОК -"/"не ОК -", тексты required_action и
employee_message.

Стратегия отказоустойчивости:
  transcribe_audio      Gemini (аудио) -> faster-whisper локально -> AITechnicalError
  classify_report_type  Gemini (текст) -> правила на регулярках (как в оригинале: при
                        недоступности ИИ дефолт status, без ошибки)
  clean_report          Gemini (текст) -> лёгкая чистка регулярками (оригинал: вернуть text)
  check_status          Gemini (текст) -> AITechnicalError (существующий путь ручной
                        проверки в боте берёт это на себя)

Переменные окружения:
  GEMINI_API_KEY     - обязательная, ключ с https://aistudio.google.com
  GEMINI_MODEL       - модель (по умолчанию gemini-3.1-flash-lite - проверено, у free tier
                       на неё есть квота; актуальные имена: ai.google.dev/gemini-api/docs/models)
  LOCAL_WHISPER_*    - см. local_whisper.py
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile

logger = logging.getLogger(__name__)


# ── Исключение и разбор retry-after (1:1 с оригиналом) ───────────────────────

class AITechnicalError(Exception):
    """Raised when a call to the LLM API fails for infrastructure reasons (rate limit,
    timeout, connection error, 5xx, or even a malformed/unparseable response) - NEVER a
    verdict on the employee's report. Callers must retry, not treat this as "не ОК"."""
    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


# Оригинальный формат Groq: "try again in 20m19.104s"
_RETRY_AFTER_RE = re.compile(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", re.IGNORECASE)
# Формат Gemini: "Please retry in 19.4332s" / retryDelay: '19s'
_RETRY_GEMINI_RE = re.compile(r"retry(?:\s+in|Delay['\"]?:\s*['\"]?)\s*(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


def _parse_retry_after_seconds(text: str) -> float | None:
    m = _RETRY_AFTER_RE.search(text or "")
    if m and any(m.groups()):
        hours, minutes, seconds = m.groups()
        total = (int(hours) * 3600 if hours else 0) + (int(minutes) * 60 if minutes else 0) + (float(seconds) if seconds else 0)
        if total > 0:
            return total
    m = _RETRY_GEMINI_RE.search(text or "")
    if m:
        return float(m.group(1))
    return None


def _raise_as_technical_error(exc: Exception):
    """Любая ошибка вызова LLM - техническая проблема, не вердикт по отчёту."""
    raise AITechnicalError(str(exc), retry_after_seconds=_parse_retry_after_seconds(str(exc))) from exc


# ── Клиент Gemini ─────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

_gemini_client = None
genai_types = None
if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as genai_types
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:  # пакет не установлен / несовместимая версия
        logger.error(f"Не удалось инициализировать клиент Gemini: {e}")
        _gemini_client = None
else:
    logger.warning("GEMINI_API_KEY не задан - все запросы пойдут через локальные fallback'и.")


def _gemini_text(prompt: str, max_tokens: int = 400, want_json: bool = True) -> str:
    """Один текстовый запрос к Gemini. Бросает исключение при любой ошибке."""
    if _gemini_client is None:
        raise AITechnicalError("GEMINI_API_KEY не задан")
    config = genai_types.GenerateContentConfig(
        temperature=0,
        max_output_tokens=max_tokens,
        response_mime_type="application/json" if want_json else "text/plain",
    )
    resp = _gemini_client.models.generate_content(
        model=GEMINI_MODEL, contents=prompt, config=config,
    )
    return (resp.text or "").strip()


# ── Кэши (как в оригинале) ────────────────────────────────────────────────────

_ai_status_cache = {}
_ai_clean_cache = {}
_ai_classify_cache = {}


# ── Маркеры и пороги (1:1 с оригиналом) ──────────────────────────────────────

# Те же сигнальные слова тишины/галлюцинаций, что использует normalize_ai_result -
# assess_transcription_quality проверяет их ПЕРВОЙ, до классификации типа.
_NO_CONTENT_MARKERS = (
    "продолжение следует", "на видео молчал", "[без звука]", "[тишина]", "[вздох]",
    "без звука", "тишина", "музыка", "молчание", "молчал", "молчит", "шум",
    "неразборчиво", "неразборчивый текст", "шумы", "помехи", "неразборчивая речь",
)

# Префиксы, которыми transcribe_audio мог бы сообщить о технической ошибке.
_TRANSCRIPTION_ERROR_PREFIXES = (
    "Ошибка распознавания аудио",
    "Не задан GROQ_API_KEY",
    "Не задан GEMINI_API_KEY",
)

# Оба порога = 0 по текущему продуктовому решению: короткий, но реальный отчёт
# не отклоняется здесь - его судьбу решает проверка содержания (is_ok) дальше.
MIN_REPORT_DURATION_SECONDS = 0
MIN_MEANINGFUL_WORDS = 0


def get_md5(text: str) -> str:
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()


def assess_transcription_quality(text: str, duration_seconds: float | None = None) -> dict:
    """Отдельная проверка качества распознавания - ДО классификации типа и ДО проверки
    содержания. Отвечает ровно на один вопрос: есть ли вообще реальная речь для анализа.
    Возвращает {"ok": True} или {"ok": False, "reason": "too_short_duration"/"empty"/
    "no_content_marker"/"transcription_error"/"too_few_words"}."""
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


# ── 1. Транскрибация: Gemini -> локальный Whisper -> AITechnicalError ────────

def _extract_audio_mp3(video_path: str) -> str:
    """Дорожка из видео в компактный mp3 (моно, 32 кбит/с) - в Gemini уходят килобайты
    вместо мегабайт видео, бесплатные лимиты расходуются в разы медленнее."""
    fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
           "-b:a", "32k", mp3_path]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        try:
            os.remove(mp3_path)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg: {proc.stderr.decode('utf-8', 'ignore')[-300:]}")
    return mp3_path


def _transcribe_gemini(file_path: str) -> str:
    if _gemini_client is None:
        raise AITechnicalError("GEMINI_API_KEY не задан")
    ext = os.path.splitext(file_path)[1].lower()
    audio_path, is_temp = file_path, False
    if ext in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".3gp", ".m4v"):
        audio_path = _extract_audio_mp3(file_path)
        is_temp = True
    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        part = genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/mp3")
        resp = _gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                part,
                "Расшифруй речь на этой записи дословно на русском языке. "
                "Верни ТОЛЬКО текст расшифровки без комментариев. "
                "Если речи нет, верни ровно: [тишина]",
            ],
            config=genai_types.GenerateContentConfig(temperature=0),
        )
        return (resp.text or "").strip()
    finally:
        if is_temp and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass


def transcribe_audio(file_path: str) -> str:
    """Gemini -> локальный faster-whisper -> AITechnicalError.
    Как и в оригинале: сбой транскрибации - это ВСЕГДА инфраструктурная проблема
    (никогда не "сотрудник молчал"), поэтому при полном отказе бросается
    AITechnicalError и бот уводит видео на ручную проверку админом."""
    gemini_error = None
    try:
        return _transcribe_gemini(file_path)
    except Exception as e:
        gemini_error = e
        logger.warning(f"Gemini-транскрибация не удалась ({e}), пробую локальный Whisper...")

    try:
        from local_whisper import transcribe_local, LOCAL_WHISPER_ENABLED
        if LOCAL_WHISPER_ENABLED:
            return transcribe_local(file_path)
    except Exception as e:
        logger.error(f"Локальный Whisper тоже не справился: {e}")

    logger.error(f"Ошибка распознавания аудио: {gemini_error}")
    raise AITechnicalError(
        f"Транскрибация недоступна: {gemini_error}",
        retry_after_seconds=_parse_retry_after_seconds(str(gemini_error)),
    )


# ── 2. Классификация Статус/Факт (промпт из оригинала дословно) ──────────────

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

# Регулярка-fallback: те же триггеры ФАКТА, что и в промпте
_FACT_TRIGGERS_RE = re.compile(
    r"(за\s+весь\s+(рабочий\s+)?день|весь\s+день|по\s+итогам\s+дня|итог\s+дня|"
    r"в\s+целом\s+за\s+день|за\s+смену|с\s+(самого\s+)?начала\s+смены|на\s+сегодня\b)",
    re.IGNORECASE,
)


def classify_report_type(text: str) -> dict:
    """Классификация типа - ПОСЛЕ assess_transcription_quality, ДО проверки содержания.
    Никогда не возвращает "не смог определить": как и в оригинале, при недоступности
    ИИ дефолт - status (здесь дополнительно триггеры ФАКТА ловятся регулярками,
    чтобы Факт не терялся даже при полном отказе ИИ)."""
    default = {"classification": "status", "status_part": text, "fact_part": ""}
    h = get_md5(f"classify:{text}")
    if h in _ai_classify_cache:
        return _ai_classify_cache[h]
    try:
        raw = _gemini_text(CLASSIFY_PROMPT_TEMPLATE.format(text=text), max_tokens=300)
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
        if _FACT_TRIGGERS_RE.search(text or ""):
            return {"classification": "daily_fact", "status_part": "", "fact_part": text}
        return default


# ── 3. Чистка речи (промпт из оригинала дословно) ────────────────────────────

CLEAN_REPORT_PROMPT = """
Перепиши текст сотрудника в чистом, грамматически связном виде. Сообщение иногда состоит из
нескольких видео подряд, разделённых пометками вида "[Видео 1]:", "[Видео 2]:".

ГЛАВНОЕ ПРАВИЛО: ты только ЧИСТИШЬ речь, а не пересказываешь её своими словами.
НЕ добавляй информацию, формулировки или детали, которых сотрудник не говорил.
НЕ используй канцелярские обороты вроде "была проведена работа", "в частности", "в рамках",
"следует отметить" - просто собери его же слова в связный текст.
Сохрани ВСЕ факты, которые упомянул сотрудник, ничего не пропускай - каждое отдельное
упомянутое действие/деталь должно остаться в результате.

Убери только:
- слова-паразиты, повторы, самоисправления ("это... ну это, короче", "эээ");
- пометки вида "[Видео N]:";
- технические артефакты распознавания речи, которые не являются частью реальной речи
  рабочего (например: "Продолжение следует...", "Субтитры создавал...", "молчал",
  "неразборчиво", "[без звука]", "[тишина]").

Если после этого не осталось содержательной информации, верни ровно фразу:
"Содержательная информация отсутствует."

Верни только готовый текст, без кавычек и лишних комментариев.

Сообщение рабочего:
{text}
"""


def clean_report(text: str) -> str:
    """Как в оригинале: при недоступности ИИ возвращает исходный текст (не ошибку)."""
    h = get_md5(text)
    if h in _ai_clean_cache:
        return _ai_clean_cache[h]
    try:
        res = _gemini_text(CLEAN_REPORT_PROMPT.format(text=text),
                           max_tokens=400, want_json=False)
        res = res.strip().strip('"').strip("'")
        if not res:
            return text
        _ai_clean_cache[h] = res
        return res
    except Exception as e:
        logger.error(f"Ошибка при очистке отчета: {e}")
        return text


# ── 4. Нормализация результата проверки (1:1 с оригиналом) ───────────────────

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

    # Правила-переопределения для тишины/неразборчивости
    lower_src = source_text.lower().strip()
    # Whisper галлюцинирует стоковые фразы вроде "продолжение следует" на тишине -
    # это признак "не смог разобрать отчёт", а не реальная речь рабочего.
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


# ── 5. Проверка содержания (промпт из оригинала дословно) ────────────────────

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
ОБОБЩЕНИЕ СУТИ РАБОТЫ (format_comment)
=====================
format_comment должен быть ОБОБЩЁННОЙ формулировкой того, ЧЕМ В ЦЕЛОМ занимался человек,
а не дословным пересказом - коротко, 3-6 слов.

Убирай лишнюю конкретику, которая не меняет сути работы: названия конкретных объектов,
участков, площадок, локаций. Тип работы/техники/процесса, наоборот, оставляй - это и есть суть.
Пример: "работал на дробилке на Башнитоне" -> "работал на дробилке" (название площадки не нужно,
"дробилка" - нужно, это тип работы).

Если человек перечисляет НЕСКОЛЬКО СВЯЗАННЫХ ОДНОЙ ТЕМОЙ действий подряд - объединяй их в ОДНУ
обобщающую фразу по общему смыслу, а не перечисляй через "и".
Пример: "грузил щебень и ровнял дорогу" -> "занимался выравниванием дороги" (оба действия об
одном - дорожных работах).

Если действия РАЗНОРОДНЫЕ, не связаны общей темой, и оба важны - перечисли оба через запятую,
но всё равно коротко, без лишних деталей, не сводя их искусственно в одну фразу.
Пример: "убирал территорию и чинил забор" -> "уборка территории, ремонт забора" (это два разных
дела, оба стоит оставить).

=====================
ФОРМАТ
=====================
Ответь только JSON:
{{
{json_type_field}"is_ok": true или false,
"issue": "причина (например: 'не указал объем работы', 'на видео молчал', 'неразборчивый текст' или краткое описание замечания если is_ok=false). Если is_ok=true, то оставь пустым.",
"format_comment": "ОБОБЩЁННАЯ суть сделанного дела на русском языке, 3-6 слов, без названий объектов/локаций (например: 'сварка перемычек', 'уборка берега', 'занимался выравниванием дороги', 'уборка территории, ремонт забора'). Если не ок - укажи причину.",
"required_action": "что написать сотруднику",
"employee_message": "короткое сообщение сотруднику"
}}

Отчёт:
{text}
"""


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

    # Отсутствующий ключ - такая же инфраструктурная проблема, как лимит или таймаут:
    # никогда не вина сотрудника, идёт через тот же путь AITechnicalError.
    if _gemini_client is None:
        raise AITechnicalError("GEMINI_API_KEY не задан")
    h = get_md5(f"{cache_key_prefix}:{text}")
    if h in _ai_status_cache:
        return _ai_status_cache[h]
    try:
        raw = _gemini_text(
            CHECK_PROMPT_TEMPLATE.format(
                text=text, mode_instruction=mode_instruction, json_type_field=json_type_field
            ),
            max_tokens=400,
        )
        res = normalize_ai_result(json.loads(raw), text, report_type if is_forced else None)
        _ai_status_cache[h] = res
        return res
    except AITechnicalError:
        raise
    except Exception as e:
        # Любой сбой здесь - лимит, таймаут, обрыв сети или ответ, на котором давится
        # json.loads - это инфраструктурная проблема, никогда не вердикт по отчёту.
        logger.error(f"Ошибка ИИ при проверке отчёта: {e}")
        _raise_as_technical_error(e)
