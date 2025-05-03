import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI

# ——— Загрузка настроек ———
load_dotenv()
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
RENDER_URL        = os.getenv("RENDER_EXTERNAL_URL")
PORT              = int(os.getenv("PORT", 10000))

# ——— Клиенты ———
client = OpenAI(api_key=OPENAI_API_KEY)

# ——— Обработчик с памятью ———
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    app_data  = context.application.user_data
    history   = app_data.setdefault(user_id, {"history": []})["history"]

    user_text = update.message.text
    history.append({"role": "user", "content": user_text})

    # Строим контекст
    messages = [{"role": "system", "content":
                 "Ты ассистент клиники. Помоги записаться: уточни услугу, дату и время."}]
    messages += history

    try:
        resp  = client.chat.completions.create(model="gpt-4o", messages=messages)
        reply = resp.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        # Оставляем в памяти только последние 10 сообщений
        app_data[user_id]["history"] = history[-10:]
        await update.message.reply_text(reply)

    except Exception as e:
        print("❌ Ошибка OpenAI:", e)
        await update.message.reply_text("Ошибка, проверь логи на Render.")

# ——— Точка входа ———
def main():
    print("🚀 Запускаем бот через Webhook…")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Путь и URL вебхука
    webhook_path = "webhook"  # ← без ведущего слэша
    webhook_url  = f"{RENDER_URL}/{webhook_path}"
    print("🔗 Webhook URL:", webhook_url)

    # Встроенный Tornado-сервер из extra-webhooks
    app.run_webhook(
        listen     ="0.0.0.0",
        port       =PORT,
        url_path   =webhook_path,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
