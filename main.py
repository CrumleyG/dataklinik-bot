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

# ——— 1. Загрузка ENV ———
load_dotenv()
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY")
AIRTABLE_TOKEN      = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID    = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME")  # Должен быть ID таблицы, не ее имя
RENDER_URL          = os.getenv("RENDER_EXTERNAL_URL")
PORT                = int(os.getenv("PORT", 10000))

if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_URL]):
    raise RuntimeError(
        "Нужно задать все ENV: TELEGRAM_TOKEN, OPENAI_API_KEY, "
        "AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_EXTERNAL_URL"
    )

# ——— 2. Клиенты и константы ———
openai = OpenAI(api_key=OPENAI_API_KEY)
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json",
}

# ——— 3. Функция для выдергивания полей из текста ———
def extract_fields(text: str):
    # Имя: «меня зовут Иван», «зовут Мария», «я Ольга»
    m_name = re.search(r'(?:меня зовут|зовут|я)\s*([А-ЯЁ][а-яё]+)', text, re.IGNORECASE)
    # Услуга: «хочу чистку зубов», «на отбеливание»
    m_serv = re.search(r'(?:хочу|на)\s+([А-Яа-яёЁ\s]+?)(?=\s*(?:в|завтра|\d|\.)|$)', text, re.IGNORECASE)
    # Дата+время: «15.05.2025 в 14:00», «завтра в 9:30», «послезавтра в 11:00»
    m_dt = re.search(
        r'(?:(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})|(завтра|послезавтра))\s*(?:в\s*)?(\d{1,2}:\d{2})',
        text, re.IGNORECASE
    )

    name    = m_name.group(1).capitalize() if m_name else None
    service = m_serv.group(1).strip()      if m_serv else None

    date_raw = None
    time_raw = None
    if m_dt:
        if m_dt.group(1):
            date_raw = m_dt.group(1)
        else:
            # «завтра» или «послезавтра»
            days = 2 if m_dt.group(2).lower() == "послезавтра" else 1
            date_raw = (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y")
        time_raw = m_dt.group(4)

    return name, service, date_raw, time_raw

# ——— 4. Основной хендлер ———
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # 4.1. Накопление истории
    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-30:]

    # 4.2. Накопление формы
    form = user_data.get("form", {})
    n, s, d, t = extract_fields(text)
    if n: form["name"]    = n
    if s: form["service"] = s
    if d: form["date"]    = d
    if t: form["time"]    = t
    user_data["form"] = form

    # 4.3. Сбор контекста для GPT
    messages = [
        {
            "role": "system",
            "content": (
                "Вы — помощница стоматологической клиники. Говорите от женского лица, "
                "вежливо и приятно. Ваша задача — записать клиента на услугу: "
                "узнать имя, услугу, дату и время. Если чего-то не хватает — спросите."
            )
        }
    ] + history[-10:]

    # 4.4. Запрос к OpenAI
    try:
        resp  = openai.chat.completions.create(model="gpt-4o", messages=messages)
        reply = resp.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI error:", e)
        await update.message.reply_text("Произошла ошибка при обращении к OpenAI.")
        return

    # 4.5. Ответ пользователю
    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    user_data["history"] = history[-30:]

    # 4.6. Проверка готовности формы и запись в Airtable
    form = user_data["form"]
    print("🔍 Текущая форма:", form)
    if all(k in form for k in ("name", "service", "date", "time")):
        dt_full = f"{form['date']} {form['time']}"
        payload = {
            "fields": {
                "Имя": form["name"],
                "Фамилия": update.effective_user.last_name or "",
                "Username": update.effective_user.username or "",
                "Chat ID": update.effective_user.id,
                "Услуга": form["service"],
                "Дата и время записи": dt_full,
                "Дата и время заявки": datetime.now().isoformat()
            }
        }
        print("▶️ POST Airtable:", AIRTABLE_URL, payload)
        res = requests.post(AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=payload)
        print("📤 Airtable response:", res.status_code, res.text)

        if res.status_code in (200, 201):
            await update.message.reply_text(
                f"✅ Записала вас, {form['name']}, на {form['service']} в {dt_full}. "
                "Спасибо! До встречи."
            )
            user_data.pop("form")
        else:
            await update.message.reply_text("⚠️ Ошибка при записи в таблицу. Проверьте лог.")
    else:
        # Бот сам уточнит недостающее поле в следующем сообщении
        print("⚠️ Недостаточно данных, продолжаем диалог.")

# ——— 5. Запуск через Webhook ———
def main():
    print("🚀 Запуск бота через Webhook…")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    external = RENDER_URL if RENDER_URL.startswith("http") else "https://" + RENDER_URL
    webhook_url = f"{external}/webhook"
    print("🔗 Webhook установлен на:", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
