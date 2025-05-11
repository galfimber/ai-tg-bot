import os
import json
import logging
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update,
    BufferedInputFile
)
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web, ClientSession
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = os.getenv("BASE_URL")
PORT = int(os.getenv("PORT"))
SITE_URL = os.getenv("SITE_URL", "https://ainv-assistant-bot.com")
SITE_NAME = os.getenv("SITE_NAME", "AI Telegram Bot")

# Проверка обязательных переменных
if not TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("Не заданы обязательные переменные окружения")

# Инициализация бота
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Хранение данных
user_context: Dict[int, List[dict]] = {}
http_session: Optional[ClientSession] = None

async def make_api_request(url: str, method: str = "POST", **kwargs) -> Tuple[bool, dict]:
    """Универсальная функция для API-запросов через OpenRouter"""
    global http_session
    try:
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": SITE_URL,
            "X-Title": SITE_NAME,
            **kwargs.pop('headers', {})
        }
        
        async with http_session.request(method, url, headers=headers, **kwargs) as response:
            response_text = await response.text()
            
            if response.status != 200:
                logger.error(f"API error {response.status}: {response_text}")
                return False, {
                    "error": f"API error {response.status}",
                    "details": response_text[:500]
                }
            
            try:
                return True, await response.json()
            except json.JSONDecodeError:
                return False, {
                    "error": "Invalid JSON response",
                    "response": response_text[:500]
                }
    except Exception as e:
        logger.error(f"Request failed: {str(e)}", exc_info=True)
        return False, {"error": str(e)}

def get_main_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🖼 Сгенерировать изображение"),
        KeyboardButton(text="🔄 Сбросить контекст")
    )
    return builder.as_markup(resize_keyboard=True)

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "✨ <b>AI Бот с функциями:</b>\n"
        "- Генерация изображений (Stable Diffusion XL)\n"
        "- Умный чат на основе Gemini Flash\n\n"
        "Используйте кнопки ниже:",
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "🔄 Сбросить контекст")
async def reset_context(message: Message):
    user_context.pop(message.from_user.id, None)
    await message.answer("Контекст очищен!", reply_markup=get_main_kb())

@dp.message(F.text == "🖼 Сгенерировать изображение")
async def ask_gen_prompt(message: Message):
    await message.answer("Введите описание изображения:")

@dp.message(
    F.text,
    F.reply_to_message.func(
        lambda msg: msg and msg.text == "Введите описание изображения:"
    )
)
async def generate_image(message: Message):
    prompt = message.text.strip()
    
    try:
        await message.answer_chat_action("upload_photo")
        
        payload = {
            "model": "stabilityai/stable-diffusion-xl-base-1.0",
            "input": {
                "prompt": prompt,
                "negative_prompt": "размыто, низкое качество",
                "width": 1024,
                "height": 1024
            }
        }
        
        success, result = await make_api_request(
            "https://openrouter.ai/api/v1/images/generations",
            json=payload
        )
        
        if success:
            image_url = result["data"][0]["url"]
            async with http_session.get(image_url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    await message.answer_photo(
                        BufferedInputFile(image_data, "generated.png"),
                        caption=f"🎨 {prompt}"
                    )
                else:
                    await message.answer("❌ Ошибка загрузки изображения")
        else:
            await message.answer(f"❌ Ошибка генерации: {result.get('error', 'Unknown error')}")
            
    except Exception as e:
        logger.error(f"Generation error: {str(e)}", exc_info=True)
        await message.answer(f"⚠️ Ошибка: {str(e)}")

@dp.message(F.text)
async def handle_ai_chat(message: Message):
    if message.text in ["🖼 Сгенерировать изображение", "🔄 Сбросить контекст"]:
        return
    
    user_id = message.from_user.id
    
    if user_id not in user_context:
        user_context[user_id] = []
    
    user_context[user_id].append({"role": "user", "content": message.text})
    
    try:
        payload = {
            "model": "google/gemini-2.0-flash-exp:free",
            "messages": user_context[user_id][-6:],  # Последние 6 сообщений
            "temperature": 0.7
        }
        
        success, response = await make_api_request(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload
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

async def on_startup(app: web.Application):
    global http_session
    http_session = ClientSession()
    
    await bot.set_webhook(
        url=f"{BASE_URL}/webhook",
        drop_pending_updates=True
    )
    logger.info("Webhook установлен")

async def on_shutdown(app: web.Application):
    global http_session
    if http_session:
        await http_session.close()
    await bot.delete_webhook()
    logger.info("Webhook удален")

if __name__ == "__main__":
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
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