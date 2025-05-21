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

# === Загрузка услуг из внешнего JSON-файла ===
with open("services.json", "r", encoding="utf-8") as f:
    SERVICE_DICT = json.load(f)

# === Распознавание данных ===
def extract_fields(text):
    result = {}
    lower = text.lower()

    # Имя
    m_name = re.search(r'(?:меня зовут|зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)', text)
    if m_name:
        result["Имя"] = m_name.group(1)
        print("✅ Имя:", result["Имя"])

    # Телефон
    m_phone = re.search(r'(\+?\d{7,15})', text)
    if m_phone:
        result["Телефон"] = m_phone.group(1)
        print("✅ Телефон:", result["Телефон"])

    # Время
    m_time = re.search(r'(\d{1,2}[:\.-]\d{2})', text)
    if m_time:
        result["Время"] = m_time.group(1).replace(".", ":").replace("-", ":")
        print("✅ Время:", result["Время"])

    # Дата (с базовой точкой отсчёта)
    parsed_date = dateparser.parse(text, settings={
        "TIMEZONE": "Asia/Almaty",
        "TO_TIMEZONE": "Asia/Almaty",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "RELATIVE_BASE": datetime(2025, 5, 21)
    })
    if parsed_date:
        result["Дата"] = parsed_date.strftime("%d.%m.%Y")
        print("✅ Дата:", result["Дата"])

    # Услуга (по ключевым словам)
    for key, value in SERVICE_DICT.items():
        for synonym in value["ключи"]:
            if synonym in lower:
                result["Услуга"] = value["название"] + " — " + value["цена"]
                print("✅ Услуга:", result["Услуга"])
                return result

    return result

# === Ответ на /id ===
async def show_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: `{update.message.chat_id}`", parse_mode='Markdown')

# === Запись в таблицу и отправка уведомлений ===
def record_submission(form, context):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [form["Имя"], form["Телефон"], form["Услуга"], form["Дата"], form["Время"], now]
    print("📋 Сохраняем строку в Google Sheets:", row)
    sheet.append_row(row)
    message = (
        f"🆕 Новая запись:\n"
        f"Имя: {row[0]}\n"
        f"Телефон: {row[1]}\n"
        f"Услуга: {row[2]}\n"
        f"Дата: {row[3]} в {row[4]}"
    )
    context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message)

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

    print("🔎 Итог формы:", form)

    # Ответ GPT (всегда генерируем)
    messages = [{
        "role": "system",
        "content": "Ты — вежливая помощница стоматологической клиники. Отвечай только по услугам из предоставленного списка. Не выдумывай услуги. Уточняй недостающие поля: имя, услугу, дату, время, номер."
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

    # Повторная проверка после ответа: если всё собрано, записать
    required = ("Имя", "Услуга", "Дата", "Время", "Телефон")
    if all(form.get(k) for k in required):
        print("✅ Все поля найдены, сохраняем в таблицу")
        record_submission(form, context)
        await update.message.reply_text("✅ Вы успешно записаны! Спасибо 😊")
        user_data["form"] = {}
    else:
        print("⚠️ Недостаточно данных. Ожидаем...", form)

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
