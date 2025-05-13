import os
import json
import logging
import asyncio
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
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
SITE_URL = os.getenv("SITE_URL", "https://your-telegram-bot.com")
SITE_NAME = os.getenv("SITE_NAME", "AI Telegram Bot")

# Проверка обязательных переменных
if not TOKEN or not OPENROUTER_API_KEY:
    raise ValueError("Не заданы обязательные переменные окружения")

# Инициализация бота
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Хранение данных
user_context: Dict[int, Dict] = {}
http_session: Optional[ClientSession] = None

def get_main_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🖼 Сгенерировать изображение"),
        KeyboardButton(text="🔄 Сбросить контекст")
    )
    return builder.as_markup(resize_keyboard=True)

async def generate_image(prompt: str) -> Optional[bytes]:
    """Генерация изображения через Stable Diffusion XL"""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": SITE_NAME
    }
    
    payload = {
        "model": "stabilityai/stable-diffusion-xl-base-1.0",
        "input": {
            "prompt": prompt,
            "negative_prompt": "размыто, низкое качество, артефакты",
            "width": 1024,
            "height": 1024,
            "num_inference_steps": 30
        }
    }
    
    try:
        async with http_session.post(
            "https://openrouter.ai/api/v1/images/generations",
            headers=headers,
            json=payload
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Ошибка генерации: {response.status} - {error_text}")
                return None
                
            data = await response.json()
            
            if "data" in data and data["data"]:
                image_url = data["data"][0]["url"]
                async with http_session.get(image_url) as img_response:
                    if img_response.status == 200:
                        return await img_response.read()
                    logger.error(f"Ошибка загрузки изображения: {img_response.status}")
            return None
    except Exception as e:
        logger.error(f"Ошибка при генерации изображения: {str(e)}")
        return None

async def ask_llama(prompt: str, user_id: int) -> str:
    """Запрос к Llama 4 Mavericks через OpenRouter"""
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": SITE_URL,
        "X-Title": SITE_NAME
    }
    
    # Инициализация контекста
    if user_id not in user_context:
        user_context[user_id] = {"chat_history": []}
    
    # Добавляем новый запрос в историю
    user_context[user_id]["chat_history"].append({"role": "user", "content": prompt})
    
    payload = {
        "model": "meta/llama-4-maverick:free",  # Используем Llama 4 Mavericks
        "messages": user_context[user_id]["chat_history"][-6:],  # Последние 6 сообщений
        "temperature": 0.7
    }
    
    try:
        async with http_session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Ошибка API: {response.status} - {error_text}")
                return f"❌ Ошибка API (код {response.status})"
            
            data = await response.json()
            
            # Обработка ответа
            if "choices" in data and data["choices"]:
                reply = data["choices"][0]["message"]["content"]
                user_context[user_id]["chat_history"].append({"role": "assistant", "content": reply})
                return reply
            
            logger.error(f"Неожиданный формат ответа: {data}")
            return "⚠️ Получен неожиданный формат ответа"
            
    except Exception as e:
        logger.error(f"Ошибка запроса: {str(e)}")
        return f"⚠️ Ошибка при обработке запроса: {str(e)}"

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "✨ <b>AI Бот с функциями:</b>\n"
        "- Генерация изображений через Stable Diffusion\n"
        "- Умный чат на основе Llama 4 Mavericks\n\n"
        "Используйте кнопки ниже:",
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "🔄 Сбросить контекст")
async def reset_context(message: Message):
    user_context.pop(message.from_user.id, None)
    await message.answer("Контекст очищен!", reply_markup=get_main_kb())

@dp.message(F.text == "🖼 Сгенерировать изображение")
async def ask_gen_prompt(message: Message):
    user_id = message.from_user.id
    if user_id not in user_context:
        user_context[user_id] = {}
    user_context[user_id]["awaiting_image_prompt"] = True
    await message.answer("Введите описание изображения:")

@dp.message(F.text)
async def handle_text(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    if text.lower() == "отмена":
        user_context.pop(user_id, None)
        await message.answer("Операция отменена", reply_markup=get_main_kb())
        return
    
    if user_id in user_context and user_context[user_id].get("awaiting_image_prompt"):
        user_context[user_id].pop("awaiting_image_prompt")
        
        if len(text) > 500:
            await message.answer("❌ Слишком длинное описание. Максимум 500 символов.")
            return
            
        await process_image_generation(message, text)
        return
    
    if text not in ["🖼 Сгенерировать изображение", "🔄 Сбросить контекст"]:
        reply = await ask_llama(text, user_id)
        await message.answer(reply, reply_markup=get_main_kb())

async def process_image_generation(message: Message, prompt: str):
    await bot.send_chat_action(message.chat.id, "upload_photo")
    image_data = await generate_image(prompt)
    
    if image_data:
        await message.answer_photo(
            BufferedInputFile(image_data, "generated_image.png"),
            caption=f"🎨 {prompt}"
        )
    else:
        await message.answer("❌ Не удалось сгенерировать изображение. Попробуйте другой запрос.")

async def keep_alive():
    """Периодические запросы для поддержания активности"""
    while True:
        try:
            async with http_session.get(f"{BASE_URL}/healthz") as resp:
                logger.info(f"Keep-alive: {resp.status}")
        except Exception as e:
            logger.error(f"Keep-alive failed: {str(e)}")
        await asyncio.sleep(300)  # Каждые 5 минут

async def health_check(request):
    return web.Response(text="OK")

async def on_startup(app: web.Application):
    global http_session
    http_session = ClientSession()
    
    await bot.set_webhook(
        url=f"{BASE_URL}/webhook",
        drop_pending_updates=True
    )
    asyncio.create_task(keep_alive())
    logger.info("Bot started with keep-alive")

async def on_shutdown(app: web.Application):
    global http_session
    if http_session:
        await http_session.close()
    await bot.delete_webhook()
    logger.info("Bot stopped")

if __name__ == "__main__":
    app = web.Application()
    app.router.add_get("/healthz", health_check)
    
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
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