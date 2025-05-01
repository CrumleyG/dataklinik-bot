import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_message = update.message.text
        print(f"📩 Получено сообщение: {user_message}")

        completion = client.chat.completions.create(
            model="gpt-3.5-turbo",  # или "gpt-4o", если у тебя точно есть доступ
            messages=[
                {"role": "system", "content": "Ты ассистент клиники. Помоги человеку записаться: уточни услугу, дату и время."},
                {"role": "user", "content": user_message}
            ]
        )

        reply = completion.choices[0].message.content
        await update.message.reply_text(reply)
        print(f"✅ Ответ отправлен: {reply}")

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text("Произошла ошибка. Подробности в логах.")

def main():
    print("🚀 Запускаем Telegram-бота...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Бот работает. Ждём сообщений...")
    app.run_polling()

if __name__ == "__main__":
    main()
