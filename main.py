import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, filters
)
from openai import OpenAI

# ——————————————
# 1. Загрузка конфигов
# ——————————————
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))
RENDER_URL      = os.getenv("RENDER_EXTERNAL_URL")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Не найдены TELEGRAM_TOKEN или OPENAI_API_KEY в окружении")

# ——————————————
# 2. Клиенты OpenAI и Telegram
# ——————————————
client = OpenAI(api_key=OPENAI_API_KEY)

# ——————————————
# 3. Хендлер с памятью (последние 10 сообщений)
# ——————————————
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": update.message.text})

    messages = [
        {"role": "system", "content": "Ты ассистент стоматологической клиники. Помоги человеку записаться: уточни услугу, дату и время. Будь дружелюбен. Используй навыки дополнительных продаж."}
    ] + history

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = resp.choices[0].message.content

        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-10:]  # храним только последние 10

        await update.message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка в handle_message:", e)
        await update.message.reply_text("Произошла внутренняя ошибка. Проверь логи.")

# ——————————————
# 4. Настройка webhook и HTTP-сервера aiohttp
# ——————————————
async def main():
    print("🚀 Запускаем Telegram-бота через Webhook…")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # URL, по которому Render будет пингануть ваш бот
    if not RENDER_URL:
        raise RuntimeError("RENDER_EXTERNAL_URL не установлен в окружении")

    webhook_path = "/webhook"
    webhook_url  = f"{RENDER_URL}{webhook_path}"

    # Регистрируем webhook у Telegram
    await app.bot.set_webhook(webhook_url)
    print("🔗 Установлен webhook на", webhook_url)

    # Запускаем встроенный сервер aiohttp
    # в python-telegram-bot[webhooks] он автоматически стартует
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path
    )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
