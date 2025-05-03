import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

# 1) Загружаем переменные окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
RENDER_URL      = os.getenv("RENDER_EXTERNAL_URL")
PORT            = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN or not OPENAI_API_KEY or not RENDER_URL:
    raise RuntimeError("Не найдены обязательные переменные TELEGRAM_TOKEN, OPENAI_API_KEY или RENDER_EXTERNAL_URL")

# 2) Инициализируем OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# 3) Обработчик входящих текстовых сообщений с памятью переписки
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    # Берём историю из user_data, или создаём новую
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_text})

    # Формируем prompt: системное + диалог
    messages = [
        {"role": "system", "content": (
            "Ты ассистент клиники. Помоги человеку записаться: "
            "уточни услугу, дату и время. Веди себя дружелюбно."
        )}
    ] + history

    try:
        # 4) Вызываем OpenAI
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        reply = resp.choices[0].message.content

        # Сохраняем ответ в историю, держим только последние 10
        history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = history[-10:]

        # Отправляем пользователю
        await update.message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка в OpenAI / Telegram:", e)
        await update.message.reply_text("Произошла внутренняя ошибка, проверь логи.")

# 5) Основная точка входа
def main():
    print("🚀 Запускаем Telegram-бота через Webhook...")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    # Регистрируем только текстовые сообщения (не команды)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Устанавливаем webhook в Telegram на наш URL
    webhook_url = f"{RENDER_URL}/webhook"
    app.bot.set_webhook(webhook_url)
    print(f"🔗 Webhook установлен на {webhook_url}")

    # Запускаем встроенный веб-сервер под python-telegram-bot
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
