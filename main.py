import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

# Обработчик входящих сообщений с памятью
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    history = context.user_data.get("history", [])

    # Добавляем сообщение пользователя в историю
    history.append({"role": "user", "content": user_message})

    # Формируем запрос с системной ролью + историей
    messages = [{"role": "system", "content": "Ты ассистент клиники. Помоги человеку записаться: уточни услугу, дату и время. Веди себя дружелюбно."}] + history

    try:
        completion = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = completion.choices[0].message.content

        # Добавляем ответ бота в память и сохраняем только последние 10 сообщений
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-10:]

        await update.message.reply_text(reply)

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text("Произошла ошибка. Проверь логи.")

# Запуск через webhook
def main():
    print("🚀 Запускаем Telegram-бота через Webhook...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    render_url = os.getenv("RENDER_EXTERNAL_URL")  # Render сам подставит это значение
    if not render_url:
        raise Exception("RENDER_EXTERNAL_URL не установлен")

    webhook_url = f"{render_url}/webhook"
    print(f"🔗 Webhook URL: {webhook_url}")

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
