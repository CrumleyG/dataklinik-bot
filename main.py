import os
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI

# Загрузка переменных
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")

client = OpenAI(api_key=OPENAI_API_KEY)

# Функция добавления записи в Airtable
def add_to_airtable(user_id, question, answer):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "fields": {
            "User ID": str(user_id),
            "Question": question,
            "Answer": answer
        }
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code != 200:
        print(f"❌ Ошибка записи в Airtable: {response.text}")

# Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_message})

    messages = [{"role": "system", "content": "Ты ассистент клиники. Помоги человеку записаться: уточни услугу, дату и время. Будь дружелюбен."}] + history

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-20:]

        # запись в Airtable
        add_to_airtable(user_id, user_message, reply)

        await update.message.reply_text(reply)

    except Exception as e:
        print(f"❌ Ошибка в handle_message: {e}")
        await update.message.reply_text("Произошла внутренняя ошибка. Проверь логи.")

# Webhook запуск
def main():
    print("🚀 Запускаем Telegram-бота через Webhook…")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL не установлен")

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
