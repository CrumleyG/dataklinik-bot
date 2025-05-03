import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

# Загружаем переменные окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Инициализируем OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Обработчик сообщений с памятью
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        context.user_data["history"] = history[-10:]  # сохраняем последние 10 сообщений
        await update.message.reply_text(reply)

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text("Произошла ошибка. Проверь логи на Render.")

# Запуск бота через Webhook
def main():
    print("🚀 Запускаем Telegram-бота через Webhook...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url:
        raise Exception("RENDER_EXTERNAL_URL не установлен")

    webhook_url = f"{render_url}/webhook"
    print(f"🔗 Webhook URL: {webhook_url}")

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        webhook_url=webhook_url,
        path="/webhook"  # ← ДОБАВЬ ЭТУ СТРОКУ
    )

if __name__ == "__main__":
    main()
