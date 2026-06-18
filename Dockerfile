FROM python:3.11-slim

WORKDIR /app

# Устанавливаем шрифты для обработки фото
RUN apt-get update && apt-get install -y \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Создаем папку для шрифтов
RUN mkdir -p fonts

# Запускаем бота
CMD ["python", "bot.py"]
