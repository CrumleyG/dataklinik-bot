import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, filters
)
from openai import OpenAI
from aiohttp import web

# Загрузка переменных
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

client = OpenAI(api_key=OPENAI_API_KEY)

# Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = context.application.user_data.setdefault(user_id, {"history": []})

    user_message = update.message.text
    user_data["history"].append({"role": "user", "content": user_message})

    messages = [
        {"role": "system", "content": "Ты ассистент клиники. Помоги человеку записаться: уточни услугу, дату и время. Будь дружелюбен."}
    ] + user_data["history"]

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = response.choices[0].message.content
        user_data["history"].append({"role": "assistant", "content": reply})
        user_data["history"] = user_data["history"][-10:]  # Обрезаем до последних 10 сообщений

        await update.message.reply_text(reply)

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text("Произошла ошибка. Проверь логи на Render.")

# Главная функция
async def main():
    print("🚀 Запускаем Telegram-бота через Webhook (aiohttp)...")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Устанавливаем webhook
    webhook_path = "/webhook"
    webhook_url = f"{RENDER_URL}{webhook_path}"
    await app.bot.set_webhook(webhook_url)
    print(f"🔗 Установлен webhook: {webhook_url}")

    # aiohttp-приложение
    aio_app = web.Application()
    aio_app.router.add_post(webhook_path, app.webhook_handler())

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print("✅ Бот готов к приёму сообщений")

# Запуск через asyncio
if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
