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
    m = re.match(r"\b(\d{1,2})\b", q)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(SERVICES):
            return SERVICES[idx]["название"]
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

# --- Четкие функции вытаскивания полей ---
def extract_name(text):
    m = re.search(r"(?:меня зовут|имя)\s*[:,\-]?[\s]*([А-ЯЁA-Z][а-яёa-zA-Z]+)", text, re.I)
    if m:
        return m.group(1).capitalize()
    m = re.match(r"^\s*я\s+([А-ЯЁA-Z][а-яёa-zA-Z]+)\b", text, re.I)
    if m:
        return m.group(1).capitalize()
    m = re.match(r"^\s*([А-ЯЁA-Z][а-яёa-zA-Z]+)\s*$", text, re.I)
    if m:
        return m.group(1).capitalize()
    return None

def extract_phone(text):
    m = re.search(r"(\+7\d{10}|8\d{10}|7\d{10}|\d{10,11})", text.replace(" ", ""))
    if m:
        phone = m.group(1)
        if phone.startswith("8"):
            phone = "+7" + phone[1:]
        elif phone.startswith("7") and len(phone) == 11:
            phone = "+7" + phone[1:]
        return phone
    return None

def extract_date(text):
    date_keywords = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    m = re.search(r"(сегодня|завтра|послезавтра|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?)", text.lower())
    now = datetime.now()
    if m:
        d = m.group(1)
        if d in date_keywords:
            return (now + timedelta(days=date_keywords[d])).strftime("%d.%m.%Y")
        for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m", "%d/%m", "%d-%m"):
            try:
                date_obj = datetime.strptime(d, fmt)
                if date_obj.year < 2000:
                    date_obj = date_obj.replace(year=now.year)
                return date_obj.strftime("%d.%m.%Y")
            except:
                continue
        return d
    return None

def extract_time(text):
    m = re.search(r"(\d{1,2})[:.\-](\d{2})", text)
    if m:
        h, m_ = int(m.group(1)), m.group(2)
        if 0 <= h <= 23 and 0 <= int(m_) <= 59:
            return f"{h:02d}:{m_}"
    return None

def get_service_object(service_name):
    for s in SERVICES:
        if s["название"].strip().lower() == service_name.strip().lower():
            return s
    return None

def is_form_complete(form):
    return all(form.get(k) for k in ("Имя", "Телефон", "Услуга", "Дата", "Время"))

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

def find_last_booking(chat_id):
    records = sheet.get_all_records()
    # Список возможных названий столбца для Chat ID
    id_keys = ["Chat ID", "chat_id", "Chat Id", "chatid", "id"]
    last = None
    for idx, rec in enumerate(records, start=2):
        rec_chat_id = None
        for key in id_keys:
            if key in rec:
                rec_chat_id = rec[key]
                break
        if rec_chat_id is not None and str(rec_chat_id).strip() == str(chat_id).strip():
            last = (idx, rec)
    return last if last else (None, None)

async def register_and_notify(form, update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    now_ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    row = [
        form["Имя"],
        form["Телефон"],
        form["Услуга"],
        form["Дата"],
        form["Время"],
        chat_id,
        now_ts
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
    # Сброс состояния после успешной записи
    context.user_data.clear()

async def handle_cancel_or_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.effective_chat.id
    row_idx, rec = find_last_booking(chat_id)
    if not rec:
        await update.message.reply_text("❗ У вас нет активных записей.")
        return
    if "отменить" in text or "удалить" in text:
        sheet.delete_row(row_idx)
        msg = (
            f"❌ Пациент отменил запись:\n"
            f"{rec['Имя']}, {rec['Услуга']} на {rec['Дата']} {rec['Время']}"
        )
        await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
        await update.message.reply_text("✅ Ваша запись отменена.")
        return
    svc = rec["Услуга"]
    date = rec["Дата"]
    slots = []
    for key, s in SERVICES_DICT.items():
        if s["название"].strip().lower() == svc.strip().lower():
            slots = s.get("слоты", [])
            break
    if not slots:
        await update.message.reply_text("Нет информации о слотах для этой услуги.")
        return
    taken_slots = get_taken_slots(svc, date)
    free_slots = [t for t in slots if t not in taken_slots or t == rec.get("Время")]
    if not free_slots:
        await update.message.reply_text("Все слоты на этот день заняты.")
        return
    text_slots = ["Выберите новый слот:"]
    for i, t in enumerate(free_slots, 1):
        text_slots.append(f"{i}. {t}")
    await update.message.reply_text("\n".join(text_slots))
    context.user_data["awaiting_slot"] = {"row": row_idx, "slots": free_slots, "record": rec}

async def handle_slot_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("awaiting_slot")
    if not state:
        return False
    text = update.message.text.strip()
    if not re.fullmatch(r"\d+", text):
        return False
    idx = int(text) - 1
    slots = state["slots"]
    if idx < 0 or idx >= len(slots):
        return False
    new_time = slots[idx]
    row_idx = state["row"]
    sheet.update_cell(row_idx, 5, new_time)
    rec = state["record"]
    await update.message.reply_text(f"✅ Время изменено на {new_time}.")
    msg = (
        f"✏️ Пациент поменял время:\n"
        f"{rec['Имя']}, услуга {rec['Услуга']}\n"
        f"Новая дата/время: {rec['Дата']} {new_time}"
    )
    await context.bot.send_message(DOCTORS_GROUP_ID, msg, parse_mode="Markdown")
    del context.user_data["awaiting_slot"]
    return True

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%d.%m.%Y")
    records = sheet.get_all_records()
    for rec in records:
        if rec.get("Дата") == today:
            chat_id = rec.get("Chat ID")
            svc = rec.get("Услуга")
            time_ = rec.get("Время")
            await context.bot.send_message(
                chat_id,
                f"🔔 Напоминание: у вас сегодня запись на *{svc}* в *{time_}*.",
                parse_mode="Markdown"
            )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_data = context.user_data

    # --- Блок отмены/изменения записи и слотов не трогаем ---
    if is_cancel_intent(text):
        return await handle_cancel_or_edit(update, context)
    if user_data.get("awaiting_slot"):
        handled = await handle_slot_selection(update, context)
        if handled:
            return

    state = user_data.get("state", "consult")
    form = user_data.get("form", {})

    # --- КОНСУЛЬТАЦИОННЫЙ РЕЖИМ ---
    if state == "consult":
        # --- 1. Сначала проверяем: хочет ли пользователь записаться? ---
        if is_booking_intent(text):
            service_candidate = match_service(text)
            if service_candidate:
                form["Услуга"] = service_candidate
                user_data["form"] = form
                user_data["state"] = "reg_name"
                await update.message.reply_text("Пожалуйста, напишите ваше имя для записи.")
                return
            await update.message.reply_text("На какую услугу вы хотите записаться?\n" + build_services_list())
            user_data["state"] = "reg_service"
            user_data["form"] = form
            return

        # --- 2. Всё остальное: консультация через OpenAI ---
        services_text = []
        for i, s in enumerate(SERVICES, 1):
            line = f"{i}. {s['название']} — {s['цена']}"
            if 'описание' in s:
                line += f". {s['описание']}"
            services_text.append(line)
        services_prompt = "\n".join(services_text)

        system_prompt = (
            "Ты — внимательный и доброжелательный администратор стоматологической клиники. "
            "Объясняй услуги из списка, как будто общаешься с обычным человеком: просто, тепло, дружелюбно и по делу. "
            "Если тебя спрашивают про что-то конкретное (например, 'пластинки', 'элайнеры', 'цены', 'прикус'), расскажи об этой услуге более подробно (цена, преимущества, показания, для кого подходит и т.д.). "
            "Если вопрос общий — кратко перечисли основные услуги и спроси, нужна ли подробная консультация. "
            "Если человек пока не просит записать его — НЕ переходи к регистрации и НЕ пиши ничего о записи. "
            "Если тебя просят сравнить услуги — объясни плюсы и минусы каждой.\n"
            f"Вот список услуг клиники:\n{services_prompt}"
        )

        history = user_data.get("history", [])
        history.append({"role": "user", "content": text})
        user_data["history"] = history[-10:]
        messages = [{"role": "system", "content": system_prompt}] + history[-10:]

        try:
            resp = openai.chat.completions.create(model="gpt-4o", messages=messages)
            reply = resp.choices[0].message.content
        except Exception:
            reply = "Извините, сейчас не могу ответить 🤖"
        await update.message.reply_text(reply)
        return

        # Если пользователь сразу пишет "записаться на ...", начни оформление
        if is_booking_intent(text):
            service_candidate = match_service(text)
            if service_candidate:
                form["Услуга"] = service_candidate
                user_data["form"] = form
                user_data["state"] = "reg_name"
                await update.message.reply_text("Пожалуйста, напишите ваше имя для записи.")
                return
            # Если услуга не найдена, просим выбрать услугу из списка
            await update.message.reply_text("На какую услугу вы хотите записаться?\n" + build_services_list())
            user_data["state"] = "reg_service"
            user_data["form"] = form
            return

        # Если ничего из вышеперечисленного — просто консультация
        await update.message.reply_text(
            "Здравствуйте! Чем могу помочь? Если хотите узнать об услугах или записаться — напишите."
        )
        return

    # 3. Выбор услуги, если сразу не была указана
    if state == "reg_service":
        service_candidate = match_service(text)
        if service_candidate:
            form["Услуга"] = service_candidate
            user_data["form"] = form
            user_data["state"] = "reg_name"
            await update.message.reply_text("Пожалуйста, напишите ваше имя для записи.")
        else:
            await update.message.reply_text("Пожалуйста, выберите услугу из списка:\n" + build_services_list())
        return

    # 4. Имя
    if state == "reg_name":
        name = extract_name(text)
        if name:
            form["Имя"] = name
            user_data["form"] = form
            user_data["state"] = "reg_date"
            await update.message.reply_text("На какую дату вы хотите записаться? (например, 02.06.2025 или 'завтра')")
        else:
            await update.message.reply_text("Пожалуйста, укажите ваше имя (например, 'Я Иван').")
        return

    # 5. Дата
    if state == "reg_date":
        date = extract_date(text)
        if date:
            form["Дата"] = date
            user_data["form"] = form
            user_data["state"] = "reg_time"
            service_obj = get_service_object(form["Услуга"])
            if not service_obj:
                await update.message.reply_text("Ошибка: услуга не найдена. Попробуйте выбрать услугу заново.")
                user_data["state"] = "reg_service"
                return
            taken_slots = get_taken_slots(form["Услуга"], date)
            free_slots = [t for t in service_obj.get("слоты", []) if t not in taken_slots]
            if not free_slots:
                await update.message.reply_text("На эту дату нет свободных слотов. Пожалуйста, введите другую дату.")
                return
            slot_lines = [f"{i+1}. {slot}" for i, slot in enumerate(free_slots)]
            await update.message.reply_text("Свободные слоты:\n" + "\n".join(slot_lines) + "\nНапишите номер или время.")
            user_data["free_slots"] = free_slots
        else:
            await update.message.reply_text("Пожалуйста, напишите дату в формате ДД.ММ.ГГГГ или 'завтра'.")
        return

    # 6. Время
    if state == "reg_time":
        free_slots = user_data.get("free_slots", [])
        value = text.strip()
        chosen_time = None
        if re.fullmatch(r"\d+", value):
            idx = int(value) - 1
            if 0 <= idx < len(free_slots):
                chosen_time = free_slots[idx]
        else:
            for t in free_slots:
                if value in t:
                    chosen_time = t
        if chosen_time:
            form["Время"] = chosen_time
            user_data["form"] = form
            user_data["state"] = "reg_phone"
            await update.message.reply_text("Пожалуйста, укажите ваш контактный телефон (например, +77001112233).")
        else:
            slot_lines = [f"{i+1}. {slot}" for i, slot in enumerate(free_slots)]
            await update.message.reply_text("Пожалуйста, выберите время только из списка:\n" + "\n".join(slot_lines))
        return

    # 7. Телефон
    if state == "reg_phone":
        phone = extract_phone(text)
        if phone:
            form["Телефон"] = phone
            user_data["form"] = form
            # Всё собрано, делаем запись
            if is_form_complete(form):
                await register_and_notify(form, update, context)
                user_data.clear()
                return
            else:
                await update.message.reply_text("Произошла ошибка заполнения формы, начните заново.")
                user_data.clear()
                return
        else:
            await update.message.reply_text("Пожалуйста, введите телефон в формате +77001112233.")
        return

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()

    async def start_scheduler(_: ContextTypes.DEFAULT_TYPE):
        scheduler.add_job(send_reminders, "cron", hour=9, minute=0, args=[app.bot])
        scheduler.start()

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

