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
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Настройки окружения и ключи ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "10000").strip())
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
DOCTORS_GROUP_ID = -1002529967465

openai = OpenAI(api_key=OPENAI_API_KEY)

# --- Google Sheets ---
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

# --- Услуги и слоты ---
with open("services.json", "r", encoding="utf-8") as f:
    SERVICES_DICT = json.load(f)
SERVICES = list(SERVICES_DICT.values())

CANCEL_KEYWORDS = ["отменить", "отмена", "удалить", "поменять время"]
BOOKING_KEYWORDS = [
    "запис", "приём", "appointment", "запишите", "хочу записаться", "можно записаться"
]
CONSULT_WORDS = ["стоимость", "цена", "прайс", "услуги", "какие есть", "сколько стоит"]

def is_cancel_intent(text):
    q = text.lower()
    return any(kw in q for kw in CANCEL_KEYWORDS)

def is_booking_intent(text):
    q = text.lower()
    return any(kw in q for kw in BOOKING_KEYWORDS)

def is_consult_intent(text):
    q = text.lower()
    return any(w in q for w in CONSULT_WORDS)

def match_service(text):
    q = text.lower()
    # По номеру из списка (строго!)
    m = re.match(r"\b(\d{1,2})\b", q)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx]["название"]
    # По ключевым словам — точное совпадение только!
    for key, s in SERVICES_DICT.items():
        if s["название"].lower() == q.strip():
            return s["название"]
        for kw in s.get("ключи", []):
            if kw.lower() == q.strip():
                return s["название"]
    # Частичное совпадение только если нет других совпадений
    for key, s in SERVICES_DICT.items():
        if s["название"].lower() in q:
            return s["название"]
        for kw in s.get("ключи", []):
            if kw.lower() in q:
                return s["название"]
    return None

def build_services_list():
    lines = ["📋 *Список услуг:*"]
    for i, s in enumerate(SERVICES, 1):
        lines.append(f"{i}. *{s['название']}* — {s['цена']}")
    return "\n".join(lines)

def extract_fields(text):
    data = {}
    # Имя — только если четко указано!
    m = re.search(r"(?:меня зовут|имя)\s*[:,\-]?[\s]*([А-ЯЁA-Z][а-яёa-zA-Z]+)", text, re.I)
    if m:
        data["Имя"] = m.group(1).capitalize()
    # Телефон
    m = re.search(r"(\+7\d{10}|8\d{10}|7\d{10}|\d{10,11})", text.replace(" ", ""))
    if m:
        phone = m.group(1)
        if phone.startswith("8"):
            phone = "+7" + phone[1:]
        elif phone.startswith("7") and len(phone) == 11:
            phone = "+7" + phone[1:]
        data["Телефон"] = phone
    # Услуга — только если четко указано!
    svc = match_service(text)
    if svc:
        data["Услуга"] = svc
    # Дата
    date_keywords = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    m = re.search(r"(сегодня|завтра|послезавтра|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)", text.lower())
    if m:
        d = m.group(1)
        now = datetime.now()
        if d in date_keywords:
            data["Дата"] = (now + timedelta(days=date_keywords[d])).strftime("%d.%m.%Y")
        else:
            for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m", "%d/%m", "%d-%m"):
                try:
                    date_obj = datetime.strptime(d, fmt)
                    if date_obj.year < 2000:
                        date_obj = date_obj.replace(year=now.year)
                    data["Дата"] = date_obj.strftime("%d.%m.%Y")
                    break
                except:
                    continue
            else:
                data["Дата"] = d
    # Время
    m = re.search(r"(\d{1,2})[:.\-](\d{2})", text)
    if m:
        h, m_ = int(m.group(1)), m.group(2)
        if 0 <= h <= 23 and 0 <= int(m_) <= 59:
            data["Время"] = f"{h:02d}:{m_}"
    return data

def is_form_complete(form):
    return all(form.get(k) for k in ("Имя", "Телефон", "Услуга", "Дата", "Время"))

def is_valid_name(name):
    bad = {"здравствуйте", "добрый", "доброго", "привет", "hello", "hi", "админ", "пациент", "клиент"}
    if not name or name.lower() in bad or len(name) > 50:
        return False
    parts = name.strip().split()
    # Имя или имя+фамилия, только буквы, каждая часть с большой буквы
    if 1 <= len(parts) <= 2 and all(p[0].isupper() and p.isalpha() for p in parts):
        return True
    return False

def get_taken_slots(услуга, дата):
    records = sheet.get_all_records()
    taken = []
    for rec in records:
        usluga_cell = str(rec.get("Услуга", "")).strip().lower()
        date_cell = str(rec.get("Дата", "")).strip()
        if usluga_cell == услуга.strip().lower() and date_cell == дата.strip():
            time_cell = str(rec.get("Время", "")).strip()
            taken.append(time_cell)
    return taken

async def register_and_notify(form, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [
        form["Имя"],
        form["Телефон"],
        form["Услуга"],
        form["Дата"],
        form["Время"],
        now_ts,
        chat_id
    ]
    sheet.append_row(row)
    msg = (
        f"🦷 *Новая запись!*\n"
        f"Имя: {form['Имя']}\n"
        f"Телефон: {form['Телефон']}\n"
        f"Услуга: {form['Услуга']}\n"
        f"Дата: {form['Дата']}\n"
        f"Время: {form['Время']}"
    )
    await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
    await update.message.reply_text("✅ Запись подтверждена! Спасибо, ждём вас!")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    # 1. Собираем историю и форму
    history = context.user_data.get("history", [])
    history.append({"role": "user", "content": text})
    context.user_data["history"] = history[-20:]
    form = context.user_data.get("form", {})

    # 2. Консультация
    if is_consult_intent(text):
        await update.message.reply_text(build_services_list(), parse_mode="Markdown")
        return

    # 3. Явно собираем по шагам: имя -> телефон -> услуга -> дата -> время
    fields_order = ["Имя", "Телефон", "Услуга", "Дата", "Время"]
    prompts = {
        "Имя": "Пожалуйста, напишите, как к вам обращаться (имя и фамилия, если можно).",
        "Телефон": "Укажите, пожалуйста, ваш контактный номер телефона для подтверждения записи.",
        "Услуга": "На какую услугу вы хотите записаться? (Можно выбрать из списка или описать коротко.)",
        "Дата": "На какую дату хотите записаться? (например: завтра, 30.05.25 и т.д.)",
        "Время": "Пожалуйста, выберите удобное время приёма из доступных слотов (ответьте номером или временем)."
    }
    extracted = extract_fields(text)
    for k, v in extracted.items():
        if v and (not form.get(k) or form.get(k).lower() != v.lower()):
            form[k] = v
    context.user_data["form"] = form

    # 4. Найди первое отсутствующее поле
    for field in fields_order:
        if not form.get(field):
            # Если услуга уже выбрана, покажи список слотов для времени
            if field == "Время" and form.get("Услуга") and form.get("Дата"):
                svc = form["Услуга"]
                slots = []
                for key, s in SERVICES_DICT.items():
                    if s["название"].strip().lower() == svc.strip().lower():
                        slots = s.get("слоты", [])
                        break
                taken_slots = get_taken_slots(svc, form["Дата"])
                free_slots = [t for t in slots if t not in taken_slots]
                if free_slots:
                    slot_texts = [f"{i+1}. {t}" for i, t in enumerate(free_slots)]
                    await update.message.reply_text(
                        "Свободные слоты на выбранную дату:\n" +
                        "\n".join(slot_texts) +
                        "\nНапишите номер или время (например: 2 или 12:00)."
                    )
                    context.user_data["awaiting_time"] = {"slots": free_slots}
                else:
                    await update.message.reply_text("На эту дату нет свободных слотов. Попробуйте другую дату или услугу.")
                return
            # Если услуга — покажи список
            if field == "Услуга":
                await update.message.reply_text(build_services_list(), parse_mode="Markdown")
            await update.message.reply_text(prompts[field])
            return

    # 5. Если ожидаем время — запишем его в форму
    if context.user_data.get("awaiting_time"):
        slots = context.user_data["awaiting_time"]["slots"]
        value = text.strip()
        slot_num = None
        if re.fullmatch(r"\d+", value):
            slot_num = int(value) - 1
            if 0 <= slot_num < len(slots):
                form["Время"] = slots[slot_num]
        else:
            for t in slots:
                if value in t:
                    form["Время"] = t
        context.user_data["form"] = form
        if form.get("Время"):
            del context.user_data["awaiting_time"]
        else:
            await update.message.reply_text("Пожалуйста, выберите время из списка (номер или время).")
            return

    # 6. Финальная проверка имени и запись
    if is_form_complete(form):
        # --- Проверка имени ---
        if not is_valid_name(form["Имя"]):
            await update.message.reply_text(
                "Пожалуйста, укажите настоящее имя (например: Иван Иванов). Это важно для записи!"
            )
            form["Имя"] = ""
            context.user_data["form"] = form
            return
        svc = form["Услуга"]
        slots = []
        for key, s in SERVICES_DICT.items():
            if s["название"].strip().lower() == svc.strip().lower():
                slots = s.get("слоты", [])
                break
        if slots and form["Время"] not in slots:
            await update.message.reply_text(
                f"Время {form['Время']} недоступно для услуги {svc}. Пожалуйста, выберите только из доступных слотов."
            )
            form["Время"] = ""
            context.user_data["form"] = form
            return
        taken_slots = get_taken_slots(form["Услуга"], form["Дата"])
        if form["Время"] in taken_slots:
            await update.message.reply_text("Этот слот уже занят. Выберите другое время.")
            form["Время"] = ""
            context.user_data["form"] = form
            return
        await register_and_notify(form, update, context)
        context.user_data["form"] = {}
        return

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def start_scheduler(_: ContextTypes.DEFAULT_TYPE):
        pass  # reminders можно подключить как у тебя

    app.post_init = start_scheduler

    RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if RENDER_URL.startswith("https://"):
        RENDER_URL = RENDER_URL.replace("https://", "")
    if RENDER_URL.startswith("http://"):
        RENDER_URL = RENDER_URL.replace("http://", "")
    if RENDER_URL.endswith("/"):
        RENDER_URL = RENDER_URL.rstrip("/")
    webhook = f"https://{RENDER_URL}/webhook"

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=webhook,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
