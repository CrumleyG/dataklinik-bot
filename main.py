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

# --- Настройки окружения и ключи ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "10000").strip())
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DOCTORS_GROUP_ID = -1002529967465  # твоя группа

# --- OpenAI ---
openai = OpenAI(api_key=OPENAI_API_KEY)

# --- Google Sheets ---
with open("/etc/secrets/GOOGLE_SHEETS_KEY", "r") as f:
    key_data = json.load(f)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_dict(key_data, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1_w2CVitInb118oRGHgjsufuwsY4ks4H07aoJJMs_W5I/edit").sheet1

# --- Услуги клиники ---
with open("services.json", "r", encoding="utf-8") as f:
    SERVICES_DICT = json.load(f)
    SERVICES = list(SERVICES_DICT.values())

# --- Ключевые слова и шаблоны ---
BOOKING_KEYWORDS = [
    "запис", "хочу на", "на прием", "на приём", "appointment", "приём",
    "на консультацию", "запишите", "хочу записаться", "хочу попасть", "могу ли я записаться",
    "хотел бы записаться", "запиши меня", "запишись", "готов записаться"
]
CONSULT_WORDS = [
    "услуг", "прайс", "стоимость", "цены", "сколько стоит", "какие есть", "перечень", "что делаете", "прайслист"
]
CONFIRM_WORDS = [
    "всё верно", "все верно", "да", "ок", "подтверждаю", "спасибо",
    "подтвердить", "верно", "готово", "вы записаны", "запись подтверждена", "ваша запись подтверждена"
]

def is_booking_intent(text):
    q = text.lower()
    return any(kw in q for kw in BOOKING_KEYWORDS)

def is_consult_intent(text):
    q = text.lower()
    return any(w in q for w in CONSULT_WORDS)

def is_confirm_intent(text):
    q = text.lower()
    return any(w in q for w in CONFIRM_WORDS)

def match_service(text):
    q = text.lower()
    # Числовой выбор услуги (например, "1" или "2")
    m = re.match(r"^\s*(\d+)\s*$", text)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx]['название']
    # Поиск по названию или ключам
    for s in SERVICES:
        if s['название'].lower() in q:
            return s['название']
        for kw in s.get('ключи', []):
            if kw.lower() in q:
                return s['название']
    return None

def build_services_list():
    result = ["📋 *Список услуг нашей клиники:*"]
    for i, s in enumerate(SERVICES, 1):
        result.append(f"{i}. *{s['название']}* ({s['цена']})")
    return "\n".join(result)

def extract_fields(text):
    # Имя
    name = None
    m = re.search(r"(зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)", text)
    if m:
        name = m.group(2)
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

    # 1. Если спрашивают справку об услугах — только справка
    if is_consult_intent(text):
        await update.message.reply_text(build_services_list(), parse_mode="Markdown")
        return

    # 2. Собираем данные из сообщения
    form = user_data.get("form", {})
    extracted = extract_fields(text)
    for k, v in extracted.items():
        if v:
            form[k] = v
    user_data["form"] = form

    # 3. Если человек только выбирает услугу, но написал несуществующую — даём правильный список!
    if not form.get("Услуга") and is_booking_intent(text):
        await update.message.reply_text("Пожалуйста, выберите услугу по номеру или названию из списка:\n" + build_services_list(), parse_mode="Markdown")
        return

    # 4. Если все поля собраны — автоматом записываем и очищаем форму
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

    # 5. Если не хватает полей — AI помогает дособрать без зацикливаний
    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-20:]

    ai_prompt = (
        "Ты — вежливая помощница стоматологической клиники, всегда в контексте записи. "
        "Твоя задача — аккуратно узнать имя, услугу (только из списка услуг клиники!), дату, время, телефон. "
        "Если человек написал не все данные, задай только нужный вопрос, не повторяя уже собранные данные. "
        "Если просят выбрать услугу — предложи только услуги из этого списка:\n"
        + build_services_list() +
        "\nНе дублируй вопросы. Если человек выбрал номер, преобразуй его в название услуги из списка."
    )

    messages = [{"role": "system", "content": ai_prompt}] + history[-10:]

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
