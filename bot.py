import asyncio
import sqlite3
import os
import re
import io
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID")
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DB_PATH = "republished.db"

# Настройки для оформления фото
FONT_PATH = os.getenv("FONT_PATH", "Montserrat-Black.ttf")

# ==================== НАСТРОЙКИ ДЛЯ КНОПКИ ====================
BUTTON_URL = os.getenv("BUTTON_URL", "")
BUTTON_TEXT = os.getenv("BUTTON_TEXT", "📢 Подписаться на канал")

# ==================== НАСТРОЙКИ БРЕНДИРОВАНИЯ ====================
BRAND_NAME = os.getenv("BRAND_NAME", "Fider.by")
BRAND_TEAM = os.getenv("BRAND_TEAM", f"Команда {BRAND_NAME}")

# ==================== ПРОВЕРКА НАСТРОЕК ====================
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не задан!")
    exit(1)

if not SOURCE_CHANNEL_ID:
    logger.error("❌ SOURCE_CHANNEL_ID не задан!")
    exit(1)

if not TARGET_CHANNEL_ID:
    logger.error("❌ TARGET_CHANNEL_ID не задан!")
    exit(1)

logger.info(f"📥 Канал-источник: {SOURCE_CHANNEL_ID}")
logger.info(f"📤 Целевой канал: {TARGET_CHANNEL_ID}")
logger.info(f"🤖 DeepSeek AI: {'✅ Подключен' if DEEPSEEK_API_KEY else '❌ Не настроен'}")
logger.info(f"🏷️ Бренд: {BRAND_NAME}")

if BUTTON_URL:
    logger.info(f"🔗 Ссылка для кнопки: {BUTTON_URL}")
    logger.info(f"📝 Текст кнопки: {BUTTON_TEXT}")
else:
    logger.info("ℹ️ Кнопка не настроена (BUTTON_URL не задан)")

# ==================== ПРОМПТЫ ДЛЯ DEEPSEEK ====================
DEEPSEEK_PROMPT = """Перепиши новость в новостном формате на 600-650 символов.

Правила:
- Сохрани все важные факты, цифры, даты, имена
- Перепиши текст в новостном стиле
- Разбей на 2-3 абзаца (пустая строка между абзацами)
- Удали смайлики, рекламу, кликбейт
- Сделай заголовок коротким и информативным

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст.

Пример правильного ответа:
Новый парк открыли в Гродно

В центре Гродно состоялось торжественное открытие нового парка культуры и отдыха. На мероприятии присутствовали городские власти и жители.

Парк занимает площадь 5 гектаров. Здесь установлены скамейки, фонари и детская площадка. Полностью завершить благоустройство планируют к концу года."""

deepseek_client = None
if DEEPSEEK_API_KEY:
    try:
        deepseek_client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com"
        )
        logger.info("✅ DeepSeek клиент инициализирован")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации DeepSeek: {e}")

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS republished_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER,
                    channel_id TEXT,
                    title TEXT,
                    republished_at TIMESTAMP,
                    original_text TEXT,
                    adapted_text TEXT,
                    has_media BOOLEAN DEFAULT 0,
                    media_type TEXT,
                    adaptation_type TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER,
                    channel_id TEXT,
                    error TEXT,
                    failed_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id INTEGER PRIMARY KEY,
                    channel_id TEXT,
                    processed_at TIMESTAMP
                )
            """)
        logger.info("✅ База данных готова")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}")
        raise

def is_message_processed(message_id: int, channel_id: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            result = conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_id = ? AND channel_id = ?",
                (message_id, channel_id)
            ).fetchone()
            return result is not None
    except Exception as e:
        logger.error(f"Ошибка проверки обработанного сообщения: {e}")
        return False

def save_processed_message(message_id: int, channel_id: str):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO processed_messages (message_id, channel_id, processed_at) VALUES (?, ?, ?)",
                (message_id, channel_id, datetime.now())
            )
    except Exception as e:
        logger.error(f"Ошибка сохранения обработанного сообщения: {e}")

def save_republished(message_id: int, channel_id: str, title: str, original_text: str, adapted_text: str, has_media: bool = False, media_type: str = None, adaptation_type: str = None):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO republished_posts 
                   (message_id, channel_id, title, republished_at, original_text, adapted_text, has_media, media_type, adaptation_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_id, channel_id, title, datetime.now(), original_text, adapted_text, has_media, media_type, adaptation_type)
            )
    except Exception as e:
        logger.error(f"Ошибка сохранения републикации: {e}")

def save_failed(message_id: int, channel_id: str, error: str):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO failed_posts (message_id, channel_id, error, failed_at) VALUES (?, ?, ?, ?)",
                (message_id, channel_id, error, datetime.now())
            )
    except Exception as e:
        logger.error(f"Ошибка сохранения ошибки: {e}")

def get_last_processed_message(channel_id: str) -> Optional[int]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            result = conn.execute(
                "SELECT MAX(message_id) FROM republished_posts WHERE channel_id = ?",
                (channel_id,)
            ).fetchone()
            return result[0] if result and result[0] else None
    except Exception as e:
        logger.error(f"Ошибка получения последнего обработанного сообщения: {e}")
        return None

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С ТЕКСТОМ ====================
def remove_emojis(text: str) -> str:
    if not text:
        return ""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub(r'', text)

def format_caption(title: str, body: str) -> str:
    title = remove_emojis(title) if title else ""
    body = remove_emojis(body) if body else ""
    
    if not title and not body:
        return ""
    if title and not body:
        return f"<b>{title}</b>"
    if not title and body:
        return body
    return f"<b>{title}</b>\n\n{body}"

def wrap_text_auto(text: str, font, max_width: int, max_lines: int = 6) -> List[str]:
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = ' '.join(current_line + [word])
        try:
            bbox = font.getbbox(test_line)
            width = bbox[2] - bbox[0]
        except:
            width = len(test_line) * 20
        if width <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
                current_line = [word]
            else:
                lines.append(word)
        if len(lines) >= max_lines:
            break
    if current_line and len(lines) < max_lines:
        lines.append(' '.join(current_line))
    return lines

def process_photo(photo_bytes: bytes, title_text: str) -> io.BytesIO:
    if not photo_bytes or len(photo_bytes) == 0:
        raise ValueError("Фото пустое")
    
    logger.info(f"🖼️ Обработка фото, размер: {len(photo_bytes) / 1024:.1f}KB")
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = img.size
    
    target_ratio = 4 / 5
    cur_ratio = w / h
    if cur_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    
    img = img.resize((1080, 1350), Image.Resampling.LANCZOS)
    
    img = ImageEnhance.Brightness(img).enhance(0.85)
    w, h = img.size
    gh = int(h * 0.48)
    if gh > 0:
        overlay_alpha = Image.new("L", (w, h), 0)
        grad = Image.new("L", (1, gh), 0)
        for y in range(gh):
            a = int(220 * (y / max(1, gh - 1)))
            grad.putpixel((0, y), a)
        grad = grad.resize((w, gh))
        overlay_alpha.paste(grad, (0, h - gh))
        black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        base = img.convert("RGBA")
        overlay = Image.composite(black, Image.new("RGBA", (w, h), (0, 0, 0, 0)), overlay_alpha)
        img = Image.alpha_composite(base, overlay).convert("RGB")
    
    draw = ImageDraw.Draw(img)
    
    font = None
    font_size = 68
    
    font_paths = [
        FONT_PATH,
        "Montserrat-Black.ttf",
        "fonts/Montserrat-Black.ttf",
        "/app/Montserrat-Black.ttf",
        "Montserrat-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    
    for font_path in font_paths:
        try:
            if os.path.exists(font_path):
                font = ImageFont.truetype(font_path, font_size)
                logger.info(f"✅ Загружен шрифт: {font_path}")
                break
        except:
            continue
    
    if font is None:
        font = ImageFont.load_default()
        logger.warning("⚠️ Шрифт не найден, использую стандартный")
    
    margin_x = int(img.width * 0.05)
    margin_bottom = int(img.height * 0.08)
    max_text_width = img.width - 2 * margin_x
    title = title_text.upper()
    lines = wrap_text_auto(title, font, max_text_width, max_lines=6)
    
    if font == ImageFont.load_default():
        line_height = 35
        spacing = 10
    else:
        line_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
        spacing = int(line_height * 0.25)
    
    total_text_height = len(lines) * line_height + (len(lines) - 1) * spacing
    y = img.height - margin_bottom - total_text_height
    
    for line in lines:
        if font == ImageFont.load_default():
            line_width = len(line) * 20
        else:
            bbox = font.getbbox(line)
            line_width = bbox[2] - bbox[0]
        x = (img.width - line_width) // 2
        
        offsets = [(-2, -2), (-2, 2), (2, -2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)]
        for dx, dy in offsets:
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height + spacing
    
    output = io.BytesIO()
    quality = 85
    while quality >= 60:
        output.seek(0)
        output.truncate()
        img.save(output, format="JPEG", quality=quality, subsampling=0, optimize=True)
        size = output.tell() / (1024 * 1024)
        if size <= 15:
            break
        quality -= 10
    output.seek(0)
    if output.getbuffer().nbytes == 0:
        raise ValueError("Результирующий файл пустой")
    logger.info(f"✅ Фото готово: {output.getbuffer().nbytes / (1024 * 1024):.2f}MB, строк: {len(lines)}")
    return output

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С AI ====================
async def call_deepseek_with_retry(prompt: str, text: str, max_attempts: int = 2) -> str:
    if not deepseek_client:
        return text
    
    async def make_request(current_prompt, current_text):
        try:
            response = await deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": current_prompt},
                    {"role": "user", "content": f"Перепиши этот текст:\n\n{current_text}"}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"Ошибка запроса к DeepSeek: {e}")
            raise e
    
    for attempt in range(max_attempts):
        try:
            content = await make_request(prompt, text)
            
            if not content or len(content.strip()) < 20:
                logger.warning(f"Попытка {attempt + 1}: Получен пустой или слишком короткий ответ")
                if attempt == max_attempts - 1:
                    return ""
                continue
            
            return content
                
        except Exception as e:
            logger.error(f"Ошибка при попытке {attempt + 1}: {e}")
            if attempt == max_attempts - 1:
                return ""
    
    return ""

def parse_ai_response(content: str) -> Tuple[str, str]:
    if not content:
        return "", ""
    
    content = content.strip()
    
    lines = content.split('\n')
    clean_lines = []
    for line in lines:
        line_clean = line.strip()
        if line_clean.lower().startswith("заголовок:") or line_clean.lower().startswith("текст:"):
            continue
        clean_lines.append(line)
    
    content = '\n'.join(clean_lines).strip()
    
    parts = content.split('\n\n', 1)
    if len(parts) == 2:
        title = parts[0].strip()
        body = parts[1].strip()
    else:
        first_newline = content.find('\n')
        if first_newline != -1 and first_newline < 100:
            title = content[:first_newline].strip()
            body = content[first_newline:].strip()
        else:
            if len(content) < 200:
                title = content[:70].strip()
                body = content[70:].strip() if len(content) > 70 else content
            else:
                first_line = content.split('\n')[0]
                if len(first_line) < 100:
                    title = first_line
                    body = '\n'.join(content.split('\n')[1:]).strip()
                else:
                    body = content
    
    title = re.sub(r'^[#*\-_\s]+', '', title).strip()
    body = re.sub(r'^[#*\-_\s]+', '', body).strip()
    
    if not title and body:
        title = body[:70].strip()
        body = body[70:].strip() if len(body) > 70 else body
    
    if not body and title:
        body = title
        title = ""
    
    return title, body

async def adapt_text_by_length(text: str) -> Tuple[str, str, str]:
    """
    Адаптирует текст в зависимости от его длины:
    - Короткие тексты (до 200 символов): небольшая адаптация
    - Средние тексты (200-300 символов): умеренная адаптация
    - Длинные тексты (300-500 символов): полная переработка в новостном формате
    - Очень длинные тексты (500+ символов): журналистский стиль с брендированием
    
    Возвращает: (заголовок, тело, тип_адаптации)
    """
    if not text:
        return "", "", "original"
    
    text_length = len(text)
    logger.info(f"📊 Длина текста: {text_length} символов")
    
    # Если AI не настроен, используем оригинальный текст
    if not deepseek_client:
        logger.info("ℹ️ AI не настроен, используем оригинальный текст")
        title = text[:70].strip()
        body = text[70:] if len(text) > 70 else text
        return title, body, "original"
    
    try:
        # Короткий текст (до 200 символов) - легкая адаптация
        if text_length <= 200:
            logger.info("📝 Короткий текст (≤200 символов) - применяем легкую адаптацию")
            
            short_prompt = """Адаптируй этот короткий текст для публикации в новостном канале. 
Сделай текст более читаемым, убери смайлики и лишние символы, но сохрани все факты. 
НЕ переписывай полностью, а лишь слегка отредактируй.
Если текст уже хороший, можешь оставить его как есть.

Важно:
- Сохрани все факты, цифры, имена
- Удали смайлики и эмодзи
- Сделай текст грамотным и читаемым
- НЕ добавляй новые факты
- НЕ увеличивай объем текста более чем на 20%

Ответ должен быть в формате:
Короткий заголовок (если есть)
Пустая строка
Основной текст"""

            response = await call_deepseek_with_retry(short_prompt, text, max_attempts=1)
            
            if response and len(response.strip()) > 20:
                if len(response) <= text_length * 1.3:
                    logger.info(f"✅ Легкая адаптация выполнена (было {text_length}, стало {len(response)})")
                    title, body = parse_ai_response(response)
                    return title, body, "light_adaptation"
                else:
                    logger.warning(f"⚠️ Ответ AI слишком длинный ({len(response)} > {text_length * 1.3}), используем оригинал")
            
            title = text[:70].strip()
            body = text[70:] if len(text) > 70 else text
            return title, body, "original"
        
        # Средний текст (200-300 символов) - умеренная адаптация
        elif 200 < text_length <= 300:
            logger.info("📝 Средний текст (200-300 символов) - применяем умеренную адаптацию")
            
            medium_prompt = """Адаптируй этот текст для новостного канала. 
Сделай текст более структурированным и читаемым.
Удали смайлики, рекламу и кликбейт.

Правила:
- Сохрани все важные факты, цифры, даты, имена
- Сделай текст грамотным и структурированным
- Разбей на абзацы, если это уместно
- Удали смайлики и эмодзи
- Можно немного расширить текст, добавляя связки и пояснения
- Итоговый текст должен быть примерно на 10-20% длиннее оригинала

Ответ должен быть в формате:
Заголовок (краткий и информативный)
Пустая строка
Основной текст (без маркеров "Заголовок:" и "Текст:")"""

            response = await call_deepseek_with_retry(medium_prompt, text, max_attempts=2)
            
            if response and len(response.strip()) > 50:
                if len(response) <= text_length * 1.5:
                    logger.info(f"✅ Умеренная адаптация выполнена (было {text_length}, стало {len(response)})")
                    title, body = parse_ai_response(response)
                    return title, body, "medium_adaptation"
                else:
                    logger.warning(f"⚠️ Ответ AI слишком длинный ({len(response)} > {text_length * 1.5})")
            
            title = text[:70].strip()
            body = text[70:] if len(text) > 70 else text
            return title, body, "original"
        
        # Длинный текст (300-500 символов) - полная переработка
        elif 300 < text_length <= 500:
            logger.info("📝 Длинный текст (300-500 символов) - применяем полную переработку")
            
            response = await call_deepseek_with_retry(DEEPSEEK_PROMPT, text, max_attempts=2)
            
            if response and len(response.strip()) > 100:
                logger.info(f"✅ Полная переработка выполнена (было {text_length}, стало {len(response)})")
                title, body = parse_ai_response(response)
                return title, body, "full_adaptation"
            else:
                logger.warning("⚠️ Полная переработка не удалась, используем оригинал")
                title = text[:70].strip()
                body = text[70:] if len(text) > 70 else text
                return title, body, "original"
        
        # Очень длинный текст (500+ символов) - журналистский стиль с брендированием
        else:
            logger.info("📝 Очень длинный текст (500+ символов) - применяем журналистский стиль с брендированием")
            
            journalistic_prompt = f"""Перепиши эту новость в профессиональном журналистском стиле с элементами брендирования.

Важно использовать следующие фразы (НЕ ВСЕ СРАЗУ, а распределяя по тексту):
- "Как стало известно редакции {BRAND_NAME}"
- "{BRAND_TEAM} сообщает"
- "Передает журналист {BRAND_NAME}"
- "Как сообщают подписчики {BRAND_NAME}"
- "По информации {BRAND_NAME}"
- "Источник в {BRAND_NAME} уточняет"

Правила:
- Сохрани ВСЕ важные факты, цифры, даты, имена, места
- Используй 2-3 журналистские фразы с упоминанием {BRAND_NAME} (НЕ больше, чтобы не было навязчиво)
- Пиши в нейтральном, информационном стиле
- Разбей на 3-4 абзаца (пустая строка между абзацами)
- Удали смайлики, рекламу, кликбейт, личные оценки
- Сделай заголовок ярким и информативным
- Объем: 600-700 символов
- Добавь в текст элементы интриги, но без перегибов

ВАЖНО: НЕ пиши слова "Заголовок:" и "Текст:". Просто напиши сначала заголовок, потом пустую строку, потом текст.

Пример правильного ответа:
Новый парк открыли в Гродно

Как стало известно редакции {BRAND_NAME}, в центре Гродно состоялось торжественное открытие нового парка культуры и отдыха. На мероприятии присутствовали городские власти и местные жители.

{BRAND_TEAM} сообщает, что парк занимает площадь 5 гектаров. Здесь установлены современные скамейки, фонари и детская площадка.

По информации {BRAND_NAME}, полностью завершить благоустройство планируют к концу года. Жители города уже активно посещают новое общественное пространство."""

            response = await call_deepseek_with_retry(journalistic_prompt, text, max_attempts=2)
            
            if response and len(response.strip()) > 150:
                logger.info(f"✅ Журналистский стиль применен (было {text_length}, стало {len(response)})")
                title, body = parse_ai_response(response)
                return title, body, "journalistic"
            else:
                logger.warning("⚠️ Журналистский стиль не удался, пробуем стандартную переработку")
                # Пробуем стандартную переработку как запасной вариант
                response = await call_deepseek_with_retry(DEEPSEEK_PROMPT, text, max_attempts=1)
                if response and len(response.strip()) > 100:
                    title, body = parse_ai_response(response)
                    return title, body, "full_adaptation_fallback"
                else:
                    title = text[:70].strip()
                    body = text[70:] if len(text) > 70 else text
                    return title, body, "original"
                
    except Exception as e:
        logger.error(f"❌ Ошибка адаптации текста: {e}")
        title = text[:70].strip()
        body = text[70:] if len(text) > 70 else text
        return title, body, "original"

def get_post_publish_keyboard():
    """Создает клавиатуру с кнопкой для подписки"""
    if not BUTTON_URL:
        return None
    
    url = BUTTON_URL.strip()
    
    if url.startswith('@'):
        url = f"https://t.me/{url[1:]}"
    elif not url.startswith('http'):
        url = f"https://t.me/{url}"
    
    logger.info(f"🔗 Создаем кнопку со ссылкой: {url}")
    
    keyboard = [
        [InlineKeyboardButton(BUTTON_TEXT, url=url)],
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТКА СООБЩЕНИЙ ====================
async def process_message(bot: Bot, message, source_channel_id: str, target_channel_id: str):
    """Обрабатывает одно сообщение"""
    message_id = message.message_id
    
    # Проверяем, не обработано ли уже
    if is_message_processed(message_id, source_channel_id):
        logger.info(f"⏭️ Сообщение {message_id} уже обработано, пропускаем")
        return
    
    try:
        # Получаем текст
        text = message.text or message.caption or ""
        if not text:
            logger.info(f"ℹ️ Сообщение {message_id} без текста, сохраняем как обработанное")
            save_processed_message(message_id, source_channel_id)
            return
        
        logger.info(f"📝 Обрабатываем сообщение {message_id}")
        logger.info(f"📄 Длина текста: {len(text)} символов")
        logger.info(f"📄 Текст: {text[:200]}...")
        
        # Адаптируем текст в зависимости от длины
        title, body, adaptation_type = await adapt_text_by_length(text)
        
        # Если адаптация не дала результата, используем оригинал
        if not title and not body:
            title = text[:70].strip()
            body = text[70:] if len(text) > 70 else text
            adaptation_type = "original"
        
        caption = format_caption(title, body)
        has_media = False
        media_type = None
        
        # Получаем клавиатуру (если настроена)
        reply_markup = get_post_publish_keyboard()
        if reply_markup:
            logger.info("✅ Кнопка добавлена к посту")
        else:
            logger.info("ℹ️ Кнопка не добавлена (BUTTON_URL не задан)")
        
        # Проверяем наличие медиа
        if message.photo:
            logger.info("📸 Обрабатываем фото...")
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            photo_bytes = await file.download_as_bytearray()
            
            try:
                processed_photo = process_photo(photo_bytes, title)
                await bot.send_photo(
                    chat_id=target_channel_id,
                    photo=InputFile(processed_photo, filename="post.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
                has_media = True
                media_type = "photo"
                logger.info(f"✅ Опубликовано оформленное фото в {target_channel_id}")
            except Exception as e:
                logger.error(f"⚠️ Ошибка оформления: {e}")
                await bot.send_photo(
                    chat_id=target_channel_id,
                    photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
                has_media = True
                media_type = "photo"
                logger.info(f"✅ Опубликовано оригинальное фото в {target_channel_id}")
        
        elif message.video:
            logger.info("🎬 Обрабатываем видео...")
            video = message.video
            file = await bot.get_file(video.file_id)
            video_bytes = await file.download_as_bytearray()
            
            await bot.send_video(
                chat_id=target_channel_id,
                video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
            has_media = True
            media_type = "video"
            logger.info(f"✅ Опубликовано видео в {target_channel_id}")
        
        elif message.document:
            doc = message.document
            if doc.mime_type and doc.mime_type.startswith('image/'):
                logger.info("📄 Обрабатываем документ-изображение...")
                file = await bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                
                try:
                    processed_photo = process_photo(file_bytes, title)
                    await bot.send_photo(
                        chat_id=target_channel_id,
                        photo=InputFile(processed_photo, filename=doc.file_name or "document.jpg"),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                    has_media = True
                    media_type = "document_image"
                    logger.info(f"✅ Опубликовано оформленное изображение из документа")
                except Exception as e:
                    logger.error(f"⚠️ Ошибка оформления документа: {e}")
                    await bot.send_photo(
                        chat_id=target_channel_id,
                        photo=InputFile(io.BytesIO(file_bytes), filename=doc.file_name or "document.jpg"),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )
                    has_media = True
                    media_type = "document_image"
            else:
                logger.info("📝 Отправляем только текст (документ не изображение)")
                await bot.send_message(
                    chat_id=target_channel_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
        
        else:
            logger.info("📝 Только текст...")
            await bot.send_message(
                chat_id=target_channel_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        
        # Сохраняем в БД (адаптированный текст)
        adapted_text = f"{title}\n\n{body}" if title and body else text
        save_republished(message_id, source_channel_id, title, text, adapted_text, has_media, media_type, adaptation_type)
        save_processed_message(message_id, source_channel_id)
        logger.info(f"✅ Сообщение {message_id} успешно обработано (тип: {adaptation_type}) и сохранено")
        
    except Exception as e:
        logger.error(f"❌ Ошибка обработки сообщения {message_id}: {e}")
        import traceback
        traceback.print_exc()
        save_failed(message_id, source_channel_id, str(e))
        save_processed_message(message_id, source_channel_id)

# ==================== ОСНОВНАЯ ФУНКЦИЯ ====================
async def check_and_process(bot: Bot):
    """Проверяет новые посты в канале"""
    try:
        logger.info("🔍 Начинаем проверку канала...")
        
        # Проверяем доступ к каналу
        try:
            chat = await bot.get_chat(SOURCE_CHANNEL_ID)
            logger.info(f"✅ Доступ к каналу: {chat.title} (ID: {chat.id})")
        except Exception as e:
            logger.error(f"❌ Нет доступа к каналу {SOURCE_CHANNEL_ID}: {e}")
            return
        
        # Получаем последние сообщения используя правильный метод
        last_id = get_last_processed_message(SOURCE_CHANNEL_ID)
        logger.info(f"📊 Последний обработанный ID: {last_id}")
        
        try:
            # Используем get_updates для получения новых сообщений
            messages = []
            async for update in bot.get_updates(limit=10):
                if update.channel_post:
                    messages.append(update.channel_post)
            
            if messages:
                logger.info(f"🆕 Найдено {len(messages)} новых сообщений через get_updates")
                for message in messages:
                    if message.chat_id == int(SOURCE_CHANNEL_ID):
                        if not last_id or message.message_id > last_id:
                            if not is_message_processed(message.message_id, SOURCE_CHANNEL_ID):
                                logger.info(f"  - Новое сообщение {message.message_id}: {message.text[:50] if message.text else '[без текста]'}...")
                                await process_message(bot, message, SOURCE_CHANNEL_ID, TARGET_CHANNEL_ID)
                                await asyncio.sleep(0.5)
            else:
                logger.info("ℹ️ Новых сообщений в обновлениях нет")
                
        except Exception as e:
            logger.error(f"⚠️ Ошибка получения обновлений: {e}")
            import traceback
            traceback.print_exc()
            
    except Exception as e:
        logger.error(f"❌ Ошибка проверки: {e}")
        import traceback
        traceback.print_exc()

# ==================== КОМАНДЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    button_status = "✅ Настроена" if BUTTON_URL else "❌ Не настроена"
    
    await update.message.reply_text(
        "🤖 *Бот-репостер новостей*\n\n"
        "Я автоматически забираю посты из одного канала, адаптирую их через ИИ и публикую в другом.\n\n"
        f"📥 *Источник:* {SOURCE_CHANNEL_ID}\n"
        f"📤 *Целевой канал:* {TARGET_CHANNEL_ID}\n"
        f"🤖 *DeepSeek AI:* {'✅ Подключен' if deepseek_client else '❌ Не настроен'}\n"
        f"🏷️ *Бренд:* {BRAND_NAME}\n"
        f"🔗 *Кнопка:* {button_status}\n"
        f"📝 *Текст кнопки:* {BUTTON_TEXT if BUTTON_URL else '—'}\n\n"
        "📊 *Типы адаптации:*\n"
        "• ≤200 символов: легкая редактура\n"
        "• 200-300 символов: умеренная адаптация\n"
        "• 300-500 символов: полная переработка\n"
        "• 500+ символов: журналистский стиль\n\n"
        "Команды:\n"
        "/start - Это сообщение\n"
        "/status - Статус\n"
        "/stats - Статистика\n"
        "/check - Принудительная проверка новых постов",
        parse_mode=ParseMode.MARKDOWN
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM republished_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        ).fetchone()[0]
        
        failed = conn.execute(
            "SELECT COUNT(*) FROM failed_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        ).fetchone()[0]
        
        last = conn.execute(
            "SELECT MAX(republished_at) FROM republished_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        ).fetchone()[0]
        
        # Статистика по типам адаптации
        adaptation_stats = conn.execute(
            """SELECT adaptation_type, COUNT(*) 
               FROM republished_posts 
               WHERE channel_id = ? AND adaptation_type IS NOT NULL
               GROUP BY adaptation_type""",
            (SOURCE_CHANNEL_ID,)
        ).fetchall()
    
    button_status = "✅" if BUTTON_URL else "❌"
    
    text = f"📊 *Статус*\n\n"
    text += f"📝 Обработано: {count}\n"
    text += f"❌ Ошибок: {failed}\n"
    text += f"🕐 Последняя: {last or 'Нет'}\n"
    text += f"🤖 AI: {'✅' if deepseek_client else '❌'}\n"
    text += f"🔗 Кнопка: {button_status}\n"
    text += f"🏷️ Бренд: {BRAND_NAME}\n\n"
    
    if adaptation_stats:
        text += "*Типы адаптации:*\n"
        type_names = {
            'original': 'Оригинал',
            'light_adaptation': 'Легкая редактура',
            'medium_adaptation': 'Умеренная адаптация',
            'full_adaptation': 'Полная переработка',
            'journalistic': 'Журналистский стиль',
            'full_adaptation_fallback': 'Полная переработка (запасной)'
        }
        for atype, count in adaptation_stats:
            name = type_names.get(atype, atype)
            text += f"  • {name}: {count}\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with sqlite3.connect(DB_PATH) as conn:
        daily = conn.execute(
            """SELECT DATE(republished_at), COUNT(*) 
               FROM republished_posts 
               WHERE channel_id = ? 
               GROUP BY DATE(republished_at)
               ORDER BY DATE(republished_at) DESC 
               LIMIT 7""",
            (SOURCE_CHANNEL_ID,)
        ).fetchall()
    
    text = "📊 *Статистика*\n\n"
    if daily:
        for date, count in daily:
            text += f"📅 {date}: {count} постов\n"
    else:
        text += "Нет публикаций"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительная проверка новых постов"""
    await update.message.reply_text("🔍 Начинаю проверку новых постов...")
    await check_and_process(context.bot)
    await update.message.reply_text("✅ Проверка завершена")

# ==================== ЗАПУСК ====================
async def run_bot():
    """Главный цикл бота"""
    try:
        init_db()
    except Exception as e:
        logger.error(f"❌ Не удалось инициализировать БД: {e}")
        return
    
    logger.info("\n" + "="*50)
    logger.info("🤖 БОТ-РЕПОСТЕР НОВОСТЕЙ (POLLING)")
    logger.info("="*50)
    logger.info(f"📥 Канал-источник: {SOURCE_CHANNEL_ID}")
    logger.info(f"📤 Целевой канал: {TARGET_CHANNEL_ID}")
    logger.info(f"🤖 DeepSeek AI: {'✅ Подключен' if deepseek_client else '❌ Не настроен'}")
    logger.info(f"🏷️ Бренд: {BRAND_NAME}")
    logger.info(f"🔗 Кнопка: {'✅ Настроена' if BUTTON_URL else '❌ Не настроена'}")
    if BUTTON_URL:
        logger.info(f"   Ссылка: {BUTTON_URL}")
        logger.info(f"   Текст: {BUTTON_TEXT}")
    logger.info("="*50 + "\n")
    
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчики для channel_post (это ключевой момент!)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("check", check_command))
    
    # Добавляем обработчик для постов из канала
    async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает новые посты в канале"""
        if update.channel_post:
            message = update.channel_post
            chat_id = str(message.chat_id)
            
            if chat_id == SOURCE_CHANNEL_ID:
                logger.info(f"📨 Получен новый пост в канале: {message.message_id}")
                await process_message(context.bot, message, SOURCE_CHANNEL_ID, TARGET_CHANNEL_ID)
    
    application.add_handler(MessageHandler(filters.TEXT & filters.Chat(chat_id=int(SOURCE_CHANNEL_ID)), channel_post_handler))
    # Добавляем обработчик для всех сообщений из канала (включая медиа)
    application.add_handler(MessageHandler(filters.Chat(chat_id=int(SOURCE_CHANNEL_ID)), channel_post_handler))
    
    # Запускаем
    try:
        await application.initialize()
        await application.start()
        
        # Проверяем подключение к боту
        me = await application.bot.get_me()
        logger.info(f"✅ Бот подключен: @{me.username} (ID: {me.id})")
        
        # Проверяем доступ к каналам
        try:
            source_chat = await application.bot.get_chat(SOURCE_CHANNEL_ID)
            logger.info(f"✅ Доступ к каналу-источнику: {source_chat.title}")
        except Exception as e:
            logger.error(f"❌ НЕТ ДОСТУПА К КАНАЛУ-ИСТОЧНИКУ: {e}")
            logger.error("Проверьте, что бот добавлен как администратор в канал-источник!")
            
        try:
            target_chat = await application.bot.get_chat(TARGET_CHANNEL_ID)
            logger.info(f"✅ Доступ к целевому каналу: {target_chat.title}")
        except Exception as e:
            logger.error(f"❌ НЕТ ДОСТУПА К ЦЕЛЕВОМУ КАНАЛУ: {e}")
            logger.error("Проверьте, что бот добавлен как администратор в целевой канал!")
        
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации: {e}")
        return
    
    # Удаляем webhook (ВАЖНО!)
    try:
        logger.info("🔄 Удаляем webhook...")
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook удален")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка удаления webhook: {e}")
    
    await asyncio.sleep(2)
    
    # Запускаем polling
    try:
        logger.info("🔄 Запускаем polling...")
        await application.updater.start_polling(
            allowed_updates=["message", "channel_post"],
            drop_pending_updates=True,
            poll_interval=1.0,
            timeout=30
        )
        logger.info("✅ Бот запущен и слушает обновления!")
    except Exception as e:
        logger.error(f"⚠️ Ошибка запуска polling: {e}")
        logger.info("🔄 Пробуем еще раз через 5 секунд...")
        await asyncio.sleep(5)
        try:
            await application.updater.start_polling(
                allowed_updates=["message", "channel_post"],
                drop_pending_updates=True,
                poll_interval=1.0,
                timeout=30
            )
            logger.info("✅ Бот запущен со второй попытки!")
        except Exception as e2:
            logger.error(f"❌ Не удалось запустить polling: {e2}")
            return
    
    # Основной цикл проверки новых постов (уже не нужен, но оставим для обратной совместимости)
    logger.info("⚡ Бот ожидает новые посты в реальном времени...")
    
    # Бесконечное ожидание, чтобы бот не завершился
    while True:
        try:
            await asyncio.sleep(60)
            # Периодически проверяем, не упал ли polling
            if not application.updater.running:
                logger.warning("⚠️ Updater остановлен, перезапускаем...")
                await application.updater.start_polling(
                    allowed_updates=["message", "channel_post"],
                    drop_pending_updates=True,
                    poll_interval=1.0,
                    timeout=30
                )
        except asyncio.CancelledError:
            logger.info("🔄 Цикл остановлен")
            break
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        import traceback
        traceback.print_exc()
