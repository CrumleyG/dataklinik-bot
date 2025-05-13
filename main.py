import os
import re
import socket
import time
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from openai import OpenAI
from datetime import datetime, timedelta

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN", "").strip()
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "").strip()
AIRTABLE_TOKEN      = os.getenv("AIRTABLE_TOKEN", "").strip()
AIRTABLE_BASE_ID    = os.getenv("AIRTABLE_BASE_ID", "").strip()
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
RENDER_URL          = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT                = int(os.getenv("PORT", "10000").strip())
SERVICES_TABLE_ID   = "tbllp4WUVCDXrCjrP"  # ID таблицы "Услуги"

if not all([TELEGRAM_TOKEN, OPENAI_API_KEY, AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, RENDER_URL]):
    raise RuntimeError("❌ ENV-переменные не заданы")

# Инициализация клиентов
openai = OpenAI(api_key=OPENAI_API_KEY)
HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}

# Синонимы услуг
SERVICE_SYNONYMS = {
    "чистка": "Чистка зубов",
    "осмотр": "Профилактический осмотр",
    "профосмотр": "Профилактический осмотр",
    "лечение": "Терапия",
    "чистка зубов": "Чистка зубов"
}

# Парсинг данных из текста
def extract_fields(text):
    name  = re.search(r'(?:зовут|меня зовут|я)\s*([А-ЯЁ][а-яё]+)', text, re.IGNORECASE)
    serv  = re.search(r'(?:на|хочу)\s+([а-яё\s]+?)(?=\s*(?:в|\d|\.)|$)', text, re.IGNORECASE)
    dt    = re.search(r'(?:(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})|завтра|послезавтра)\s*в\s*(\d{1,2}:\d{2})', text, re.IGNORECASE)
    phone = re.search(r'(\+?\d{7,15})', text)

    name = name.group(1).capitalize() if name else None
    serv_raw = serv.group(1).strip().lower() if serv else None
    serv = SERVICE_SYNONYMS.get(serv_raw, serv_raw) if serv_raw else None

    date = None
    if dt:
        if dt.group(1):
            date = dt.group(1)
        else:
            offset = 1 if "завтра" in dt.group(0).lower() else 2
            date = (datetime.now() + timedelta(days=offset)).strftime("%d.%m.%Y")
        time_ = dt.group(2)
    else:
        time_ = None

    return name, serv, date, time_, phone.group(1) if phone else None

# Поиск ID услуги в Airtable
def find_service_id(service_name):
    print(f"🌐 Ищем услугу в Airtable: {service_name}")
    params = {"filterByFormula": f"{{Название}}='{service_name}'"}
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{SERVICES_TABLE_ID}"
    res = requests.get(url, headers=HEADERS, params=params)
    print("📥 Ответ от Airtable (услуги):", res.status_code, res.text)
    if res.status_code == 200 and res.json().get("records"):
        return res.json()["records"][0]["id"]
    return None

# Хендлер сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_data = context.user_data

    # Тестовая вставка
    if text.strip().lower() == "тест запись":
        context.user_data["form"] = {
            "name": "Тестов",
            "service": "Чистка зубов",  # ← точное название из Airtable
            "date": "15.05.2025",
            "time": "14:00",
            "phone": "+77001112233"
        }
        await update.message.reply_text("🧪 Данные формы вставлены вручную для теста.")
        text = "запиши"

    history = user_data.get("history", [])
    history.append({"role": "user", "content": text})
    user_data["history"] = history[-30:]

    form = user_data.get("form", {})
    name, serv, date, time_, phone = extract_fields(text)
    if name:  form["name"] = name
    if serv:  form["service"] = serv
    if date:  form["date"] = date
    if time_: form["time"] = time_
    if phone: form["phone"] = phone
    user_data["form"] = form

    print("📋 Формуляр:", form)

    try:
        messages = [{"role": "system", "content": "Вы — помощница стоматологии. Записывайте клиента: имя, услуга, дата, время, телефон. Если чего-то не хватает — спросите."}] + history[-10:]
        response = openai.chat.completions.create(model="gpt-4o", messages=messages)
        reply = response.choices[0].message.content
        await update.message.reply_text(reply)
        history.append({"role": "assistant", "content": reply})
    except Exception as e:
        print("❌ GPT Error:", e)
        return await update.message.reply_text("Ошибка OpenAI")

    form = user_data["form"]
    if all(k in form for k in ("name", "service", "date", "time", "phone")):
        print("✅ Все поля есть, ищем ID услуги:", form["service"])
        service_id = find_service_id(form["service"])
        print("🔍 Найденный ID:", service_id)

        if not service_id:
            print("❌ Услуга не найдена в Airtable.")
            return await update.message.reply_text("❌ Услуга не найдена. Проверьте название.")

        payload = {
            "fields": {
                "Клиент": form["name"],
                "Телефон": form["phone"],
                "Дата записи": form["date"],
                "Время": form["time"],
                "Услуга": [service_id],
                "Статус": "Новая"
            }
        }

        airtable_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
        res = requests.post(airtable_url, headers=HEADERS, json=payload)
        print("📤 Airtable:", res.status_code, res.text)

        if res.status_code in (200, 201):
            await update.message.reply_text(f"✅ Вы записаны на {form['service']} {form['date']} в {form['time']}. До встречи!")
            user_data.pop("form")
        else:
            await update.message.reply_text("⚠️ Ошибка при записи в Airtable.")
    else:
        print("⏳ Ожидаем дополнительные данные от клиента.")

# Запуск бота
def main():
    print("🚀 Бот стартует…")

    # Принудительно открываем порт для Render
    sock = socket.socket()
    sock.bind(("0.0.0.0", PORT))
    sock.listen(1)
    sock.close()
    time.sleep(1)

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
        drop_pending_updates=True
    )

if __name__ == "__main__":
    main()
