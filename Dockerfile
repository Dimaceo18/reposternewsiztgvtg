FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p fonts

CMD ["python", "bot_webhook_fixed.py"]
