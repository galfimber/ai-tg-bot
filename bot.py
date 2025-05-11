import os
import json
import logging
from typing import Dict, List

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiohttp import ClientSession, FormData

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
BASE_URL = os.getenv("BASE_URL")
SITE_NAME = "AI Telegram Bot"

if not all([TOKEN, OPENROUTER_API_KEY]):
    raise ValueError("Не заданы обязательные переменные окружения")

# Инициализация бота
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Хранение контекста
user_context: Dict[int, List[dict]] = {}
http_session: ClientSession = None

# ========== Клавиатура ==========
def get_main_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🖼 Сгенерировать изображение"),
        KeyboardButton(text="🔄 Сбросить контекст")
    )
    return builder.as_markup(resize_keyboard=True)

# ========== Gemini API ==========
async def ask_gemini(prompt: str, user_id: int) -> str:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": BASE_URL,
        "X-Title": SITE_NAME
    }
    
    if user_id not in user_context:
        user_context[user_id] = []
    
    # Добавляем новое сообщение в контекст
    user_context[user_id].append({"role": "user", "content": prompt})
    
    payload = {
        "model": "google/gemini-2.0-flash-exp:free",
        "messages": user_context[user_id][-6:],  # Берем последние 6 сообщений
        "temperature": 0.7
    }
    
    try:
        async with http_session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload
        ) as response:
            data = await response.json()
            
            if response.status == 200:
                reply = data["choices"][0]["message"]["content"]
                # Сохраняем ответ модели в контекст
                user_context[user_id].append({"role": "assistant", "content": reply})
                return reply
            else:
                error_msg = data.get("error", {}).get("message", "Unknown error")
                logger.error(f"Gemini error: {error_msg}")
                return f"❌ Ошибка: {error_msg}"
                
    except Exception as e:
        logger.error(f"Gemini request failed: {str(e)}")
        return "⚠️ Произошла ошибка при обработке запроса"

# ========== Stable Diffusion API ==========
async def generate_image(prompt: str) -> bytes:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": BASE_URL,
        "X-Title": SITE_NAME
    }
    
    payload = {
        "model": "stabilityai/stable-diffusion-xl-base-1.0",
        "input": {
            "prompt": prompt,
            "negative_prompt": "blurry, low quality, text, watermark",
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
            data = await response.json()
            
            if response.status == 200:
                image_url = data["data"][0]["url"]
                # Скачиваем изображение
                async with http_session.get(image_url) as img_response:
                    return await img_response.read()
            else:
                error_msg = data.get("error", {}).get("message", "Unknown error")
                logger.error(f"Image gen error: {error_msg}")
                return None
                
    except Exception as e:
        logger.error(f"Image generation failed: {str(e)}")
        return None

# ========== Обработчики сообщений ==========
@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "✨ <b>AI Бот с функциями:</b>\n"
        "- Умный чат на основе Gemini Flash\n"
        "- Генерация изображений через Stable Diffusion\n\n"
        "Команды:\n"
        "/imagine [описание] - быстрая генерация изображения",
        reply_markup=get_main_kb()
    )

@dp.message(F.text == "🔄 Сбросить контекст")
async def reset_context(message: Message):
    user_context.pop(message.from_user.id, None)
    await message.answer("Контекст очищен!", reply_markup=get_main_kb())

@dp.message(F.text == "🖼 Сгенерировать изображение")
async def ask_gen_prompt(message: Message):
    await message.answer("Введите описание изображения:")

@dp.message(Command("imagine"))
async def quick_generate(message: Message):
    prompt = message.text.split("/imagine", 1)[1].strip()
    if not prompt:
        await message.answer("❌ Укажите описание изображения после команды /imagine")
        return
    
    await process_image_generation(message, prompt)

@dp.message(
    F.text,
    F.reply_to_message.func(
        lambda msg: msg and msg.text == "Введите описание изображения:"
    )
)
async def handle_image_prompt(message: Message):
    await process_image_generation(message, message.text)

async def process_image_generation(message: Message, prompt: str):
    await message.answer_chat_action("upload_photo")
    
    image_data = await generate_image(prompt)
    if image_data:
        await message.answer_photo(
            BufferedInputFile(image_data, filename="generated_image.png"),
            caption=f"🎨 {prompt}"
        )
    else:
        await message.answer("❌ Не удалось сгенерировать изображение")

@dp.message(F.text)
async def handle_text_message(message: Message):
    if message.text in ["🖼 Сгенерировать изображение", "🔄 Сбросить контекст"]:
        return
        
    reply = await ask_gemini(message.text, message.from_user.id)
    await message.answer(reply, reply_markup=get_main_kb())

# ========== Запуск бота ==========
async def on_startup():
    global http_session
    http_session = ClientSession()
    logger.info("Bot started")

async def on_shutdown():
    await http_session.close()
    await bot.session.close()
    logger.info("Bot stopped")

if __name__ == "__main__":
    from aiogram import executor
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )