import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

# Загружаем .env файл
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Инициализируем OpenAI клиент
client = OpenAI(api_key=OPENAI_API_KEY)

# Обработчик сообщений с историей диалога
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text

    # Получаем историю переписки для пользователя
    history = context.user_data.get("history", [])

    # Добавляем сообщение пользователя в историю
    history.append({"role": "user", "content": user_message})

    # Стартовое сообщение от бота — задаёт стиль общения
    messages = [
        {"role": "system", "content": "Ты ассистент клиники. Помоги человеку записаться: уточни услугу, дату и время. Веди себя вежливо, не пиши сухо."}
    ] + history

    try:
        # Отправляем запрос в OpenAI с полной историей
        completion = client.chat.completions.create(
            model="gpt-4o",  # Или "gpt-3.5-turbo"
            messages=messages
        )

        # Получаем ответ от GPT
        reply = completion.choices[0].message.content

        # Добавляем ответ ассистента в историю
        history.append({"role": "assistant", "content": reply})

        # Сохраняем последние 10 сообщений (оптимизация)
        context.user_data["history"] = history[-10:]

        # Отправляем сообщение в Telegram
        await update.message.reply_text(reply)

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await update.message.reply_text("Произошла ошибка. Проверьте логи.")

# Точка входа — запуск бота
def main():
    print("🚀 Запускаем Telegram-бота...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Обрабатываем любые текстовые сообщения (не команды)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Бот работает. Ждём сообщений...")
    app.run_polling()

if __name__ == "__main__":
    main()
