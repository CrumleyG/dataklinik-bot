import os
import re
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
from datetime import datetime

# Загрузка переменных окружения
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_URL]):
    raise RuntimeError("Нужно задать все ENV: TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_EXTERNAL_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

# Извлечение полей из текста
def extract_fields(text):
    name_match = re.search(r"(?:меня зовут|я|имя)\s*([А-Яа-яЁё]+)", text, re.IGNORECASE)
    service_match = re.search(r"(?:на|хочу|записаться на)\s+([а-яА-ЯёЁ\s]+?)(?:\s+на\s+|\s+в\s+|\s+|$)", text)
    datetime_match = re.search(r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}).*?(?:в|в\s)?\s*(\d{1,2}:\d{2})", text)

    name = name_match.group(1) if name_match else None
    service = service_match.group(1).strip() if service_match else None
    date_str = datetime_match.group(1) if datetime_match else None
    time_str = datetime_match.group(2) if datetime_match else None

    return name, service, date_str, time_str

# Обработка сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_data = context.user_data

    history = user_data.get("history", [])
    history.append({"role": "user", "content": user_input})

    messages = [{
        "role": "system",
        "content": (
            "Ты — ассистент стоматологической клиники. Говори от женского лица, веди себя вежливо и уверенно. "
            "Помоги человеку записаться: уточни услугу, имя, дату и время. Люди могут писать в свободной форме."
        )
    }] + history[-30:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        user_data["history"] = history[-30:]

        await update.message.reply_text(reply)

        # Извлечение данных
        name, service, date_str, time_str = extract_fields(user_input + reply)
        print(f"🔍 Извлечено: name={name}, service={service}, date={date_str}, time={time_str}")

        # Пишем всё в Airtable, даже если не все поля есть
        dt_full = f"{date_str or '—'} {time_str or '—'}"
        data = {
            "fields": {
                "Имя": name or "—",
                "Фамилия": update.effective_user.last_name or "",
                "Username": update.effective_user.username or "",
                "Chat ID": update.effective_user.id,
                "Услуга": service or "—",
                "Дата и время записи": dt_full,
                "Сообщение": user_input,
                "Дата и время заявки": datetime.now().isoformat()
            }
        }

        airtable_response = requests.post(AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=data)
        print("📤 Airtable response:", airtable_response.status_code, airtable_response.text)

        if airtable_response.status_code in [200, 201]:
            await update.message.reply_text("📝 Клиент успешно записан.")
        else:
            await update.message.reply_text("⚠️ Ошибка при записи в таблицу. Проверь логи.")

    except Exception as e:
        print("❌ Ошибка:", e)
        await update.message.reply_text("Произошла ошибка. Попробуйте ещё раз позже.")

# Запуск бота
def main():
    print("🚀 Запуск Telegram-бота через Webhook…")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if not RENDER_URL.startswith("http"):
        external = "https://" + RENDER_URL
    else:
        external = RENDER_URL

    url_path = "webhook"
    webhook_url = f"{external}/{url_path}"
    print("🔗 Устанавливаем webhook:", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
