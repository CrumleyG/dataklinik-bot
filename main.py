import os
import re
import json
import gspread
import dateparser
from datetime import datetime
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
GROUP_CHAT_ID  = int(os.getenv("GROUP_CHAT_ID", "-1002529967465"))

# === Подключение OpenAI ===
openai = OpenAI(api_key=OPENAI_API_KEY)

# === Подключение Google Sheets через Secret File ===
with open("/etc/secrets/GOOGLE_SHEETS_KEY", "r") as f:
    key_data = json.load(f)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url(
    "https://docs.google.com/spreadsheets/d/1_w2CVitInb118oRGHgjsufuwsY4ks4H07aoJJMs_W5I/edit"
).sheet1

# === Загрузка списка услуг ===
with open("services.json", "r", encoding="utf-8") as f:
    SERVICE_DICT = json.load(f)

# === Парсинг полей из текста ===
def extract_fields(text):
    result = {}
    lower = text.lower()

    # Имя
    m = re.search(r'(?:меня зовут|зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)|^([А-ЯЁA-Z][а-яёa-z]+)\b', text)
    if m: 
       result["Имя"] = m.group(1) or m.group(2)


    # Телефон
    m = re.search(r'(\+?\d{7,15})', text)
    if m:
        result["Телефон"] = m.group(1)

    # Время
    m = re.search(r'(\d{1,2}[:\.-]\d{2})', text)
    if m:
        result["Время"] = m.group(1).replace(".", ":").replace("-", ":")

    # Дата с базой 21.05.2025
    dt = dateparser.parse(
        text,
        settings={
            "TIMEZONE": "Asia/Almaty",
            "TO_TIMEZONE": "Asia/Almaty",
            "RETURN_AS_TIMEZONE_AWARE": False,
            "RELATIVE_BASE": datetime(2025, 5, 21)
        }
    )
    if dt:
        result["Дата"] = dt.strftime("%d.%m.%Y")

    # Услуга по ключам
    for srv_key, srv in SERVICE_DICT.items():
        for synonym in srv["ключи"]:
            if synonym in lower:
                result["Услуга"] = f"{srv['название']} — {srv['цена']}"
                return result  # выходим сразу после первого совпадения

    return result

# === Команда /id для отладки ===
async def show_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Chat ID: `{update.message.chat_id}`", parse_mode='Markdown')

# === Запись в Google Sheets и уведомление в группу ===
def record_submission(form, context):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [form["Имя"], form["Телефон"], form["Услуга"], form["Дата"], form["Время"], now]
    sheet.append_row(row)
    msg = (
        "🆕 *Новая запись*\n"
        f"👤 {form['Имя']}\n"
        f"📞 {form['Телефон']}\n"
        f"🦷 {form['Услуга']}\n"
        f"📅 {form['Дата']} в {form['Время']}"
    )
    context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg, parse_mode='Markdown')

# === Обработка любого текста ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # Обновляем историю сообщений
    hist = user_data.get("history", [])
    hist.append({"role": "user", "content": text})
    user_data["history"] = hist[-20:]

    # Собираем поля
    form = user_data.get("form", {})
    found = extract_fields(text)
    form.update(found)
    user_data["form"] = form

    print("🔎 Текущий form:", form)

    # Если все есть — записываем
    needed = ["Имя", "Телефон", "Услуга", "Дата", "Время"]
    if all(form.get(k) for k in needed):
        record_submission(form, context)
        await update.message.reply_text(
            f"✅ Записала вас, {form['Имя']}! До встречи 😊"
        )
        user_data["form"] = {}
        return

    # Иначе спрашиваем недостающее через GPT
    sys = {
        "role": "system",
        "content": (
            "Ты — вежливая помощница стоматологической клиники. "
            "Уточняй недостающие данные: имя, услугу из списка, дату, время и телефон."
        )
    }
    msgs = [sys] + hist[-10:]
    try:
        resp = openai.chat.completions.create(model="gpt-4o", messages=msgs)
        reply = resp.choices[0].message.content
    except Exception as e:
        print("OpenAI Error:", e)
        return await update.message.reply_text("❌ Ошибка при обращении к OpenAI.")

    await update.message.reply_text(reply)
    hist.append({"role": "assistant", "content": reply})
    user_data["history"] = hist[-20:]

# === Запуск приложения ===
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("id", show_chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    url = RENDER_URL if RENDER_URL.startswith("http") else f"https://{RENDER_URL}"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=f"{url}/webhook",
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
