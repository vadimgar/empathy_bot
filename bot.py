import os
import tempfile
import logging
import mimetypes
import base64
from dotenv import load_dotenv
from io import BytesIO
from PIL import Image
import fitz  # PyMuPDF
from openai import AsyncOpenAI
import aiohttp
import asyncio
import subprocess
from datetime import datetime, timedelta
from dateutil import parser as date_parser
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile, Voice, Document, PhotoSize
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.utils.markdown import hbold

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

reminders = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "👋 Привет! Я — Empathy Copilot.\n\n"
        "Можешь отправить:\n"
        "📄 текст — я помогу найти ответ\n"
        "🎙 голос — отвечу голосом (и пришлю текстом)\n"
        "📎 PDF или 🖼️ изображение — извлеку и объясню содержание\n\n"
        "🔍 Добавь в текст или голосовое слова 'поиск', 'новости' или 'последняя информация' — найду данные в интернете.\n"
        "⏰ Скажи или напиши 'Напомни...' — установлю напоминание.\n"
        "💎 Работаю c GPT и Perplexity без VPN."
    )

async def respond(message: Message, answer: str, prefer_voice: bool = False):
    if prefer_voice:
        response = await openai_client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=answer
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as temp:
            temp.write(await response.aread())
            temp.flush()
            audio = FSInputFile(temp.name)
            await message.answer_voice(audio, caption="🔊 Ответ голосом")
            os.remove(temp.name)
        await message.answer(answer)
    else:
        await message.answer(answer)

def extract_reminder(text: str):
    lowered = text.lower()
    if lowered.startswith("напомни") or lowered.startswith("напомню"):
        try:
            parsed_dt = date_parser.parse(text, fuzzy=True)
            clean_text = text.replace("напомни", "").replace("напомню", "").strip()
            return parsed_dt, clean_text
        except Exception:
            return None, None
    return None, None

async def check_reminders():
    while True:
        now = datetime.now().replace(second=0, microsecond=0)
        for user_id, reminders_list in list(reminders.items()):
            for reminder in reminders_list[:]:
                if reminder['time'] <= now:
                    try:
                        await bot.send_message(user_id, f"⏰ Напоминание: {reminder['text']}")
                        reminders[user_id].remove(reminder)
                    except Exception as e:
                        logger.error(f"Ошибка при отправке напоминания: {e}")
        await asyncio.sleep(60)

@dp.message(F.text)
async def text_handler(message: Message):
    user_text = message.text.strip()
    use_voice = "ответ голосом" in user_text.lower()

    dt, reminder_text = extract_reminder(user_text)
    if dt:
        user_id = message.from_user.id
        reminders.setdefault(user_id, []).append({"time": dt, "text": reminder_text})
        logger.info(f"⏰ Добавлено напоминание для {user_id}: {reminder_text} в {dt}")
        await message.answer(f"✅ Напоминание установлено: {reminder_text}")
        return

    trigger_words = ["поиск", "новости", "последняя информация"]
    if any(word in user_text.lower() for word in trigger_words):
        headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        json_data = {
            "model": "sonar",
            "messages": [{"role": "user", "content": user_text}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.perplexity.ai/chat/completions", headers=headers, json=json_data) as resp:
                data = await resp.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "Не удалось получить ответ.").strip()
        await respond(message, answer, use_voice)
    else:
        completion = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": user_text}]
        )
        answer = completion.choices[0].message.content.strip()
        await respond(message, answer, use_voice)

@dp.message(F.voice)
async def voice_handler(message: Message):
    file = await bot.get_file(message.voice.file_id)
    voice_file = await bot.download_file(file.file_path)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_ogg:
        temp_ogg.write(voice_file.read())
        temp_ogg_path = temp_ogg.name

    temp_wav = temp_ogg_path.replace(".ogg", ".wav")
    subprocess.call(["ffmpeg", "-i", temp_ogg_path, temp_wav])

    with open(temp_wav, "rb") as f:
        transcript = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text"
        )

    os.remove(temp_ogg_path)
    os.remove(temp_wav)

    user_text = transcript.strip()
    dt, reminder_text = extract_reminder(user_text)
    if dt:
        user_id = message.from_user.id
        reminders.setdefault(user_id, []).append({"time": dt, "text": reminder_text})
        logger.info(f"⏰ Добавлено напоминание для {user_id}: {reminder_text} в {dt}")
        await message.answer(f"✅ Напоминание установлено: {reminder_text}")
        return

    use_text_reply = "ответ текстом" in user_text.lower()
    trigger_words = ["поиск", "новости", "последняя информация"]
    if any(word in user_text.lower() for word in trigger_words):
        headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        json_data = {
            "model": "sonar",
            "messages": [{"role": "user", "content": user_text}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.perplexity.ai/chat/completions", headers=headers, json=json_data) as resp:
                data = await resp.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "Не удалось получить ответ.").strip()
    else:
        completion = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": user_text}]
        )
        answer = completion.choices[0].message.content.strip()

    await respond(message, answer, prefer_voice=not use_text_reply)

@dp.message(F.document)
async def document_handler(message: Message):
    if not message.document.file_name.lower().endswith(".pdf"):
        await message.answer("Пожалуйста, отправь PDF-документ.")
        return

    file = await bot.get_file(message.document.file_id)
    pdf_bytes = await bot.download_file(file.file_path)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
        temp_pdf.write(pdf_bytes.read())
        temp_pdf_path = temp_pdf.name

    doc = fitz.open(temp_pdf_path)
    full_text = "\n".join(page.get_text() for page in doc)
    doc.close()
    os.remove(temp_pdf_path)

    prompt = f"Сделай краткое саммари этого PDF-файла:\n\n{full_text[:4000]}"
    completion = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    summary = completion.choices[0].message.content.strip()
    await message.answer(summary)

@dp.message(F.photo)
async def image_handler(message: Message):
    photo: PhotoSize = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    image_data = await bot.download_file(file.file_path)

    base64_image = base64.b64encode(image_data.read()).decode("utf-8")
    image_url = f"data:image/png;base64,{base64_image}"

    completion = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "Что изображено на картинке?"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": image_url}}]}
        ]
    )

    answer = completion.choices[0].message.content.strip()
    await message.answer(answer)

if __name__ == "__main__":
    async def main():
        asyncio.create_task(check_reminders())
        await dp.start_polling(bot)

    asyncio.run(main())
