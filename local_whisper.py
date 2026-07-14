# -*- coding: utf-8 -*-
"""Локальная транскрибация через faster-whisper.

Используется как ЗАПАСНОЙ путь, когда Gemini недоступен (429, таймаут, нет сети).
Модель загружается лениво при первом вызове и остаётся в памяти.

Настройки через переменные окружения (все необязательные):
  LOCAL_WHISPER_MODEL   - размер модели: tiny/base/small/medium (по умолчанию small)
  LOCAL_WHISPER_THREADS - число потоков CPU (по умолчанию 4)
  LOCAL_WHISPER_ENABLED - "0" чтобы полностью выключить fallback (по умолчанию включён)
  WHISPER_VOCAB         - свои термины через запятую (добавляются к встроенному словарю)

Замеры на Pentium G4620: small - 16-сек видео за ~20 сек; medium - за ~52 сек.
"""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

LOCAL_WHISPER_ENABLED = os.environ.get("LOCAL_WHISPER_ENABLED", "1") != "0"
_MODEL_SIZE = os.environ.get("LOCAL_WHISPER_MODEL", "small")
_CPU_THREADS = int(os.environ.get("LOCAL_WHISPER_THREADS", "4"))

# Словарь-подсказка: Whisper "настраивается" на эти слова и гораздо реже коверкает их
# в шумной уличной записи (проверено: "Шахману от шибелков" -> должно стать "шахман щебёнки").
# Сюда же попадают названия объектов из отчётов.
_BUILTIN_VOCAB = (
    "стройка, объект, отчёт за последние два часа, за весь рабочий день, "
    "шахман, щебёнка, щебень, песок, гравий, планировка, грузил, погрузчик, "
    "экскаватор, каток, катковать, бульдозер, дробилка, бетон, опалубка, "
    "траншея, котлован, бордюр, брусчатка, плитка, профнастил, ячейки, листы, "
    "арматура, сварка, замок, врезал, накладки, контейнер, канализация, "
    "труба, колодец, цистерна, водоснабжение, манометр, разметка, "
    "Industriala, Molovata, Cricova, Burebista, Дубоссары, Складовка"
)
_EXTRA_VOCAB = os.environ.get("WHISPER_VOCAB", "").strip()
_INITIAL_PROMPT = _BUILTIN_VOCAB + (", " + _EXTRA_VOCAB if _EXTRA_VOCAB else "")

_model = None  # ленивый синглтон


def _get_model():
    """Загружает модель при первом обращении. Скачивание весов (~500 МБ для small)
    происходит один раз и кэшируется в ~/.cache/huggingface."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # импорт тут, чтобы бот стартовал даже без пакета
        logger.info(f"Загружаю локальную модель Whisper '{_MODEL_SIZE}' (int8, CPU)...")
        _model = WhisperModel(
            _MODEL_SIZE,
            device="cpu",
            compute_type="int8",
            cpu_threads=_CPU_THREADS,
        )
        logger.info("Локальная модель Whisper загружена.")
    return _model


def extract_audio(video_path: str) -> str:
    """Вытаскивает звуковую дорожку из видео в WAV 16 кГц моно. Возвращает путь к
    временному файлу - вызывающий код обязан удалить его сам. Требует ffmpeg в PATH."""
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-f", "wav", wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        try:
            os.remove(wav_path)
        except OSError:
            pass
        raise RuntimeError(
            f"ffmpeg не смог извлечь аудио: {proc.stderr.decode('utf-8', 'ignore')[-500:]}"
        )
    return wav_path


def transcribe_local(file_path: str) -> str:
    """Распознаёт речь локально. Принимает и видео, и аудио - если это видео,
    сначала извлекает дорожку через ffmpeg. Возвращает текст.
    Бросает исключение при любой проблеме (вызывающий код решает, что делать)."""
    if not LOCAL_WHISPER_ENABLED:
        raise RuntimeError("Локальный Whisper выключен (LOCAL_WHISPER_ENABLED=0)")

    audio_path = None
    is_temp = False
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in (".mp4", ".mov", ".mkv", ".avi", ".webm", ".3gp", ".m4v"):
            audio_path = extract_audio(file_path)
            is_temp = True
        else:
            audio_path = file_path

        model = _get_model()
        segments, _info = model.transcribe(
            audio_path,
            language="ru",
            vad_filter=True,           # отсекает тишину - быстрее и меньше галлюцинаций
            beam_size=1,               # жадный поиск - в разы быстрее на слабом CPU
            condition_on_previous_text=False,  # меньше "зацикливаний" на плохом звуке
            initial_prompt=_INITIAL_PROMPT,    # словарь стройтерминов и объектов
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        logger.info(f"Локальная транскрибация готова, {len(text)} символов.")
        return text
    finally:
        if is_temp and audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass
