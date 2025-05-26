import os
import re
import json
import gspread
from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from oauth2client.service_account import ServiceAccountCredentials

# Загрузка .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
RENDER_URL     = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT           = int(os.getenv("PORT", "10000").strip())
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# ID группы врачей (замени на свой ID)
DOCTORS_GROUP_ID = -1002529967465

# OpenAI
openai = OpenAI(api_key=OPENAI_API_KEY)

# Загрузка ключа Google
with open("/etc/secrets/GOOGLE_SHEETS_KEY", "r") as f:
    key_data = json.load(f)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1_w2CVitInb118oRGHgjsufuwsY4ks4H07aoJJMs_W5I/edit").sheet1

# Загрузка услуг из services.json
with open("services.json", "r", encoding="utf-8") as f:
    SERVICES = json.load(f)

def get_service_info(query):
    """Отвечает на вопросы по услугам на основе services.json"""
    q = query.lower()

    # Если спрашивают полный список или цену/прайс
    if any(word in q for word in [
        "услуг", "прайс", "стоимость", "цены", "сколько стоит", "какие есть", "перечень", "что делаете", "прайслист"
    ]):
        result = ["📋 *Список услуг нашей клиники:*"]
        for key, data in SERVICES.items():
            line = f"— *{data['название']}* ({data['цена']})"
            result.append(line)
        return "\n".join(result)
    
    # Поиск конкретной услуги по ключам или названию
    for key, data in SERVICES.items():
        # Проверка по названию
        if data['название'].lower() in q:
            text = f"*{data['название']}*\nЦена: {data['цена']}"
            return text
        # Проверка по ключам
        for kw in data.get('ключи', []):
            if kw.lower() in q:
                text = f"*{data['название']}*\nЦена: {data['цена']}"
                return text
    return None

# Функция извлечения полей
def extract_fields(text):
    name = re.search(r'(зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)', text)
    serv = re.search(r'(на|хочу)\s+([а-яёa-z\s]+?)(?=\s*(в|\d{1,2}[.:]))', text, re.IGNORECASE)
    date = re.search(r'(завтра|послезавтра|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})', text)
    time_ = re.search(r'\b(\d{1,2}:\d{2})\b', text)
    phone = re.search(r'(\+?\d{7,15})', text)

    date_str = None
    if date:
        d = date.group(1)
        if "завтра" in d:
            date_str = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
        elif "послезавтра" in d:
            date_str = (datetime.now() + timedelta(days=2)).strftime("%d.%m.%Y")
        else:
            date_str = d

    return {
        "Имя": name.group(2) if name else None,
        "Услуга": serv.group(2).strip().capitalize() if serv else None,
        "Дата": date_str,
        "Время": time_.group(1) if time_ else None,
        "Телефон": phone.group(1) if phone else None,
    }

# Хендлер сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # ---- Консультация по услугам ----
    service_reply = get_service_info(text)
    if service_reply:
        await update.message.reply_text(service_reply, parse_mode="Markdown")
        return

    # ---- Ведение истории для OpenAI ----
    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-20:]

    form = user_data.get("form", {})
    extracted = extract_fields(text)
    for k, v in extracted.items():
        if v:
            form[k] = v
    user_data["form"] = form

    # GPT: для естественного диалога
    messages = [
        {
            "role": "system",
            "content": "Ты — вежливая помощница стоматологической клиники. "
                       "Уточни, если чего-то не хватает: имя, услугу, дату, время и номер телефона."
        }
    ] + history[-10:]

    try:
        completion = openai.chat.completions.create(model="gpt-4o", messages=messages)
        reply = completion.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI Error:", e)
        return await update.message.reply_text("Ошибка при ответе от AI 😔")

    await update.message.reply_text(reply)
    history.append({"role": "assistant", "content": reply})
    user_data["history"] = history[-20:]

    # Если форма полная — пишем в таблицу и шлём врачам
    required = ("Имя", "Услуга", "Дата", "Время", "Телефон")
    if all(form.get(k) for k in required):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        row = [form["Имя"], form["Телефон"], form["Услуга"], form["Дата"], form["Время"], now]
        sheet.append_row(row)

        # Шаблон для группы врачей
        doctors_msg = (
            f"🦷 *Новая запись пациента!*\n"
            f"Имя: {form['Имя']}\n"
            f"Телефон: {form['Телефон']}\n"
            f"Услуга: {form['Услуга']}\n"
            f"Дата: {form['Дата']}\n"
            f"Время: {form['Время']}"
        )
        # Отправка в группу врачей
        await context.bot.send_message(
            chat_id=DOCTORS_GROUP_ID,
            text=doctors_msg,
            parse_mode="Markdown"
        )

        await update.message.reply_text("✅ Вы успешно записаны! Спасибо 😊")
        user_data["form"] = {}

# Запуск
def main():
    print("🚀 Бот запущен")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
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
