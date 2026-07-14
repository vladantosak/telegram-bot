FROM python:3.11-slim

# ffmpeg нужен ai_gemini.py, чтобы вытаскивать звуковую дорожку из видео
# перед отправкой в Gemini (экономит бесплатные лимиты в разы)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]
