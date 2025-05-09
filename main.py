import os
import re
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)
from openai import OpenAI
from datetime import datetime, timedelta

# 1. Загрузка переменных окружения
load_dotenv()

TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
AIRTABLE_TOKEN       = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID     = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME  = os.getenv("AIRTABLE_TABLE_NAME")
RENDER_URL           = os.getenv("RENDER_EXTERNAL_URL")
PORT                 = int(os.getenv("PORT", 10000))

# 2. Проверка, что всё передано
if not all([
    TELEGRAM_TOKEN,
    OPENAI_API_KEY,
    AIRTABLE_TOKEN,
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_NAME,
    RENDER_URL,
]):
    raise RuntimeError(
        "Нужно задать все ENV:\n"
        "TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN,\n"
        "AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_EXTERNAL_URL"
    )

# 3. Инициализация клиентов
openai = OpenAI(api_key=OPENAI_API_KEY)
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

# 4. Функция для извлечения полей из текста
def extract_fields(text: str):
    # имя: «меня зовут Иван», «я Иван», «Иван»
    m_name = re.search(r'(?:меня зовут|зовут|я)\s*([А-ЯЁ][а-яё]+)', text, re.IGNORECASE)
    # услуга: «на чистку зубов», «хочу чистку зубов»
    m_serv = re.search(r'(?:на|хочу)\s+([а-яё\s]+?)(?:\s+в\s+\d|\s+завтра|\.$)', text, re.IGNORECASE)
    # дата+время: «15.05.2025 в 14:00», «послезавтра в 9:30»
    m_dt   = re.search(
        r'(?:(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})|(?:завтра|послезавтра))'
        r'(?:\s*в\s*)(\d{1,2}:\d{2})',
        text, re.IGNORECASE
    )
    name    = m_name.group(1).capitalize() if m_name else None
    service = m_serv.group(1).strip()      if m_serv else None
    date    = m_dt.group(1)               if m_dt and m_dt.group(1) else None
    time    = m_dt.group(2)               if m_dt else None
    # обрабатываем «завтра»/«послезавтра»
    if not date and m_dt and "завтра" in m_dt.group(0).lower():
        days = 1 if "послезавтра" not in m_dt.group(0).lower() else 2
        date = (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y")
    return name, service, date, time

# 5. Основной обработчик
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": user_text})

    messages = [
        {
            "role": "system",
            "content": (
                "Вы — помощница стоматологической клиники (женский стиль). "
                "Ведите диалог вежливо и эффективно. Ваша задача — записать клиента "
                "на услугу. Узнайте имя, услугу, дату и время. Подтвердите и сохраните запись."
            )
        }
    ] + history[-10:]

    # запрос к OpenAI
    try:
        resp  = openai.chat.completions.create(model="gpt-4o", messages=messages)
        reply = resp.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI error:", e)
        await update.message.reply_text("Произошла ошибка при общении с OpenAI.")
        return

    # отправляем ответ и сохраняем историю
    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    context.user_data["history"] = history[-30:]

    # извлекаем поля
    name, service, date_str, time_str = extract_fields(user_text + " " + reply)
    print(f"🔍 Извлечено: name={name}, service={service}, date={date_str}, time={time_str}")

    # если всё есть — пишем в Airtable
    if all([name, service, date_str, time_str]):
        dt_full = f"{date_str} {time_str}"
        payload = {
            "fields": {
                "Имя": name,
                "Фамилия": update.effective_user.last_name or "",
                "Username": update.effective_user.username or "",
                "Chat ID": update.effective_user.id,
                "Услуга": service,
                "Дата и время записи": dt_full,
                "Сообщение": user_text,
                "Дата и время заявки": datetime.now().isoformat()
            }
        }
        print("▶️ POST URL:", AIRTABLE_URL)
        print("▶️ PAYLOAD:", payload)
        try:
            res = requests.post(AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=payload)
            print("📤 Airtable status:", res.status_code, res.text)
            if res.status_code in (200, 201):
                await update.message.reply_text(f"✅ Записала: {name}, {service}, {dt_full}.")
            else:
                await update.message.reply_text("⚠️ Ошибка при записи в таблицу. Проверьте логи.")
        except Exception as e:
            print("❌ Airtable request error:", e)
            await update.message.reply_text("❌ Не удалось связаться с Airtable.")

# 6. Настройка и запуск
def main():
    print("🚀 Запуск Telegram-бота…")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    external = RENDER_URL if RENDER_URL.startswith("http") else "https://" + RENDER_URL
    webhook_url = f"{external}/webhook"
    print("🔗 Устанавливаем webhook:", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
