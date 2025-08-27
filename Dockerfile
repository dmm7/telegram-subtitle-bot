# Используем образ с ffmpeg и Python
FROM python:3.10-slim

# Установка ffmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Копируем и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --upgrade pip
RUN python-telegram-bot --upgrade
RUN pip install --no-cache-dir -r requirements.txt

# Копируем скрипт
COPY tbot_sub.py .

# Запуск бота
CMD ["python", "tbot_sub.py"]
