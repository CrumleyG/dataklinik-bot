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

# 1) Загрузка .env (если ты пушишь .env.example, а настоящие ключи держишь в Dashboard)
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не задан TELEGRAM_TOKEN или OPENAI_API_KEY")

# 2) Инициализируем OpenAI-клиент
client = OpenAI(api_key=OPENAI_API_KEY)

# 3) Обработчик — сохраняем историю в context.user_data["history"]
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": update.message.text})

    messages = [
        {"role": "system", "content": (
            "Ты ассистент клиники. Помоги человеку записаться: "
            "уточни услугу, дату и время. Веди себя дружелюбно."
        )}
    ] + history

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = resp.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        # храним только последние 10
        context.user_data["history"] = history[-10:]
        await update.message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка в handle_message:", e)
        await update.message.reply_text("Произошла внутренняя ошибка, проверь логи.")

# 4) Запуск Webhook-сервера (синхронно, без asyncio.run)
def main():
    print("🚀 Запускаем Telegram-бота через Webhook…")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    webhook_url = f"{RENDER_URL}/webhook"
    print("🔗 Устанавливаем webhook:", webhook_url)
    app.bot.set_webhook(webhook_url)

    # сюда Render направит все POST /webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
    )

if __name__ == "__main__":
    main()
