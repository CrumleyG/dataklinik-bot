import os
import re
import json
import gspread
import dateparser
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from oauth2client.service_account import ServiceAccountCredentials

# === Загрузка переменных ===
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "10000").strip())
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
GROUP_CHAT_ID = -1002529967465

# === Подключение OpenAI ===
openai = OpenAI(api_key=OPENAI_API_KEY)

# === Подключение Google Sheets ===
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

# === Функция распознавания полей ===
def extract_fields(text: str) -> dict:
    result = {}
    lower = text.lower()
    print("🔍 Текст для распознавания:", lower)

    # Имя
    m_name = re.search(r'(?:меня зовут|зовут|я)\s+([А-ЯЁA-Z][а-яёa-z]+)', text)
    if m_name:
        result['Имя'] = m_name.group(1)
        print("✅ Имя:", result['Имя'])

    # Телефон
    m_phone = re.search(r'(\+?\d{7,15})', text)
    if m_phone:
        result['Телефон'] = m_phone.group(1)
        print("✅ Телефон:", result['Телефон'])

    # Время
    m_time = re.search(r'(\d{1,2}[:\.-]\d{2})', text)
    if m_time:
        result['Время'] = m_time.group(1).replace('.', ':').replace('-', ':')
        print("✅ Время:", result['Время'])

    # Дата с фиксированной базой для теста
    parsed_date = dateparser.parse(
        text,
        settings={
            'TIMEZONE': 'Asia/Almaty',
            'TO_TIMEZONE': 'Asia/Almaty',
            'RETURN_AS_TIMEZONE_AWARE': False,
            'RELATIVE_BASE': datetime(2025, 5, 21)
        }
    )
    if parsed_date:
        result['Дата'] = parsed_date.strftime("%d.%m.%Y")
        print("✅ Дата:", result['Дата'])

    # Услуга по ключевым словам
    print("🔍 Поиск услуги...")
    for key, val in SERVICE_DICT.items():
        for syn in val['ключи']:
            if syn in lower:
                result['Услуга'] = f"{val['название']} — {val['цена']}"
                print(f"🛠 Найдено совпадение '{syn}' => Услуга: {result['Услуга']}")
                break
        if 'Услуга' in result:
            break
    if 'Услуга' not in result:
        print("⚠️ Услуга не распознана")

    return result

# === Команда для получения Chat ID ===
async def show_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Chat ID: `{update.message.chat_id}`", parse_mode='Markdown'
    )

# === Функция записи в Google Sheets и уведомления врачей ===
def record_submission(form: dict, context):
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [
        form['Имя'], form['Телефон'], form['Услуга'],
        form['Дата'], form['Время'], now
    ]
    print("📋 Сохраняем в Google Sheets:", row)
    try:
        sheet.append_row(row)
        print("✅ Успешно добавлено в таблицу")
    except Exception as e:
        print("❌ Ошибка записи в таблицу:", e)
    msg = (
        "🆕 Новая запись:\n"
        f"Имя: {row[0]}\n"
        f"Телефон: {row[1]}\n"
        f"Услуга: {row[2]}\n"
        f"Дата: {row[3]} в {row[4]}"
    )
    context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg)

# === Обработчик сообщений ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # История для GPT
    history = user_data.get('history', [])
    history.append({'role': 'user', 'content': text})
    user_data['history'] = history[-20:]

    # Формируем форму
    form = user_data.get('form', {})
    extracted = extract_fields(text)
    form.update({k: v for k, v in extracted.items() if v})
    user_data['form'] = form

    print("🔎 Итог формы:", form)

    # Если все поля есть — сразу записать и выйти
    needed = ('Имя', 'Телефон', 'Услуга', 'Дата', 'Время')
    if all(form.get(k) for k in needed):
        record_submission(form, context)
        await update.message.reply_text(
            f"✅ Записала вас, {form['Имя']}! Если появятся вопросы — пишите 😊"
        )
        user_data['form'] = {}
        return

    # Запрашиваем у GPT ответ
    sys_prompt = (
        "Ты — вежливая помощница стоматологической клиники. "
        "Отвечай по услугам из списка, уточняй имя, услугу, дату, время, телефон."
    )
    msgs = [{'role': 'system', 'content': sys_prompt}] + history[-10:]
    try:
        resp = openai.chat.completions.create(model='gpt-4o', messages=msgs)
        reply = resp.choices[0].message.content
    except Exception as e:
        print("❌ OpenAI Error:", e)
        return await update.message.reply_text("Ошибка OpenAI")

    await update.message.reply_text(reply)
    history.append({'role': 'assistant', 'content': reply})
    user_data['history'] = history[-20:]

# === Запуск приложения ===
def main():
    print("🚀 Бот запущен")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('id', show_chat_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    url = RENDER_URL if RENDER_URL.startswith('http') else f"https://{RENDER_URL}"
    webhook = f"{url}/webhook"
    app.run_webhook(
        listen='0.0.0.0', port=PORT,
        url_path='webhook', webhook_url=webhook,
        drop_pending_updates=True
    )

if __name__ == '__main__':
    main()
