import os
import re
import json
import gspread
import dateparser
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes,
    MessageHandler, CommandHandler, filters
)
from oauth2client.service_account import ServiceAccountCredentials

# === Загрузка переменных ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
RENDER_URL     = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT           = int(os.getenv("PORT", "10000").strip())
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GROUP_CHAT_ID  = -1002529967465

# === Подключение OpenAI ===
openai = OpenAI(api_key=OPENAI_API_KEY)

# === Подключение Google Sheets через Secret File ===
with open("/etc/secrets/GOOGLE_SHEETS_KEY", "r") as f:
    key_data = json.load(f)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1_w2CVitInb118oRGHgjsufuwsY4ks4H07aoJJMs_W5I/edit").sheet1

# === Услуги клиники ===
SERVICES = {
    "консультация": "Консультация врача — Бесплатно / от 2000 ₸",
    "рентген": "Рентген зуба — от 3000 ₸",
    "чистка": "Чистка зубов (профессиональная гигиена) — от 8000 ₸",
    "отбеливание": "Отбеливание зубов — от 30000 ₸",
    "кариес": "Лечение кариеса — от 10000 ₸",
    "пломба": "Пломба световая — от 12000 ₸",
    "пульпит": "Лечение пульпита — от 18000 ₸",
    "детская": "Детская консультация — от 2000 ₸",
    "фторирование": "Фторирование зубов — от 6000 ₸",
    "коронка": "Коронка металлокерамика — от 35000 ₸",
    "цирконий": "Циркониевая коронка — от 60000 ₸",
    "протез": "Съемный протез — от 45000 ₸",
    "удаление": "Удаление зуба — от 7000 ₸",
    "мудрости": "Удаление зуба мудрости — от 15000 ₸",
    "резекция": "Резекция корня — от 25000 ₸",
    "имплант": "Имплант — от 120000 ₸",
    "брекеты": "Брекеты — от 150000 ₸",
    "элайнеры": "Элайнеры — от 300000 ₸"
}

# === Распознавание данных ===
def extract_fields(text):
    name = re.search(r'(зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)', text)
    phone = re.search(r'(\+?\d{7,15})', text)
    time_ = re.search(r'(\d{1,2}:\d{2})', text)
    date = dateparser.parse(text, settings={"TIMEZONE": "Asia/Almaty", "TO_TIMEZONE": "Asia/Almaty", "RETURN_AS_TIMEZONE_AWARE": False})
    service = next((key for key in SERVICES if key in text.lower()), None)
    return {
        "Имя": name.group(2) if name else None,
        "Телефон": phone.group(1) if phone else None,
        "Время": time_.group(1) if time_ else None,
        "Дата": date.strftime("%d.%m.%Y") if date else None,
        "Услуга": SERVICES[service] if service else None
    }

# === Ответ на /id ===
async def show_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: `{update.message.chat_id}`", parse_mode='Markdown')

# === Ответ на текст ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data
    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-20:]

    form = user_data.get("form", {})
    extracted = extract_fields(text)
    for k, v in extracted.items():
        if v:
            form[k] = v
    user_data["form"] = form

    # Ответ GPT
    messages = [{
        "role": "system",
        "content": "Ты — вежливая помощница стоматологической клиники. Рассказывай про услуги, уточняй недостающие поля (имя, услугу, дату, время, номер)."
    }] + history[-10:]
    try:
        completion = openai.chat.completions.create(model="gpt-4o", messages=messages)
        reply = completion.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI Error:", e)
        return await update.message.reply_text("Ошибка OpenAI")

    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    user_data["history"] = history[-20:]

    # Если форма полная — записываем
    required = ("Имя", "Услуга", "Дата", "Время", "Телефон")
    if all(form.get(k) for k in required):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        row = [form["Имя"], form["Телефон"], form["Услуга"], form["Дата"], form["Время"], now]
        sheet.append_row(row)
        await update.message.reply_text("✅ Вы успешно записаны! Спасибо 😊")
        user_data["form"] = {}

        # Уведомление в чат врачей
        message = (
            f"🆕 Новая запись:\n"
            f"Имя: {row[0]}\n"
            f"Телефон: {row[1]}\n"
            f"Услуга: {row[2]}\n"
            f"Дата: {row[3]} в {row[4]}"
        )
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message)

# === Запуск ===
def main():
    print("🚀 Бот запущен")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("id", show_chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    webhook_url = f"https://{RENDER_URL}/webhook" if not RENDER_URL.startswith("http") else f"{RENDER_URL}/webhook"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
