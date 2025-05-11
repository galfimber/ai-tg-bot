import os
import json
import logging
import tempfile
from typing import Dict, List, Optional, Tuple
from mimetypes import guess_extension

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update
)
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiohttp import web, ClientSession, FormData
from dotenv import load_dotenv

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

# Проверка обязательных переменных
if not TOKEN or not STABILITY_API_KEY:
    raise ValueError("Не заданы обязательные переменные окружения")

# Инициализация бота
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Хранение данных
user_context: Dict[int, List[dict]] = {}
user_edit_state: Dict[int, dict] = {}
http_session: Optional[ClientSession] = None

async def make_api_request(url: str, method: str = "POST", **kwargs) -> Tuple[bool, dict]:
    """Универсальная функция для асинхронных API-запросов"""
    global http_session
    try:
        async with http_session.request(method, url, **kwargs) as response:
            response_text = await response.text()
            
            if response.status != 200:
                logger.error(f"API error {response.status}: {response_text}")
                return False, {
                    "error": f"API error {response.status}",
                    "details": response_text[:500]  # Логируем первые 500 символов
                }
            
            try:
                if response_text:  # Проверка на пустой ответ
                    return True, await response.json()
                return False, {"error": "Empty API response"}
            except json.JSONDecodeError:
                return False, {
                    "error": "Invalid JSON response",
                    "response": response_text[:500]
                }
    except Exception as e:
        logger.error(f"Request failed: {str(e)}", exc_info=True)
        return False, {"error": str(e)}

def validate_message(message: Optional[types.Message]) -> bool:
    """Проверка валидности сообщения"""
    if not message:
        logger.warning("Received None message")
        return False
    if not message.from_user:
        logger.warning("Message without sender")
        return False
    return True

def get_main_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🖼 Сгенерировать изображение"),
        KeyboardButton(text="✏️ Редактировать изображение")
    )
    builder.row(KeyboardButton(text="🔄 Сбросить контекст"))
    return builder.as_markup(resize_keyboard=True)

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
        
        form_data = FormData()
        form_data.add_field('prompt', prompt)
        form_data.add_field('output_format', 'png')
        
        headers = {
            "Authorization": f"Bearer {STABILITY_API_KEY}",
            "Accept": "image/*"  # Явно указываем ожидаемый тип ответа
        }
        
        success, result = await make_api_request(
            "https://api.stability.ai/v2beta/stable-image/generate/sd3",
            method="POST",
            headers=headers,
            data=form_data
        )
        
        if success:
            # Для бинарных ответов (изображений) используем response.read()
            async with http_session.post(
                "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                headers=headers,
                data=form_data
            ) as response:
                if response.status == 200:
                    image_data = await response.read()
                    await message.answer_photo(image_data, caption=f"🎨 {prompt}")
                else:
                    error_text = await response.text()
                    await message.answer(f"❌ Ошибка генерации: {response.status}\n{error_text[:300]}")
        else:
            await message.answer(f"❌ Ошибка API: {result.get('error', 'Unknown error')}")
            
    except Exception as e:
        logger.error(f"Generation error: {str(e)}", exc_info=True)
        await message.answer(f"⚠️ Ошибка: {str(e)}")

@dp.message(F.text)
async def handle_ai_chat(message: Message):
    if not validate_message(message):
        return
    
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
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}" if DEEPSEEK_API_KEY else ""
            },
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

async def on_startup(app: web.Application):
    global http_session
    http_session = ClientSession()
    
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

async def on_shutdown(app: web.Application):
    global http_session
    if http_session:
        await http_session.close()
    await bot.delete_webhook()

if __name__ == "__main__":
    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
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