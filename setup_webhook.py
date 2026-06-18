import os
import requests
import json

# Ваши данные
BOT_TOKEN = "ВАШ_ТОКЕН_БОТА"
RENDER_URL = "https://ваш-сервис.onrender.com"  # URL вашего сервиса на Render

def setup_webhook():
    webhook_url = f"{RENDER_URL}/{BOT_TOKEN}"
    
    # Удаляем старый webhook
    print("🔄 Удаляем старый webhook...")
    response = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
        params={"drop_pending_updates": True}
    )
    print(response.json())
    
    # Устанавливаем новый webhook
    print("🔄 Устанавливаем новый webhook...")
    response = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
        params={
            "url": webhook_url,
            "drop_pending_updates": True,
            "allowed_updates": json.dumps(["message", "channel_post", "callback_query"])
        }
    )
    print(response.json())
    
    # Проверяем установку
    print("🔄 Проверяем webhook...")
    response = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
    )
    print(json.dumps(response.json(), indent=2))

if __name__ == "__main__":
    if not BOT_TOKEN or BOT_TOKEN == "ВАШ_ТОКЕН_БОТА":
        print("❌ Замените BOT_TOKEN на ваш токен!")
        exit(1)
    
    if not RENDER_URL or RENDER_URL == "https://ваш-сервис.onrender.com":
        print("❌ Замените RENDER_URL на URL вашего сервиса!")
        exit(1)
    
    setup_webhook()
