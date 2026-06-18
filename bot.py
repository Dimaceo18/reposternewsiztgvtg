import asyncio
import sqlite3
import os
import re
import io
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto, InputMediaVideo
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID")
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DB_PATH = "republished.db"

# Настройки для оформления фото
FONT_PATH = os.getenv("FONT_PATH", "Montserrat-Black.ttf")

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

deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
) if DEEPSEEK_API_KEY else None

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    """Инициализация базы данных"""
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
                media_type TEXT
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
    print("✅ База данных готова")

def is_message_processed(message_id: int, channel_id: str) -> bool:
    """Проверяет, обработано ли сообщение"""
    with sqlite3.connect(DB_PATH) as conn:
        result = conn.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ? AND channel_id = ?",
            (message_id, channel_id)
        ).fetchone()
        return result is not None

def save_processed_message(message_id: int, channel_id: str):
    """Сохраняет ID обработанного сообщения"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO processed_messages (message_id, channel_id, processed_at) VALUES (?, ?, ?)",
            (message_id, channel_id, datetime.now())
        )

def save_republished(message_id: int, channel_id: str, title: str, original_text: str, adapted_text: str, has_media: bool = False, media_type: str = None):
    """Сохраняет информацию о републикации"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO republished_posts 
               (message_id, channel_id, title, republished_at, original_text, adapted_text, has_media, media_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (message_id, channel_id, title, datetime.now(), original_text, adapted_text, has_media, media_type)
        )

def save_failed(message_id: int, channel_id: str, error: str):
    """Сохраняет информацию об ошибке"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO failed_posts (message_id, channel_id, error, failed_at) VALUES (?, ?, ?, ?)",
            (message_id, channel_id, error, datetime.now())
        )

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С ТЕКСТОМ ====================
def remove_emojis(text: str) -> str:
    """Удаляет эмодзи из текста"""
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
    """Форматирует подпись для поста"""
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
    """Переносит текст на несколько строк"""
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
    """Оформляет фото с текстом"""
    if not photo_bytes or len(photo_bytes) == 0:
        raise ValueError("Фото пустое")
    
    print(f"🖼️ Обработка фото, размер: {len(photo_bytes) / 1024:.1f}KB")
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    w, h = img.size
    
    # Обрезаем до соотношения 4:5
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
    
    # Изменяем размер
    img = img.resize((1080, 1350), Image.Resampling.LANCZOS)
    
    # Затемняем нижнюю часть
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
    
    # Загружаем шрифт
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
                print(f"✅ Загружен шрифт: {font_path}")
                break
        except:
            continue
    
    if font is None:
        font = ImageFont.load_default()
        print("⚠️ Шрифт не найден, использую стандартный")
    
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
        
        # Тень для текста
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
    print(f"✅ Фото готово: {output.getbuffer().nbytes / (1024 * 1024):.2f}MB, строк: {len(lines)}")
    return output

# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С AI ====================
async def call_deepseek_with_retry(prompt: str, text: str, max_attempts: int = 2) -> str:
    """Вызов DeepSeek API с повторными попытками"""
    if not deepseek_client:
        return text
    
    async def make_request(current_prompt, current_text):
        try:
            response = await deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": current_prompt},
                    {"role": "user", "content": f"Перепиши эту новость в новостном формате на 600-650 символов. Сохрани ВСЕ важные факты, цифры, даты, имена. НЕ ОБРЕЗАЙ текст, а ПЕРЕПИШИ его, сохраняя смысл. НЕ пиши слова ЗАГОЛОВОК и ТЕКСТ. Просто напиши сначала заголовок, потом пустую строку, потом текст.\n\n{current_text}"}
                ],
                temperature=0.7,
                max_tokens=1000
            )
            return response.choices[0].message.content
        except Exception as e:
            print(f"Ошибка запроса к DeepSeek: {e}")
            raise e
    
    for attempt in range(max_attempts):
        try:
            content = await make_request(prompt, text)
            
            if not content or len(content.strip()) < 50:
                print(f"Попытка {attempt + 1}: Получен пустой или слишком короткий ответ")
                if attempt == max_attempts - 1:
                    return content
                continue
            
            char_count = len(content)
            print(f"Попытка {attempt + 1}: Получен текст длиной {char_count} символов")
            
            if 550 <= char_count <= 700 or attempt == max_attempts - 1:
                return content
            
            if char_count < 550:
                text = f"СДЕЛАЙ ТЕКСТ ДЛИННЕЕ (сейчас {char_count} символов, нужно 600-650). Добавь больше деталей, фактов, цифр. Вот исходный текст:\n\n{text}"
            else:
                text = f"СДЕЛАЙ ТЕКСТ КОРОЧЕ (сейчас {char_count} символов, нужно 600-650). Убери лишние слова, но сохрани все важные факты. Вот исходный текст:\n\n{text}"
                
        except Exception as e:
            print(f"Ошибка при попытке {attempt + 1}: {e}")
            if attempt == max_attempts - 1:
                return ""
    
    return ""

def parse_ai_response(content: str) -> Tuple[str, str]:
    """Разбирает ответ AI на заголовок и текст"""
    if not content:
        return "", ""
    
    content = content.strip()
    
    # Убираем лишние маркеры
    lines = content.split('\n')
    clean_lines = []
    for line in lines:
        line_clean = line.strip()
        if line_clean.lower().startswith("заголовок:") or line_clean.lower().startswith("текст:"):
            continue
        clean_lines.append(line)
    
    content = '\n'.join(clean_lines).strip()
    
    # Пытаемся разделить на заголовок и текст
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
    
    # Очищаем от лишних символов
    title = re.sub(r'^[#*\-_\s]+', '', title).strip()
    body = re.sub(r'^[#*\-_\s]+', '', body).strip()
    
    if not title and body:
        title = body[:70].strip()
        body = body[70:].strip() if len(body) > 70 else body
    
    if not body and title:
        body = title
        title = ""
    
    return title, body

# ==================== КНОПКИ ДЛЯ ПОСТОВ ====================
def get_post_publish_keyboard():
    """Клавиатура для опубликованных постов"""
    keyboard = [
        [InlineKeyboardButton("📢 Подписаться на канал", url=f"https://t.me/{TARGET_CHANNEL_ID.replace('-100', '')}")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ==================== ХРАНИЛИЩЕ ДЛЯ МЕДИА-ГРУПП ====================
media_group_cache = {}

# ==================== ОБРАБОТКА МЕДИА-ГРУПП ====================
async def process_media_group(bot: Bot, message, target_channel_id: str, caption: str, title: str, source_channel_id: str, message_id: int):
    """Обрабатывает медиа-группу (несколько фото/видео)"""
    try:
        print(f"📦 Обработка медиа-группы, первое сообщение: {message_id}")
        
        # Собираем все медиа из группы
        media_group = []
        has_video = False
        has_photo = False
        photo_count = 0
        
        # Проверяем, есть ли видео в группе
        if message.video:
            has_video = True
            print(f"🎬 В группе есть видео")
            
            # Скачиваем видео
            video_file = await bot.get_file(message.video.file_id)
            video_bytes = await video_file.download_as_bytearray()
            
            media_group.append({
                'type': 'video',
                'file_id': message.video.file_id,
                'bytes': video_bytes,
                'caption': None
            })
            
        elif message.photo:
            has_photo = True
            photo_count += 1
            print(f"📸 В группе есть фото #{photo_count}")
            
            # Скачиваем фото
            photo = message.photo[-1]
            photo_file = await bot.get_file(photo.file_id)
            photo_bytes = await photo_file.download_as_bytearray()
            
            # Определяем, нужно ли оформлять (только первое фото, если нет видео)
            should_design = (photo_count == 1 and not has_video)
            
            if should_design:
                print(f"🎨 Оформляем первое фото с заголовком")
                try:
                    processed_photo = process_photo(photo_bytes, title)
                    processed_bytes = processed_photo.getvalue()
                    media_group.append({
                        'type': 'photo',
                        'bytes': processed_bytes,
                        'caption': caption,
                        'is_designed': True
                    })
                except Exception as e:
                    print(f"⚠️ Ошибка оформления первого фото: {e}")
                    media_group.append({
                        'type': 'photo',
                        'bytes': photo_bytes,
                        'caption': caption,
                        'is_designed': False
                    })
            else:
                # Остальные фото или если есть видео - без оформления
                media_group.append({
                    'type': 'photo',
                    'bytes': photo_bytes,
                    'caption': None,
                    'is_designed': False
                })
        
        # Отправляем медиа-группу
        if media_group:
            # Если в группе есть видео - отправляем отдельно
            if has_video:
                print(f"🎬 Отправляем видео из медиа-группы")
                # Отправляем видео с подписью
                for item in media_group:
                    if item['type'] == 'video':
                        await bot.send_video(
                            chat_id=target_channel_id,
                            video=InputFile(io.BytesIO(item['bytes']), filename="video.mp4"),
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=get_post_publish_keyboard()
                        )
                    elif item['type'] == 'photo':
                        # Отправляем фото без подписи (подпись уже была с видео)
                        await bot.send_photo(
                            chat_id=target_channel_id,
                            photo=InputFile(io.BytesIO(item['bytes']), filename="photo.jpg")
                        )
            else:
                # Только фото - отправляем как медиа-группу
                print(f"📸 Отправляем {len(media_group)} фото как медиа-группу")
                
                # Создаем медиа-группу для отправки
                media_to_send = []
                for i, item in enumerate(media_group):
                    if item['type'] == 'photo':
                        if i == 0 and item.get('caption'):  # Первое фото с подписью
                            media_to_send.append(
                                InputMediaPhoto(
                                    media=io.BytesIO(item['bytes']),
                                    caption=item['caption'],
                                    parse_mode=ParseMode.HTML
                                )
                            )
                        else:  # Остальные без подписи
                            media_to_send.append(
                                InputMediaPhoto(
                                    media=io.BytesIO(item['bytes'])
                                )
                            )
                
                # Отправляем медиа-группу
                if media_to_send:
                    await bot.send_media_group(
                        chat_id=target_channel_id,
                        media=media_to_send
                    )
                    
                    # Добавляем кнопку подписки после медиа-группы
                    await bot.send_message(
                        chat_id=target_channel_id,
                        text="📢 Подпишитесь на канал, чтобы не пропустить новости!",
                        reply_markup=get_post_publish_keyboard()
                    )
        
        # Сохраняем в БД
        media_type = "video_photo_mixed" if has_video and has_photo else ("video" if has_video else "photo_group")
        has_media = True
        
        save_republished(message_id, source_channel_id, title, message.text or message.caption or "", 
                        message.text or message.caption or "", has_media, media_type)
        save_processed_message(message_id, source_channel_id)
        
        print(f"✅ Медиа-группа успешно обработана и сохранена")
        
    except Exception as e:
        print(f"❌ Ошибка при обработке медиа-группы: {e}")
        save_failed(message_id, source_channel_id, str(e))
        save_processed_message(message_id, source_channel_id)

# ==================== ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ ====================
async def process_and_republish(bot: Bot, message, source_channel_id: str, target_channel_id: str):
    """Обрабатывает и републикует сообщение"""
    message_id = message.message_id
    
    # Проверяем, не обработано ли уже сообщение
    if is_message_processed(message_id, source_channel_id):
        print(f"⏭️ Пропускаем сообщение {message_id} - уже обработано")
        return
    
    try:
        # Получаем текст сообщения
        text = message.text or message.caption or ""
        if not text:
            print(f"⏭️ Пропускаем сообщение {message_id} - нет текста")
            save_processed_message(message_id, source_channel_id)
            return
        
        print(f"📝 Обрабатываем сообщение {message_id}")
        print(f"📄 Длина текста: {len(text)} символов")
        
        # Обрабатываем через DeepSeek
        if deepseek_client:
            print("🤖 Отправляем запрос к DeepSeek...")
            adapted_text = await call_deepseek_with_retry(DEEPSEEK_PROMPT, text)
            
            if adapted_text and len(adapted_text.strip()) > 50:
                title, body = parse_ai_response(adapted_text)
                print(f"✅ Текст адаптирован через AI")
                print(f"📊 Длина адаптированного текста: {len(adapted_text)} символов")
            else:
                print("⚠️ AI не смог обработать текст, используем оригинал")
                title = text[:70].strip()
                body = text[70:] if len(text) > 70 else text
                adapted_text = text
        else:
            print("⚠️ DeepSeek не настроен, используем оригинальный текст")
            title = text[:70].strip()
            body = text[70:] if len(text) > 70 else text
            adapted_text = text
        
        # Формируем подпись
        caption = format_caption(title, body)
        has_media = False
        media_type = None
        
        # ============================================================
        # 1. ПРОВЕРЯЕМ: ЕСЛИ ЕСТЬ МЕДИА-ГРУППА (НЕСКОЛЬКО ФОТО/ВИДЕО)
        # ============================================================
        if message.media_group_id:
            print(f"📦 Обнаружена медиа-группа: {message.media_group_id}")
            await process_media_group(
                bot, 
                message, 
                target_channel_id, 
                caption, 
                title, 
                source_channel_id, 
                message_id
            )
            return
        
        # ============================================================
        # 2. ОДИНОЧНЫЕ МЕДИА
        # ============================================================
        
        # Проверяем наличие медиа
        if message.photo:
            # ========== ОДНО ФОТО ==========
            print("📸 Обнаружено одно фото, оформляем...")
            photo = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            photo_bytes = await file.download_as_bytearray()
            
            # Оформляем фото с заголовком
            try:
                processed_photo = process_photo(photo_bytes, title)
                await bot.send_photo(
                    chat_id=target_channel_id,
                    photo=InputFile(processed_photo, filename="post.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_post_publish_keyboard()
                )
                has_media = True
                media_type = "photo"
                print(f"📸 Опубликовано оформленное фото из сообщения {message_id}")
            except Exception as e:
                print(f"⚠️ Ошибка оформления фото: {e}, отправляем без оформления")
                await bot.send_photo(
                    chat_id=target_channel_id,
                    photo=InputFile(io.BytesIO(photo_bytes), filename="post.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_post_publish_keyboard()
                )
                has_media = True
                media_type = "photo"
            
            # Сохраняем в БД
            save_republished(message_id, source_channel_id, title, text, adapted_text, has_media, media_type)
            save_processed_message(message_id, source_channel_id)
            return
            
        elif message.video:
            # ========== ВИДЕО (БЕЗ ОФОРМЛЕНИЯ) ==========
            print("🎬 Обнаружено видео, отправляем с адаптированным текстом...")
            video = message.video
            file = await bot.get_file(video.file_id)
            video_bytes = await file.download_as_bytearray()
            
            await bot.send_video(
                chat_id=target_channel_id,
                video=InputFile(io.BytesIO(video_bytes), filename="video.mp4"),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=get_post_publish_keyboard()
            )
            has_media = True
            media_type = "video"
            print(f"🎬 Опубликовано видео из сообщения {message_id}")
            
            # Сохраняем в БД
            save_republished(message_id, source_channel_id, title, text, adapted_text, has_media, media_type)
            save_processed_message(message_id, source_channel_id)
            return
            
        elif message.document:
            # ========== ДОКУМЕНТЫ ==========
            doc = message.document
            if doc.mime_type and doc.mime_type.startswith('image/'):
                print("📄 Обнаружен документ-изображение, оформляем...")
                file = await bot.get_file(doc.file_id)
                file_bytes = await file.download_as_bytearray()
                
                try:
                    processed_photo = process_photo(file_bytes, title)
                    await bot.send_photo(
                        chat_id=target_channel_id,
                        photo=InputFile(processed_photo, filename=doc.file_name or "document.jpg"),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_post_publish_keyboard()
                    )
                    has_media = True
                    media_type = "document_image"
                except:
                    await bot.send_photo(
                        chat_id=target_channel_id,
                        photo=InputFile(io.BytesIO(file_bytes), filename=doc.file_name or "document.jpg"),
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_post_publish_keyboard()
                    )
                    has_media = True
                    media_type = "document_image"
            else:
                # Просто текст
                await bot.send_message(
                    chat_id=target_channel_id,
                    text=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_post_publish_keyboard()
                )
            
            # Сохраняем в БД
            save_republished(message_id, source_channel_id, title, text, adapted_text, has_media, media_type)
            save_processed_message(message_id, source_channel_id)
            return
        else:
            # ========== ТОЛЬКО ТЕКСТ ==========
            await bot.send_message(
                chat_id=target_channel_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=get_post_publish_keyboard()
            )
            print(f"📝 Опубликован текст из сообщения {message_id}")
            
            # Сохраняем в БД
            save_republished(message_id, source_channel_id, title, text, adapted_text, False, None)
            save_processed_message(message_id, source_channel_id)
            return
        
    except Exception as e:
        print(f"❌ Ошибка при обработке сообщения {message_id}: {e}")
        save_failed(message_id, source_channel_id, str(e))
        save_processed_message(message_id, source_channel_id)

# ==================== ОБРАБОТЧИКИ СООБЩЕНИЙ ====================
async def handle_new_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает новые посты в канале-источнике"""
    # Проверяем, что сообщение из канала-источника
    if str(update.channel_post.chat_id) != SOURCE_CHANNEL_ID:
        return
    
    message = update.channel_post
    
    # Если это медиа-группа - обрабатываем через специальный обработчик
    if message.media_group_id:
        await handle_media_group_message(update, context)
        return
    
    # Запускаем обработку в фоновом режиме
    asyncio.create_task(
        process_and_republish(
            context.bot,
            message,
            SOURCE_CHANNEL_ID,
            TARGET_CHANNEL_ID
        )
    )

async def handle_media_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сообщения из медиа-группы"""
    message = update.channel_post
    media_group_id = message.media_group_id
    
    if not media_group_id:
        return
    
    # Сохраняем сообщение в кэш
    if media_group_id not in media_group_cache:
        media_group_cache[media_group_id] = {
            'messages': [],
            'processed': False,
            'created_at': datetime.now()
        }
    
    media_group_cache[media_group_id]['messages'].append(message)
    
    # Ждем немного, чтобы собрать все сообщения группы
    await asyncio.sleep(1)
    
    # Проверяем, не обработана ли уже группа
    if media_group_cache[media_group_id]['processed']:
        return
    
    # Проверяем, собраны ли все сообщения (по таймауту)
    if (datetime.now() - media_group_cache[media_group_id]['created_at']).seconds > 2:
        media_group_cache[media_group_id]['processed'] = True
        
        # Берем первое сообщение для обработки
        first_message = media_group_cache[media_group_id]['messages'][0]
        
        # Получаем текст
        text = first_message.text or first_message.caption or ""
        
        # Обрабатываем через DeepSeek для получения заголовка
        if deepseek_client and text:
            adapted_text = await call_deepseek_with_retry(DEEPSEEK_PROMPT, text)
            if adapted_text and len(adapted_text.strip()) > 50:
                title, body = parse_ai_response(adapted_text)
            else:
                title = text[:70].strip()
        else:
            title = text[:70].strip() if text else "Новость"
        
        # Формируем подпись
        caption = format_caption(title, body if 'body' in locals() else text)
        
        # Обрабатываем медиа-группу
        await process_media_group(
            context.bot,
            first_message,
            TARGET_CHANNEL_ID,
            caption,
            title,
            SOURCE_CHANNEL_ID,
            first_message.message_id
        )
        
        # Очищаем кэш
        del media_group_cache[media_group_id]

# ==================== КОМАНДЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "🤖 *Бот-репостер новостей*\n\n"
        "Я автоматически забираю посты из одного канала, адаптирую их через ИИ и публикую в другом.\n\n"
        f"📥 *Источник:* {SOURCE_CHANNEL_ID}\n"
        f"📤 *Целевой канал:* {TARGET_CHANNEL_ID}\n"
        f"🤖 *DeepSeek AI:* {'✅ Подключен' if deepseek_client else '❌ Не настроен'}\n"
        f"🎨 *Оформление фото:* ✅ Активно\n"
        f"🎬 *Видео:* Адаптация текста, без оформления\n"
        f"📸 *Медиа-группы:* Первое фото оформляется, остальные без изменений\n\n"
        "⚡ Реагирую на новые посты в реальном времени\n"
        "🔄 Автоматическая адаптация текста\n"
        "🎨 Оформление фото с заголовком\n"
        "📊 Сохранение истории публикаций\n\n"
        "*Команды:*\n"
        "/start - Показать это сообщение\n"
        "/status - Статус бота\n"
        "/stats - Статистика\n"
        "/clear - Очистить историю",
        parse_mode=ParseMode.MARKDOWN
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статус бота"""
    with sqlite3.connect(DB_PATH) as conn:
        republished_count = conn.execute(
            "SELECT COUNT(*) FROM republished_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        ).fetchone()[0]
        
        failed_count = conn.execute(
            "SELECT COUNT(*) FROM failed_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        ).fetchone()[0]
        
        # Статистика по типам
        media_stats = conn.execute(
            """SELECT media_type, COUNT(*) 
               FROM republished_posts 
               WHERE channel_id = ? 
               GROUP BY media_type""",
            (SOURCE_CHANNEL_ID,)
        ).fetchall()
        
        last_post = conn.execute(
            "SELECT republished_at FROM republished_posts WHERE channel_id = ? ORDER BY republished_at DESC LIMIT 1",
            (SOURCE_CHANNEL_ID,)
        ).fetchone()
    
    status_text = (
        f"📊 *Статус бота*\n\n"
        f"📥 Канал-источник: `{SOURCE_CHANNEL_ID}`\n"
        f"📤 Целевой канал: `{TARGET_CHANNEL_ID}`\n"
        f"🤖 DeepSeek AI: {'✅ Активен' if deepseek_client else '❌ Не настроен'}\n"
        f"🎨 Оформление фото: ✅ Включено\n"
        f"🎬 Видео: Без оформления\n"
        f"📸 Медиа-группы: Первое фото с заголовком\n"
        f"📝 Всего републиковано: {republished_count}\n"
        f"❌ Ошибок: {failed_count}\n"
    )
    
    # Добавляем статистику по типам
    if media_stats:
        status_text += f"\n*📊 По типам:*\n"
        for media_type, count in media_stats:
            if media_type == "photo":
                icon = "📸"
            elif media_type == "video":
                icon = "🎬"
            elif media_type == "document_image":
                icon = "📄"
            elif media_type == "photo_group":
                icon = "🖼️"
            elif media_type == "video_photo_mixed":
                icon = "🎬📸"
            else:
                icon = "📝"
            status_text += f"  {icon} {media_type or 'текст'}: {count}\n"
    
    status_text += f"\n🕐 Последняя публикация: {last_post[0] if last_post else 'Нет'}\n"
    status_text += f"⏰ Текущее время: {datetime.now().strftime('%H:%M:%S')}"
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику"""
    with sqlite3.connect(DB_PATH) as conn:
        # Статистика по дням
        daily_stats = conn.execute(
            """SELECT DATE(republished_at), COUNT(*) 
               FROM republished_posts 
               WHERE channel_id = ? 
               GROUP BY DATE(republished_at)
               ORDER BY DATE(republished_at) DESC 
               LIMIT 7""",
            (SOURCE_CHANNEL_ID,)
        ).fetchall()
        
        # Статистика по типам медиа
        media_stats = conn.execute(
            """SELECT media_type, COUNT(*) 
               FROM republished_posts 
               WHERE channel_id = ? 
               GROUP BY media_type""",
            (SOURCE_CHANNEL_ID,)
        ).fetchall()
        
        # Последние 5 публикаций
        last_posts = conn.execute(
            """SELECT title, republished_at, media_type 
               FROM republished_posts 
               WHERE channel_id = ? 
               ORDER BY republished_at DESC 
               LIMIT 5""",
            (SOURCE_CHANNEL_ID,)
        ).fetchall()
    
    stats_text = "📊 *Статистика публикаций*\n\n"
    
    # Статистика по дням
    if daily_stats:
        stats_text += "*📅 Последние 7 дней:*\n"
        for date, count in daily_stats:
            stats_text += f"  • {date}: {count} постов\n"
    else:
        stats_text += "📭 Нет публикаций за последние 7 дней\n"
    
    # Статистика по типам
    if media_stats:
        stats_text += f"\n*📊 По типам контента:*\n"
        for media_type, count in media_stats:
            if media_type == "photo":
                icon = "📸 Фото (оформленное)"
            elif media_type == "video":
                icon = "🎬 Видео"
            elif media_type == "document_image":
                icon = "📄 Документ-изображение"
            elif media_type == "photo_group":
                icon = "🖼️ Группа фото"
            elif media_type == "video_photo_mixed":
                icon = "🎬📸 Видео + Фото"
            else:
                icon = "📝 Текст"
            stats_text += f"  • {icon}: {count}\n"
    
    # Последние публикации
    if last_posts:
        stats_text += f"\n*🕐 Последние 5 публикаций:*\n"
        for title, date, media_type in last_posts:
            if media_type == "photo":
                icon = "📸"
            elif media_type == "video":
                icon = "🎬"
            elif media_type == "document_image":
                icon = "📄"
            elif media_type == "photo_group":
                icon = "🖼️"
            elif media_type == "video_photo_mixed":
                icon = "🎬📸"
            else:
                icon = "📝"
            stats_text += f"  • {icon} {title[:40]}... ({date.strftime('%d.%m %H:%M')})\n"
    
    await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очищает историю"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM republished_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        )
        conn.execute(
            "DELETE FROM failed_posts WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        )
        conn.execute(
            "DELETE FROM processed_messages WHERE channel_id = ?",
            (SOURCE_CHANNEL_ID,)
        )
    await update.message.reply_text("✅ История очищена!")

# ==================== ЗАПУСК ====================
async def run_bot():
    """Запуск бота"""
    init_db()
    
    print("\n" + "="*50)
    print("🤖 БОТ-РЕПОСТЕР НОВОСТЕЙ")
    print("="*50)
    print(f"📥 Канал-источник: {SOURCE_CHANNEL_ID}")
    print(f"📤 Целевой канал: {TARGET_CHANNEL_ID}")
    print(f"🤖 DeepSeek AI: {'✅ Подключен' if deepseek_client else '❌ Не настроен'}")
    print(f"🎨 Оформление фото: ✅ Включено")
    print(f"🎬 Видео: Без оформления, только текст")
    print(f"📸 Медиа-группы: Первое фото оформляется, остальные без изменений")
    print("="*50 + "\n")
    
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("clear", clear_history))
    
    # Обработчик новых постов в канале
    application.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL,
        handle_new_channel_post
    ))
    
    # Запускаем бота
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=True,
        poll_interval=1.0,
        timeout=30
    )
    
    print("✅ Бот запущен и работает!")
    print("⚡ Ожидание новых постов в канале-источнике...")
    
    # Держим бота запущенным
    while True:
        await asyncio.sleep(1)

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("❌ Ошибка: BOT_TOKEN не задан!")
        exit(1)
    
    if not SOURCE_CHANNEL_ID:
        print("❌ Ошибка: SOURCE_CHANNEL_ID не задан!")
        exit(1)
    
    if not TARGET_CHANNEL_ID:
        print("❌ Ошибка: TARGET_CHANNEL_ID не задан!")
        exit(1)
    
    if not DEEPSEEK_API_KEY:
        print("⚠️ Предупреждение: DEEPSEEK_API_KEY не задан! Будет работать без AI.")
    
    asyncio.run(run_bot())
# ==================== HEALTH CHECK ====================
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok", "mode": "webhook"}

# Запускаем FastAPI в отдельном потоке
import threading
import uvicorn

def run_health_server():
    port = int(os.getenv("PORT", 10000)) + 1
    uvicorn.run(app, host="0.0.0.0", port=port)

# Запускаем health check сервер
threading.Thread(target=run_health_server, daemon=True).start()
