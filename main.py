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

DOCTORS_GROUP_ID = -1002529967465

openai = OpenAI(api_key=OPENAI_API_KEY)

with open("/etc/secrets/GOOGLE_SHEETS_KEY", "r") as f:
    key_data = json.load(f)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1_w2CVitInb118oRGHgjsufuwsY4ks4H07aoJJMs_W5I/edit").sheet1

with open("services.json", "r", encoding="utf-8") as f:
    SERVICES = json.load(f)

BOOKING_KEYWORDS = [
    "запис", "хочу на", "на прием", "на приём", "appointment", "приём",
    "на консультацию", "запишите", "хочу записаться", "хочу попасть", "могу ли я записаться",
    "хотел бы записаться", "запиши меня", "запишись", "готов записаться"
]
CONFIRM_WORDS = ["всё верно", "все верно", "да", "ок", "подтверждаю", "спасибо", "подтвердить", "верно", "готово"]

def is_booking_intent(text):
    q = text.lower()
    return any(kw in q for kw in BOOKING_KEYWORDS)

def is_confirm_intent(text):
    q = text.lower()
    return any(w in q for w in CONFIRM_WORDS)

def match_service(text):
    q = text.lower()
    # Поиск по названию услуги
    for key, data in SERVICES.items():
        if data['название'].lower() in q:
            return data['название']
        # Поиск по ключам
        for kw in data.get('ключи', []):
            if kw.lower() in q:
                return data['название']
    return None

def get_service_info(query, for_booking=False):
    q = query.lower()
    if for_booking:
        return None
    # Если спрашивают полный список
    if any(word in q for word in [
        "услуг", "прайс", "стоимость", "цены", "сколько стоит", "какие есть", "перечень", "что делаете", "прайслист"
    ]):
        result = ["📋 *Список услуг нашей клиники:*"]
        for data in SERVICES.values():
            line = f"— *{data['название']}* ({data['цена']})"
            result.append(line)
        return "\n".join(result)
    # Если про одну услугу (но не про запись)
    for key, data in SERVICES.items():
        if data['название'].lower() in q:
            return f"*{data['название']}*\nЦена: {data['цена']}"
        for kw in data.get('ключи', []):
            if kw.lower() in q:
                return f"*{data['название']}*\nЦена: {data['цена']}"
    return None

def extract_fields(text):
    # Имя
    name = None
    match = re.search(r"(зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)", text)
    if match:
        name = match.group(2)
    # Услуга
    service = match_service(text)
    # Дата
    date = None
    date_match = re.search(r'(завтра|послезавтра|\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})', text)
    if date_match:
        d = date_match.group(1)
        if "завтра" in d:
            date = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
        elif "послезавтра" in d:
            date = (datetime.now() + timedelta(days=2)).strftime("%d.%m.%Y")
        else:
            date = d
    # Время
    time_ = None
    time_match = re.search(r'\b(\d{1,2}[:.]\d{2})\b', text)
    if time_match:
        time_ = time_match.group(1).replace('.', ':')
    # Телефон
    phone = None
    phone_match = re.search(r'(\+?\d{7,15})', text)
    if phone_match:
        phone = phone_match.group(1)
    return {
        "Имя": name,
        "Услуга": service,
        "Дата": date,
        "Время": time_,
        "Телефон": phone,
    }

def is_form_complete(form):
    required = ("Имя", "Услуга", "Дата", "Время", "Телефон")
    return all(form.get(k) for k in required)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # 1. Если идёт запись, НЕ показываем справку по услуге
    booking_intent = is_booking_intent(text)
    service_reply = get_service_info(text, for_booking=booking_intent)
    if service_reply:
        await update.message.reply_text(service_reply, parse_mode="Markdown")
        return

    # 2. Всегда обновляем форму
    form = user_data.get("form", {})
    extracted = extract_fields(text)
    for k, v in extracted.items():
        if v:
            form[k] = v
    user_data["form"] = form

    # 3. Если форма заполнена — сразу пишем в таблицу и врачам!
    if is_form_complete(form):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        row = [form["Имя"], form["Телефон"], form["Услуга"], form["Дата"], form["Время"], now]
        sheet.append_row(row)
        doctors_msg = (
            f"🦷 *Новая запись пациента!*\n"
            f"Имя: {form['Имя']}\n"
            f"Телефон: {form['Телефон']}\n"
            f"Услуга: {form['Услуга']}\n"
            f"Дата: {form['Дата']}\n"
            f"Время: {form['Время']}"
        )
        await context.bot.send_message(
            chat_id=DOCTORS_GROUP_ID,
            text=doctors_msg,
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ Вы успешно записаны! Спасибо 😊")
        user_data["form"] = {}
        return

    # 4. Если форма НЕ заполнена, но человек пишет "всё верно" или "подтверждаю" — тоже пробуем записать (на случай, если поля не уловились с первого раза)
    if is_confirm_intent(text) and is_form_complete(form):
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        row = [form["Имя"], form["Телефон"], form["Услуга"], form["Дата"], form["Время"], now]
        sheet.append_row(row)
        doctors_msg = (
            f"🦷 *Новая запись пациента!*\n"
            f"Имя: {form['Имя']}\n"
            f"Телефон: {form['Телефон']}\n"
            f"Услуга: {form['Услуга']}\n"
            f"Дата: {form['Дата']}\n"
            f"Время: {form['Время']}"
        )
        await context.bot.send_message(
            chat_id=DOCTORS_GROUP_ID,
            text=doctors_msg,
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ Ваша запись подтверждена и сохранена! Спасибо 😊")
        user_data["form"] = {}
        return

    # 5. Ведём дальше диалог (OpenAI)
    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-20:]

    messages = [
        {
            "role": "system",
            "content": "Ты — вежливая помощница стоматологической клиники. "
                       "Если пользователь говорит 'записать', 'запишись', 'я хочу на услугу', 'записаться', 'на приём', 'на консультацию', то не отвечай справкой по услуге, а веди диалог записи. "
                       "Тебе нужно последовательно узнать имя, услугу (название из списка клиники), дату, время, телефон."
                       "Если услуга называется по-разному — спрашивай только из списка клиники (services.json)."
                       "Не дублируй вопросы, если пользователь уже всё написал."
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
