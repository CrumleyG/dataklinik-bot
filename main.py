import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI
import requests
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

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Airtable URL
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

# Основной обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": update.message.text})

    messages = [
        {
            "role": "system",
            "content": (
                "Ты ассистент клиники. Веди диалог вежливо и эффективно. "
                "Твоя задача — записать пользователя на услугу. "
                "Уточни имя, услугу, дату и время. Когда всё будет — подтверди запись и передай данные в Airtable."
            ),
        }
    ] + history[-10:]

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = resp.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-30:]

        await update.message.reply_text(reply)

        # Простая проверка для записи в Airtable
        if "записать" in reply.lower() and any(x in reply.lower() for x in ["услуга", "дата", "время"]):
            data = {
                "fields": {
                    "Имя": update.effective_user.first_name,
                    "Фамилия": update.effective_user.last_name or "",
                    "Username": update.effective_user.username or "",
                    "Chat ID": update.effective_user.id,
                    "Сообщение": update.message.text,
                    "Дата и время заявки": datetime.now().isoformat()
                }
            }
            requests.post(AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=data)

    except Exception as e:
        print("❌ Ошибка в handle_message:", e)
        await update.message.reply_text("Произошла ошибка. Попробуйте позже.")

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
