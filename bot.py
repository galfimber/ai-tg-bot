import os
import json
import logging
import tempfile
from typing import Dict, List, Optional, Tuple
from mimetypes import guess_extension

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiohttp import web
from dotenv import load_dotenv
import requests

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Загрузка конфигурации
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = os.getenv("BASE_URL")
PORT = int(os.getenv("PORT", 10000))

# Инициализация бота
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# Хранение данных
user_context: Dict[int, List[dict]] = {}
user_edit_state: Dict[int, dict] = {}

# --- Вспомогательные функции ---
async def make_api_request(url: str, method: str = "POST", **kwargs) -> Tuple[bool, dict]:
    """Универсальная функция для API-запросов с обработкой ошибок"""
    try:
        response = await requests.request(
            method,
            url,
            **kwargs,
            timeout=30
        )
        
        if response.status_code != 200:
            error_msg = f"API error: {response.status_code} - {response.text}"
            logger.error(error_msg)
            return False, {"error": error_msg}
        
        try:
            return True, response.json()
        except ValueError as e:
            error_msg = f"JSON decode error: {str(e)}"
            logger.error(error_msg)
            return False, {"error": error_msg}
            
    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {str(e)}"
        logger.error(error_msg)
        return False, {"error": error_msg}

def validate_message(message: Optional[types.Message]) -> bool:
    """Проверка валидности сообщения"""
    if not message:
        logger.warning("Received None message")
        return False
    if not message.from_user:
        logger.warning("Message without sender")
        return False
    return True

# --- Клавиатуры ---
def get_main_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🖼 Сгенерировать изображение"),
        KeyboardButton(text="✏️ Редактировать изображение")
    )
    builder.row(KeyboardButton(text="🔄 Сбросить контекст"))
    return builder.as_markup(resize_keyboard=True)

# --- Команды ---
@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    if not validate_message(message):
        return
    
    await message.answer(
        "✨ <b>AI Бот с функциями:</b>\n"
        "- Умный чат, отвечу на ваши вопросы, сгенерирую текст, проанализирую документ\n"
        "- Генерация изображений\n"
        "- Редактирование изображения",
        reply_markup=get_main_kb()
    )

# --- Текстовые сообщения ---
@dp.message(F.text == "🔄 Сбросить контекст")
async def reset_context(message: Message):
    if not validate_message(message):
        return
    
    user_context.pop(message.from_user.id, None)
    await message.answer("Контекст очищен!", reply_markup=get_main_kb())

@dp.message(F.text == "🖼 Сгенерировать изображение")
async def ask_gen_prompt(message: Message):
    if not validate_message(message):
        return
    
    await message.answer("Введите запрос для генерации:")

@dp.message(F.text == "✏️ Редактировать изображение")
async def ask_edit_photo(message: Message):
    if not validate_message(message):
        return
    
    user_edit_state[message.from_user.id] = {}
    await message.answer("Загрузите изображение:")

# --- Генерация изображений ---
@dp.message(
    F.text,
    F.reply_to_message.func(
        lambda msg: msg and hasattr(msg, 'text') and msg.text == "Введите запрос для генерации:"
    )
)
async def generate_image(message: Message):
    if not validate_message(message):
        return
    
    prompt = message.text.strip()
    
    try:
        await message.answer_chat_action("upload_photo")
        
        success, result = await make_api_request(
            "https://api.stability.ai/v2beta/stable-image/generate/sd3",
            method="POST",
            headers={"Authorization": f"Bearer {STABILITY_API_KEY}"},
            files={"none": ""},
            data={"prompt": prompt, "output_format": "png"}
        )
        
        if success:
            await message.answer_photo(result.content, caption=f"🎨 {prompt}")
        else:
            await message.answer(f"❌ Ошибка генерации: {result.get('error', 'Unknown error')}")
            
    except Exception as e:
        logger.error(f"Generation error: {str(e)}", exc_info=True)
        await message.answer(f"⚠️ Ошибка: {str(e)}")

# --- Загрузка изображений ---
@dp.message(F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT}))
async def handle_image_upload(message: Message):
    if not validate_message(message):
        return
    
    user_id = message.from_user.id
    
    if user_id not in user_edit_state:
        return
    
    try:
        if message.document:
            if not message.document.mime_type.startswith("image/"):
                await message.answer("Отправьте только JPEG/PNG!")
                return
            
            file = await bot.get_file(message.document.file_id)
            ext = guess_extension(message.document.mime_type) or ".jpg"
        else:
            file = await bot.get_file(message.photo[-1].file_id)
            ext = ".jpg"
        
        downloaded = await bot.download_file(file.file_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(downloaded.read())
            user_edit_state[user_id]["image_path"] = tmp.name
        
        await message.answer("Теперь опишите изменения (например: 'Убери фон'):")
        
    except Exception as e:
        logger.error(f"Upload error: {str(e)}", exc_info=True)
        await message.answer(f"⚠️ Ошибка загрузки: {str(e)}")
        user_edit_state.pop(user_id, None)

# --- Редактирование изображений ---
@dp.message(F.text, F.from_user.id.in_(user_edit_state.keys()))
async def process_image_edit(message: Message):
    if not validate_message(message):
        return
    
    user_id = message.from_user.id
    edit_prompt = message.text
    image_path = user_edit_state[user_id].get("image_path")
    
    if not image_path or not os.path.exists(image_path):
        await message.answer("❌ Изображение не найдено. Начните заново.")
        user_edit_state.pop(user_id, None)
        return
    
    try:
        await message.answer_chat_action("upload_photo")
        
        with open(image_path, "rb") as img_file:
            response = requests.post(
                "https://api.stability.ai/v2beta/stable-image/edit/inpaint",
                headers={"Authorization": f"Bearer {STABILITY_API_KEY}"},
                files={"image": img_file},
                data={
                    "prompt": edit_prompt,
                    "output_format": "png",
                },
                timeout=90
            )
        
        if response.status_code == 200:
            await message.answer_photo(
                response.content,
                caption=f"✏️ Редактирование: {edit_prompt}"
            )
        else:
            error = response.json().get("message", "Unknown error")
            await message.answer(f"❌ Ошибка редактирования: {error}")
            
    except Exception as e:
        logger.error(f"Edit error: {str(e)}", exc_info=True)
        await message.answer(f"⚠️ Ошибка API: {str(e)}")
    finally:
        if image_path and os.path.exists(image_path):
            os.unlink(image_path)
        user_edit_state.pop(user_id, None)

# --- Текстовый чат ---
@dp.message(F.text)
async def handle_ai_chat(message: Message):
    if not validate_message(message):
        return
    
    # Пропускаем служебные команды
    if message.text in ["🖼 Сгенерировать изображение", 
                       "✏️ Редактировать изображение",
                       "🔄 Сбросить контекст"]:
        return
    
    user_id = message.from_user.id
    
    if user_id not in user_context:
        user_context[user_id] = []
    
    user_context[user_id].append({"role": "user", "content": message.text})
    
    try:
        success, response = await make_api_request(
            "https://api.deepseek.com/v1/chat/completions",
            method="POST",
            headers={"Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": user_context[user_id],
                "temperature": 0.7
            }
        )
        
        if success:
            answer = response["choices"][0]["message"]["content"]
            user_context[user_id].append({"role": "assistant", "content": answer})
            await message.answer(answer, reply_markup=get_main_kb())
        else:
            await message.answer(f"⚠️ Ошибка API: {response.get('error', 'Unknown error')}")
            
    except Exception as e:
        logger.error(f"Chat error: {str(e)}", exc_info=True)
        await message.answer(f"⚠️ Ошибка: {str(e)}")

# --- Обработчик вебхука ---
async def webhook_handler(request: web.Request):
    try:
        if request.method != 'POST':
            return web.Response(status=405)
        
        if request.content_type != 'application/json':
            return web.Response(status=415)
        
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.Response(status=400)
        
        try:
            update = Update(**data)
            if not validate_message(update.message):
                return web.Response(status=400)
        except Exception:
            return web.Response(status=400)
        
        try:
            await dp.feed_update(bot, update)
            return web.Response()
        except Exception:
            return web.Response(status=500)

    except Exception:
        return web.Response(status=500)

# --- Запуск приложения ---
async def on_startup(app: web.Application):
    try:
        await bot.set_webhook(
            url=f"{BASE_URL}/webhook",
            drop_pending_updates=True,
            allowed_updates=["message"]
        )
        logger.info("Webhook установлен")
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        raise

if __name__ == "__main__":
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    app.on_startup.append(on_startup)
    
    try:
        web.run_app(
            app,
            host="0.0.0.0",
            port=PORT,
            access_log=logger
        )
    except Exception as e:
        logger.error(f"Server error: {str(e)}")
        raise