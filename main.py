# main.py
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

# 1. Загрузка ENV и strip()
load_dotenv()
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "").strip()
AIRTABLE_TOKEN      = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID    = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
RENDER_URL          = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT                = int(os.getenv("PORT", "10000").strip())

if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_URL]):
    raise RuntimeError(
        "Нужно задать все ENV: TELEGRAM_TOKEN, OPENAI_API_KEY, "
        "AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_EXTERNAL_URL"
    )

# 2. Клиенты
openai = OpenAI(api_key=OPENAI_API_KEY)
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json"
}

# 3. Извлечение полей (имя, услуга, дата, время, телефон)
def extract_fields(text: str):
    m_name = re.search(r'(?:меня зовут|зовут|я)\s*([А-ЯЁ][а-яё]+)', text, re.IGNORECASE)
    m_serv = re.search(r'(?:на процедуру|на|хочу)\s+([а-яё\s]+?)(?=\s*(?:в|завтра|\d|\.)|$)', text, re.IGNORECASE)
    m_dt   = re.search(
        r'(?:(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})|(?:завтра|послезавтра))'
        r'(?:\s*в\s*)(\d{1,2}:\d{2})',
        text, re.IGNORECASE
    )
    m_phone = re.search(r'(\+?\d{7,15})', text)

    name    = m_name.group(1).capitalize() if m_name else None
    service = m_serv.group(1).strip()      if m_serv else None

    date_raw = None
    time_raw = None
    if m_dt:
        if m_dt.group(1):
            date_raw = m_dt.group(1)
        else:
            days = 2 if "послезавтра" in m_dt.group(0).lower() else 1
            date_raw = (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y")
        time_raw = m_dt.group(2)

    phone = m_phone.group(1) if m_phone else None

    return name, service, date_raw, time_raw, phone

# 4. Хендлер
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # история
    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-30:]

    # форма
    form = user_data.get("form", {})
    n, s, d, t, p = extract_fields(text)
    if n: form["name"]     = n
    if s: form["service"]  = s
    if d: form["date"]     = d
    if t: form["time"]     = t
    if p: form["phone"]    = p
    user_data["form"] = form

    # GPT-запрос
    messages = [
        {
            "role": "system",
            "content": (
                "Вы — помощница стоматологической клиники. Говорите от женского лица, "
                "вежливо и понятно. Ваша задача — записать клиента: узнать имя, услугу, дату, время и телефон. "
                "Если каких-то данных не хватает — спросите."
            )
        }
    ] + history[-10:]

    try:
        resp  = openai.chat.completions.create(model="gpt-4o", messages=messages)
        reply = resp.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI error:", e)
        await update.message.reply_text("Произошла ошибка при обращении к OpenAI.")
        return

    # отвечаем
    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    user_data["history"] = history[-30:]

    # если форма полная — пишем в Airtable
    form = user_data["form"]
    print("🔍 Текущая форма:", form)
    required = ("name", "service", "date", "time", "phone")
    if all(k in form for k in required):
        dt_full = f"{form['date']} {form['time']}"
        payload = {
            "fields": {
                "Дата записи":        form["date"],
                "Время":             form["time"],
                "Услуга":            form["service"],
                "Клиент":            form["name"],
                "Телефон":           form["phone"],
                "Статус":            "Новая",
            }
        }
        print("▶️ POST Airtable:", AIRTABLE_URL, payload)
        res = requests.post(AIRTABLE_URL, headers=AIRTABLE_HEADERS, json=payload)
        print("📤 Airtable response:", res.status_code, res.text)
        if res.status_code in (200, 201):
            await update.message.reply_text(
                f"✅ Записала вас, {form['name']}, на {form['service']} "
                f"в {dt_full}. Спасибо! До встречи."
            )
            user_data.pop("form")
        else:
            await update.message.reply_text("⚠️ Ошибка при записи в таблицу. Проверьте лог.")
    else:
        print("⚠️ Недостаточно данных, ждём клиента.")

# 5. Запуск
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
        drop_pending_updates=True,  # один раз
    )

if __name__ == "__main__":
    main()
