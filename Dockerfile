FROM python:3.11-slim

WORKDIR /app

# Сначала копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# КОПИРУЕМ ВСЕ ФАЙЛЫ ПРОЕКТА (db.py, ai.py, report_handlers.py, admin_handlers.py и т.д.)
COPY . .

CMD ["python", "bot.py"]
