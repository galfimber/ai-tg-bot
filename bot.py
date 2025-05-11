import os
import tempfile
import argparse
from typing import Dict, List
from mimetypes import guess_extension

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
import requests
from aiohttp import web
from dotenv import load_dotenv

# Загрузка конфигурации
load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
STABILITY_API_KEY = os.getenv("STABILITY_API_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = os.getenv("BASE_URL")

# Инициализация бота
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# Хранение данных
user_context: Dict[int, List[dict]] = {}
user_edit_state: Dict[int, dict] = {}

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
    await message.answer(
        "✨ <b>AI Бот с функциями:</b>\n"
        "- Умный чат, отвечу на ваши вопросы, сгенерирую текст, проанализирую документ\n"
        "- Генерация изображений\n"
        "- Редактирование изображения",
        reply_markup=get_main_kb()
    )

# --- Установка вебхука ---
async def on_startup(bot: Bot):
    await bot.set_webhook(
        url=f"{BASE_URL}/webhook",
        drop_pending_updates=True
    )
    print(f"Webhook установлен на {BASE_URL}/webhook")

# --- Текстовые сообщения ---
@dp.message(F.text == "🔄 Сбросить контекст")
async def reset_context(message: Message):
    user_context.pop(message.from_user.id, None)
    await message.answer("Контекст очищен!", reply_markup=get_main_kb())

@dp.message(F.text == "🖼 Сгенерировать изображение")
async def ask_gen_prompt(message: Message):
    await message.answer("Введите запрос для генерации:")

@dp.message(F.text == "✏️ Редактировать изображение")
async def ask_edit_photo(message: Message):
    user_edit_state[message.from_user.id] = {}
    await message.answer("Загрузите изображение:")

# --- Генерация изображений ---
@dp.message(F.text & F.reply_to_message.func(lambda msg: msg.text == "Введите запрос для генерации:"))
async def generate_image(message: Message):
    prompt = message.text.strip()
    
    try:
        await message.answer_chat_action("upload_photo")
        
        resp = requests.post(
            "https://api.stability.ai/v2beta/stable-image/generate/sd3",
            headers={"Authorization": f"Bearer {STABILITY_API_KEY}"},
            files={"none": ""},
            data={"prompt": prompt, "output_format": "png"},
            timeout=60
        )
        
        if resp.status_code == 200:
            await message.answer_photo(resp.content, caption=f"🎨 {prompt}")
        else:
            error = resp.json().get("message", "Unknown error")
            await message.answer(f"❌ Ошибка генерации: {error}")
            
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {str(e)}")

# --- Загрузка изображений ---
@dp.message(F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT}))
async def handle_image_upload(message: Message):
    user_id = message.from_user.id
    
    if user_id not in user_edit_state:
        return
    
    try:
        # Для документов проверяем MIME-тип
        if message.document:
            if not message.document.mime_type.startswith("image/"):
                await message.answer("Отправьте только JPEG/PNG!")
                return
            
            file = await bot.get_file(message.document.file_id)
            ext = guess_extension(message.document.mime_type) or ".jpg"
        else:  # Для фото
            file = await bot.get_file(message.photo[-1].file_id)
            ext = ".jpg"
        
        # Сохраняем во временный файл
        downloaded = await bot.download_file(file.file_path)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(downloaded.read())
            user_edit_state[user_id]["image_path"] = tmp.name
        
        await message.answer("Теперь опишите изменения (например: 'Добавь мяч в углу', 'Убери фон' или 'Сделай стиль киберпанк'):")
        
    except Exception as e:
        await message.answer(f"⚠️ Ошибка загрузки: {str(e)}")
        user_edit_state.pop(user_id, None)

# --- Редактирование изображений ---
@dp.message(F.text & F.from_user.id.in_(user_edit_state.keys()))
async def process_image_edit(message: Message):
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
        await message.answer(f"⚠️ Ошибка API: {str(e)}")
    finally:
        # Очистка
        if image_path and os.path.exists(image_path):
            os.unlink(image_path)
        user_edit_state.pop(user_id, None)

# --- Текстовый чат ---
@dp.message(F.text & ~F.text.in_([
    "🖼 Сгенерировать изображение", 
    "✏️ Редактировать изображение",
    "🔄 Сбросить контекст"
]))
async def handle_ai_chat(message: Message):
    user_id = message.from_user.id
    
    if user_id not in user_context:
        user_context[user_id] = []
    
    user_context[user_id].append({"role": "user", "content": message.text})
    
    try:
        response = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": user_context[user_id],
                "temperature": 0.7
            },
            timeout=30
        ).json()
        
        answer = response["choices"][0]["message"]["content"]
        user_context[user_id].append({"role": "assistant", "content": answer})
        await message.answer(answer, reply_markup=get_main_kb())
        
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {str(e)}")

# --- Запуск приложения ---
if __name__ == "__main__":
    # Регистрация startup-функции (без передачи base_url)
    dp.startup.register(on_startup)

    # Настройка aiohttp-сервера
    app = web.Application()
    SimpleRequestHandler(dp, bot=bot).register(app, path="/webhook")

    # Запуск
    web.run_app(app, host="0.0.0.0", port=10000)