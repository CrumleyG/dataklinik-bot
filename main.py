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

# 1. Загрузка переменных из .env (или из Render ENV)
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не заданы TELEGRAM_TOKEN или OPENAI_API_KEY")

# 2. Инициализация OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# 3. Обработчик сообщений с памятью последних 10 запросов
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": update.message.text})

    messages = [
        {
            "role": "system",
            "content": (
                "Ты ассистент клиники. Помоги человеку записаться: "
                "уточни услугу, дату и время. Веди себя дружелюбно."
            ),
        }
    ] + history

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = resp.choices[0].message.content

        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-10:]

        await update.message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка в handle_message:", e)
        await update.message.reply_text("Произошла внутренняя ошибка, смотрите логи.")

# 4. Запуск webhook-сервера
def main():
    print("🚀 Запускаем Telegram-бота через Webhook…")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL не установлен")

    # если RENDER_URL пришёл без протокола — дополним
    if not RENDER_URL.startswith("http"):
        external = "https://" + RENDER_URL
    else:
        external = RENDER_URL

    # path и полный URL webhook-а
    url_path = "webhook"
    webhook_url = f"{external}/{url_path}"
    print("🔗 Устанавливаем webhook:", webhook_url)

    # Запускаем сервер, Telegram сам вызовет этот путь
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
